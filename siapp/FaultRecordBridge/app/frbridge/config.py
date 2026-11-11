"""Configuration loading for the Fault Record Bridge SIAPP.

Precedence (lowest -> highest):
  1. Built-in DEFAULTS
  2. JSON config file  (FRB_CONFIG env var, else first existing CONFIG_CANDIDATES path)
  3. SICAM Device Manager SIAPP parameters (flat KEY -> value, see DM_PARAM_MAP)
  4. Environment overrides  FRB__SECTION__KEY=value  (e.g. FRB__MQTT__HOST=10.0.0.5)

The location where SICAM Device Manager hands parameter values to a running
SIAPP is not documented in the public SIAPP SDK.  load_dm_params() therefore
looks in FRB_DM_PARAMS (env) and a list of candidate paths, and accepts either
JSON ({"KEY": value}) or KEY=value lines.  Confirm the real path on the bench
and add it to DM_PARAM_CANDIDATES.
"""
from __future__ import annotations

import base64
import copy
import json
import logging
import os
import re
from typing import Any

log = logging.getLogger("frbridge.config")

DEFAULTS: dict[str, Any] = {
    "bridge_id": "frb01",
    "topic_prefix": "utility/site01",
    "log_level": "INFO",
    "mqtt": {
        "host": "127.0.0.1",
        "port": 8883,
        "tls": True,
        "ca_file": "",
        "client_cert": "",
        "client_key": "",
        "tls_insecure": False,          # lab only: skip broker hostname check
        "username": "",
        "password": "",
        "client_id": "",                # default: "<bridge_id>"
        "keepalive": 30,
        "protocol": "v311",             # "v311" or "v5"
        "qos": 1,
        "max_inflight": 20,
        "chunk_size": 131072,           # bytes per MQTT message carrying file data
        "publish_timeout_s": 30,
    },
    "ingest": {
        "http": {                       # receiver for SICAM 8 xConnect file transfer
            "enabled": True,
            "bind": "0.0.0.0",
            "port": 8443,
            "tls": True,
            "cert_file": "",
            "key_file": "",
            "auth": "basic",            # "basic", "apikey" or "none"
            "username": "xconnect",
            "password": "",
            "api_key_header": "X-API-Key",
            "api_key": "",
            "max_body_mb": 12,
            "capture": False,           # bench aid: save request headers/body preview to storage.root/capture
        },
        "ftps": {                       # pull from SICAM 8 File Archive Access (FTPS server)
            "enabled": False,
            "host": "",
            "port": 21,
            "username": "ftp",
            "password": "",
            "ca_file": "",
            "verify": True,
            "root": "/",
            "poll_s": 60,
            "max_depth": 6,
            "delete_after": False,      # leave files on the File Archive by default
        },
        "folder": {                     # watch folder (bench testing / manual drop)
            "enabled": False,
            "path": "/persist_data/frb/inbox",
            "poll_s": 5,
        },
    },
    "delivery": {
        "require_ack": True,
        "ack_timeout_s": 120,
        "max_attempts": 0,              # 0 = keep retrying until acked
        "loop_s": 2,
    },
    "storage": {
        "root": "/persist_data/frb",
        "retention_days": 30,           # keep acked records at least this long (CLO-003 R7 asks >= 20 d)
        "max_store_mb": 700,            # A8000 SIAPP slot <= 1000 MB; S8000 up to 32 GB
        "max_record_mb": 50,
    },
    "naming": {
        "c37232": True,
        "company": "COMPANY",
        "station": "",                  # blank: take station name from COMTRADE .cfg
        "device": "",                   # blank: take rec_dev_id from COMTRADE .cfg
        "template": "{date},{time},{tcode},{station},{device},{company}",
    },
    "ieds": {
        "default": "ied01",
        "map": {},                      # substring of station / device / path -> IED id used in topics
    },
    "edge": {
        "enabled": True,
        "lib_path": "/edgedata.so",
        "signals": [],                  # [{"topic": "DFR01_RcdMade", "ied": "7ke85-01", "role": "record_made"}, ...]
        "outputs": {                    # SIAPP -> RTU status signals (write topics); blank = unused
            "broker_connected": "",
            "queue_depth": "",
            "delivery_ok": "",
            "store_alarm": "",
            "heartbeat": "",
        },
    },
}

