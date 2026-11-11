"""HTTPS receiver for SICAM 8 xConnect file transfer (REST PUT/POST).

xConnect is documented to send archive files (zipped COMTRADE, PQDIF, COMFEDE ...)
to "any REST API service" with PUT or POST, OAuth2 / basic / API-key / no auth.
The exact body encoding is not documented in the material we have, so this
receiver accepts all common forms:
  * raw body (application/zip, application/octet-stream, ...) - file name from
    Content-Disposition, X-File-Name, ?filename= or the last URL path segment
  * multipart/form-data - first part that carries a file
  * JSON {"fileName"|"filename"|"name": .., "data"|"content"|"content_base64"|"file": base64}
Enable ingest.http.capture on the bench to record what xConnect really sends.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import os
import re
import ssl
import threading
import time
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, unquote, urlparse

log = logging.getLogger("frbridge.http")

SAFE_NAME = re.compile(r"[^A-Za-z0-9._,+-]+")


def safe_filename(name: str) -> str:
    name = os.path.basename(unquote(name or "").replace("\\", "/")).strip()
    name = SAFE_NAME.sub("_", name)[:200]
    return name or f"upload_{int(time.time())}.bin"


def _cd_filename(value: str) -> Optional[str]:
    if not value:
        return None
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", value, re.IGNORECASE)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename="?([^";]+)"?', value, re.IGNORECASE)
    return m.group(1) if m else None


def extract_file(headers, path: str, body: bytes) -> tuple[bytes, str]:
    ctype = (headers.get("Content-Type") or "").lower()
    q = parse_qs(urlparse(path).query)
    if ctype.startswith("multipart/"):
        msg = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + headers.get("Content-Type").encode("latin-1") + b"\r\n\r\n" + body)
        fallback = None
        for part in msg.iter_parts():
            payload = part.get_payload(decode=True) or b""
            fname = part.get_filename()
            if fname and payload:
                return payload, safe_filename(fname)
            if payload and fallback is None:
                fallback = (payload, part.get_param("name", header="content-disposition") or "")
        if fallback:
            return fallback[0], safe_filename(fallback[1])
        raise ValueError("multipart body contains no file")
    if ctype.startswith("application/json"):
        obj = json.loads(body.decode("utf-8"))
        if isinstance(obj, dict):
            fname = obj.get("fileName") or obj.get("filename") or obj.get("name") or ""
            for key in ("data", "content", "content_base64", "file", "fileContent"):
                if isinstance(obj.get(key), str):
                    try:
                        return base64.b64decode(obj[key], validate=True), safe_filename(fname)
                    except binascii.Error as e:
                        raise ValueError(f"field '{key}' is not base64") from e
        raise ValueError("JSON body has no base64 file field")
    fname = (_cd_filename(headers.get("Content-Disposition", ""))
             or headers.get("X-File-Name") or headers.get("X-Filename")
             or (q.get("filename") or q.get("fileName") or [None])[0])
    if not fname:
        last = urlparse(path).path.rstrip("/").rsplit("/", 1)[-1]
        fname = last if "." in last else ""
    return body, safe_filename(fname)


class _Handler(BaseHTTPRequestHandler):
    server_version = "FaultRecordBridge/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # set by HttpIngest
    cfg: dict = {}
    submit: Callable = None  # type: ignore[assignment]
    status_fn: Callable = None  # type: ignore[assignment]
    capture_dir: Optional[str] = None

    def log_message(self, fmt, *args):  # route to logging, not stderr
        log.debug("%s - %s", self.client_address[0], fmt % args)

    def _send(self, code: int, obj: dict, extra: Optional[dict] = None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        mode = self.cfg.get("auth", "basic")
        if mode == "none":
            return True
        if mode == "apikey":
            got = self.headers.get(self.cfg.get("api_key_header", "X-API-Key"), "")
            if not got and self.headers.get("Authorization", "").lower().startswith("bearer "):
                got = self.headers["Authorization"][7:].strip()
            return bool(got) and hmac.compare_digest(got, self.cfg.get("api_key", ""))
        auth = self.headers.get("Authorization", "")
        if not auth.lower().startswith("basic "):
            return False
        try:
            user, _, pw = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
        except Exception:  # noqa: BLE001
            return False
        return (hmac.compare_digest(user, self.cfg.get("username", ""))
                and hmac.compare_digest(pw, self.cfg.get("password", "")))

    def _capture(self, body: bytes):
        if not self.capture_dir:
            return
        try:
            os.makedirs(self.capture_dir, exist_ok=True)
            hdrs = {k: (v if k.lower() != "authorization" else v.split(" ")[0] + " ***") for k, v in self.headers.items()}
            rec = {"time": time.time(), "method": self.command, "path": self.path, "headers": hdrs,
                   "length": len(body), "head_hex": body[:64].hex(), "head_text": body[:512].decode("latin-1")}
            fn = os.path.join(self.capture_dir, f"{int(time.time() * 1000)}.json")
            with open(fn, "w", encoding="utf-8") as f:
                json.dump(rec, f, indent=1)
        except OSError as e:
            log.warning("capture failed: %s", e)

    def do_GET(self):
        if urlparse(self.path).path.rstrip("/") in ("/health", "/api/health"):
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"}, {"WWW-Authenticate": 'Basic realm="frbridge"'})
            return self._send(200, {"status": "ok", **(self.status_fn() if self.status_fn else {})})
        return self._send(404, {"error": "not found"})

    def _receive(self):
        if not self._authorized():
            log.warning("Rejected unauthorized upload from %s", self.client_address[0])
            return self._send(401, {"error": "unauthorized"}, {"WWW-Authenticate": 'Basic realm="frbridge"'})
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        limit = int(self.cfg.get("max_body_mb", 12)) * 1024 * 1024
        if length < 0:
            return self._send(411, {"error": "Content-Length required"})
        if length > limit:
            return self._send(413, {"error": f"body larger than {limit} bytes"})
        body = self.rfile.read(length)
        self._capture(body)
        try:
            data, fname = extract_file(self.headers, self.path, body)
            if not data:
                return self._send(400, {"error": "empty file"})
            rid, new = self.submit(data, fname, "xconnect", {"remote": self.client_address[0], "path": self.path})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001  StoreFull and anything unexpected
            log.exception("Upload failed")
            code = 507 if e.__class__.__name__ == "StoreFull" else 500
            return self._send(code, {"error": str(e)})
        return self._send(201 if new else 200, {"status": "stored" if new else "duplicate",
                                                "record_id": rid, "file_name": fname})

    do_POST = _receive
    do_PUT = _receive


class HttpIngest:
    def __init__(self, cfg: dict, submit: Callable, status_fn: Callable, storage_root: str):
        self.cfg = cfg
        handler = type("Handler", (_Handler,), {
            "cfg": cfg, "submit": staticmethod(submit), "status_fn": staticmethod(status_fn),
            "capture_dir": os.path.join(storage_root, "capture") if cfg.get("capture") else None})
        self.httpd = ThreadingHTTPServer((cfg.get("bind", "0.0.0.0"), int(cfg["port"])), handler)
        self.httpd.daemon_threads = True
        if cfg.get("tls"):
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(cfg["cert_file"], cfg["key_file"])
            # handshake in the per-connection thread, not in the accept loop
            self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True, do_handshake_on_connect=False)
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="http-ingest", daemon=True)

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def start(self):
        self.thread.start()
        log.info("xConnect receiver listening on %s:%s (%s, auth=%s)", self.cfg.get("bind"), self.port,
                 "https" if self.cfg.get("tls") else "http", self.cfg.get("auth"))

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
