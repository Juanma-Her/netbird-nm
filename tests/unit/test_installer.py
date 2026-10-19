import os
import stat
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from netbird_nm.config import Config
from netbird_nm.const import LEGACY_MARKER, MARKER, SERVICE
from netbird_nm.installer import (
    Installer,
    InstallOptions,
    Layout,
    find_python,
    is_ours,
    launcher,
    parse_name_file,
)
from netbird_nm.nmcli import Nmcli


class FakeNM(Nmcli):
    """Just enough NetworkManager for the installer."""

    def __init__(self):
        self.profiles = {}           # uuid -> dict(name, type, service, persistent, state)
        self.log = []
        self.nmrunning = True

    def running(self):
        return self.nmrunning

    def connections(self):
        return [(u, p["type"], p["name"]) for u, p in self.profiles.items()]

    def field(self, uuid, name):
        p = self.profiles.get(uuid, {})
        return {"vpn.service-type": p.get("service", ""), "connection.id": p.get("name", "")}.get(name, "")

    def connection_state(self, uuid):
        return self.profiles[uuid].get("state", "")

    def add_vpn(self, name, service, extra=()):
        uuid = f"uuid-{len(self.profiles)}"
        opts = dict(zip(extra[::2], extra[1::2]))
        self.profiles[uuid] = {"name": name, "type": "vpn", "service": service,
                               "persistent": opts.get("vpn.persistent", "no"), "opts": opts}
        self.log.append(("add", name))
        import subprocess
        return subprocess.CompletedProcess([], 0, "", "")

    def modify(self, uuid, *settings):
        self.profiles[uuid].update({"persistent": settings[1]} if settings[0] == "vpn.persistent" else {})
        self.log.append(("modify", uuid, *settings))

    def down(self, uuid):
        self.log.append(("down", uuid))
        self.profiles[uuid]["state"] = ""

    def delete(self, uuid):
        self.log.append(("delete", uuid))
        del self.profiles[uuid]

    def reload_conf(self):
        return True


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "root"
    (r / "usr/lib/NetworkManager/VPN").mkdir(parents=True)   # like Arch: no /etc/NetworkManager/VPN
    (r / "etc/NetworkManager/dispatcher.d").mkdir(parents=True)
    return r


@pytest.fixture
def fake_netbird(tmp_path):
    nb = tmp_path / "netbird"
    nb.write_text("#!/bin/sh\n")
    nb.chmod(0o755)
    return str(nb)


def make(root, nm=None):
    msgs = []
    return Installer(Layout(root=root), nm or FakeNM(), say=msgs.append, warn=msgs.append)


def opts(netbird, **kw):
    return InstallOptions(netbird=netbird, python=sys.executable, iface=kw.pop("iface", "wt0"),
                          manage_system=False, **kw)


# -- choosing the VPN plugin directory (the "not installed" bug) ------------------------

def test_prefers_lib_dir_that_nm_watches(root):
    d, restart = make(root).choose_vpn_dir()
    assert d == root / "usr/lib/NetworkManager/VPN"
    assert restart is False


def test_lib_dir_preferred_even_when_etc_exists(root):
    (root / "etc/NetworkManager/VPN").mkdir(parents=True)
    d, _ = make(root).choose_vpn_dir()
    assert d == root / "usr/lib/NetworkManager/VPN"


def test_uses_etc_when_lib_dir_missing(root):
    (root / "usr/lib/NetworkManager/VPN").rmdir()
    (root / "etc/NetworkManager/VPN").mkdir(parents=True)
    d, restart = make(root).choose_vpn_dir()
    assert d == root / "etc/NetworkManager/VPN" and restart is False


def test_needs_restart_when_no_vpn_dir_exists(root):
    (root / "usr/lib/NetworkManager/VPN").rmdir()
    d, restart = make(root).choose_vpn_dir()
    assert d == root / "usr/lib/NetworkManager/VPN" and restart is True


# -- install ------------------------------------------------------------------------------

