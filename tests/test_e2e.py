"""End-to-end: TLS Mosquitto with passwords + ACL, bridge (HTTPS xConnect receiver + folder ingest),
reference EMS receiver.  Covers delivery, chunking, acks, resend, data requests, events, broker outage."""
import hashlib
import json
import os
import threading
import time

import paho.mqtt.client as mqtt
import pytest
from paho.mqtt.enums import CallbackAPIVersion

from conftest import free_port
from frbridge import config
from frbridge.bridge import Bridge
import ems_receiver
import make_sample_comtrade as msc
import xconnect_sim


def wait_for(cond, timeout=20, step=0.1):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(step)
    return False


def make_bridge(tmp_path, pki, broker, **over):
    cfg = config.deep_merge(config.DEFAULTS, {
        "bridge_id": "frb01", "topic_prefix": "utility/site01",
        "mqtt": {"host": "127.0.0.1", "port": broker.port, "tls": True, "ca_file": f"{pki}/ca.pem",
                 "username": "frb01", "password": broker.pw["frb01"], "chunk_size": 4096, "publish_timeout_s": 10},
        "ingest": {"http": {"enabled": True, "bind": "127.0.0.1", "port": free_port(), "tls": True,
                            "cert_file": f"{pki}/receiver.pem", "key_file": f"{pki}/receiver.key",
                            "auth": "basic", "username": "xconnect", "password": "xc-pw", "capture": True},
                   "folder": {"enabled": True, "path": str(tmp_path / "inbox"), "poll_s": 0.3}},
        "delivery": {"require_ack": True, "ack_timeout_s": 5, "loop_s": 0.2},
        "storage": {"root": str(tmp_path / "store"), "retention_days": 30},
        "naming": {"company": "ACME"},
        "ieds": {"default": "ied01", "map": {"7KE85_01": "7ke85-01", "7SX85": "7sx85-01"}},
        "edge": {"enabled": False},
    })
    cfg = config.deep_merge(cfg, over)
    cfg = config.validate(cfg)
    b = Bridge(cfg)
    t = threading.Thread(target=b.run, daemon=True)
    t.start()
    assert wait_for(b.pub.connected.is_set), "bridge did not connect to broker"
    return b, t


def make_receiver(tmp_path, pki, broker, auto_ack=True):
    c = mqtt.Client(CallbackAPIVersion.VERSION2, client_id="ems-test", protocol=mqtt.MQTTv311, clean_session=True)
    c.username_pw_set("ems", broker.pw["ems"])
    import ssl
    c.tls_set_context(ssl.create_default_context(cafile=f"{pki}/ca.pem"))
    r = ems_receiver.Receiver(c, "utility/site01", str(tmp_path / "received"), "ems-test", incomplete_timeout=3)
    if not auto_ack:
        r._ack_orig = r._ack
        r._ack = lambda meta, status, reason="": None
    c.on_message = r.on_message
    ready = threading.Event()

    def on_connect(cl, u, f, rc, p=None):
        cl.subscribe(r.subscriptions())
        ready.set()
    c.on_connect = on_connect
    c.connect("127.0.0.1", broker.port)
    c.loop_start()
    assert ready.wait(10)
    time.sleep(0.3)
    return c, r


def post(b, path, mode="raw", user="xconnect", pw="xc-pw"):
    url = f"https://127.0.0.1:{b.cfg['ingest']['http']['port']}/api/files"
    return xconnect_sim.send(url, path, user, pw, cafile=os.path.join(b.cfg["mqtt"]["ca_file"]), mode=mode)


@pytest.fixture
def sample(tmp_path):
    data, stem, trig = msc.build(device="7KE85_01", seconds=1.0, rate=3840, fault_no=7)
    p = tmp_path / f"{stem}.zip"
    p.write_bytes(data)
    return p, data


