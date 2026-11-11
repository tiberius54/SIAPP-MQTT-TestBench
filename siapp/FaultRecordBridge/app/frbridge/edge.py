"""Edge Data API adapter (RTU signals <-> SIAPP).

Read signals (assigned to the SIAPP slot in SICAM Device Manager) are mapped to
roles in config "edge.signals":
    record_made   - e.g. 7KE85 RDRE.RcdMade via the RTU IEC 61850 client; rising edge -> event
    fault_number  - e.g. RDRE.FltNum; latest value is attached to record_made events
    health        - recorder / IED healthy (1 = OK); every change -> event
    status        - any other signal; every change -> event
Write signals ("edge.outputs") report bridge health back to the RTU so it can be
alarmed to SCADA like any other point (CLO-003 R8 recording-failure monitoring).

When edgedata.so is not present (development PC) the adapter disables itself.
"""
from __future__ import annotations

import logging
import os
import queue
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("frbridge.edge")


def ns_to_iso(ns: int) -> Optional[str]:
    if not ns:
        return None
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class EdgeEvents:
    """Turns raw signal updates into bridge events.  Pure logic - unit tested without the API."""

    def __init__(self, signals: list[dict]):
        self.by_topic = {s["topic"]: s for s in signals}
        self.last: dict[str, Any] = {}
        self.fault_numbers: dict[str, Any] = {}

    def handle(self, upd: dict) -> Optional[dict]:
        sig = self.by_topic.get(upd.get("topic"))
        if not sig:
            return None
        topic, value = upd["topic"], upd.get("value")
        prev = self.last.get(topic)
        self.last[topic] = value
        ied, role = sig.get("ied", "ied01"), sig.get("role", "status")
        base = {"ied": ied, "signal": topic, "value": value, "quality": upd.get("quality", []),
                "time": ns_to_iso(int(upd.get("timestamp") or 0))}
        if role == "fault_number":
            self.fault_numbers[ied] = value
            return None
        if role == "record_made":
            if value and not prev:
                return {"event": "record_made", "fault_number": self.fault_numbers.get(ied), **base}
            return None
        if role == "health":
            if prev is None or bool(value) != bool(prev):
                return {"event": "recorder_health", "healthy": bool(value), **base}
            return None
        if prev != value:
            return {"event": "status_change", **base}
        return None


class Edge:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enabled = bool(cfg.get("enabled"))   # wanted by configuration
        self.connected = False                    # Edge Data API session up
        self.events: "queue.Queue[dict]" = queue.Queue(maxsize=1000)
        self.logic = EdgeEvents(cfg.get("signals", []))
        self.api = None
        self.outputs = {k: v for k, v in (cfg.get("outputs") or {}).items() if v}
        self._lock = threading.Lock()

    def start(self) -> bool:
        if not self.enabled or self.connected:
            return self.connected
        lib = self.cfg.get("lib_path", "/edgedata.so")
        if not os.path.exists(lib):
            log.warning("Edge Data API library %s not found - RTU signal exchange disabled", lib)
            self.enabled = False
            return False
        os.environ.setdefault("EDGEDATA_LIB", lib)
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
        try:
            import edgedataapi as eda  # SDK wrapper (ctypes)
        except Exception as e:  # noqa: BLE001
            log.error("Cannot load Edge Data API: %s", e)
            self.enabled = False
            return False
        outer = self

        class _CB(eda.edgecallbacks):
            def cb_edge_data_subscribe(self, event):
                try:
                    outer._on_update(event)
                except Exception:  # noqa: BLE001
                    log.exception("edge callback failed")

            def cb_edge_data_logger(self, text):
                log.debug("edgedata: %s", text)

        self._cb = _CB()
        self.api = eda.edgedataapi
        if not self.api.connect():
            log.error("Edge Data API connect failed - will retry")
            return False   # Bridge.housekeeping() retries while enabled
        disc = self.api.discover() or {"read": [], "write": []}
        log.info("Edge Data API connected: %d read, %d write topics", len(disc["read"]), len(disc["write"]))
        for s in self.cfg.get("signals", []):
            if s["topic"] not in disc["read"]:
                log.warning("Configured read signal %s is not assigned to this SIAPP slot", s["topic"])
        for k, t in self.outputs.items():
            if t not in disc["write"]:
                log.warning("Configured output %s=%s is not a write topic of this SIAPP slot", k, t)
        # snapshot current values so 'health' and 'fault_number' start known
        if self.api.sync_read():
            for t in disc["read"]:
                d = self.api.read(t)
                if d:
                    self._on_update(d, initial=True)
        self.connected = True
        return True

    def _on_update(self, upd: dict, initial: bool = False):
        ev = self.logic.handle(upd)
        if ev and not initial:
            try:
                self.events.put_nowait(ev)
            except queue.Full:
                log.warning("edge event queue full - dropping %s", ev.get("event"))

    def write_status(self, values: dict[str, Any]) -> None:
        if not (self.connected and self.api and self.outputs):
            return
        with self._lock:
            wrote = False
            for key, val in values.items():
                topic = self.outputs.get(key)
                if topic:
                    wrote |= bool(self.api.write(topic, int(val), ["VALID_VALUE"]))
            if wrote:
                self.api.sync_write()

    def stop(self):
        if self.api and self.connected:
            try:
                self.write_status({"broker_connected": 0, "delivery_ok": 0})
                self.api.disconnect()
            except Exception:  # noqa: BLE001
                pass
