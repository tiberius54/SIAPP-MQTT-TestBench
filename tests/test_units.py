import base64
import io
import json
import os
import time
import zipfile
from datetime import datetime, timezone

import pytest

from frbridge import config, records
from frbridge.edge import EdgeEvents
from frbridge.ingest_files import FolderIngest, bundle, group_entries, group_ready
from frbridge.ingest_http import extract_file, safe_filename
from frbridge.store import Store, StoreFull
import make_sample_comtrade as msc


# ---------------- records ----------------
def test_inspect_comtrade_zip_and_c37232():
    data, stem, trig = msc.build(station="SS01", device="7KE85_01", tz_hours=-7)
    info = records.inspect(data, f"{stem}_20261110T221531Z.zip")
    assert info.rtype == "comtrade"
    assert info.station == "SS01" and info.device == "7KE85_01" and info.rev_year == "2013"
    assert info.channels_analog == 6 and info.channels_digital == 2
    assert info.time_code == "-7"
    assert info.trigger_utc == "2026-11-10T22:15:30.123456Z"           # local -7 h converted back to UTC
    assert info.xconnect_utc == "2026-11-10T22:15:31.000000Z"
    assert info.original_name == stem
    name = records.c37232_name(info, {"company": "ACME", "template": config.DEFAULTS["naming"]["template"]})
    assert name == "261110,151530123456,-7h,SS01,7KE85_01,ACME.zip"   # local time + time code


def test_time_code_and_classify():
    from datetime import timedelta
    assert records.parse_time_code("-5h30") == -timedelta(hours=5, minutes=30)
    assert records.parse_time_code("+1") == timedelta(hours=1)
    assert records.parse_time_code("x") is None
    assert records.classify_members(["a.CFG", "a.DAT"]) == "comtrade"
    assert records.classify_members(["trend.pqd"]) == "pqdif"
    assert records.classify_members(["log.xml"]) == "comfede"


def test_cfg_without_timecode_assumes_utc():
    cfg = "ST,DEV,1999\r\n1,1A,0D\r\n1,IA,A,,A,1,0,0,-1,1,1,1,P\r\n60\r\n1\r\n1000,10\r\n" \
          "10/11/2026,10:00:00.000000\r\n10/11/2026,10:00:00.100000\r\nASCII\r\n1\r\n"
    info = records.RecordInfo()
    records.parse_comtrade_cfg(cfg, info)
    assert info.trigger_utc == "2026-11-10T10:00:00.100000Z"
    assert any("assumed UTC" in w for w in info.warnings)


# ---------------- config ----------------
def test_config_dm_params_env_and_blob(tmp_path):
    p = tmp_path / "params.json"
    pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
    p.write_text(json.dumps({"MQTT_HOST": "10.1.1.5", "MQTT_PORT": "8883", "CHUNK_KB": "64", "REQUIRE_ACK": "0",
                             "HTTP_TLS": "0", "HTTP_PASS": "pw", "MQTT_CA": base64.b64encode(pem.encode()).decode(),
                             "UNKNOWN_KEY": "x"}))
    cfgfile = tmp_path / "c.json"
    cfgfile.write_text(json.dumps({"storage": {"root": str(tmp_path / "store")}}))
    cfg = config.load(str(cfgfile), str(p), env={"FRB__BRIDGE_ID": "frb-test", "FRB__DELIVERY__ACK_TIMEOUT_S": "33"})
    assert cfg["mqtt"]["host"] == "10.1.1.5" and cfg["mqtt"]["chunk_size"] == 65536
    assert cfg["delivery"]["require_ack"] is False and cfg["delivery"]["ack_timeout_s"] == 33
    assert cfg["bridge_id"] == "frb-test" and cfg["mqtt"]["client_id"] == "frb-test"
    assert open(cfg["mqtt"]["ca_file"]).read() == pem
    assert config.redacted(cfg)["ingest"]["http"]["password"] == "***"


