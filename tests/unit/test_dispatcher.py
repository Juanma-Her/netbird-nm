import pytest

from netbird_nm import dispatcher
from netbird_nm.config import Config
from netbird_nm.const import SERVICE
from netbird_nm.dispatcher import Decision, adopt, decide, handle
from netbird_nm.nmcli import Nmcli
from netbird_nm.state import StateDir

OURS = "netbird-uuid"
OTHER = "openvpn-uuid"


def is_ours(uuid):
    return uuid == OURS


@pytest.fixture
def state(tmp_path):
    return StateDir(tmp_path / "run")


@pytest.mark.parametrize("dev_state, expected", [
    ("100 (connected)", Decision("clear-resume")),          # you switched it off
    ("110 (deactivating)", Decision("set-resume", OURS)),   # Wi-Fi switch
    ("30 (disconnected)", Decision("set-resume", OURS)),
    ("20 (unavailable)", Decision("set-resume", OURS)),     # cable unplugged
    ("", Decision("set-resume", OURS)),                     # device gone
])
def test_vpn_down_tells_user_from_network(state, dev_state, expected):
    env = {"CONNECTION_UUID": OURS, "DEVICE_IFACE": "wlan0"}
    assert decide("vpn-down", env, is_ours=is_ours, device_state=lambda d: dev_state,
                  state=state) == expected


def test_other_vpns_are_ignored(state):
    env = {"CONNECTION_UUID": OTHER, "DEVICE_IFACE": "wlan0"}
    for action in ("vpn-down", "vpn-up"):
        assert decide(action, env, is_ours=is_ours, device_state=lambda d: "",
                      state=state) == Decision("none")


def test_vpn_up_clears_resume(state):
    assert decide("vpn-up", {"CONNECTION_UUID": OURS}, is_ours=is_ours,
                  device_state=lambda d: "", state=state) == Decision("clear-resume")


def test_network_up_resumes_or_adopts(state):
    kw = dict(is_ours=is_ours, device_state=lambda d: "", state=state)
    assert decide("up", {}, **kw) == Decision("adopt")
    state.set_resume(OURS)
    assert decide("up", {}, **kw) == Decision("resume")


@pytest.mark.parametrize("action", ["down", "pre-up", "dhcp4-change", "connectivity-change", "hostname"])
def test_other_actions_do_nothing(state, action):
    assert decide(action, {"CONNECTION_UUID": OURS}, is_ours=is_ours,
                  device_state=lambda d: "", state=state) == Decision("none")


# -- handle(): side effects ------------------------------------------------------------

def nm_with(runner, dev_state="110 (deactivating)"):
    runner.on(["nmcli", "-g", "vpn.service-type", "connection", "show", OURS], stdout=SERVICE)
    runner.on(["nmcli", "-g", "GENERAL.STATE", "device", "show"], stdout=dev_state)
    return Nmcli(runner)


def test_handle_network_drop_then_return(runner, state):
    spawned = []
    nm = nm_with(runner)
    env = {"CONNECTION_UUID": OURS, "DEVICE_IFACE": "wlan0"}
    handle("vpn-down", env, nm=nm, state=state, launcher="/hook", spawn=lambda *a: spawned.append(a),
           log=lambda m: None)
    assert state.has_resume()
    handle("up", {"CONNECTION_ID": "Cafe"}, nm=nm, state=state, launcher="/hook",
           spawn=lambda *a: spawned.append(a), log=lambda m: None)
    assert spawned == [("/hook", "--resume", OURS)]
    assert not state.has_resume()


def test_handle_user_switch_off_does_not_resume(runner, state):
    spawned = []
    nm = nm_with(runner, dev_state="100 (connected)")
    state.set_resume(OURS)   # stale flag from earlier
    handle("vpn-down", {"CONNECTION_UUID": OURS, "DEVICE_IFACE": "wlan0"}, nm=nm, state=state,
           launcher="/hook", spawn=lambda *a: spawned.append(a), log=lambda m: None)
    assert not state.has_resume()
    handle("up", {}, nm=nm, state=state, launcher="/hook",
           spawn=lambda *a: spawned.append(a), log=lambda m: None)
    assert spawned == [("/hook", "--adopt")]


# -- adopt() ---------------------------------------------------------------------------------

def adopt_nm(runner, active=""):
    runner.on(["nmcli", "-t", "-f", "UUID,TYPE,NAME"], stdout=f"{OURS}:vpn:NetBird\n")
    runner.on(["nmcli", "-g", "vpn.service-type", "connection", "show", OURS], stdout=SERVICE)
    runner.on(["nmcli", "-g", "GENERAL.STATE", "connection", "show", OURS], stdout=active)
    return Nmcli(runner)


def test_adopt_brings_vpn_up_when_netbird_runs(runner, state, clock):
    nm = adopt_nm(runner)
    assert adopt(nm=nm, cfg=Config(), state=state, sleep=clock, exists=lambda i: True,
                 log=lambda m: None)
    assert runner.ran("nmcli", "connection", "up", "uuid", OURS)
    assert clock.slept == Config().adopt_delay + 3


@pytest.mark.parametrize("why", ["no-netbird", "already-active", "down-pending"])
def test_adopt_leaves_things_alone(runner, state, clock, why):
    nm = adopt_nm(runner, active="activated" if why == "already-active" else "")
    if why == "down-pending":
        state.mark_down_pending()
    assert not adopt(nm=nm, cfg=Config(), state=state, sleep=clock,
                     exists=lambda i: why != "no-netbird", log=lambda m: None)
    assert not runner.ran("nmcli", "connection", "up")


def test_adopt_rechecks_before_acting(runner, state):
    """A toggle that starts between the two checks must win."""
    nm = adopt_nm(runner)

    def sleep(s):
        if s == 3:   # between the two checks the user turns it on themselves
            runner.on(["nmcli", "-g", "GENERAL.STATE", "connection", "show", OURS], stdout="activating")
    assert not adopt(nm=nm, cfg=Config(), state=state, sleep=sleep, exists=lambda i: True,
                     log=lambda m: None)


def test_adopt_without_profile(runner, state, clock):
    runner.on(["nmcli", "-t", "-f", "UUID,TYPE,NAME"], stdout="")
    assert not adopt(nm=Nmcli(runner), cfg=Config(), state=state, sleep=clock,
                     exists=lambda i: True, log=lambda m: None)


def test_main_ignores_irrelevant_actions(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(dispatcher, "handle", lambda *a, **k: called.append(a))
    monkeypatch.setattr(dispatcher.Config, "load", classmethod(
        lambda cls, path=None: Config(state_dir=str(tmp_path))))
    dispatcher.main(["wlan0", "dhcp4-change"], launcher="/hook")
    dispatcher.main(["wlan0", "connectivity-change"], launcher="/hook")
    assert called == []
    dispatcher.main(["wlan0", "up"], launcher="/hook")
    assert len(called) == 1
