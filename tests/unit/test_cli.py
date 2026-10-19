import sys

import pytest

from netbird_nm import cli
from netbird_nm.installer import Installer, Layout


def test_parser_defaults():
    a = cli.build_parser().parse_args(["install"])
    assert a.name == "NetBird" and a.restart_nm is None and not a.yes and not a.no_deps


def test_parser_restart_flags_are_exclusive(capsys):
    p = cli.build_parser()
    assert p.parse_args(["install", "--restart-nm"]).restart_nm is True
    assert p.parse_args(["install", "--no-restart-nm"]).restart_nm is False
    with pytest.raises(SystemExit):
        p.parse_args(["install", "--restart-nm", "--no-restart-nm"])


def test_parser_requires_a_command():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_ensure_root_reexecs_with_sudo(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(cli.os, "execvp", lambda f, args: calls.append(args))
    cli.ensure_root(["install", "--name", "Mesh"])
    (args,) = calls
    assert args[:3] == ["sudo", sys.executable, "-c"]
    assert cli.SRC_DIR in args[3] and args[4:] == ["install", "--name", "Mesh"]


def test_ensure_root_noop_as_root(monkeypatch):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli.os, "execvp", lambda *a: pytest.fail("should not re-exec"))
    cli.ensure_root(["install"])


class StubNM:
    def __init__(self, running=True):
        self._running = running

    def running(self):
        return self._running


def test_install_fails_cleanly_without_networkmanager(tmp_path, capsys):
    inst = Installer(Layout(root=tmp_path), StubNM(running=False))
    args = cli.build_parser().parse_args(["install"])
    assert cli.cmd_install(args, inst) == 1
    assert "NetworkManager isn't running" in capsys.readouterr().err


def test_install_asks_before_restarting_nm(tmp_path, monkeypatch):
    """No VPN dir exists → NM must restart; without -y we ask, and pass the answer on."""
    seen = {}

    class Inst(Installer):
        def install(self, opts):
            seen["restart"] = opts.restart_nm
            raise RuntimeError("stop here")

    monkeypatch.setattr(cli, "find_python", lambda: sys.executable)
    monkeypatch.setattr(cli, "ask", lambda q, default: seen.setdefault("asked", q) and False)
    inst = Inst(Layout(root=tmp_path), StubNM())
    assert cli.cmd_install(cli.build_parser().parse_args(["install"]), inst) == 1
    assert "restarted" in seen["asked"] and seen["restart"] is False

    seen.clear()
    cli.cmd_install(cli.build_parser().parse_args(["install", "-y"]), inst)
    assert "asked" not in seen and seen["restart"] is True


def test_install_does_not_ask_when_dir_is_watched(tmp_path, monkeypatch):
    (tmp_path / "usr/lib/NetworkManager/VPN").mkdir(parents=True)
    seen = {}

    class Inst(Installer):
        def install(self, opts):
            seen["restart"] = opts.restart_nm
            raise RuntimeError("stop here")

    monkeypatch.setattr(cli, "find_python", lambda: sys.executable)
    monkeypatch.setattr(cli, "ask", lambda *a: pytest.fail("should not ask"))
    cli.cmd_install(cli.build_parser().parse_args(["install"]), Inst(Layout(root=tmp_path), StubNM()))
    assert seen["restart"] is False


def test_install_reports_missing_bindings_with_no_deps(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "find_python", lambda: None)
    inst = Installer(Layout(root=tmp_path), StubNM())
    assert cli.cmd_install(cli.build_parser().parse_args(["install", "--no-deps"]), inst) == 1
    assert "libnm" in capsys.readouterr().err


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as e:
        cli.build_parser().parse_args(["--version"])
    assert e.value.code == 0
    assert "netbird-nm" in capsys.readouterr().out


def test_install_asks_before_installing_bindings(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "find_python", lambda: None)
    monkeypatch.setattr(cli, "deps_command", lambda: ["pacman", "-S", "python-gobject"])
    monkeypatch.setattr(cli, "ask", lambda q, default: False)
    ran = []
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **k: ran.append(cmd))
    inst = Installer(Layout(root=tmp_path), StubNM())
    assert cli.cmd_install(cli.build_parser().parse_args(["install"]), inst) == 1
    assert ran == []
    assert "libnm bindings" in capsys.readouterr().err


def test_install_falls_back_to_cli_when_grpcio_declined(tmp_path, monkeypatch):
    seen = {}

    class Inst(Installer):
        def install(self, opts):
            seen["backend"] = opts.backend
            raise RuntimeError("stop here")

    monkeypatch.setattr(cli, "find_python", lambda: sys.executable)
    monkeypatch.setattr(cli, "has_grpc", lambda py: False)
    monkeypatch.setattr(cli, "deps_command", lambda table=None: ["pacman", "-S", "python-grpcio"])
    monkeypatch.setattr(cli, "ask", lambda q, default: False)
    (tmp_path / "usr/lib/NetworkManager/VPN").mkdir(parents=True)
    cli.cmd_install(cli.build_parser().parse_args(["install"]), Inst(Layout(root=tmp_path), StubNM()))
    assert seen["backend"] == "cli"


def test_install_forced_grpc_without_grpcio_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "find_python", lambda: sys.executable)
    monkeypatch.setattr(cli, "has_grpc", lambda py: False)
    monkeypatch.setattr(cli, "deps_command", lambda table=None: None)
    inst = Installer(Layout(root=tmp_path), StubNM())
    assert cli.cmd_install(cli.build_parser().parse_args(["install", "--backend", "grpc"]), inst) == 1
    assert "grpcio" in capsys.readouterr().err


def test_new_install_options_parse():
    a = cli.build_parser().parse_args(["install", "--backend", "cli", "--daemon-addr", "unix:///x",
                                       "--vpn-dir", "/etc/NetworkManager/VPN"])
    assert (a.backend, a.daemon_addr, a.vpn_dir) == ("cli", "unix:///x", "/etc/NetworkManager/VPN")
