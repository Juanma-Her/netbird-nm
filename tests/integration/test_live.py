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
# The fakes get copied somewhere every user can execute them.
FAKE_DIR = Path("/usr/local/lib/netbird-nm-test")
FAKE_NETBIRD = str(FAKE_DIR / "fake-netbird")
FAKE_SOCK = "/run/fake-netbird.sock"
MGMT_IP = "198.51.100.10"        # the fake daemon's management server (TEST-NET-2)
FAKE_STATE = Path("/tmp/fake-netbird")
TEST_USER = "nbtest"          # ordinary users who "log in" to the desktop in these tests
TEST_USER2 = "nbtest2"
SEAT0 = Path("/run/systemd/seats/seat0")   # what systemd-logind writes; emulated here
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


def has_ipv4() -> bool:
    return "inet " in sh("ip", "-4", "-o", "addr", "show", "dev", "wt0").stdout


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
    r = sh(*CLI, "install", "--netbird", FAKE_NETBIRD, "--daemon-addr", "unix://" + FAKE_SOCK,
           "-y", *extra)
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


def logged_in(user: str | None) -> None:
    """Pretend ``user`` has the active desktop session (None: nobody logged in)."""
    if user is None:
        SEAT0.unlink(missing_ok=True)
        return
    SEAT0.parent.mkdir(parents=True, exist_ok=True)
    SEAT0.write_text(f"IS_SEAT0=1\nACTIVE=c1\nACTIVE_UID={uid_of(user)}\n")


def uid_of(user: str) -> int:
    import pwd
    return pwd.getpwnam(user).pw_uid


def callers(cmd: str) -> list[int]:
    """uids that made RPC ``cmd`` ("up" → Up) to the fake daemon, in order."""
    method = " " + cmd.capitalize() + " "
    return [int(ln.split("uid=")[1].split()[0]) for ln in daemon_log() if method in ln + " "]


def daemon_log() -> list[str]:
    try:
        return (FAKE_STATE / "log").read_text().splitlines()
    except FileNotFoundError:
        return []


def reset_fake_state() -> None:
    FAKE_STATE.mkdir(exist_ok=True)
    for f in FAKE_STATE.iterdir():
        f.unlink()


def speed_up_timers() -> None:
    """Shorter dispatcher delays so the suite runs in reasonable time."""
    cfg = json.loads(CONFIG.read_text())
    cfg.update(adopt_delay=3.0)
    CONFIG.write_text(json.dumps(cfg))


# -- fixtures --------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def sandbox():
    sh(*SANDBOX, "start", check=True)
    for user in (TEST_USER, TEST_USER2):
        if sh("id", user).returncode != 0:
            sh("useradd", "--create-home", user, check=True)
    FAKE_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("fake-netbird", "fake-netbirdd"):
        shutil.copy(HERE / name, FAKE_DIR / name)
        (FAKE_DIR / name).chmod(0o755)
    # the fakes speak gRPC with the package's own codec; give them a copy any user can read
    shutil.rmtree(FAKE_DIR / "netbird_nm", ignore_errors=True)
    shutil.copytree(REPO / "src" / "netbird_nm", FAKE_DIR / "netbird_nm",
                    ignore=shutil.ignore_patterns("__pycache__"))
    sh("chmod", "-R", "a+rX", str(FAKE_DIR))
    sh("pkill", "-f", "[f]ake-netbirdd")
    daemon = subprocess.Popen([sys.executable, str(FAKE_DIR / "fake-netbirdd")],
                              stderr=subprocess.DEVNULL, start_new_session=True)
    assert wait_for(lambda: Path("/run/fake-netbird.sock").exists(), 10)
    yield
    sh(*CLI, "uninstall")
    sh("ip", "link", "del", "wt0")
    logged_in(None)
    daemon.terminate()


@pytest.fixture
def installed():
    reset_fake_state()
    sh("ip", "link", "del", "wt0")
    base("up")
    logged_in(TEST_USER)
    install()
    speed_up_timers()
    yield
    nm_down()
    sh(*CLI, "uninstall")
    reset_fake_state()
    sh(FAKE_NETBIRD, "down")      # also cancels a delayed interface from a "slow" test
    sh("ip", "link", "del", "wt0")


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
    assert has_ipv4(), "NetBird must keep its address, not just its interface"
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


# -- 0.2.1 -----------------------------------------------------------------------------

def test_install_does_not_pin_a_user(installed):
    assert json.loads(CONFIG.read_text())["run_as"] == "session"


def test_netbird_runs_as_the_logged_in_user(installed):
    """Connect and disconnect both reach the daemon as the desktop user, not root."""
    assert nm_up().returncode == 0
    settle("activated", "up")
    nm_down()
    settle("inactive", "down")
    assert callers("up") == [uid_of(TEST_USER)]
    assert callers("down") == [uid_of(TEST_USER)]


def test_user_follows_whoever_is_logged_in(installed):
    nm_up()
    settle("activated", "up")
    nm_down()
    settle("inactive", "down")
    logged_in(TEST_USER2)                      # another person logs in
    assert nm_up().returncode == 0
    settle("activated", "up")
    assert callers("up") == [uid_of(TEST_USER), uid_of(TEST_USER2)]


