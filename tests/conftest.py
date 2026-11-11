import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "siapp", "FaultRecordBridge", "app"))
sys.path.insert(0, os.path.join(ROOT, "tools"))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def find_bash() -> str:
    """A POSIX bash. On Windows, plain "bash" resolves to System32\\bash.exe (the WSL launcher),
    which cannot see Windows temp paths, so prefer Git for Windows' bash."""
    if os.name != "nt":
        return "bash"
    candidates = []
    git = shutil.which("git")
    if git:  # ...\Git\cmd\git.exe -> ...\Git\bin\bash.exe
        candidates.append(os.path.join(os.path.dirname(os.path.dirname(git)), "bin", "bash.exe"))
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"), os.environ.get("LOCALAPPDATA")):
        if base:
            candidates += [os.path.join(base, "Git", "bin", "bash.exe"),
                           os.path.join(base, "Programs", "Git", "bin", "bash.exe")]
    for c in candidates:
        if os.path.isfile(c):
            return c
    pytest.skip("Git Bash not found (needed for tools/gen_lab_certs.sh on Windows)")


@pytest.fixture(scope="session")
def pki():
    d = tempfile.mkdtemp(prefix="frb-pki-")
    subprocess.run([find_bash(), os.path.join(ROOT, "tools", "gen_lab_certs.sh"), d, "127.0.0.1", "127.0.0.1"],
                   check=True, capture_output=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


class Broker:
    def __init__(self, pki, port):
        self.pki, self.port = pki, port
        self.dir = tempfile.mkdtemp(prefix="frb-broker-")
        self.pw = {"frb01": "bridge-pw", "ems": "ems-pw"}
        pwfile = os.path.join(self.dir, "passwd")
        open(pwfile, "w").close()
        for u, p in self.pw.items():
            subprocess.run(["mosquitto_passwd", "-b", pwfile, u, p], check=True, capture_output=True)
        acl = open(os.path.join(ROOT, "tools", "broker", "acl")).read()
        with open(os.path.join(self.dir, "acl"), "w") as f:
            f.write(acl)
        os.chmod(pwfile, 0o600)
        os.chmod(os.path.join(self.dir, "acl"), 0o600)
        conf = f"""user root
per_listener_settings false
allow_anonymous false
password_file {pwfile}
acl_file {self.dir}/acl
listener {port} 127.0.0.1
cafile {pki}/ca.pem
certfile {pki}/broker.pem
keyfile {pki}/broker.key
max_packet_size 2097152
persistence true
persistence_location {self.dir}/
log_dest file {self.dir}/broker.log
"""
        self.conf = os.path.join(self.dir, "mosquitto.conf")
        with open(self.conf, "w") as f:
            f.write(conf)
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(["mosquitto", "-c", self.conf], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("broker did not start: " + open(os.path.join(self.dir, "broker.log")).read())

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(5)
            self.proc = None


@pytest.fixture
def broker(pki):
    b = Broker(pki, free_port())
    b.start()
    yield b
    b.stop()
    shutil.rmtree(b.dir, ignore_errors=True)
