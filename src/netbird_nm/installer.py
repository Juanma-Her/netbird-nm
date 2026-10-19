"""Installing and removing the plugin, the dispatcher hook and the profile.

Everything is written relative to ``Layout.root`` so tests can install into a
temporary directory.
"""

from __future__ import annotations

import configparser
import contextlib
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Config
from .const import LEGACY_MARKER, MARKER, SERVICE
from .netbird import NetBird, RunAs
from .nmcli import Nmcli
from .state import StateDir

PACKAGE_DIR = Path(__file__).resolve().parent

Say = Callable[[str], None]


@dataclass
class Layout:
    root: Path = Path("/")
    lib_dir: str = "/usr/local/lib/netbird-nm"
    plugin_bin: str = "/usr/local/libexec/nm-netbird-service"
    config: str = "/etc/netbird-nm.json"
    dispatcher: str = "/etc/NetworkManager/dispatcher.d/90-netbird-vpn"
    unmanaged_conf: str = "/etc/NetworkManager/conf.d/99-netbird-unmanaged.conf"
    dbus_conf: str = "/etc/dbus-1/system.d/nm-netbird-service.conf"
    # NetworkManager reads VPN plugin descriptions from both of these, but it
    # only *watches* a directory that existed when NM started. Arch ships
    # /usr/lib/NetworkManager/VPN but not /etc/NetworkManager/VPN, so the
    # former comes first.
    vpn_dirs: tuple[str, ...] = ("/usr/lib/NetworkManager/VPN", "/etc/NetworkManager/VPN")
    name_file_name: str = "nm-netbird-service.name"
    legacy: dict[str, str] = field(default_factory=lambda: {
        # files from the single-file shell version (netbird-nm-vpn.sh)
        "plugin": "/usr/local/libexec/nm-netbird-service",
        "name": "/etc/NetworkManager/VPN/nm-netbird-service.name",
        "dbus": "/etc/dbus-1/system.d/nm-netbird-service.conf",
        "dispatcher": "/etc/NetworkManager/dispatcher.d/90-netbird-vpn",
        "unmanaged": "/etc/NetworkManager/conf.d/99-netbird-unmanaged.conf",
        # from the manual dummy-connection workaround suggested before that
        "old-dispatcher": "/etc/NetworkManager/dispatcher.d/90-netbird",
        "old-unmanaged": "/etc/NetworkManager/conf.d/99-netbird.conf",
    })

    def p(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def name_file_candidates(self) -> list[Path]:
        return [self.p(d) / self.name_file_name for d in self.vpn_dirs]


# --------------------------------------------------------------- file content ---

def launcher(python: str, lib_dir: str, module: str) -> str:
    return f"""#!{python}
# {MARKER}
import sys
sys.path.insert(0, {lib_dir!r})
from netbird_nm.{module} import main
sys.exit(main())
"""


def name_file(plugin_bin: str) -> str:
    return f"""# {MARKER}
[VPN Connection]
name=netbird
service={SERVICE}
program={plugin_bin}
supports-multiple-connections=false

[libnm]

[GNOME]
supports-external-ui-mode=false
"""


def dbus_policy() -> str:
    return f"""<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<!-- {MARKER} -->
<busconfig>
  <policy user="root">
    <allow own_prefix="{SERVICE}"/>
    <allow send_destination="{SERVICE}"/>
  </policy>
  <policy context="default">
    <deny own_prefix="{SERVICE}"/>
    <deny send_destination="{SERVICE}"/>
  </policy>
</busconfig>
"""


def unmanaged_conf(iface: str) -> str:
    return f"""# {MARKER}
# Keep NetworkManager from showing NetBird's interface as a separate
# "externally connected" device (switching that off is what used to break
# reconnecting). The VPN connection is the way to control NetBird.
[keyfile]
unmanaged-devices=interface-name:{iface}
"""


def is_ours(path: Path, markers: tuple[str, ...] = (MARKER, LEGACY_MARKER)) -> bool:
    try:
        head = path.read_text(errors="replace")[:4096]
    except (OSError, IsADirectoryError):
        return False
    return any(m in head for m in markers)


def write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


# ------------------------------------------------------------------- helpers ---

def find_python(candidates: list[str] | None = None,
                runner=subprocess.run) -> str | None:
    """An interpreter that can import the libnm GObject bindings."""
    if candidates is None:
        candidates = ["/usr/bin/python3"] + sorted(
            str(p) for p in Path("/usr/bin").glob("python3.*")
            if p.name.replace("python3.", "").isdigit())
    probe = 'import gi; gi.require_version("NM", "1.0"); from gi.repository import NM'
    for py in candidates:
        if not os.access(py, os.X_OK):
            continue
        try:
            if runner([py, "-c", probe], capture_output=True, timeout=20).returncode == 0:
                return py
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


DEPS = {
    "pacman": ["pacman", "-S", "--needed", "--noconfirm", "python-gobject", "libnm"],
    "apt-get": ["apt-get", "install", "-y", "python3-gi", "gir1.2-nm-1.0"],
    "dnf": ["dnf", "install", "-y", "python3-gobject", "NetworkManager-libnm"],
    "zypper": ["zypper", "--non-interactive", "install", "python3-gobject", "typelib-1_0-NM-1_0"],
    "xbps-install": ["xbps-install", "-y", "python3-gobject", "libnm"],
}


GRPC_DEPS = {
    "pacman": ["pacman", "-S", "--needed", "--noconfirm", "python-grpcio"],
    "apt-get": ["apt-get", "install", "-y", "python3-grpcio"],
    "dnf": ["dnf", "install", "-y", "python3-grpcio"],
    "zypper": ["zypper", "--non-interactive", "install", "python3-grpcio"],
    "xbps-install": ["xbps-install", "-y", "python3-grpcio"],
}


def deps_command(table: dict[str, list[str]] = DEPS) -> list[str] | None:
    for tool, cmd in table.items():
        if shutil.which(tool):
            return cmd
    return None


def has_grpc(python: str, runner=subprocess.run) -> bool:
    try:
        return runner([python, "-c", "import grpc"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def nm_start_time() -> float | None:
    """Wall-clock time NetworkManager started, from /proc."""
    try:
        pid = subprocess.run(["pidof", "NetworkManager"], capture_output=True,
                             text=True).stdout.split()[0]
        return os.stat(f"/proc/{pid}").st_mtime
    except (OSError, IndexError, subprocess.SubprocessError):
        return None


def reload_dbus() -> None:
    for cmd in (["busctl", "call", "org.freedesktop.DBus", "/org/freedesktop/DBus",
                 "org.freedesktop.DBus", "ReloadConfig"],
                ["dbus-send", "--system", "--type=method_call", "--dest=org.freedesktop.DBus",
                 "/org/freedesktop/DBus", "org.freedesktop.DBus.ReloadConfig"]):
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            if subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0:
                return


def systemctl(*args: str) -> subprocess.CompletedProcess[str] | None:
    if not shutil.which("systemctl"):
        return None
    try:
        return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None


def restart_networkmanager() -> bool:
    """Restart NM. NETBIRD_NM_RESTART_CMD overrides the command, for systems
    without systemd (and the integration sandbox)."""
    custom = os.environ.get("NETBIRD_NM_RESTART_CMD")
    if custom:
        try:
            return subprocess.run(custom, shell=True, timeout=120).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    r = systemctl("restart", "NetworkManager.service")
    return bool(r and r.returncode == 0)


def dispatcher_service_enabled() -> bool | None:
    """Whether NM's dispatcher can run hooks. None when we can't tell."""
    r = systemctl("is-enabled", "NetworkManager-dispatcher.service")
    if r is None:
        return None
    state = r.stdout.strip()
    if state in ("enabled", "static", "alias", "indirect", "enabled-runtime"):
        return True
    if state in ("disabled", "masked"):
        return False
    return None


# --------------------------------------------------------------- the installer ---

@dataclass
class InstallOptions:
    name: str = "NetBird"
    iface: str | None = None
    netbird: str | None = None
    python: str | None = None
    run_as: str | None = None        # "session", "root" or a user name; None = keep / default
    backend: str | None = None       # "grpc" or "cli"; None = grpc when grpcio is available
    daemon_addr: str | None = None   # NetBird daemon socket; None = keep / default
    vpn_dir: str | None = None       # where NM's plugin description goes; None = automatic
    restart_nm: bool = False         # restart NM if it can't see the plugin otherwise
    manage_system: bool = True       # reload D-Bus/NM, enable dispatcher; off in tests


class Installer:
    def __init__(self, layout: Layout | None = None, nm: Nmcli | None = None,
                 say: Say = print, warn: Say = print):
        self.layout = layout or Layout()
        self.nm = nm or Nmcli()
        self.say = say
        self.warn = warn

    # -- choosing where the plugin description goes -------------------------
    def choose_vpn_dir(self, override: str | None = None) -> tuple[Path, bool]:
        """(directory, needs_nm_restart).

        Prefer a directory that already exists (NM watches it); if none does,
        create the first one, and NM has to be restarted to notice it."""
        if override:
            path = self.layout.p(override)
            return path, not path.is_dir()
        for d in self.layout.vpn_dirs:
            path = self.layout.p(d)
            if path.is_dir():
                return path, False
        return self.layout.p(self.layout.vpn_dirs[0]), True

    def name_files(self) -> list[Path]:
        """Where a plugin description of ours may be: the standard directories,
        plus a --vpn-dir recorded by an earlier install."""
        files = self.layout.name_file_candidates()
        recorded = Config.load(self.layout.p(self.layout.config)).vpn_dir
        if recorded:
            files.append(self.layout.p(recorded) / self.layout.name_file_name)
        return list(dict.fromkeys(files))

    def installed_name_file(self) -> Path | None:
        for f in self.name_files():
            if f.exists() and is_ours(f, (MARKER,)):
                return f
        return None

    # -- legacy cleanup ------------------------------------------------------
    def remove_legacy(self, keep_names: tuple[str, ...] = ()) -> list[Path]:
        """Remove files from earlier versions (shell script, manual workaround).
        Returns what was removed."""
        removed = []
        for key, rel in self.layout.legacy.items():
            path = self.layout.p(rel)
            if not path.exists():
                continue
            if key.startswith("old-"):
                text = path.read_text(errors="replace")
                if "netbird" not in text and "wt0" not in text:
                    continue
            elif not is_ours(path, (LEGACY_MARKER,)):
                continue
            if key in keep_names:
                continue
            path.unlink()
            removed.append(path)
        return removed

    def remove_legacy_dummy(self, name: str) -> bool:
        for uuid, ctype, cname in self.nm.connections():
            if ctype == "dummy" and cname == name:
                self.nm.delete(uuid)
                return True
        return False

    # -- install ---------------------------------------------------------------
    def install_files(self, cfg: Config, python: str, vpn_dir_override: str | None = None
                      ) -> tuple[Path, bool]:
        L = self.layout
        old_name_files = self.name_files()      # before the config (and a --vpn-dir) changes
        # the package itself
        lib = L.p(L.lib_dir)
        pkg = lib / "netbird_nm"
        if pkg.exists():
            shutil.rmtree(pkg)
        pkg.mkdir(parents=True)
        for src in PACKAGE_DIR.glob("*.py"):
            shutil.copy2(src, pkg / src.name)
        write(lib / "INSTALLED", f"# {MARKER}\n", 0o644)

        write(L.p(L.config), cfg.dumps(), 0o644)
        write(L.p(L.plugin_bin), launcher(python, L.lib_dir, "service"), 0o755)
        write(L.p(L.dispatcher), launcher(python, L.lib_dir, "dispatcher"), 0o755)
        write(L.p(L.dbus_conf), dbus_policy(), 0o644)
        write(L.p(L.unmanaged_conf), unmanaged_conf(cfg.iface), 0o644)

        vpn_dir, needs_restart = self.choose_vpn_dir(vpn_dir_override)
        moved = False
        for other in old_name_files + self.name_files():
            if other.parent != vpn_dir and other.exists() and is_ours(other):
                other.unlink()
                moved = True
        if moved and L.root == Path("/"):
            # NM watches each plugin directory separately and may see the new
            # file before the old one's removal; it then ignores the new one
            # as a duplicate and unregisters the plugin when the removal lands.
            time.sleep(1.5)
        write(vpn_dir / L.name_file_name, name_file(L.plugin_bin), 0o644)
        return vpn_dir, needs_restart

    def ensure_profile(self, name: str) -> str:
        existing = self.nm.vpn_connections(SERVICE)
        if existing:
            for uuid in existing:
                # persistent=yes makes NM restart NetBird after you stop it yourself
                self.nm.modify(uuid, "vpn.persistent", "no")
            return "kept"
        r = self.nm.add_vpn(name, SERVICE, [
            "connection.autoconnect", "no", "vpn.persistent", "no",
            "ipv4.never-default", "yes", "ipv6.method", "ignore"])
        if r.returncode != 0:
            raise RuntimeError(f"nmcli could not create the connection: {r.stderr.strip()}")
        return "created"

    def pick_run_as(self, requested: str | None) -> str:
        """--user NAME / --as-root if given, else what an earlier install
        chose, else "session" (whoever is logged in, decided at each connect)."""
        if requested is not None:
            return requested
        return Config.load(self.layout.p(self.layout.config)).run_as

    @staticmethod
    def check_user_can_reach_netbird(nb: NetBird) -> str:
        """'' if 'netbird status' works as nb's user, else the problem."""
        try:
            r = nb.status(timeout=15)
        except (OSError, subprocess.SubprocessError) as e:
            return str(e)
        if r.returncode != 0:
            lines = (r.stderr or r.stdout or "").strip().splitlines()
            return lines[-1] if lines else f"exit code {r.returncode}"
        return ""

    def install(self, opts: InstallOptions) -> dict:
        from .config import detect_iface

        python = opts.python or find_python()
        if not python:
            raise RuntimeError("no Python with the libnm GObject bindings was found")
        previous = Config.load(self.layout.p(self.layout.config))
        backend = opts.backend or ("grpc" if has_grpc(python) else "cli")
        if backend == "grpc" and not has_grpc(python):
            raise RuntimeError(f"the grpc backend needs grpcio for {python} (python-grpcio)")
        netbird = opts.netbird or shutil.which("netbird") or "/usr/bin/netbird"
        if backend == "cli" and not os.access(netbird, os.X_OK):
            raise RuntimeError(f"netbird not found ({netbird}); install NetBird first "
                               "or pass --netbird PATH")
        from .controller import make_netbird, resolve_user

        run_as = self.pick_run_as(opts.run_as)
        if run_as not in ("session", "root"):
            try:
                RunAs.lookup(run_as)
            except KeyError:
                raise RuntimeError(f"user {run_as!r} doesn't exist") from None
        cfg = Config(netbird=netbird, run_as=run_as, iface=opts.iface or detect_iface(),
                     backend=backend, daemon_addr=opts.daemon_addr or previous.daemon_addr)

        now = resolve_user(cfg)          # for "session": whoever is logged in right now
        result: dict = {"netbird": netbird, "python": python, "iface": cfg.iface,
                        "run_as": run_as, "user_now": now, "backend": backend}
        result["legacy"] = [str(p) for p in self.remove_legacy()]
        if self.remove_legacy_dummy(opts.name):
            result["legacy"].append(f"dummy connection '{opts.name}'")

        if opts.vpn_dir:
            cfg.vpn_dir = opts.vpn_dir
        elif previous.vpn_dir and self.layout.p(previous.vpn_dir).is_dir():
            cfg.vpn_dir = previous.vpn_dir          # keep an earlier --vpn-dir
        vpn_dir, needs_restart = self.install_files(cfg, python, cfg.vpn_dir or None)
        result["vpn_dir"] = str(vpn_dir)
        result["needs_restart"] = needs_restart
        if now and opts.manage_system:
            # through the installed copy, the way the plugin will run it
            nb = make_netbird(cfg, now, python=python, lib=str(self.layout.p(self.layout.lib_dir)))
            result["user_check"] = self.check_user_can_reach_netbird(nb)

        if opts.manage_system:
            reload_dbus()
            self.nm.reload_conf()
            if dispatcher_service_enabled() is False:
                r = systemctl("enable", "--now", "NetworkManager-dispatcher.service")
                result["enabled_dispatcher"] = bool(r and r.returncode == 0)
            if needs_restart and opts.restart_nm:
                result["restarted_nm"] = restart_networkmanager()
                for _ in range(30):
                    if self.nm.running():
                        break
                    time.sleep(1)
            time.sleep(1)

        result["profile"] = self.ensure_profile(opts.name)
        return result

    # -- uninstall -------------------------------------------------------------
    def uninstall(self, manage_system: bool = True, keep_netbird: bool = True) -> dict:
        L = self.layout
        result: dict = {"profiles": [], "removed": [], "skipped": []}
        cfg = Config.load(L.p(L.config))
        state = StateDir(cfg.state_dir if L.root == Path("/") else L.p(cfg.state_dir))
        if keep_netbird:
            state.set_keep()
        if self.nm.running():
            for uuid in self.nm.vpn_connections(SERVICE):
                self.nm.down(uuid)
                self.nm.delete(uuid)
                result["profiles"].append(uuid)
        if manage_system:
            # let an in-flight 'netbird down' helper finish first
            for _ in range(40):
                if not state.down_pending():
                    break
                time.sleep(0.5)
            subprocess.run(["pkill", "-f", L.plugin_bin], capture_output=True)
            if keep_netbird and result["profiles"]:
                result["restored_netbird"] = self.restore_netbird_address(cfg)

        paths = [L.p(x) for x in (L.plugin_bin, L.config, L.dispatcher, L.dbus_conf,
                                  L.unmanaged_conf)]
        paths += self.name_files()
        paths += [L.p(x) for x in L.legacy.values()]
        for path in dict.fromkeys(paths):
            if not path.exists():
                continue
            if is_ours(path):
                path.unlink()
                result["removed"].append(str(path))
            else:
                result["skipped"].append(str(path))
        lib = L.p(L.lib_dir)
        if (lib / "INSTALLED").exists() and is_ours(lib / "INSTALLED"):
            shutil.rmtree(lib)
            result["removed"].append(str(lib))
        state.wipe()

        if manage_system:
            reload_dbus()
            self.nm.reload_conf()
        return result

    @staticmethod
    def restore_netbird_address(cfg: Config, wait: float = 3.0) -> bool:
        """Taking the VPN down makes NetworkManager remove NetBird's address
        from its interface. When NetBird is meant to keep running, restart it
        so it gets the address back. True if it had to."""
        from .controller import make_netbird
        from .netbird import iface_exists, iface_ipv4
        from .state import StateDir
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:          # NM strips it shortly after 'down'
            if not iface_exists(cfg.iface):
                return False                        # NetBird wasn't running
            if not iface_ipv4(cfg.iface):
                break
            time.sleep(0.5)
        else:
            return False                            # address still there
        # act for whoever the connection was acting for
        user = StateDir(cfg.state_dir).get_user()
        try:
            nb = make_netbird(cfg, "" if user in (None, "root") else user)
        except KeyError:
            nb = NetBird(cfg.netbird)
        nb.down()
        nb.up(timeout=cfg.up_timeout)
        return True

    # -- status ------------------------------------------------------------------
    def status(self) -> dict:
        L = self.layout
        files = {}
        for label, rel in (("plugin", L.plugin_bin), ("config", L.config),
                           ("dispatcher", L.dispatcher), ("dbus policy", L.dbus_conf),
                           ("unmanaged conf", L.unmanaged_conf)):
            files[label] = (str(L.p(rel)), L.p(rel).exists())
        nf = self.installed_name_file()
        files["plugin description"] = (str(nf) if nf else "-", nf is not None)
        profiles = []
        if self.nm.running():
            for uuid in self.nm.vpn_connections(SERVICE):
                profiles.append((self.nm.field(uuid, "connection.id"), uuid,
                                 self.nm.connection_state(uuid) or "inactive"))
        return {"files": files, "profiles": profiles}


def parse_name_file(text: str) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(comment_prefixes=("#",), interpolation=None)
    cp.read_string(text)
    return cp
