"""FTPS pull path against a local explicit-TLS FTP server laid out like File Archive Access
(topology folders, loose COMTRADE members and zipped records)."""
import io
import threading
import time
import zipfile

import pytest

pytest.importorskip("pyftpdlib")
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import TLS_FTPHandler
from pyftpdlib.servers import FTPServer

from conftest import free_port
from frbridge.ingest_files import FtpsIngest
from frbridge.store import Store
import make_sample_comtrade as msc


@pytest.fixture
def ftps_server(tmp_path, pki):
    root = tmp_path / "archive"
    (root / "Region" / "SS01" / "138kV" / "Bay01" / "7KE85_01" / "Fault records").mkdir(parents=True)
    auth = DummyAuthorizer()
    auth.add_user("ftp", "ftp-pw", str(root), perm="elr")
    handler = type("H", (TLS_FTPHandler,), {})
    handler.certfile = f"{pki}/receiver.pem"
    handler.keyfile = f"{pki}/receiver.key"
    handler.authorizer = auth
    handler.tls_control_required = True
    handler.tls_data_required = True
    handler.passive_ports = range(free_port(), free_port() + 1)
    port = free_port()
    srv = FTPServer(("127.0.0.1", port), handler)
    th = threading.Thread(target=srv.serve_forever, kwargs={"timeout": 0.2}, daemon=True)
    th.start()
    yield root, port
    srv.close_all()


def test_ftps_pull_groups_loose_comtrade(tmp_path, pki, ftps_server):
    root, port = ftps_server
    folder = root / "Region" / "SS01" / "138kV" / "Bay01" / "7KE85_01" / "Fault records"
    data, stem, _ = msc.build(device="7KE85_01", fault_no=11)
    with zipfile.ZipFile(io.BytesIO(data)) as z:          # loose members, as an archive may expose them
        for n in z.namelist():
            (folder / n).write_bytes(z.read(n))
    zdata, zstem, _ = msc.build(device="7KE85_01", fault_no=12)
    (folder / f"{zstem}.zip").write_bytes(zdata)          # and an already zipped record
    store = Store(str(tmp_path / "st"))
    got = []
    ing = FtpsIngest({"host": "127.0.0.1", "port": port, "username": "ftp", "password": "ftp-pw",
                      "ca_file": f"{pki}/ca.pem", "verify": True, "root": "/", "poll_s": 1, "max_depth": 8},
                     store, lambda d, n, src, h: got.append((n, d, h)))
    assert ing.poll_once() == 0                   # first listing: wait until sizes are stable
    assert ing.poll_once() == 2
    names = sorted(n for n, _, _ in got)
    assert names == [f"{stem}.zip", f"{zstem}.zip"]
    loose = [d for n, d, _ in got if n == f"{stem}.zip"][0]
    assert sorted(zipfile.ZipFile(io.BytesIO(loose)).namelist()) == sorted(f"{stem}{e}" for e in (".cfg", ".dat", ".hdr", ".inf"))
    assert got[0][2]["path"].endswith("7KE85_01/Fault records")
    assert ing.poll_once() == 0                   # already taken, not taken again
