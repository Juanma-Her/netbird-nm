"""Our gRPC client against a real gRPC server (the fake NetBird daemon).
Skipped when grpcio isn't importable for this Python."""

import importlib.machinery
import importlib.util
import json
import os
import pwd
import threading
import time
from pathlib import Path

import pytest

from netbird_nm import daemon

grpc = pytest.importorskip("grpc", exc_type=ImportError)   # also a broken/foreign build

REPO = Path(__file__).resolve().parents[2]


# -- against a real gRPC server ----------------------------------------------------------



def load_fake_daemon():
    path = REPO / "tests/integration/fake-netbirdd"
    loader = importlib.machinery.SourceFileLoader("fake_netbirdd", str(path))
    spec = importlib.util.spec_from_loader("fake_netbirdd", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class FakeIface:
    name = "wt0"

    def __init__(self):
        self.up = False

    def exists(self):
        return self.up

    def create(self):
        self.up = True

    def remove(self):
        self.up = False


@pytest.fixture
def server(tmp_path):
    mod = load_fake_daemon()
    iface = FakeIface()
    fake = mod.FakeNetBird(tmp_path / "state", iface, delay=0)
    sock = str(tmp_path / "nb.sock")
    threading.Thread(target=fake.serve, args=(sock,), daemon=True).start()
    for _ in range(100):
        if os.path.exists(sock):
            break
        time.sleep(0.05)
    return fake, iface, "unix://" + sock


def run_main(capsys, addr, *args):
    rc = daemon.main(["--addr", addr, *args])
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_main_up_status_down(server, capsys):
    fake, iface, addr = server
    me = pwd.getpwuid(os.getuid()).pw_name
    assert run_main(capsys, addr, "up")[:2] == (0, "Connected\n")
    assert iface.up
    rc, out, _ = run_main(capsys, addr, "status")
    st = json.loads(out)
    assert st["status"] == "Connected" and st["management_url"] == "https://198.51.100.10:443"
    assert st["ip"] == "100.92.1.5/16" and st["profile"] == "default"
    assert run_main(capsys, addr, "up")[1] == "Already connected\n"
    assert run_main(capsys, addr, "down")[:2] == (0, "Disconnected\n")
    assert not iface.up
    log = (fake.state / "log").read_text()
    assert f"uid={os.getuid()} Up username={me}" in log      # caller identified via SO_PEERCRED


def test_main_needs_login(server, capsys):
    fake, iface, addr = server
    fake.state.mkdir(parents=True, exist_ok=True)
    (fake.state / "fail").touch()
    rc, _, err = run_main(capsys, addr, "up")
    assert rc == 1 and "log in" in err and not iface.up


def test_main_daemon_not_running(tmp_path, capsys):
    rc, _, err = run_main(capsys, f"unix://{tmp_path}/nothing.sock", "status")
    assert rc == 1 and "can't reach the NetBird daemon" in err


def test_available():
    assert daemon.available() is True