def test_install_writes_everything(root, fake_netbird):
    inst = make(root)
    res = inst.install(opts(fake_netbird))
    assert res["profile"] == "created" and res["needs_restart"] is False
    name = root / "usr/lib/NetworkManager/VPN/nm-netbird-service.name"
    assert name.exists()
    assert not (root / "etc/NetworkManager/VPN").exists()

    cp = parse_name_file(name.read_text())
    assert cp["VPN Connection"]["service"] == SERVICE
    assert cp["VPN Connection"]["program"] == "/usr/local/libexec/nm-netbird-service"

    plugin = root / "usr/local/libexec/nm-netbird-service"
    disp = root / "etc/NetworkManager/dispatcher.d/90-netbird-vpn"
    for exe in (plugin, disp):
        assert stat.S_IMODE(exe.stat().st_mode) == 0o755
        assert exe.read_text().startswith(f"#!{sys.executable}\n")
    assert "netbird_nm.service" in plugin.read_text()
    assert "netbird_nm.dispatcher" in disp.read_text()

    pkg = root / "usr/local/lib/netbird-nm/netbird_nm"
    assert (pkg / "service.py").exists() and (pkg / "dispatcher.py").exists()

    cfg = Config.load(root / "etc/netbird-nm.json")
    assert cfg.netbird == fake_netbird and cfg.iface == "wt0"

    ET.fromstring((root / "etc/dbus-1/system.d/nm-netbird-service.conf").read_text())
    assert "interface-name:wt0" in (root / "etc/NetworkManager/conf.d/99-netbird-unmanaged.conf").read_text()


def test_installed_files_are_world_readable_not_writable(root, fake_netbird):
    make(root).install(opts(fake_netbird))
    for path in root.rglob("*"):
        if path.is_file():
            mode = stat.S_IMODE(path.stat().st_mode)
            assert not mode & 0o022, path      # NM ignores group/world-writable plugin files
            assert mode & 0o004, path