def test_config_dm_params_key_value_format(tmp_path):
    p = tmp_path / "params.txt"
    p.write_text("MQTT_HOST = broker.example\nHTTP_PASS=pw\nHTTP_TLS=0\n")
    assert config.load_dm_params(str(p)) == {"MQTT_HOST": "broker.example", "HTTP_PASS": "pw", "HTTP_TLS": "0"}


def test_config_validation_errors():
    bad = config.deep_merge(config.DEFAULTS, {"topic_prefix": "a/#", "mqtt": {"host": ""}, "ingest": {"http": {"password": ""}}})
    with pytest.raises(config.ConfigError) as e:
        config.validate(bad)
    msg = str(e.value)
    assert "topic_prefix" in msg and "mqtt.host" in msg and "ingest.http.password" in msg


# ---------------- store ----------------
def _add(store, data, name="r.zip"):
    return store.add(data, filename=name, rtype="comtrade", ied="ied1", std_name=name, trigger_utc=None,
                     source="test", meta={})


def test_store_lifecycle(tmp_path):
    s = Store(str(tmp_path), max_store_mb=1)
    rid, new = _add(s, b"PK" + b"x" * 1000)
    assert new and len(rid) == 16
    assert _add(s, b"PK" + b"x" * 1000) == (rid, False)               # dedupe by content
    assert [r["id"] for r in s.due(time.time(), True, 60, 0)] == [rid]
    s.mark_published(rid, True)
    assert s.due(time.time(), True, 60, 0) == []                         # waiting for ack
    assert [r["id"] for r in s.due(time.time() + 61, True, 60, 0)] == [rid]  # ack timeout -> due again
    assert s.mark_acked(rid) and s.stats()["acked"] == 1
    assert s.purge(retention_days=0) == 1 and s.stats()["bytes"] == 0


def test_store_full_never_drops_unacked(tmp_path):
    s = Store(str(tmp_path), max_store_mb=1)
    _add(s, os.urandom(700_000))
    with pytest.raises(StoreFull):
        _add(s, os.urandom(700_000))
    assert s.store_alarm
    rid = s.find()[0]["id"]
    s.mark_acked(rid)
    _add(s, os.urandom(700_000))                                       # acked one is evicted to make room
    assert s.get(rid) is None


# ---------------- http extract ----------------
class H(dict):
    def get(self, k, d=None):
        for kk, v in self.items():
            if kk.lower() == k.lower():
                return v
        return d


def test_extract_raw_multipart_json():
    data = b"PK\x03\x04payload"
    assert extract_file(H({"Content-Type": "application/zip", "Content-Disposition": 'attachment; filename="A_20261110T000000Z.zip"'}),
                        "/upload", data) == (data, "A_20261110T000000Z.zip")
    assert extract_file(H({"Content-Type": "application/octet-stream"}), "/upload?filename=x.zip", data) == (data, "x.zip")
    assert extract_file(H({"Content-Type": "application/octet-stream"}), "/files/rec.zip", data) == (data, "rec.zip")
    b = "XYZ"
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"m.zip\"\r\n"
            f"Content-Type: application/zip\r\n\r\n").encode() + data + f"\r\n--{b}--\r\n".encode()
    assert extract_file(H({"Content-Type": f"multipart/form-data; boundary={b}"}), "/", body) == (data, "m.zip")
    js = json.dumps({"fileName": "j.zip", "data": base64.b64encode(data).decode()}).encode()
    assert extract_file(H({"Content-Type": "application/json"}), "/", js) == (data, "j.zip")
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("a b;c.zip") == "a_b_c.zip"


# ---------------- file grouping ----------------
def test_grouping_and_bundle():
    ents = [("/d/R1.cfg", 10), ("/d/R1.dat", 20), ("/d/R1.hdr", 5), ("/d/R2.cfg", 10), ("/d/t.pqd", 7), ("/d/x.tmp", 1)]
    g = group_entries(ents)
    assert set(g) == {"/d/r1", "/d/r2", "/d/t.pqd"}
    assert group_ready(g["/d/r1"]) and not group_ready(g["/d/r2"]) and group_ready(g["/d/t.pqd"])
    data, name = bundle([("/d/R1.cfg", b"c"), ("/d/R1.dat", b"d")])
    assert name == "R1.zip" and sorted(zipfile.ZipFile(io.BytesIO(data)).namelist()) == ["R1.cfg", "R1.dat"]
    assert bundle([("/d/a.zip", b"PKz")]) == (b"PKz", "a.zip")