CONFIG_CANDIDATES = [
    "/persist_data/frb/config.json",
    "/persist_data/config.json",
    "/app/config.json",
]

DM_PARAM_CANDIDATES = [
    # TODO(bench): confirm where SICAM Device Manager parameters appear inside the SIAR
    "/cmd/parameter.json",
    "/cmd/parameters.json",
    "/cmd/param.json",
    "/persist_data/parameter.json",
]

# Device Manager parameter key -> (config path, type).  Keys must match cmd/parameter_schema.json.
DM_PARAM_MAP: dict[str, tuple[str, str]] = {
    "BRIDGE_ID": ("bridge_id", "str"),
    "TOPIC_PREFIX": ("topic_prefix", "str"),
    "LOG_LEVEL": ("log_level", "str"),
    "MQTT_HOST": ("mqtt.host", "str"),
    "MQTT_PORT": ("mqtt.port", "int"),
    "MQTT_TLS": ("mqtt.tls", "bool"),
    "MQTT_USER": ("mqtt.username", "str"),
    "MQTT_PASS": ("mqtt.password", "str"),
    "MQTT_PROTOCOL": ("mqtt.protocol", "str"),
    "MQTT_CA": ("mqtt.ca_file", "blob"),
    "MQTT_CLIENT_CERT": ("mqtt.client_cert", "blob"),
    "MQTT_CLIENT_KEY": ("mqtt.client_key", "blob"),
    "CHUNK_KB": ("mqtt.chunk_size", "kb"),
    "REQUIRE_ACK": ("delivery.require_ack", "bool"),
    "ACK_TIMEOUT_S": ("delivery.ack_timeout_s", "int"),
    "RETENTION_DAYS": ("storage.retention_days", "int"),
    "MAX_STORE_MB": ("storage.max_store_mb", "int"),
    "HTTP_ENABLE": ("ingest.http.enabled", "bool"),
    "HTTP_PORT": ("ingest.http.port", "int"),
    "HTTP_TLS": ("ingest.http.tls", "bool"),
    "HTTP_AUTH": ("ingest.http.auth", "str"),
    "HTTP_USER": ("ingest.http.username", "str"),
    "HTTP_PASS": ("ingest.http.password", "str"),
    "HTTP_API_KEY": ("ingest.http.api_key", "str"),
    "HTTP_CERT": ("ingest.http.cert_file", "blob"),
    "HTTP_KEY": ("ingest.http.key_file", "blob"),
    "FTPS_ENABLE": ("ingest.ftps.enabled", "bool"),
    "FTPS_HOST": ("ingest.ftps.host", "str"),
    "FTPS_PORT": ("ingest.ftps.port", "int"),
    "FTPS_USER": ("ingest.ftps.username", "str"),
    "FTPS_PASS": ("ingest.ftps.password", "str"),
    "FTPS_ROOT": ("ingest.ftps.root", "str"),
    "FTPS_POLL_S": ("ingest.ftps.poll_s", "int"),
    "FTPS_VERIFY": ("ingest.ftps.verify", "bool"),
    "FTPS_CA": ("ingest.ftps.ca_file", "blob"),
    "COMPANY": ("naming.company", "str"),
    "STATION": ("naming.station", "str"),
    "DEFAULT_IED": ("ieds.default", "str"),
    "IED_MAP": ("ieds.map", "map"),               # "7KE85_01=7ke85-01;7SX85=7sx85-01"
    "EDGE_SIGNALS": ("edge.signals", "signals"),  # "DFR01_RcdMade:7ke85-01:record_made;DFR01_FltNum:7ke85-01:fault_number"
    "OUT_BROKER_OK": ("edge.outputs.broker_connected", "str"),
    "OUT_QUEUE": ("edge.outputs.queue_depth", "str"),
    "OUT_DELIVERY_OK": ("edge.outputs.delivery_ok", "str"),
    "OUT_STORE_ALARM": ("edge.outputs.store_alarm", "str"),
    "OUT_HEARTBEAT": ("edge.outputs.heartbeat", "str"),
}

