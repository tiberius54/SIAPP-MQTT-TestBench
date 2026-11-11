#!/usr/bin/env python3
"""Reference receiver for the control-center side (EMS / fault-record archive integration).

Subscribes to the bridge topics, reassembles chunked records, verifies size and SHA-256,
writes  <out>/<ied>/<file_name>  plus a .json sidecar with the metadata, and acknowledges
each record so the bridge can release it.  It is meant as a working specification for the
EMS integrator and as a bench test tool - not as production software.

  python3 ems_receiver.py --host broker --port 8883 --cafile ca.pem --user ems --password ... \
      --prefix utility/site01 --out ./received
Data requests (NERC data-request support):
  python3 ems_receiver.py ... --request list --bridge frb01 --from 2026-11-01T00:00:00Z
  python3 ems_receiver.py ... --request republish --bridge frb01 --record-id 0123456789abcdef
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import ssl
import sys
import threading
import time
import uuid

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

log = logging.getLogger("ems_receiver")
SAFE = re.compile(r"[^A-Za-z0-9._,+-]")


class Receiver:
    def __init__(self, client: mqtt.Client, prefix: str, out_dir: str, receiver_id: str, incomplete_timeout: float = 300):
        self.c = client
        self.prefix = prefix
        self.out = out_dir
        self.rid = receiver_id
        self.timeout = incomplete_timeout
        self.pending: dict[str, dict] = {}     # record_id -> {"meta":..., "chunks":{n:bytes}, "t":first_seen}
        self.done: set[str] = set()
        self.lock = threading.Lock()
        self.saved: list[str] = []
        self.events: list[dict] = []
        self.responses: list[dict] = []

    def subscriptions(self):
        return [(f"{self.prefix}/+/dfr/#", 1), (f"{self.prefix}/bridge/+/status", 1),
                (f"{self.prefix}/bridge/+/health", 0), (f"{self.prefix}/bridge/+/response", 1)]

    def on_message(self, client, userdata, msg):
        parts = msg.topic.split("/")
        try:
            if parts[-1] == "meta":
                self._meta(parts[-2], json.loads(msg.payload))
            elif len(parts) >= 3 and parts[-2] == "chunk":
                self._chunk(parts[-3], int(parts[-1]), msg.payload)
            elif parts[-1] in ("event", "last_event"):
                if parts[-1] == "event":
                    ev = json.loads(msg.payload)
                    self.events.append(ev)
                    log.info("EVENT %s ied=%s fault=%s time=%s", ev.get("event"), ev.get("ied"), ev.get("fault_number"), ev.get("time"))
            elif parts[-1] == "response":
                r = json.loads(msg.payload)
                self.responses.append(r)
                log.info("RESPONSE %s: %s, %d record(s)", r.get("request_id"), r.get("status"), len(r.get("records", [])))
                for rec in r.get("records", []):
                    log.info("   %s %s %s %s %s", rec["record_id"], rec["ied"], rec["rtype"], rec["trigger_utc"], rec["state"])
            elif parts[-1] in ("status", "health"):
                log.info("%s %s", parts[-1].upper(), msg.payload.decode(errors="replace"))
        except Exception as e:  # noqa: BLE001
            log.warning("bad message on %s: %s", msg.topic, e)

    def _entry(self, record_id):
        return self.pending.setdefault(record_id, {"meta": None, "chunks": {}, "t": time.monotonic()})

    def _meta(self, record_id, meta):
        with self.lock:
            if record_id in self.done:
                self._ack(meta, "ok")             # duplicate delivery: acknowledge again
                return
            e = self._entry(record_id)
            if e["meta"] and e["meta"].get("attempt") != meta.get("attempt"):
                e["chunks"] = {}                  # a re-publish starts over
            e["meta"] = meta
            self._try_complete(record_id)

    def _chunk(self, record_id, n, data):
        with self.lock:
            if record_id in self.done:
                return
            self._entry(record_id)["chunks"][n] = bytes(data)
            self._try_complete(record_id)

    def _try_complete(self, record_id):
        e = self.pending[record_id]
        m = e["meta"]
        if not m or len(e["chunks"]) < m["chunk_count"]:
            return
        data = b"".join(e["chunks"][i] for i in range(m["chunk_count"]))
        if len(data) != m["size"] or hashlib.sha256(data).hexdigest() != m["sha256"]:
            log.error("record %s failed integrity check - requesting resend", record_id)
            del self.pending[record_id]
            self._ack(m, "resend", "integrity check failed")
            return
        d = os.path.join(self.out, SAFE.sub("_", m["ied"]))
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, SAFE.sub("_", m["file_name"]))
        if os.path.exists(path):
            with open(path, "rb") as f:
                same = hashlib.sha256(f.read()).hexdigest() == m["sha256"]
            if not same:   # two different records with the same standard name (same trigger time)
                base, ext = os.path.splitext(path)
                path = f"{base},{record_id}{ext}"
        with open(path, "wb") as f:
            f.write(data)
        with open(path + ".json", "w", encoding="utf-8") as f:
            json.dump(m, f, indent=1)
        del self.pending[record_id]
        self.done.add(record_id)
        self.saved.append(path)
        log.info("SAVED %s (%d bytes, %s) -> %s", record_id, len(data), m["rtype"], path)
        if m.get("require_ack", True):
            self._ack(m, "ok")

    def _ack(self, meta, status, reason=""):
        body = {"record_id": meta["record_id"], "status": status, "receiver": self.rid}
        if reason:
            body["reason"] = reason
        self.c.publish(meta["ack_topic"], json.dumps(body), qos=1)

    def sweep(self):
        """Ask for a resend of records that stayed incomplete too long (lost chunks)."""
        with self.lock:
            for rid, e in list(self.pending.items()):
                if e["meta"] and time.monotonic() - e["t"] > self.timeout:
                    log.warning("record %s incomplete (%d/%d chunks) - requesting resend", rid,
                                len(e["chunks"]), e["meta"]["chunk_count"])
                    self._ack(e["meta"], "resend", "incomplete")
                    del self.pending[rid]


def make_client(a) -> mqtt.Client:
    c = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=a.client_id or f"ems-{uuid.uuid4().hex[:8]}",
                    protocol=mqtt.MQTTv311, clean_session=False)
    if a.user:
        c.username_pw_set(a.user, a.password)
    if a.port == 8883 or a.cafile:
        ctx = ssl.create_default_context(cafile=a.cafile)
        if a.insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        if a.cert:
            ctx.load_cert_chain(a.cert, a.key)
        c.tls_set_context(ctx)
    return c


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8883)
    ap.add_argument("--cafile")
    ap.add_argument("--cert")
    ap.add_argument("--key")
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--client-id")
    ap.add_argument("--prefix", default="utility/site01")
    ap.add_argument("--out", default="./received")
    ap.add_argument("--request", choices=["list", "republish"])
    ap.add_argument("--bridge", default="frb01")
    ap.add_argument("--record-id")
    ap.add_argument("--ied")
    ap.add_argument("--from", dest="t_from")
    ap.add_argument("--to", dest="t_to")
    ap.add_argument("--run-for", type=float, default=0, help="exit after N seconds (0 = run forever)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    c = make_client(a)
    r = Receiver(c, a.prefix, a.out, c._client_id.decode())
    c.on_message = r.on_message
    c.on_connect = lambda cl, u, f, rc, p=None: cl.subscribe(r.subscriptions())
    c.connect(a.host, a.port, keepalive=30)
    c.loop_start()
    time.sleep(1)
    if a.request:
        req = {"request_id": uuid.uuid4().hex[:8], "cmd": a.request}
        for k, v in (("record_id", a.record_id), ("ied", a.ied), ("from", a.t_from), ("to", a.t_to)):
            if v:
                req[k] = v
        c.publish(f"{a.prefix}/bridge/{a.bridge}/request", json.dumps(req), qos=1)
        log.info("sent data request %s", req)
    t0 = time.monotonic()
    try:
        while not a.run_for or time.monotonic() - t0 < a.run_for:
            time.sleep(1)
            r.sweep()
    except KeyboardInterrupt:
        pass
    c.loop_stop()
    c.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
