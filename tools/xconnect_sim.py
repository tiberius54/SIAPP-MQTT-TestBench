#!/usr/bin/env python3
"""Simulate SICAM 8 xConnect pushing an archive file to the Fault Record Bridge receiver.

  python3 xconnect_sim.py https://172.16.0.81:8443/upload record.zip --user xconnect --password secret --cafile lab-ca.pem
  --mode raw|multipart|json chooses the body encoding (the real xConnect format is confirmed on the bench).
The file is renamed the way xConnect names fault records: <original>_<YYYYMMDDThhmmssZ>.zip
"""
import argparse
import base64
import json
import os
import ssl
import sys
import urllib.request
import uuid
from datetime import datetime, timezone


def send(url, path, user=None, password=None, api_key=None, cafile=None, insecure=False, mode="raw", method="POST"):
    with open(path, "rb") as f:
        data = f.read()
    stem = os.path.splitext(os.path.basename(path))[0]
    name = f"{stem}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.zip"
    headers = {}
    if mode == "multipart":
        b = uuid.uuid4().hex
        body = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
                f"Content-Type: application/zip\r\n\r\n").encode() + data + f"\r\n--{b}--\r\n".encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={b}"
    elif mode == "json":
        body = json.dumps({"fileName": name, "data": base64.b64encode(data).decode()}).encode()
        headers["Content-Type"] = "application/json"
    else:
        body = data
        headers["Content-Type"] = "application/zip"
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
    if user:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{password or ''}".encode()).decode()
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    ctx = None
    if url.startswith("https"):
        ctx = ssl.create_default_context(cafile=cafile)
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("file")
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--api-key")
    ap.add_argument("--cafile")
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("--mode", choices=["raw", "multipart", "json"], default="raw")
    ap.add_argument("--method", choices=["POST", "PUT"], default="POST")
    a = ap.parse_args()
    code, resp = send(a.url, a.file, a.user, a.password, a.api_key, a.cafile, a.insecure, a.mode, a.method)
    print(code, json.dumps(resp))
    sys.exit(0 if code in (200, 201) else 1)
