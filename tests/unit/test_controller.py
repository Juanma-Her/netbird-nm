import socket
import struct
import subprocess

import pytest

from netbird_nm.config import Config
from netbird_nm.controller import ConnectError, Controller, TunnelInfo, Watchdog, ip_to_u32, vpn_config
from netbird_nm.netbird import NetBird
from netbird_nm.state import StateDir


def make(tmp_path, runner, clock, ipv4=("100.92.1.5", 16), **cfg):
    config = Config(netbird="/bin/netbird", state_dir=str(tmp_path / "run"), **cfg)
    logs = []
    ctl = Controller(config, netbird=NetBird(config.netbird, runner),
                     state=StateDir(config.state_dir), log=logs.append, sleep=clock,
                     get_ipv4=(ipv4 if callable(ipv4) else (lambda iface: ipv4)),
                     exists=lambda iface: True)
    return ctl, logs


# -- connect -------------------------------------------------------------------

def test_bring_up_runs_netbird_and_reports_address(tmp_path, runner, clock):
    ctl, logs = make(tmp_path, runner, clock)
    info = ctl.bring_up()
    assert runner.calls == [["/bin/netbird", "up"]]
    assert info == TunnelInfo("wt0", "100.92.1.5", 16)
    assert "up: 100.92.1.5/16 on wt0" in logs


def test_bring_up_waits_for_pending_down(tmp_path, runner):
    pending = []

    def sleep(s):
        pending.append(s)
        if len(pending) == 4:
            ctl.state.clear_down_pending()
    ctl, _ = make(tmp_path, runner, sleep)
    ctl.state.mark_down_pending()
    ctl.bring_up()
    assert len(pending) == 4      # waited until the marker went away, then ran up
    assert runner.calls == [["/bin/netbird", "up"]]


def test_bring_up_failure_message(tmp_path, runner, clock):
    runner.on(["/bin/netbird", "up"], returncode=1, stderr="daemon is not running\n")
    ctl, _ = make(tmp_path, runner, clock)
    with pytest.raises(ConnectError, match="netbird up failed: daemon is not running"):
        ctl.bring_up()


def test_bring_up_timeout_hints_at_login(tmp_path, clock):
    def runner(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
    ctl, _ = make(tmp_path, runner, clock)
    with pytest.raises(ConnectError, match="logged in"):
        ctl.bring_up()


def test_bring_up_missing_binary(tmp_path, clock):
    def runner(cmd, **kw):
        raise FileNotFoundError(cmd[0])
    ctl, _ = make(tmp_path, runner, clock)
    with pytest.raises(ConnectError, match="cannot run /bin/netbird"):
        ctl.bring_up()


def test_bring_up_waits_for_address(tmp_path, runner, clock):
    answers = iter([None, None, ("100.92.1.5", 16)])
    ctl, _ = make(tmp_path, runner, clock, ipv4=lambda iface: next(answers))
    assert ctl.bring_up().address == "100.92.1.5"
    assert clock.slept == 1.0


def test_bring_up_gives_up_without_address(tmp_path, runner, clock):
    ctl, _ = make(tmp_path, runner, clock, ipv4=lambda iface: None)
    with pytest.raises(ConnectError, match="never got an IPv4 address"):
        ctl.bring_up()


def test_bring_up_respects_configured_iface(tmp_path, runner, clock):
    seen = []
    ctl, _ = make(tmp_path, runner, clock, iface="nb0",
                  ipv4=lambda iface: seen.append(iface) or ("10.0.0.1", 24))
    assert ctl.bring_up().iface == "nb0"
    assert seen == ["nb0"]


# -- disconnect ------------------------------------------------------------------

def test_run_down_stops_netbird_after_grace(tmp_path, runner, clock):
    ctl, logs = make(tmp_path, runner, clock)
    ctl.state.mark_down_pending()
    assert ctl.run_down() == "down"
    assert runner.calls == [["/bin/netbird", "down"]]
    assert clock.slept == ctl.cfg.down_grace
    assert not ctl.state.down_pending()


def test_run_down_keeps_netbird_on_network_change(tmp_path, runner):
    def sleep(_):
        ctl.state.set_resume("u1")        # dispatcher writes this shortly after
    ctl, logs = make(tmp_path, runner, sleep)
    ctl.state.mark_down_pending()
    assert ctl.run_down() == "resume"
    assert runner.calls == []
    assert not ctl.state.down_pending()


def test_run_down_keeps_netbird_during_uninstall(tmp_path, runner, clock):
    ctl, _ = make(tmp_path, runner, clock)
    ctl.state.set_keep()
    assert ctl.run_down() == "keep"
    assert runner.calls == []


def test_run_down_clears_marker_even_if_netbird_breaks(tmp_path, clock):
    def runner(cmd, **kw):
        raise OSError("boom")
    ctl, logs = make(tmp_path, runner, clock)
    ctl.state.mark_down_pending()
    assert ctl.run_down() == "down"
    assert not ctl.state.down_pending()
    assert any("netbird down failed" in m for m in logs)


# -- NM config ---------------------------------------------------------------------

def test_ip_to_u32_is_network_byte_order():
    assert struct.pack("=I", ip_to_u32("100.92.1.5")) == socket.inet_aton("100.92.1.5")


def test_vpn_config_contents():
    config, ip4 = vpn_config(TunnelInfo("wt0", "100.92.1.5", 16))
    assert config["tundev"] == ("s", "wt0")
    assert config["gateway"] == ("u", ip_to_u32("100.92.1.5"))
    assert config["has-ip4"] == ("b", True) and config["has-ip6"] == ("b", False)
    assert ip4["prefix"] == ("u", 16)
    assert ip4["never-default"] == ("b", True)   # don't hijack the default route


# -- watchdog --------------------------------------------------------------------------

def test_watchdog_needs_two_misses():
    alive = iter([True, False, True, False, False])
    dog = Watchdog(lambda: next(alive))
    assert [dog.tick() for _ in range(5)] == [True, True, True, True, False]