EDGE_ROLES = ("record_made", "fault_number", "health", "status")


class ConfigError(ValueError):
    pass


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def set_path(cfg: dict, path: str, value: Any) -> None:
    keys = path.split(".")
    node = cfg
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = value


def get_path(cfg: dict, path: str, default: Any = None) -> Any:
    node: Any = cfg
    for k in path.split("."):
        if not isinstance(node, dict) or k not in node:
            return default
        node = node[k]
    return node


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on", "enabled")


def _parse_map(value: str) -> dict[str, str]:
    out = {}
    for item in filter(None, (x.strip() for x in str(value).split(";"))):
        k, sep, v = item.partition("=")
        if not sep or not k.strip() or not v.strip():
            raise ConfigError(f"IED_MAP entry '{item}' must look like <match>=<ied-id>")
        out[k.strip()] = v.strip()
    return out


def _parse_signals(value: str) -> list[dict[str, str]]:
    out = []
    for item in filter(None, (x.strip() for x in str(value).split(";"))):
        parts = [p.strip() for p in item.split(":")]
        if len(parts) != 3 or parts[2] not in EDGE_ROLES:
            raise ConfigError(f"EDGE_SIGNALS entry '{item}' must look like <topic>:<ied-id>:<{'|'.join(EDGE_ROLES)}>")
        out.append({"topic": parts[0], "ied": parts[1], "role": parts[2]})
    return out


def _convert(value: Any, typ: str) -> Any:
    if typ == "map":
        return _parse_map(value)
    if typ == "signals":
        return _parse_signals(value)
    if typ == "int":
        return int(value)
    if typ == "kb":
        return int(value) * 1024
    if typ == "bool":
        return _to_bool(value)
    return value if isinstance(value, str) else str(value)


def _write_blob(value: str, name: str, store_root: str) -> str:
    """Decode a base64 blob parameter (e.g. a PEM certificate) to a file; return its path."""
    data = value.encode() if isinstance(value, str) else value
    try:
        raw = base64.b64decode(data, validate=True)
    except Exception:
        raw = data  # already plain text (PEM)
    d = os.path.join(store_root, "certs")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name.lower() + ".pem")
    with open(path, "wb") as f:
        f.write(raw)
    os.chmod(path, 0o600)
    return path


