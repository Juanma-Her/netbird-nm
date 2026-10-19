"""End-to-end tests against a real NetworkManager.

These start, stop and reconfigure NetworkManager, so they only run when
NETBIRD_NM_INTEGRATION=1 and as root, inside the sandbox from sandbox.sh
(see README.md in this directory).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CLI = [sys.executable, str(REPO / "netbird-nm")]
SANDBOX = ["sh", str(HERE / "sandbox.sh")]
FAKE_NETBIRD = str(HERE / "fake-netbird")
FAKE_STATE = Path("/tmp/fake-netbird")
CONFIG = Path("/etc/netbird-nm.json")
SERVICE = "org.freedesktop.NetworkManager.netbird"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("NETBIRD_NM_INTEGRATION") != "1" or os.geteuid() != 0,
                       reason="set NETBIRD_NM_INTEGRATION=1 and run as root in the sandbox"),
]


# -- helpers ---------------------------------------------------------------------------

def sh(*cmd: str, check: bool = False, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(cmd), capture_output=True, text=True, check=check, timeout=timeout)


def nm_up(name: str = "NetBird") -> subprocess.CompletedProcess[str]:
    return sh("nmcli", "connection", "up", name)


def nm_down(name: str = "NetBird") -> subprocess.CompletedProcess[str]:
    return sh("nmcli", "connection", "down", name)


def state(name: str = "NetBird") -> str:
    out = sh("nmcli", "-g", "GENERAL.STATE", "connection", "show", name).stdout.strip()
    return out or "inactive"


def tunnel() -> str:
    return "up" if Path("/sys/class/net/wt0").exists() else "down"


def wait_for(pred, timeout: float, step: float = 0.5) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


def settle(expect_state: str, expect_tunnel: str, timeout: float = 20) -> None:
    ok = wait_for(lambda: state() == expect_state and tunnel() == expect_tunnel, timeout)
    assert ok, f"wanted NM={expect_state} wt0={expect_tunnel}, got NM={state()} wt0={tunnel()}"


# No systemd in the sandbox: tell the installer how to restart NetworkManager.
os.environ.setdefault("NETBIRD_NM_RESTART_CMD", " ".join(SANDBOX + ["restart-nm"]))


def install(*extra: str) -> subprocess.CompletedProcess[str]:
    r = sh(*CLI, "install", "--netbird", FAKE_NETBIRD, "-y", *extra)
    assert r.returncode == 0, r.stdout + r.stderr
    return r


def uninstall(*extra: str) -> subprocess.CompletedProcess[str]:
    r = sh(*CLI, "uninstall", *extra)
    assert r.returncode == 0, r.stdout + r.stderr
    return r


def base(action: str) -> None:
    sh("nmcli", "connection", action, "sandbox-base")


def fake_netbird(cmd: str) -> None:
    sh(FAKE_NETBIRD, cmd, check=True)


def speed_up_timers() -> None:
    """Shorter dispatcher delays so the suite runs in reasonable time."""
    cfg = json.loads(CONFIG.read_text())
    cfg.update(adopt_delay=3.0)
    CONFIG.write_text(json.dumps(cfg))


# -- fixtures --------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def sandbox():
    sh(*SANDBOX, "start", check=True)
    yield
    sh(*CLI, "uninstall")
    sh("ip", "link", "del", "wt0")


@pytest.fixture
def installed():
    shutil.rmtree(FAKE_STATE, ignore_errors=True)
    sh("ip", "link", "del", "wt0")
    base("up")
    install()
    speed_up_timers()
    yield
    nm_down()
    sh(*CLI, "uninstall")
    sh("ip", "link", "del", "wt0")
    shutil.rmtree(FAKE_STATE, ignore_errors=True)


# -- the bug that started this: plugin dir not watched by NM ---------------------------

def test_works_when_etc_vpn_dir_did_not_exist_at_nm_start():
    """Arch ships /usr/lib/NetworkManager/VPN but not /etc/NetworkManager/VPN.
    NM only watches directories that exist when it starts, so the plugin
    description must go into the former (0.1 put it in the latter)."""
    sh(*CLI, "uninstall")
    etc = Path("/etc/NetworkManager/VPN")
    if etc.exists():
        shutil.rmtree(etc)
    sh(*SANDBOX, "restart-nm", check=True)
    install()
    try:
        assert not etc.exists()
        assert Path("/usr/lib/NetworkManager/VPN/nm-netbird-service.name").exists()
        r = nm_up()
        assert r.returncode == 0, r.stderr
        settle("activated", "up")
    finally:
        nm_down()
        uninstall()


def test_restarts_nm_when_no_vpn_dir_exists():
    """No VPN plugin directory at all: install must restart NM (with -y)."""
    sh(*CLI, "uninstall")
    lib = Path("/usr/lib/NetworkManager/VPN")
    moved = Path("/tmp/vpn-dir-backup")
    shutil.rmtree(moved, ignore_errors=True)
    if lib.exists():
        shutil.move(str(lib), str(moved))
    if Path("/etc/NetworkManager/VPN").exists():
        shutil.rmtree("/etc/NetworkManager/VPN")
    sh(*SANDBOX, "restart-nm", check=True)
    try:
        r = install()
        assert "Restarted NetworkManager" in r.stdout
        base("up")
        assert nm_up().returncode == 0
        settle("activated", "up")
    finally:
        nm_down()
        uninstall()
        if moved.exists():
            shutil.rmtree(lib, ignore_errors=True)
            shutil.move(str(moved), str(lib))
        sh(*SANDBOX, "restart-nm")


# -- everyday use ----------------------------------------------------------------------

def test_toggle_off_and_on_again(installed):
    assert nm_up().returncode == 0
    settle("activated", "up")
    assert nm_down().returncode == 0
    settle("inactive", "down")
    assert nm_up().returncode == 0, "reconnecting after a disconnect (the original bug)"
    settle("activated", "up")


def test_click_speed_toggling_stays_consistent(installed):
    nm_up()
    for _ in range(5):
        assert nm_down().returncode == 0
        time.sleep(0.3)
        assert nm_up().returncode == 0
        time.sleep(0.3)
    settle("activated", "up")


def test_scripted_toggling_never_desyncs(installed):
    """Back-to-back toggles can be rejected by NM itself, but NM and NetBird
    must always end up agreeing."""
    nm_up()
    for _ in range(5):
        nm_down()
        nm_up()
    time.sleep(6)
    assert (state(), tunnel()) in (("activated", "up"), ("inactive", "down"))


def test_netbird_stopped_outside_nm(installed):
    nm_up()
    settle("activated", "up")
    fake_netbird("down")
    settle("inactive", "down", timeout=15)
    time.sleep(5)
    assert state() == "inactive", "NM must not restart NetBird after you stopped it"
    assert nm_up().returncode == 0
    settle("activated", "up")


def test_network_change_keeps_netbird_and_comes_back(installed):
    nm_up()
    settle("activated", "up")
    base("down")
    settle("inactive", "up", timeout=10)        # VPN off in NM, NetBird still running
    base("up")
    settle("activated", "up", timeout=20)       # re-activated once a network is back


def test_switched_off_stays_off_across_network_change(installed):
    nm_up()
    nm_down()
    settle("inactive", "down")
    base("down")
    base("up")
    time.sleep(10)
    assert (state(), tunnel()) == ("inactive", "down")


def test_running_netbird_is_adopted_when_network_comes_up(installed):
    base("down")
    fake_netbird("up")                     # e.g. NetBird's own service connected at boot
    base("up")
    settle("activated", "up", timeout=25)


def test_failed_netbird_up_is_reported(installed):
    FAKE_STATE.mkdir(exist_ok=True)
    (FAKE_STATE / "fail").touch()
    r = nm_up()
    assert r.returncode != 0
    settle("inactive", "down")
    (FAKE_STATE / "fail").unlink()
    assert nm_up().returncode == 0
    settle("activated", "up")


# -- Quickshell (and anything else that drives NM through nmcli) ----------------------

def test_quickshell_lists_and_toggles_netbird(installed):
    """The same nmcli calls as quickshell/services/NetworkService.qml."""
    rows = sh("nmcli", "-t", "-f", "UUID,TYPE,ACTIVE,NAME", "connection", "show").stdout.splitlines()
    vpns = [r.split(":") for r in rows if r.split(":")[1] in ("vpn", "wireguard")]
    assert [(t, n) for _, t, _, n in vpns] == [("vpn", "NetBird")]
    uuid = vpns[0][0]

    monitor = subprocess.Popen(["nmcli", "monitor"], stdout=subprocess.PIPE, text=True)
    try:
        time.sleep(1)
        assert sh("nmcli", "connection", "up", "uuid", uuid).returncode == 0
        settle("activated", "up")
        rows = sh("nmcli", "-t", "-f", "UUID,TYPE,ACTIVE,NAME", "connection", "show").stdout
        assert f"{uuid}:vpn:yes:NetBird" in rows
        assert sh("nmcli", "connection", "down", "uuid", uuid).returncode == 0
        settle("inactive", "down")
    finally:
        monitor.terminate()
        out = monitor.communicate(timeout=5)[0]
    assert "NetBird" in out, "nmcli monitor (what the bar refreshes on) saw no VPN change"


def test_netbird_interface_is_not_shown_as_a_device(installed):
    nm_up()
    settle("activated", "up")
    devices = sh("nmcli", "-t", "-f", "DEVICE,STATE", "device").stdout
    assert "wt0:connected (externally)" not in devices


# -- tools -----------------------------------------------------------------------------

def test_status_and_doctor(installed):
    out = sh(*CLI, "status").stdout
    assert "VPN connection 'NetBird'" in out and "--" not in out.split("VPN connection")[0]
    r = sh(*CLI, "doctor", "--live")
    assert r.returncode == 0, r.stdout
    assert "No problems found" in r.stdout
    settle("inactive", "down")


def test_uninstall_leaves_netbird_running_and_cleans_up(installed):
    nm_up()
    settle("activated", "up")
    uninstall()
    time.sleep(4)
    assert tunnel() == "up", "uninstall must not cut the user's NetBird"
    assert "NetBird" not in sh("nmcli", "-t", "-f", "NAME", "connection", "show").stdout
    for path in ("/usr/local/libexec/nm-netbird-service", "/etc/netbird-nm.json",
                 "/usr/lib/NetworkManager/VPN/nm-netbird-service.name",
                 "/etc/NetworkManager/dispatcher.d/90-netbird-vpn", "/usr/local/lib/netbird-nm"):
        assert not Path(path).exists(), path
    fake_netbird("down")


def test_upgrade_from_shell_version(installed):
    """0.1 (netbird-nm-vpn.sh) installed: the Python install takes over."""
    legacy = HERE / "legacy" / "netbird-nm-vpn.sh"
    if not legacy.exists():
        pytest.skip("legacy script not present")
    uninstall()
    bindir = Path("/tmp/legacy-bin")
    bindir.mkdir(exist_ok=True)
    link = bindir / "netbird"
    if not link.exists():
        link.symlink_to(FAKE_NETBIRD)
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
    r = subprocess.run(["bash", str(legacy), "install"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert Path("/etc/NetworkManager/VPN/nm-netbird-service.name").exists()
    install()
    assert not Path("/etc/NetworkManager/VPN/nm-netbird-service.name").exists()
    rows = sh("nmcli", "-t", "-f", "TYPE,NAME", "connection", "show").stdout.splitlines()
    assert rows.count("vpn:NetBird") == 1, "the existing connection is kept, not duplicated"
    assert nm_up().returncode == 0
    settle("activated", "up")
