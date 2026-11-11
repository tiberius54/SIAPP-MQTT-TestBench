"""Persistent record store (SQLite + files) under storage.root (default /persist_data/frb).

State machine per record:
    queued --publish ok--> published --ack--> acked
       ^                      |
       +---- ack timeout / resend request / publish failure
Acked records are kept for retention_days (data requests can re-publish them),
then purged.  Unacked records are never purged automatically; when the store is
full, new records are rejected and the store alarm is raised instead.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    rtype TEXT NOT NULL,
    ied TEXT NOT NULL,
    filename TEXT NOT NULL,
    std_name TEXT NOT NULL,
    trigger_utc TEXT,
    size INTEGER NOT NULL,
    path TEXT NOT NULL,
    source TEXT NOT NULL,
    meta TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    published REAL,
    acked REAL,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS ix_state ON records(state);
CREATE INDEX IF NOT EXISTS ix_trigger ON records(trigger_utc);
CREATE TABLE IF NOT EXISTS seen (
    key TEXT PRIMARY KEY,
    ts REAL NOT NULL
);
"""


class StoreFull(Exception):
    pass


class Store:
    def __init__(self, root: str, max_store_mb: int = 700, max_record_mb: int = 50):
        self.root = root
        self.files = os.path.join(root, "records")
        os.makedirs(self.files, exist_ok=True)
        self.max_bytes = int(max_store_mb) * 1024 * 1024
        self.max_record = int(max_record_mb) * 1024 * 1024
        self._lock = threading.RLock()
        self.db = sqlite3.connect(os.path.join(root, "frbridge.db"), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.store_alarm = False

    # ---------- ingest ----------
    def add(self, data: bytes, *, filename: str, rtype: str, ied: str, std_name: str,
            trigger_utc: Optional[str], source: str, meta: dict[str, Any]) -> tuple[str, bool]:
        """Store a record.  Returns (record_id, is_new).  Duplicate content is ignored."""
        if len(data) > self.max_record:
            raise ValueError(f"record {filename} is {len(data)} bytes, above storage.max_record_mb")
        sha = hashlib.sha256(data).hexdigest()
        rid = sha[:16]
        with self._lock:
            row = self.db.execute("SELECT id FROM records WHERE id=?", (rid,)).fetchone()
            if row:
                return rid, False
            if self.used_bytes() + len(data) > self.max_bytes:
                self.purge(force_space=len(data))
                if self.used_bytes() + len(data) > self.max_bytes:
                    self.store_alarm = True
                    raise StoreFull("record store full of unacknowledged records")
            path = os.path.join(self.files, f"{rid}.bin")
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            self.db.execute(
                "INSERT INTO records(id,sha256,rtype,ied,filename,std_name,trigger_utc,size,path,source,meta,state,created)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, sha, rtype, ied, filename, std_name, trigger_utc, len(data), path, source,
                 json.dumps(meta), "queued", time.time()))
            return rid, True

    # ---------- delivery ----------
    def due(self, now: float, require_ack: bool, ack_timeout_s: int, max_attempts: int, limit: int = 10) -> list[sqlite3.Row]:
        with self._lock:
            q = "SELECT * FROM records WHERE state='queued'"
            args: list[Any] = []
            if require_ack:
                q += " OR (state='published' AND published < ?)"
                args.append(now - ack_timeout_s)
            q += " ORDER BY created LIMIT ?"
            args.append(limit)
            rows = self.db.execute(q, args).fetchall()
            if max_attempts:
                rows = [r for r in rows if r["attempts"] < max_attempts]
            return rows

    def mark_published(self, rid: str, require_ack: bool) -> None:
        with self._lock:
            now = time.time()
            if require_ack:
                self.db.execute("UPDATE records SET state='published', published=?, attempts=attempts+1, last_error=NULL WHERE id=?", (now, rid))
            else:
                self.db.execute("UPDATE records SET state='acked', published=?, acked=?, attempts=attempts+1, last_error=NULL WHERE id=?", (now, now, rid))

    def mark_failed(self, rid: str, err: str) -> None:
        with self._lock:
            self.db.execute("UPDATE records SET state='queued', attempts=attempts+1, last_error=? WHERE id=?", (err[:500], rid))

    def mark_acked(self, rid: str) -> bool:
        with self._lock:
            cur = self.db.execute("UPDATE records SET state='acked', acked=? WHERE id=? AND state!='acked'", (time.time(), rid))
            return cur.rowcount > 0

    def requeue(self, rid: str) -> bool:
        with self._lock:
            cur = self.db.execute("UPDATE records SET state='queued' WHERE id=?", (rid,))
            return cur.rowcount > 0

    # ---------- queries ----------
    def get(self, rid: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.db.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()

    def read_bytes(self, row: sqlite3.Row) -> bytes:
        with open(row["path"], "rb") as f:
            return f.read()

    def find(self, ied: Optional[str] = None, t_from: Optional[str] = None, t_to: Optional[str] = None,
             rtype: Optional[str] = None, limit: int = 200) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM records WHERE 1=1", []
        if ied:
            q += " AND ied=?"; args.append(ied)
        if rtype:
            q += " AND rtype=?"; args.append(rtype)
        if t_from:
            q += " AND COALESCE(trigger_utc, strftime('%Y-%m-%dT%H:%M:%SZ', created, 'unixepoch')) >= ?"; args.append(t_from)
        if t_to:
            q += " AND COALESCE(trigger_utc, strftime('%Y-%m-%dT%H:%M:%SZ', created, 'unixepoch')) <= ?"; args.append(t_to)
        q += " ORDER BY created LIMIT ?"; args.append(limit)
        with self._lock:
            return self.db.execute(q, args).fetchall()

    def stats(self) -> dict[str, int]:
        with self._lock:
            out = {"queued": 0, "published": 0, "acked": 0}
            for r in self.db.execute("SELECT state, COUNT(*) n FROM records GROUP BY state"):
                out[r["state"]] = r["n"]
            out["bytes"] = self.used_bytes()
            out["pending"] = out["queued"] + out["published"]
            return out

    def oldest_pending_age(self) -> float:
        with self._lock:
            v = self.db.execute("SELECT MIN(created) FROM records WHERE state!='acked'").fetchone()[0]
            return 0.0 if v is None else time.time() - float(v)

    def used_bytes(self) -> int:
        with self._lock:
            return int(self.db.execute("SELECT COALESCE(SUM(size),0) FROM records").fetchone()[0])

    # ---------- housekeeping ----------
    def purge(self, retention_days: Optional[int] = None, force_space: int = 0) -> int:
        """Delete acked records older than retention; with force_space, also oldest acked
        records (never unacked ones) until the new record fits."""
        removed = 0
        with self._lock:
            if retention_days is not None:
                cutoff = time.time() - retention_days * 86400
                for r in self.db.execute("SELECT id,path FROM records WHERE state='acked' AND acked < ?", (cutoff,)).fetchall():
                    self._delete(r); removed += 1
            if force_space:
                for r in self.db.execute("SELECT id,path,size FROM records WHERE state='acked' ORDER BY acked").fetchall():
                    if self.used_bytes() + force_space <= self.max_bytes:
                        break
                    self._delete(r); removed += 1
            self.store_alarm = self.used_bytes() > 0.9 * self.max_bytes
        return removed

    def _delete(self, r: sqlite3.Row) -> None:
        try:
            os.remove(r["path"])
        except FileNotFoundError:
            pass
        self.db.execute("DELETE FROM records WHERE id=?", (r["id"],))

    # ---------- remote listing memory (FTPS) ----------
    def seen(self, key: str) -> bool:
        with self._lock:
            return self.db.execute("SELECT 1 FROM seen WHERE key=?", (key,)).fetchone() is not None

    def mark_seen(self, key: str) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO seen(key,ts) VALUES(?,?)", (key, time.time()))

    def close(self) -> None:
        with self._lock:
            self.db.close()