def load_dm_params(path: str | None = None) -> dict[str, Any]:
    paths = [path] if path else ([os.environ["FRB_DM_PARAMS"]] if os.environ.get("FRB_DM_PARAMS") else DM_PARAM_CANDIDATES)
    for p in paths:
        if p and os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                text = f.read()
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    log.info("Device Manager parameters loaded from %s", p)
                    return data
            except json.JSONDecodeError:
                pass
            out = {}
            for line in text.splitlines():
                m = re.match(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$", line)
                if m:
                    out[m.group(1)] = m.group(2)
            log.info("Device Manager parameters loaded from %s (key=value)", p)
            return out
    return {}


def apply_dm_params(cfg: dict, params: dict[str, Any]) -> dict:
    cfg = copy.deepcopy(cfg)
    store_root = get_path(cfg, "storage.root")
    for key, raw in params.items():
        if key not in DM_PARAM_MAP or raw in (None, ""):
            continue
        path, typ = DM_PARAM_MAP[key]
        if typ == "blob":
            set_path(cfg, path, _write_blob(raw, key, store_root))
        else:
            set_path(cfg, path, _convert(raw, typ))
    return cfg


def apply_env(cfg: dict, env: dict[str, str] | None = None) -> dict:
    cfg = copy.deepcopy(cfg)
    env = os.environ if env is None else env
    for k, v in env.items():
        if not k.startswith("FRB__"):
            continue
        path = ".".join(p.lower() for p in k[5:].split("__"))
        cur = get_path(DEFAULTS, path)
        if isinstance(cur, bool):
            v2: Any = _to_bool(v)
        elif isinstance(cur, int):
            v2 = int(v)
        elif isinstance(cur, (list, dict)):
            v2 = json.loads(v)
        else:
            v2 = v
        set_path(cfg, path, v2)
    return cfg


TOPIC_SAFE = re.compile(r"^[^#+\x00]+$")
ID_SAFE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def validate(cfg: dict) -> dict:
    errs = []
    if not ID_SAFE.match(str(cfg.get("bridge_id", ""))):
        errs.append("bridge_id must be 1-64 chars [A-Za-z0-9_.-]")
    tp = str(cfg.get("topic_prefix", ""))
    if not tp or not TOPIC_SAFE.match(tp) or tp.startswith("/") or tp.endswith("/"):
        errs.append("topic_prefix must be non-empty, without wildcards and without leading/trailing '/'")
    m = cfg["mqtt"]
    if not m.get("host"):
        errs.append("mqtt.host is required")
    if not (1 <= int(m.get("port", 0)) <= 65535):
        errs.append("mqtt.port out of range")
    if int(m.get("qos", 1)) not in (0, 1, 2):
        errs.append("mqtt.qos must be 0, 1 or 2")
    if not (1024 <= int(m.get("chunk_size", 0)) <= 16 * 1024 * 1024):
        errs.append("mqtt.chunk_size must be 1 KiB .. 16 MiB")
    if m.get("protocol") not in ("v311", "v5"):
        errs.append("mqtt.protocol must be v311 or v5")
    h = cfg["ingest"]["http"]
    if h.get("enabled"):
        if h.get("auth") not in ("basic", "apikey", "none"):
            errs.append("ingest.http.auth must be basic, apikey or none")
        if h.get("auth") == "basic" and not h.get("password"):
            errs.append("ingest.http.password required when auth=basic")
        if h.get("auth") == "apikey" and not h.get("api_key"):
            errs.append("ingest.http.api_key required when auth=apikey")
        if h.get("tls") and not (h.get("cert_file") and h.get("key_file")):
            errs.append("ingest.http.cert_file and key_file required when ingest.http.tls is true")
    f = cfg["ingest"]["ftps"]
    if f.get("enabled") and not f.get("host"):
        errs.append("ingest.ftps.host required when FTPS ingest is enabled")
    if not any(cfg["ingest"][k].get("enabled") for k in ("http", "ftps", "folder")):
        errs.append("enable at least one ingest path (http, ftps or folder)")
    s = cfg["storage"]
    if int(s.get("retention_days", 0)) < 1:
        errs.append("storage.retention_days must be >= 1")
    for ied in list(cfg["ieds"].get("map", {}).values()) + [cfg["ieds"].get("default", "")]:
        if not ID_SAFE.match(str(ied)):
            errs.append(f"IED id '{ied}' must be 1-64 chars [A-Za-z0-9_.-]")
    if errs:
        raise ConfigError("; ".join(errs))
    if not m.get("client_id"):
        m["client_id"] = cfg["bridge_id"]
    if int(s.get("retention_days")) < 20:
        log.warning("storage.retention_days=%s is below the 20 days asked for by CLO-003 R7", s["retention_days"])
    return cfg


def load(config_path: str | None = None, dm_params_path: str | None = None,
         env: dict[str, str] | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    path = config_path or os.environ.get("FRB_CONFIG")
    if not path:
        path = next((p for p in CONFIG_CANDIDATES if os.path.isfile(p)), None)
    if path:
        with open(path, "r", encoding="utf-8") as f:
            cfg = deep_merge(cfg, json.load(f))
        log.info("Config file loaded from %s", path)
    cfg = apply_dm_params(cfg, load_dm_params(dm_params_path))
    cfg = apply_env(cfg, env)
    return validate(cfg)


def redacted(cfg: dict) -> dict:
    """Copy of cfg safe for logging (secrets masked)."""
    out = copy.deepcopy(cfg)
    for p in ("mqtt.password", "ingest.http.password", "ingest.http.api_key", "ingest.ftps.password"):
        if get_path(out, p):
            set_path(out, p, "***")
    return out