def test_down_uses_the_user_who_connected(installed):
    nm_up()
    settle("activated", "up")
    logged_in(TEST_USER2)                      # user switch while connected
    nm_down()
    settle("inactive", "down")
    assert callers("down") == [uid_of(TEST_USER)]


def test_nobody_logged_in_runs_as_root(installed):
    logged_in(None)
    assert nm_up().returncode == 0
    settle("activated", "up")
    assert callers("up") == [0]


def test_fixed_user_and_root_options(installed):
    install("--user", TEST_USER2)
    assert json.loads(CONFIG.read_text())["run_as"] == TEST_USER2
    nm_up()
    settle("activated", "up")
    nm_down()
    settle("inactive", "down")
    install("--as-root")
    nm_up()
    settle("activated", "up")
    install("--session-user")
    assert json.loads(CONFIG.read_text())["run_as"] == "session"
    assert callers("up")[-2:] == [uid_of(TEST_USER2), 0]


def test_slow_first_connection_still_succeeds(installed):
    """The interface appearing 30s after 'netbird up' returned (0.2 gave up after 20s)."""
    (FAKE_STATE / "slow").write_text("30")
    r = nm_up()
    assert r.returncode == 0, r.stderr
    settle("activated", "up")


def test_failure_explains_itself_in_the_log(installed):
    """When the interface never appears, the log says what NetBird reported."""
    log = Path(os.environ.get("NM_SANDBOX_LOG", "/tmp/nm-sandbox.log"))
    start = log.stat().st_size
    (FAKE_STATE / "slow").write_text("90")              # longer than the whole budget
    r = nm_up()
    assert r.returncode != 0
    with open(log, errors="replace") as f:
        f.seek(start)
        new = f.read()
    assert "netbird up said: Connected" in new
    assert 'netbird status: {"status": "Idle"' in new         # what the daemon reported
    assert "IPv4 addresses:" in new
    settle("inactive", "down")


def test_doctor_checks_the_user(installed):
    out = sh(*CLI, "doctor").stdout
    assert f"now: {TEST_USER}" in out and f"{TEST_USER} can reach the NetBird daemon" in out


def test_connects_when_netbird_was_left_without_its_address(installed):
    """The first failure seen on a real machine: NetBird still running from
    before, but NetworkManager had removed its address, so 'netbird up' said
    "Already connected" and the interface never got an address."""
    fake_netbird("up")
    sh("ip", "-4", "addr", "flush", "dev", "wt0")
    assert tunnel() == "up" and not has_ipv4()
    r = nm_up()
    assert r.returncode == 0, r.stderr
    settle("activated", "up")
    assert has_ipv4()


def test_network_change_keeps_address_after_resume(installed):
    nm_up()
    settle("activated", "up")
    base("down")
    base("up")
    settle("activated", "up", timeout=40)
    assert has_ipv4()


# -- 0.4.0 -----------------------------------------------------------------------------------

def test_default_backend_is_grpc(installed):
    assert json.loads(CONFIG.read_text())["backend"] == "grpc"
    nm_up()
    settle("activated", "up")
    methods = [ln.split()[2] for ln in daemon_log()]
    i = methods.index("Up")       # (install's own check made a few Status calls before)
    assert methods[i - 3:i + 1] == ["Status", "GetActiveProfile", "Login", "Up"], methods


def test_nm_leaves_netbird_address_alone(installed):
    """NM is told there's no IP config to manage, so taking the VPN down no longer
    strips NetBird's address (the bug 0.2.1 had to work around)."""
    nm_up()
    settle("activated", "up")
    touch_keep = Path("/run/nm-netbird/keep-running")
    touch_keep.touch()                       # make the disconnect leave NetBird running
    try:
        nm_down()
        settle("inactive", "up")
        time.sleep(3)
        assert has_ipv4()
    finally:
        touch_keep.unlink(missing_ok=True)
    assert nm_up().returncode == 0
    settle("activated", "up")
    assert callers("down") == [], "no restart was needed"


def test_gateway_route_points_at_management_server(installed):
    nm_up()
    settle("activated", "up")
    routes = sh("ip", "-4", "route").stdout
    assert f"{MGMT_IP} via 10.99.0.254 dev nmsbx-base" in routes, routes
    assert "100.92.1.5 via" not in routes            # 0.3 routed our own tunnel address


def test_cli_backend(installed):
    install("--backend", "cli")
    assert json.loads(CONFIG.read_text())["backend"] == "cli"
    assert nm_up().returncode == 0
    settle("activated", "up")
    nm_down()
    settle("inactive", "down")
    assert callers("up") == [uid_of(TEST_USER)] and callers("down") == [uid_of(TEST_USER)]
    routes = sh("ip", "-4", "route").stdout
    assert "100.92.1.5 via" not in routes


def test_custom_vpn_dir(installed):
    install("--vpn-dir", "/etc/NetworkManager/VPN", "--no-restart-nm")
    assert Path("/etc/NetworkManager/VPN/nm-netbird-service.name").exists()
    assert not Path("/usr/lib/NetworkManager/VPN/nm-netbird-service.name").exists()
    out = sh(*CLI, "status").stdout
    assert "/etc/NetworkManager/VPN/nm-netbird-service.name" in out
    install("--vpn-dir", "/usr/lib/NetworkManager/VPN")
    assert not Path("/etc/NetworkManager/VPN/nm-netbird-service.name").exists()
    assert nm_up().returncode == 0