def test_installed_launchers_run(root, fake_netbird):
    """The launcher must find the copied package (not the source tree)."""
    inst = make(root)
    inst.install(opts(fake_netbird))
    lib = root / "usr/local/lib/netbird-nm"
    code = launcher(sys.executable, str(lib), "config").replace(
        "from netbird_nm.config import main\nsys.exit(main())",
        "import netbird_nm.config as m; print(m.__file__)")
    script = root / "probe.py"
    script.write_text(code)
    import subprocess
    out = subprocess.run([sys.executable, "-I", str(script)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().startswith(str(lib))


def test_install_creates_profile_with_safe_settings(root, fake_netbird):
    nm = FakeNM()
    make(root, nm).install(opts(fake_netbird, name="Mesh"))
    (prof,) = nm.profiles.values()
    assert prof["name"] == "Mesh" and prof["service"] == SERVICE
    assert prof["opts"]["vpn.persistent"] == "no"        # yes = NM restarts NetBird after you stop it
    assert prof["opts"]["connection.autoconnect"] == "no"
    assert prof["opts"]["ipv4.never-default"] == "yes"


def test_reinstall_keeps_profile_and_fixes_persistent(root, fake_netbird):
    nm = FakeNM()
    nm.profiles["old"] = {"name": "NetBird", "type": "vpn", "service": SERVICE, "persistent": "yes"}
    res = make(root, nm).install(opts(fake_netbird))
    assert res["profile"] == "kept"
    assert list(nm.profiles) == ["old"] and nm.profiles["old"]["persistent"] == "no"


def test_install_is_idempotent(root, fake_netbird):
    nm = FakeNM()
    inst = make(root, nm)
    inst.install(opts(fake_netbird))
    before = sorted(p.relative_to(root) for p in root.rglob("*"))
    inst.install(opts(fake_netbird))
    after = sorted(p.relative_to(root) for p in root.rglob("*"))
    assert before == after and len(nm.profiles) == 1


def test_install_requires_netbird(root, tmp_path):
    with pytest.raises(RuntimeError, match="netbird not found"):
        make(root).install(opts(str(tmp_path / "nope")))


def test_install_moves_name_file_out_of_unwatched_etc(root, fake_netbird):
    """The 0.1 shell script put it in /etc/NetworkManager/VPN, which Arch's NM doesn't watch."""
    etc = root / "etc/NetworkManager/VPN"
    etc.mkdir(parents=True)
    (etc / "nm-netbird-service.name").write_text(f"# {LEGACY_MARKER}\n[VPN Connection]\n")
    res = make(root).install(opts(fake_netbird))
    assert not (etc / "nm-netbird-service.name").exists()
    assert (root / "usr/lib/NetworkManager/VPN/nm-netbird-service.name").exists()
    assert any("nm-netbird-service.name" in p for p in res["legacy"])


def test_install_migrates_legacy_shell_version(root, fake_netbird):
    legacy = {
        "usr/local/libexec/nm-netbird-service": f"#!/usr/bin/python3\n# {LEGACY_MARKER}\n",
        "etc/dbus-1/system.d/nm-netbird-service.conf": f"<!-- {LEGACY_MARKER} -->",
        "etc/NetworkManager/dispatcher.d/90-netbird-vpn": f"#!/bin/sh\n# {LEGACY_MARKER}\n",
        "etc/NetworkManager/dispatcher.d/90-netbird":
            '#!/bin/sh\n[ "$CONNECTION_ID" = "NetBird" ]\nnetbird up\n',
        "etc/NetworkManager/conf.d/99-netbird.conf": "[keyfile]\nunmanaged-devices=interface-name:wt0\n",
    }
    for rel, text in legacy.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    nm = FakeNM()
    nm.profiles["dummy"] = {"name": "NetBird", "type": "dummy", "service": ""}
    res = make(root, nm).install(opts(fake_netbird))
    assert not (root / "etc/NetworkManager/dispatcher.d/90-netbird").exists()
    assert not (root / "etc/NetworkManager/conf.d/99-netbird.conf").exists()
    assert "dummy" not in nm.profiles
    assert any("dummy connection" in x for x in res["legacy"])
    # new versions are in place and carry the new marker
    for rel in ("usr/local/libexec/nm-netbird-service", "etc/NetworkManager/dispatcher.d/90-netbird-vpn"):
        assert MARKER in (root / rel).read_text() and LEGACY_MARKER not in (root / rel).read_text()


def test_unrelated_files_are_never_touched(root, fake_netbird):
    foreign = root / "etc/NetworkManager/dispatcher.d/90-netbird"
    foreign.write_text("#!/bin/sh\necho someone else's hook\n")
    other_name = root / "usr/lib/NetworkManager/VPN/nm-openvpn-service.name"
    other_name.write_text("[VPN Connection]\nservice=org.freedesktop.NetworkManager.openvpn\n")
    inst = make(root)
    inst.install(opts(fake_netbird))
    inst.uninstall(manage_system=False)
    assert foreign.exists() and other_name.exists()


# -- uninstall ------------------------------------------------------------------------------

def test_uninstall_removes_everything(root, fake_netbird):
    nm = FakeNM()
    inst = make(root, nm)
    inst.install(opts(fake_netbird))
    snapshot_dirs = {p for p in root.rglob("*") if p.is_dir()}
    res = inst.uninstall(manage_system=False)
    leftovers = [p for p in root.rglob("*") if p.is_file()]
    assert leftovers == []
    assert nm.profiles == {}
    assert res["profiles"] and res["removed"]
    assert not (root / "usr/local/lib/netbird-nm").exists()
    assert snapshot_dirs   # sanity


def test_uninstall_keeps_netbird_running_by_default(root, fake_netbird, monkeypatch):
    nm = FakeNM()
    inst = make(root, nm)
    inst.install(opts(fake_netbird))
    seen = []
    orig = nm.down
    from netbird_nm.state import StateDir
    cfg = Config.load(root / "etc/netbird-nm.json")

    def down(uuid):
        seen.append(StateDir(root / cfg.state_dir.lstrip("/")).keep())
        orig(uuid)
    nm.down = down
    inst.uninstall(manage_system=False)
    assert seen == [True]     # the down helper sees keep-running and leaves NetBird alone


def test_uninstall_leaves_foreign_file_at_our_path(root, fake_netbird):
    inst = make(root)
    inst.install(opts(fake_netbird))
    conf = root / "etc/dbus-1/system.d/nm-netbird-service.conf"
    conf.write_text("<busconfig/>")   # someone replaced it
    res = inst.uninstall(manage_system=False)
    assert conf.exists() and str(conf) in res["skipped"]


def test_uninstall_when_nothing_installed(root):
    res = make(root).uninstall(manage_system=False)
    assert res["removed"] == [] and res["profiles"] == []


def test_status(root, fake_netbird):
    nm = FakeNM()
    inst = make(root, nm)
    assert not any(present for _, present in inst.status()["files"].values())
    inst.install(opts(fake_netbird))
    st = inst.status()
    assert all(present for _, present in st["files"].values())
    assert st["profiles"] == [("NetBird", "uuid-0", "inactive")]


# -- helpers --------------------------------------------------------------------------------

def test_is_ours(tmp_path):
    f = tmp_path / "f"
    f.write_text(f"x\n# {MARKER}\n")
    assert is_ours(f)
    f.write_text("nothing")
    assert not is_ours(f)
    assert not is_ours(tmp_path / "missing")


def test_find_python(runner, tmp_path):
    good, bad = tmp_path / "good", tmp_path / "bad"
    for p in (good, bad):
        p.write_text("")
        p.chmod(0o755)
    runner.on([str(bad)], returncode=1)
    assert find_python([str(tmp_path / "missing"), str(bad), str(good)], runner) == str(good)
    runner.on([str(good)], returncode=1)
    assert find_python([str(bad), str(good)], runner) is None


def test_package_files_are_all_copied(root, fake_netbird):
    make(root).install(opts(fake_netbird))
    src = Path(__file__).resolve().parents[2] / "src/netbird_nm"
    copied = root / "usr/local/lib/netbird-nm/netbird_nm"
    assert {p.name for p in src.glob("*.py")} == {p.name for p in copied.glob("*.py")}
    assert os.access(copied / "service.py", os.R_OK)


def test_restart_reports_failure(monkeypatch):
    from netbird_nm import installer
    monkeypatch.setenv("NETBIRD_NM_RESTART_CMD", "exit 3")
    assert installer.restart_networkmanager() is False
    monkeypatch.setenv("NETBIRD_NM_RESTART_CMD", "true")
    assert installer.restart_networkmanager() is True


# -- which user runs netbird ------------------------------------------------------------------

def test_default_is_session_user_not_installer(root, fake_netbird, monkeypatch):
    """Installing through sudo must not pin the installing user."""
    monkeypatch.setenv("SUDO_USER", "nobody")
    make(root).install(opts(fake_netbird))
    assert Config.load(root / "etc/netbird-nm.json").run_as == "session"


def test_pick_run_as(root, fake_netbird):
    inst = make(root)
    assert inst.pick_run_as(None) == "session"
    assert inst.pick_run_as("root") == "root"
    assert inst.pick_run_as("nobody") == "nobody"
    inst.install(opts(fake_netbird, run_as="nobody"))
    assert inst.pick_run_as(None) == "nobody"          # reinstall keeps an explicit choice
    inst.install(opts(fake_netbird, run_as="session"))
    assert inst.pick_run_as(None) == "session"


def test_upgrade_from_021_drops_pinned_user(root, fake_netbird):
    """0.2.1 pinned the installing user under "user"; that key is now ignored."""
    cfg = root / "etc/netbird-nm.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('{"netbird": "/usr/bin/netbird", "user": "juan", "iface": "wt0"}')
    assert Config.load(cfg).run_as == "session"
    make(root).install(opts(fake_netbird))
    assert Config.load(cfg).run_as == "session"


def test_install_rejects_unknown_user(root, fake_netbird):
    with pytest.raises(RuntimeError, match="no-such-user-xyz"):
        make(root).install(opts(fake_netbird, run_as="no-such-user-xyz"))