def test_end_to_end_delivery_ack_and_requests(tmp_path, pki, broker, sample):
    path, data = sample
    c, r = make_receiver(tmp_path, pki, broker)
    b, t = make_bridge(tmp_path, pki, broker)
    try:
        # unauthorized upload rejected
        code, _ = post(b, path, pw="wrong")
        assert code == 401
        # xConnect-style push (raw body) -> stored -> published in chunks -> reassembled -> acked
        code, resp = post(b, path)
        assert code == 201 and resp["status"] == "stored"
        rid = resp["record_id"]
        assert wait_for(lambda: len(r.saved) == 1)
        saved = r.saved[0]
        assert saved.endswith(os.path.join("7ke85-01", "261110,151530123456,-7h,SS01,7KE85_01,ACME.zip"))
        with open(saved, "rb") as f:
            assert hashlib.sha256(f.read()).hexdigest() == hashlib.sha256(data).hexdigest()
        meta = json.load(open(saved + ".json"))
        assert meta["chunk_count"] == -(-len(data) // 4096) > 1 and meta["trigger_utc"] == "2026-11-10T22:15:30.123456Z"
        assert wait_for(lambda: b.store.get(rid)["state"] == "acked")
        # same file again (multipart encoding) is a duplicate
        code, resp = post(b, path, mode="multipart")
        assert code == 200 and resp["status"] == "duplicate"
        # capture file written for bench analysis, with auth masked
        cap = os.listdir(os.path.join(b.cfg["storage"]["root"], "capture"))
        assert cap and "***" in open(os.path.join(b.cfg["storage"]["root"], "capture", cap[0])).read()

        # data request: list by IED, then republish one record
        c.publish("utility/site01/bridge/frb01/request", json.dumps({"request_id": "q1", "cmd": "list", "ied": "7ke85-01"}), qos=1)
        assert wait_for(lambda: any(x.get("request_id") == "q1" for x in r.responses))
        q1 = [x for x in r.responses if x["request_id"] == "q1"][0]
        assert q1["status"] == "ok" and q1["records"][0]["record_id"] == rid
        r.done.discard(rid)
        r.saved.clear()
        c.publish("utility/site01/bridge/frb01/request", json.dumps({"request_id": "q2", "cmd": "republish", "record_id": rid}), qos=1)
        assert wait_for(lambda: len(r.saved) == 1)
        # malformed request answered with an error, bridge keeps running
        c.publish("utility/site01/bridge/frb01/request", json.dumps({"request_id": "q3", "from": "yesterday"}), qos=1)
        assert wait_for(lambda: any(x.get("request_id") == "q3" and x["status"] == "error" for x in r.responses))

        # folder ingest of loose COMTRADE files (File Archive style) from a 7SX85
        d2, stem2, _ = msc.build(device="7SX85", fault_no=8)
        import zipfile, io
        inbox = tmp_path / "inbox"
        with zipfile.ZipFile(io.BytesIO(d2)) as z:
            for n in z.namelist():
                (inbox / n).write_bytes(z.read(n))
        assert wait_for(lambda: any("7sx85-01" in p for p in r.saved), timeout=15)

        # edge event (record made) published to the event topic
        b.edge.events.put({"event": "record_made", "ied": "7ke85-01", "fault_number": 7, "time": "2026-11-10T22:15:30.123456Z"})
        assert wait_for(lambda: any(e.get("event") == "record_made" and e["fault_number"] == 7 for e in r.events))

        st = b.status()
        assert st["broker_connected"] and st["counters"]["acked"] >= 2
    finally:
        b.stop(); t.join(10)
        c.loop_stop(); c.disconnect()


def test_resend_on_missing_ack_and_broker_outage(tmp_path, pki, broker, sample):
    path, data = sample
    c, r = make_receiver(tmp_path, pki, broker, auto_ack=False)
    b, t = make_bridge(tmp_path, pki, broker)
    try:
        code, resp = post(b, path)
        rid = resp["record_id"]
        assert wait_for(lambda: len(r.saved) == 1)
        assert wait_for(lambda: b.store.get(rid)["state"] == "published")
        # no ack within ack_timeout (5 s) -> bridge republishes (attempt 2)
        r.done.discard(rid); r.saved.clear()
        assert wait_for(lambda: len(r.saved) == 1, timeout=15)
        assert b.store.get(rid)["attempts"] >= 2
        # now acknowledge
        r._ack_orig(json.load(open(r.saved[0] + ".json")), "ok")
        assert wait_for(lambda: b.store.get(rid)["state"] == "acked")

        # broker outage: records received meanwhile are queued and delivered after reconnect
        broker.stop()
        assert wait_for(lambda: not b.pub.connected.is_set(), timeout=10)
        d3, stem3, _ = msc.build(device="7KE85_01", fault_no=9)
        p3 = tmp_path / f"{stem3}.zip"; p3.write_bytes(d3)
        code, resp3 = post(b, p3)
        assert code == 201
        assert b.store.get(resp3["record_id"])["state"] == "queued"
        r.saved.clear()
        broker.start()                      # both clients reconnect on their own (paho loop)
        assert wait_for(b.pub.connected.is_set, timeout=70)
        assert wait_for(lambda: any(stem3 in json.load(open(p + ".json"))["original_name"] for p in r.saved), timeout=20)
    finally:
        b.stop(); t.join(10)
        c.loop_stop(); c.disconnect()


def test_acl_blocks_foreign_publisher(tmp_path, pki, broker):
    """The EMS account must not be able to inject records on the bridge's topics."""
    import ssl
    got = []
    sub = mqtt.Client(CallbackAPIVersion.VERSION2, client_id="sub")
    sub.username_pw_set("ems", broker.pw["ems"])
    sub.tls_set_context(ssl.create_default_context(cafile=f"{pki}/ca.pem"))
    sub.on_message = lambda cl, u, m: got.append(m.topic)
    sub.connect("127.0.0.1", broker.port); sub.loop_start()
    sub.subscribe("utility/site01/#", 1); time.sleep(0.5)
    sub.publish("utility/site01/7ke85-01/dfr/comtrade/0000000000000000/meta", "{}", qos=1)
    time.sleep(1)
    sub.loop_stop(); sub.disconnect()
    assert not any("/dfr/comtrade/" in tpc for tpc in got)
