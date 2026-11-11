"""MQTT side of the bridge: TLS session, file chunking, acknowledgements, data requests.

Topic layout (see docs/MQTT_INTERFACE.md):
  {prefix}/{ied}/dfr/event                         JSON   record-made / health events (QoS 1)
  {prefix}/{ied}/dfr/{rtype}/{record_id}/meta      JSON   record metadata, published first (QoS 1)
  {prefix}/{ied}/dfr/{rtype}/{record_id}/chunk/{n} binary file bytes, n = 0..chunk_count-1 (QoS 1)
  {prefix}/bridge/{bridge_id}/status               JSON   retained online/offline (offline = last will)
  {prefix}/bridge/{bridge_id}/health               JSON   retained queue / store statistics
  {prefix}/bridge/{bridge_id}/ack                  JSON   <- from EMS: {"record_id": .., "status": "ok"|"resend"}
  {prefix}/bridge/{bridge_id}/request              JSON   <- from EMS: data request (re-publish / list)
  {prefix}/bridge/{bridge_id}/response             JSON   -> answer to a data request
"""
from __future__ import annotations

import json
import logging
import ssl
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.properties import Properties
from paho.mqtt.packettypes import PacketTypes

log = logging.getLogger("frbridge.mqtt")

SCHEMA_META = "frb.record.v1"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Topics:
    def __init__(self, prefix: str, bridge_id: str):
        self.prefix = prefix
        self.bridge = f"{prefix}/bridge/{bridge_id}"

    def event(self, ied: str) -> str:
        return f"{self.prefix}/{ied}/dfr/event"

    def last_event(self, ied: str) -> str:
        return f"{self.prefix}/{ied}/dfr/last_event"

    def record(self, ied: str, rtype: str, rid: str) -> str:
        return f"{self.prefix}/{ied}/dfr/{rtype}/{rid}"

    def meta(self, ied: str, rtype: str, rid: str) -> str:
        return self.record(ied, rtype, rid) + "/meta"

    def chunk(self, ied: str, rtype: str, rid: str, n: int) -> str:
        return self.record(ied, rtype, rid) + f"/chunk/{n}"

    @property
    def status(self) -> str:
        return self.bridge + "/status"

    @property
    def health(self) -> str:
        return self.bridge + "/health"

    @property
    def ack(self) -> str:
        return self.bridge + "/ack"

    @property
    def request(self) -> str:
        return self.bridge + "/request"

    @property
    def response(self) -> str:
        return self.bridge + "/response"