def test_folder_ingest_waits_for_stable_files(tmp_path):
    s = Store(str(tmp_path / "st"))
    got = []
    fi = FolderIngest({"path": str(tmp_path / "in"), "poll_s": 1}, s, lambda d, n, src, h: got.append((n, d)))
    (tmp_path / "in" / "R9.cfg").write_bytes(b"cfg")
    (tmp_path / "in" / "R9.dat").write_bytes(b"dat")
    assert fi.poll_once() == 0          # first sighting: not yet stable
    assert fi.poll_once() == 1
    assert got[0][0] == "R9.zip" and not (tmp_path / "in" / "R9.cfg").exists()
    assert fi.poll_once() == 0


# ---------------- edge events ----------------
def test_edge_event_logic():
    e = EdgeEvents([{"topic": "RM", "ied": "dfr1", "role": "record_made"},
                    {"topic": "FN", "ied": "dfr1", "role": "fault_number"},
                    {"topic": "HL", "ied": "dfr1", "role": "health"}])
    ts = int(datetime(2026, 11, 10, 22, 0, tzinfo=timezone.utc).timestamp() * 1e9)
    assert e.handle({"topic": "HL", "value": 1, "timestamp": ts})["healthy"] is True
    assert e.handle({"topic": "HL", "value": 1, "timestamp": ts}) is None
    assert e.handle({"topic": "FN", "value": 42}) is None
    ev = e.handle({"topic": "RM", "value": 1, "timestamp": ts, "quality": ["VALID_VALUE"]})
    assert ev["event"] == "record_made" and ev["fault_number"] == 42 and ev["time"] == "2026-11-10T22:00:00.000000Z"
    assert e.handle({"topic": "RM", "value": 1, "timestamp": ts}) is None    # no re-trigger without reset
    assert e.handle({"topic": "RM", "value": 0, "timestamp": ts}) is None
    assert e.handle({"topic": "HL", "value": 0, "timestamp": ts})["healthy"] is False
    assert e.handle({"topic": "nope", "value": 1}) is None


def test_dm_params_ied_map_and_edge_signals(tmp_path):
    params = {"MQTT_HOST": "b", "HTTP_PASS": "pw", "HTTP_TLS": "0",
              "IED_MAP": "7KE85_01=7ke85-01;7SX85=7sx85-01",
              "EDGE_SIGNALS": "DFR01_RcdMade:7ke85-01:record_made;DFR01_FltNum:7ke85-01:fault_number",
              "OUT_BROKER_OK": "FRB_BrokerOK"}
    cfg = config.apply_dm_params(config.deep_merge(config.DEFAULTS, {"storage": {"root": str(tmp_path)}}), params)
    cfg = config.validate(cfg)
    assert cfg["ieds"]["map"] == {"7KE85_01": "7ke85-01", "7SX85": "7sx85-01"}
    assert cfg["edge"]["signals"][0] == {"topic": "DFR01_RcdMade", "ied": "7ke85-01", "role": "record_made"}
    assert cfg["edge"]["outputs"]["broker_connected"] == "FRB_BrokerOK"
    with pytest.raises(config.ConfigError):
        config.apply_dm_params(config.DEFAULTS, {"EDGE_SIGNALS": "X:ied:bogus"})


def test_parameter_schema_matches_config_map():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    schema = json.load(open(os.path.join(root, "siapp", "FaultRecordBridge", "cmd", "parameter_schema.json")))
    keys = [p["key"] for g in schema["definition"] for p in g["parameters"]]
    assert len(keys) == len(set(keys))
    assert set(keys) == set(config.DM_PARAM_MAP)
    for g in schema["definition"]:
        for p in g["parameters"]:
            assert len(p["key"]) <= 50 and len(p["displayName"]) <= 50
