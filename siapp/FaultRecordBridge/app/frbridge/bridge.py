"""Fault Record Bridge - wires ingest, store, MQTT publisher and Edge Data API together."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Optional

from . import records
from .config import redacted
from .edge import Edge
from .ingest_files import FolderIngest, FtpsIngest
from .ingest_http import HttpIngest
from .publisher import Publisher, utcnow_iso
from .store import Store

log = logging.getLogger("frbridge")

RID = re.compile(r"^[0-9a-f]{16}$")
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?Z?$")
VERSION = "0.1.0"


class Bridge:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        s = cfg["storage"]
        self.store = Store(s["root"], s["max_store_mb"], s["max_record_mb"])
        self.pub = Publisher(cfg, self.on_ack, self.on_request)
        self.edge = Edge(cfg["edge"])
        self.ingesters: list[Any] = []
        self._stop = threading.Event()
        self.last_delivery_ok = True
        self.heartbeat = 0
        self.counters = {"received": 0, "duplicates": 0, "published": 0, "acked": 0, "failed": 0, "events": 0}

    # ---------- ingest ----------
    def resolve_ied(self, info: records.RecordInfo, hint: dict) -> str:
        hay = " ".join([info.station, info.device, info.original_name, str(hint.get("path", ""))]).lower()
        for needle, ied in self.cfg["ieds"].get("map", {}).items():
            if needle.lower() in hay:
                return ied
        return self.cfg["ieds"]["default"]

    def submit(self, data: bytes, filename: str, source: str, hint: Optional[dict] = None) -> tuple[str, bool]:
        hint = hint or {}
        info = records.inspect(data, filename)
        ied = self.resolve_ied(info, hint)
        naming = self.cfg["naming"]
        ext = ".zip" if data[:2] == b"PK" else (("." + filename.rsplit(".", 1)[-1]) if "." in filename else "")
        std = records.c37232_name(info, naming, ext) if naming.get("c37232") and info.rtype == "comtrade" else filename
        meta = {k: v for k, v in info.to_dict().items() if k not in ("original_name",)}
        meta["source_hint"] = {k: v for k, v in hint.items() if k in ("path", "remote")}
        rid, new = self.store.add(data, filename=filename, rtype=info.rtype, ied=ied, std_name=std,
                                  trigger_utc=info.trigger_utc or info.xconnect_utc, source=source, meta=meta)
        if new:
            self.counters["received"] += 1
            log.info("Stored %s record %s from %s: %s -> %s (%d bytes)", info.rtype, rid, source, filename, std, len(data))
            for w in info.warnings:
                log.warning("Record %s: %s", rid, w)
        else:
            self.counters["duplicates"] += 1
            log.info("Duplicate record %s from %s ignored (%s)", rid, source, filename)
        return rid, new

    # ---------- MQTT callbacks ----------
    def on_ack(self, body: dict) -> None:
        rid = str(body.get("record_id", ""))
        if not RID.match(rid):
            log.warning("ack with invalid record_id ignored")
            return
        status = str(body.get("status", "ok")).lower()
        if status == "ok":
            if self.store.mark_acked(rid):
                self.counters["acked"] += 1
                log.info("Record %s acknowledged by %s", rid, body.get("receiver", "EMS"))
        elif status == "resend":
            if self.store.requeue(rid):
                log.info("Record %s re-queued on receiver request (%s)", rid, body.get("reason", ""))
        else:
            log.warning("ack for %s with unknown status %r", rid, status)

    def on_request(self, body: dict) -> None:
        req_id = str(body.get("request_id", ""))[:64]
        cmd = str(body.get("cmd", "republish")).lower()
        resp: dict[str, Any] = {"request_id": req_id, "cmd": cmd, "bridge_id": self.cfg["bridge_id"], "time": utcnow_iso()}
        try:
            rid = body.get("record_id")
            if rid is not None:
                if not RID.match(str(rid)):
                    raise ValueError("invalid record_id")
                row = self.store.get(str(rid))
                rows = [row] if row else []
            else:
                t_from, t_to = body.get("from"), body.get("to")
                for t in (t_from, t_to):
                    if t is not None and not ISO.match(str(t)):
                        raise ValueError("from/to must be ISO 8601 UTC, e.g. 2026-11-10T00:00:00Z")
                ied = body.get("ied")
                if ied is not None and not re.match(r"^[A-Za-z0-9_.-]{1,64}$", str(ied)):
                    raise ValueError("invalid ied")
                rtype = body.get("rtype")
                if rtype is not None and rtype not in records.RTYPES:
                    raise ValueError("invalid rtype")
                rows = self.store.find(ied=ied, t_from=t_from, t_to=t_to, rtype=rtype,
                                       limit=min(int(body.get("limit", 200)), 1000))
            resp["records"] = [{"record_id": r["id"], "ied": r["ied"], "rtype": r["rtype"], "file_name": r["std_name"],
                                "trigger_utc": r["trigger_utc"], "size": r["size"], "state": r["state"]} for r in rows]
            if cmd == "republish":
                for r in rows:
                    self.store.requeue(r["id"])
                resp["status"] = "queued"
            elif cmd == "list":
                resp["status"] = "ok"
            else:
                raise ValueError("cmd must be 'list' or 'republish'")
        except (ValueError, TypeError) as e:
            resp.update({"status": "error", "error": str(e)})
        log.info("Data request %s (%s): %s, %d record(s)", req_id, cmd, resp.get("status"), len(resp.get("records", [])))
        try:
            self.pub.publish_json(self.pub.topics.response, resp, wait=False)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not publish request response: %s", e)

    # ---------- loops ----------
    def deliver_once(self) -> int:
        d = self.cfg["delivery"]
        if not self.pub.connected.is_set():
            return 0
        sent = 0
        for row in self.store.due(time.time(), d["require_ack"], d["ack_timeout_s"], d["max_attempts"]):
            try:
                data = self.store.read_bytes(row)
                self.pub.publish_record(row, data, json.loads(row["meta"]))
                self.store.mark_published(row["id"], d["require_ack"])
                self.counters["published"] += 1
                self.last_delivery_ok = True
                sent += 1
            except FileNotFoundError:
                log.error("Record file for %s missing - marking failed", row["id"])
                self.store.mark_failed(row["id"], "file missing")
            except Exception as e:  # noqa: BLE001
                self.counters["failed"] += 1
                self.last_delivery_ok = False
                self.store.mark_failed(row["id"], str(e))
                log.warning("Publishing %s failed: %s", row["id"], e)
                break
        return sent

    def flush_events(self) -> None:
        while self.pub.connected.is_set():
            try:
                ev = self.edge.events.get_nowait()
            except Exception:  # queue.Empty
                return
            ev = {"bridge_id": self.cfg["bridge_id"], "published_utc": utcnow_iso(), **ev}
            try:
                self.pub.publish_json(self.pub.topics.event(ev["ied"]), ev)
                self.pub.publish_json(self.pub.topics.last_event(ev["ied"]), ev, retain=True, wait=False)
                self.counters["events"] += 1
            except Exception as e:  # noqa: BLE001
                log.warning("event publish failed, will retry: %s", e)
                self.edge.events.put(ev)
                return

    def status(self) -> dict:
        st = self.store.stats()
        return {"version": VERSION, "broker_connected": self.pub.connected.is_set(), "queue": st["pending"],
                "store_bytes": st["bytes"], "store_alarm": self.store.store_alarm, "counters": dict(self.counters)}

    def housekeeping(self) -> None:
        if self.edge.enabled and not self.edge.connected:
            self.edge.start()
        removed = self.store.purge(self.cfg["storage"]["retention_days"])
        if removed:
            log.info("Retention: purged %d acknowledged record(s)", removed)
        st = self.status()
        self.heartbeat = (self.heartbeat + 1) % 65536
        # delivery is "not OK" if anything has been waiting far longer than the ack timeout
        oldest_ok = self.store.oldest_pending_age() <= max(3 * self.cfg["delivery"]["ack_timeout_s"], 600)
        self.edge.write_status({"broker_connected": int(st["broker_connected"]), "queue_depth": st["queue"],
                                "delivery_ok": int(self.last_delivery_ok and oldest_ok),
                                "store_alarm": int(st["store_alarm"]), "heartbeat": self.heartbeat})
        if self.pub.connected.is_set():
            try:
                self.pub.publish_json(self.pub.topics.health, {"bridge_id": self.cfg["bridge_id"], "time": utcnow_iso(), **st},
                                      retain=True, wait=False)
            except Exception as e:  # noqa: BLE001
                log.debug("health publish failed: %s", e)

    def run(self) -> None:
        log.info("Fault Record Bridge %s starting, config: %s", VERSION, redacted(self.cfg))
        self.edge.start()
        self.pub.start()
        ing = self.cfg["ingest"]
        if ing["http"]["enabled"]:
            self.ingesters.append(HttpIngest(ing["http"], self.submit, self.status, self.cfg["storage"]["root"]))
        if ing["ftps"]["enabled"]:
            self.ingesters.append(FtpsIngest(ing["ftps"], self.store, self.submit))
        if ing["folder"]["enabled"]:
            self.ingesters.append(FolderIngest(ing["folder"], self.store, self.submit))
        for i in self.ingesters:
            i.start()
        last_hk = 0.0
        loop_s = float(self.cfg["delivery"]["loop_s"])
        while not self._stop.is_set():
            try:
                self.flush_events()
                self.deliver_once()
                if time.monotonic() - last_hk > 30:
                    self.housekeeping()
                    last_hk = time.monotonic()
            except Exception:  # noqa: BLE001
                log.exception("main loop error")
            self._stop.wait(loop_s)
        self._shutdown()

    def stop(self) -> None:
        self._stop.set()

    def _shutdown(self) -> None:
        log.info("Stopping")
        for i in self.ingesters:
            try:
                i.stop()
            except Exception:  # noqa: BLE001
                pass
        self.edge.stop()
        self.pub.stop()
        self.store.close()