class Publisher:
    def __init__(self, cfg: dict, on_ack: Callable[[dict], None], on_request: Callable[[dict], None]):
        self.cfg = cfg
        m = cfg["mqtt"]
        self.m = m
        self.qos = int(m["qos"])
        self.chunk_size = int(m["chunk_size"])
        self.timeout = float(m["publish_timeout_s"])
        self.topics = Topics(cfg["topic_prefix"], cfg["bridge_id"])
        self.on_ack = on_ack
        self.on_request = on_request
        self.connected = threading.Event()
        self.v5 = m["protocol"] == "v5"
        proto = mqtt.MQTTv5 if self.v5 else mqtt.MQTTv311
        self.client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=m["client_id"], protocol=proto,
                                  clean_session=None if self.v5 else False)
        self.client.max_inflight_messages_set(int(m["max_inflight"]))
        self.client.max_queued_messages_set(0)
        self.client.reconnect_delay_set(1, 60)
        if m.get("username"):
            self.client.username_pw_set(m["username"], m.get("password") or None)
        if m.get("tls"):
            ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=m.get("ca_file") or None)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            if m.get("client_cert"):
                ctx.load_cert_chain(m["client_cert"], m.get("client_key") or None)
            if m.get("tls_insecure"):
                ctx.check_hostname = False
            self.client.tls_set_context(ctx)
        self.client.will_set(self.topics.status, json.dumps({"state": "offline", "bridge_id": cfg["bridge_id"]}),
                             qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    # ---------- lifecycle ----------
    def start(self) -> None:
        kw: dict[str, Any] = {}
        if self.v5:
            props = Properties(PacketTypes.CONNECT)
            props.SessionExpiryInterval = 3600
            kw["properties"] = props
            kw["clean_start"] = False
        self.client.connect_async(self.m["host"], int(self.m["port"]), keepalive=int(self.m["keepalive"]), **kw)
        self.client.loop_start()

    def stop(self) -> None:
        try:
            if self.connected.is_set():
                self.client.publish(self.topics.status, json.dumps({"state": "offline", "reason": "shutdown",
                                                                     "bridge_id": self.cfg["bridge_id"]}),
                                    qos=1, retain=True).wait_for_publish(5)
            self.client.disconnect()
        finally:
            self.client.loop_stop()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            log.error("MQTT connect refused: %s", reason_code)
            return
        log.info("MQTT connected to %s:%s", self.m["host"], self.m["port"])
        client.subscribe([(self.topics.ack, 1), (self.topics.request, 1)])
        client.publish(self.topics.status, json.dumps({"state": "online", "bridge_id": self.cfg["bridge_id"],
                                                       "since": utcnow_iso()}), qos=1, retain=True)
        self.connected.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected.clear()
        log.warning("MQTT disconnected: %s", reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            body = json.loads(msg.payload.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("payload is not a JSON object")
        except Exception as e:  # noqa: BLE001
            log.warning("Ignoring malformed message on %s: %s", msg.topic, e)
            return
        if msg.topic == self.topics.ack:
            self.on_ack(body)
        elif msg.topic == self.topics.request:
            self.on_request(body)

    # ---------- publishing ----------
    def _wait(self, infos: list[mqtt.MQTTMessageInfo]) -> None:
        deadline = time.monotonic() + self.timeout
        for info in infos:
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                raise ConnectionError(f"publish rejected: {mqtt.error_string(info.rc)}")
            if self.qos == 0:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("broker did not acknowledge publish in time")
            try:
                info.wait_for_publish(remaining)
            except (ValueError, RuntimeError) as e:
                raise ConnectionError(f"publish failed: {e}") from e
            if not info.is_published():
                raise TimeoutError("broker did not acknowledge publish in time")

    def publish_json(self, topic: str, obj: dict, retain: bool = False, wait: bool = True) -> None:
        info = self.client.publish(topic, json.dumps(obj, separators=(",", ":")), qos=self.qos, retain=retain)
        if wait:
            self._wait([info])

    def publish_record(self, row, data: bytes, extra_meta: dict) -> dict:
        """Publish metadata then all chunks of one record; raises on failure."""
        if not self.connected.is_set():
            raise ConnectionError("not connected to broker")
        n = max(1, -(-len(data) // self.chunk_size))
        meta = {
            "schema": SCHEMA_META,
            "record_id": row["id"],
            "bridge_id": self.cfg["bridge_id"],
            "ied": row["ied"],
            "rtype": row["rtype"],
            "file_name": row["std_name"],
            "original_name": row["filename"],
            "source": row["source"],
            "size": row["size"],
            "sha256": row["sha256"],
            "content_type": "application/zip" if data[:2] == b"PK" else "application/octet-stream",
            "chunk_size": self.chunk_size,
            "chunk_count": n,
            "trigger_utc": row["trigger_utc"],
            "published_utc": utcnow_iso(),
            "attempt": int(row["attempts"]) + 1,
            "ack_topic": self.topics.ack,
            "require_ack": bool(self.cfg["delivery"]["require_ack"]),
        }
        meta.update({k: v for k, v in extra_meta.items() if k not in meta})
        ied, rtype, rid = row["ied"], row["rtype"], row["id"]
        infos = [self.client.publish(self.topics.meta(ied, rtype, rid), json.dumps(meta, separators=(",", ":")),
                                     qos=self.qos)]
        for i in range(n):
            part = data[i * self.chunk_size:(i + 1) * self.chunk_size]
            kw: dict[str, Any] = {}
            if self.v5:
                props = Properties(PacketTypes.PUBLISH)
                props.UserProperty = [("record_id", rid), ("index", str(i)), ("count", str(n))]
                props.ContentType = meta["content_type"]
                kw["properties"] = props
            infos.append(self.client.publish(self.topics.chunk(ied, rtype, rid, i), part, qos=self.qos, **kw))
        self._wait(infos)
        log.info("Published record %s (%s, %d bytes, %d chunks) for %s", rid, rtype, len(data), n, ied)
        return meta
