"""File-based ingest: FTPS pull from SICAM 8 File Archive Access, and a local watch folder.

File Archive Access exposes fault records per device/record type folder.  Records
may appear as zips or as loose COMTRADE members (.cfg/.dat/.hdr/.inf), so loose
files are grouped by directory + file stem and zipped together before they are
stored.  A file (group) is only taken when its size has been stable for one
full poll, so partially written files are never picked up.
"""
from __future__ import annotations

import ftplib
import io
import logging
import os
import posixpath
import ssl
import threading
import zipfile
from typing import Callable, Iterable

log = logging.getLogger("frbridge.files")

COMTRADE_EXT = {".cfg", ".dat", ".hdr", ".inf", ".cff"}
SINGLE_EXT = {".zip", ".pqd", ".xml", ".csv", ".json"}


def group_entries(entries: Iterable[tuple[str, int]]) -> dict[str, list[tuple[str, int]]]:
    """Group (path, size) entries into records.  Key = group id."""
    groups: dict[str, list[tuple[str, int]]] = {}
    for path, size in entries:
        base, ext = posixpath.splitext(path)
        ext = ext.lower()
        if ext in COMTRADE_EXT:
            key = base.lower()
        elif ext in SINGLE_EXT:
            key = path
        else:
            continue
        groups.setdefault(key, []).append((path, size))
    return groups


def group_ready(members: list[tuple[str, int]]) -> bool:
    exts = {posixpath.splitext(p)[1].lower() for p, _ in members}
    if exts & COMTRADE_EXT:
        return ".cff" in exts or {".cfg", ".dat"} <= exts
    return True


def group_key(members: list[tuple[str, int]]) -> str:
    return "|".join(f"{p}:{s}" for p, s in sorted(members))


def bundle(members: list[tuple[str, bytes]]) -> tuple[bytes, str]:
    """Return (data, filename).  Single zip/pqd/... files pass through unchanged."""
    if len(members) == 1 and posixpath.splitext(members[0][0])[1].lower() in SINGLE_EXT:
        return members[0][1], posixpath.basename(members[0][0])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, data in sorted(members):
            z.writestr(posixpath.basename(path), data)
    stem = posixpath.splitext(posixpath.basename(members[0][0]))[0]
    return buf.getvalue(), stem + ".zip"


class _Poller:
    """Shared stability tracking: a group is processed when seen with identical sizes twice."""

    def __init__(self, store, submit: Callable, source: str):
        self.store = store
        self.submit = submit
        self.source = source
        self.prev: dict[str, str] = {}
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def ready_groups(self, entries: list[tuple[str, int]]) -> list[list[tuple[str, int]]]:
        out, cur = [], {}
        for gid, members in group_entries(entries).items():
            key = group_key(members)
            cur[gid] = key
            if self.store.seen(f"{self.source}:{key}"):
                continue
            if self.prev.get(gid) == key and group_ready(members):
                out.append(members)
        self.prev = cur
        return out


class FtpsIngest(_Poller):
    def __init__(self, cfg: dict, store, submit: Callable):
        super().__init__(store, submit, "ftps")
        self.cfg = cfg
        self.thread = threading.Thread(target=self._run, name="ftps-ingest", daemon=True)

    def start(self):
        self.thread.start()
        log.info("FTPS ingest polling %s:%s%s every %ss", self.cfg["host"], self.cfg["port"], self.cfg["root"], self.cfg["poll_s"])

    def _connect(self) -> ftplib.FTP_TLS:
        ctx = ssl.create_default_context(cafile=self.cfg.get("ca_file") or None)
        if not self.cfg.get("verify", True):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        ftp = ftplib.FTP_TLS(context=ctx, timeout=30)
        ftp.connect(self.cfg["host"], int(self.cfg["port"]))
        ftp.login(self.cfg.get("username", "ftp"), self.cfg.get("password", ""))
        ftp.prot_p()
        ftp.set_pasv(True)
        return ftp

    def _walk(self, ftp: ftplib.FTP_TLS, path: str, depth: int) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        if depth > int(self.cfg.get("max_depth", 6)):
            return out
        try:
            for name, facts in ftp.mlsd(path, facts=["type", "size"]):
                if name in (".", ".."):
                    continue
                full = posixpath.join(path, name)
                t = facts.get("type", "")
                if t == "dir":
                    out += self._walk(ftp, full, depth + 1)
                elif t == "file":
                    out.append((full, int(facts.get("size", 0))))
        except ftplib.error_perm:
            # server without MLSD: fall back to NLST + SIZE
            for full in ftp.nlst(path):
                full = full if full.startswith("/") else posixpath.join(path, full)
                try:
                    out.append((full, int(ftp.size(full) or 0)))
                except ftplib.error_perm:
                    out += self._walk(ftp, full, depth + 1)
        return out

    def poll_once(self) -> int:
        done = 0
        ftp = self._connect()
        try:
            entries = self._walk(ftp, self.cfg.get("root", "/"), 0)
            for members in self.ready_groups(entries):
                files = []
                for p, _ in members:
                    buf = io.BytesIO()
                    ftp.retrbinary(f"RETR {p}", buf.write)
                    files.append((p, buf.getvalue()))
                data, fname = bundle(files)
                self.submit(data, fname, "ftps", {"path": posixpath.dirname(members[0][0])})
                self.store.mark_seen(f"ftps:{group_key(members)}")
                if self.cfg.get("delete_after"):
                    for p, _ in members:
                        ftp.delete(p)
                done += 1
        finally:
            try:
                ftp.quit()
            except Exception:  # noqa: BLE001
                ftp.close()
        return done

    def _run(self):
        while not self._stop.is_set():
            try:
                n = self.poll_once()
                if n:
                    log.info("FTPS ingest took %d record(s)", n)
            except Exception as e:  # noqa: BLE001
                log.warning("FTPS poll failed: %s", e)
            self._stop.wait(int(self.cfg.get("poll_s", 60)))


class FolderIngest(_Poller):
    def __init__(self, cfg: dict, store, submit: Callable):
        super().__init__(store, submit, "folder")
        self.cfg = cfg
        self.path = cfg["path"]
        os.makedirs(self.path, exist_ok=True)
        self.done_dir = os.path.join(self.path, ".done")
        os.makedirs(self.done_dir, exist_ok=True)
        self.thread = threading.Thread(target=self._run, name="folder-ingest", daemon=True)

    def start(self):
        self.thread.start()
        log.info("Folder ingest watching %s", self.path)

    def poll_once(self) -> int:
        entries = []
        for root, dirs, files in os.walk(self.path):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                p = os.path.join(root, f)
                entries.append((p.replace(os.sep, "/"), os.path.getsize(p)))
        done = 0
        for members in self.ready_groups(entries):
            files = []
            for p, _ in members:
                with open(p, "rb") as fh:
                    files.append((p, fh.read()))
            data, fname = bundle(files)
            self.submit(data, fname, "folder", {"path": os.path.relpath(os.path.dirname(members[0][0]), self.path)})
            self.store.mark_seen(f"folder:{group_key(members)}")
            for p, _ in members:
                os.replace(p, os.path.join(self.done_dir, os.path.basename(p)))
            done += 1
        return done

    def _run(self):
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as e:  # noqa: BLE001
                log.warning("Folder poll failed: %s", e)
            self._stop.wait(float(self.cfg.get("poll_s", 5)))
