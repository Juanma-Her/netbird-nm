"""netbird-nm: make NetBird a NetworkManager VPN connection.

    sudo netbird-nm install      set everything up (asks for sudo itself)
    sudo netbird-nm uninstall    remove everything again
    netbird-nm status            what's installed, connection state
    netbird-nm doctor [--live]   check for common problems
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .config import Config
from .const import SERVICE
from .controller import make_netbird, resolve_user
from .installer import (
    GRPC_DEPS,
    Installer,
    InstallOptions,
    Layout,
    deps_command,
    dispatcher_service_enabled,
    find_python,
    has_grpc,
)
from .nmcli import Nmcli

SRC_DIR = str(Path(__file__).resolve().parent.parent)
COLOR = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def say(msg: str) -> None:
    print(f"{_c('1;34', '==>')} {msg}")


def warn(msg: str) -> None:
    print(f"{_c('1;33', 'warning:')} {msg}", file=sys.stderr)


def err(msg: str) -> None:
    print(f"{_c('1;31', 'error:')} {msg}", file=sys.stderr)


def describe_run_as(run_as: str, now: str) -> str:
    if run_as == "session":
        return ("whoever is logged in on the desktop, at each connect "
                f"(now: {now or 'nobody, so root'})")
    return run_as


def ensure_root(argv: list[str]) -> None:
    """Re-run ourselves through sudo when not root."""
    if os.geteuid() == 0:
        return
    boot = (f"import sys; sys.path.insert(0, {SRC_DIR!r}); from netbird_nm.cli import main; "
            "sys.exit(main(sys.argv[1:]))")
    os.execvp("sudo", ["sudo", sys.executable, "-c", boot, *argv])


def ask(question: str, default: bool) -> bool:
    if not sys.stdin.isatty():
        return default
    suffix = " [Y/n] " if default else " [y/N] "
    answer = input(question + suffix).strip().lower()
    return default if not answer else answer.startswith("y")


# --------------------------------------------------------------------- install ---

def cmd_install(args: argparse.Namespace, inst: Installer) -> int:
    nm = inst.nm
    if not nm.running():
        err("NetworkManager isn't running (or nmcli is missing)")
        return 1

    python = args.python or find_python()
    if not python:
        cmd = deps_command()
        if args.no_deps or not cmd:
            err("no Python with the libnm GObject bindings (PyGObject + NM typelib) found")
            return 1
        if not (args.yes or ask("The plugin needs the Python libnm bindings. Install them with '"
                                + " ".join(cmd) + "'?", True)):
            err("can't continue without the libnm bindings")
            return 1
        say("Installing the Python libnm bindings: " + " ".join(cmd))
        if subprocess.run(cmd).returncode != 0:
            err("installing dependencies failed")
            return 1
        python = find_python()
        if not python:
            err("the libnm bindings still can't be imported")
            return 1

    backend = args.backend
    if backend != "cli" and not has_grpc(python):
        cmd = deps_command(GRPC_DEPS)
        want = backend == "grpc"
        if cmd and not args.no_deps and (args.yes or ask(
                "Talking to NetBird's daemon directly needs Python's grpcio (recommended). "
                f"Install it with '{' '.join(cmd)}'? (No: use the netbird command instead)", True)):
            say("Installing grpcio: " + " ".join(cmd))
            subprocess.run(cmd)
        if not has_grpc(python):
            if want:
                err("the grpc backend needs grpcio (python-grpcio)")
                return 1
            backend = "cli"

    _, needs_restart = inst.choose_vpn_dir(args.vpn_dir)
    restart = args.restart_nm
    if needs_restart and restart is None:
        restart = args.yes or ask(
            "NetworkManager has to be restarted once to notice the new VPN plugin "
            "(your connections drop for a moment). Restart it now?", True)

    try:
        res = inst.install(InstallOptions(name=args.name, iface=args.iface,
                                          netbird=args.netbird, python=python, run_as=args.run_as,
                                          backend=backend, daemon_addr=args.daemon_addr,
                                          vpn_dir=args.vpn_dir, restart_nm=bool(restart)))
    except RuntimeError as e:
        err(str(e))
        return 1

    say(f"interface: {res['iface']}   python: {res['python']}")
    say("talks to NetBird " + ("directly over its daemon's gRPC socket" if res["backend"] == "grpc"
                               else f"through the netbird command ({res['netbird']})"))
    say("'netbird up/down' will run as: " + describe_run_as(res["run_as"], res["user_now"]))
    if res.get("user_check"):
        warn(f"'netbird status' failed as {res['user_now']}: {res['user_check']}. If that user "
             "can't control NetBird, reinstall with --as-root.")
    for item in res["legacy"]:
        say(f"Removed leftover from an earlier version: {item}")
    say(f"Plugin registered in {res['vpn_dir']}")
    if res.get("enabled_dispatcher"):
        say("Enabled NetworkManager-dispatcher.service (needed for network-change handling)")
    elif res.get("enabled_dispatcher") is False:
        warn("Couldn't enable NetworkManager-dispatcher.service; Wi-Fi switches won't "
             "bring the VPN back until it is: sudo systemctl enable --now NetworkManager-dispatcher")
    if res.get("restarted_nm"):
        say("Restarted NetworkManager")
    elif res.get("restarted_nm") is False:
        warn("Restarting NetworkManager failed; restart it yourself before using the VPN: "
             "sudo systemctl restart NetworkManager")
    elif res["needs_restart"]:
        warn("NetworkManager won't see the plugin until it restarts: "
             "sudo systemctl restart NetworkManager")
    say(("Created" if res["profile"] == "created" else "Kept the existing")
        + f" VPN connection '{args.name}'")
    print(f"\n  Try it:  nmcli connection up {args.name!r}    (or your network menu)")
    print("  Logs:    journalctl -t nm-netbird -f")
    return 0


# ------------------------------------------------------------------- uninstall ---

def cmd_uninstall(args: argparse.Namespace, inst: Installer) -> int:
    res = inst.uninstall(keep_netbird=not args.stop_netbird)
    for uuid in res["profiles"]:
        say(f"Removed VPN connection {uuid}")
    for path in res["removed"]:
        say(f"Removed {path}")
    for path in res["skipped"]:
        warn(f"{path} wasn't created by netbird-nm; left in place")
    if res.get("restored_netbird"):
        say("Restarted NetBird so it keeps its address (NetworkManager removes it with the VPN)")
    if not (res["profiles"] or res["removed"]):
        say("Nothing to remove")
    say("Done. NetBird itself is untouched"
        + ("." if args.stop_netbird else " (still connected if it was)."))
    return 0


# ---------------------------------------------------------------------- status ---

def cmd_status(args: argparse.Namespace, inst: Installer) -> int:
    st = inst.status()
    width = max(len(k) for k in st["files"])
    for label, (path, present) in st["files"].items():
        mark = _c("32", "ok") if present else _c("31", "--")
        print(f"  {mark}  {label:<{width}}  {path}")
    if st["profiles"]:
        for name, uuid, state in st["profiles"]:
            print(f"  VPN connection '{name}' ({uuid}): {state}")
    else:
        print("  no NetBird VPN connection in NetworkManager")
    return 0


# ---------------------------------------------------------------------- doctor ---

def cmd_doctor(args: argparse.Namespace, inst: Installer) -> int:
    problems = 0

    def check(ok: bool, good: str, bad: str) -> None:
        nonlocal problems
        print(f"  {_c('32', 'ok') if ok else _c('31', '!!')}  {good if ok else bad}")
        problems += not ok

    L = inst.layout
    nm = inst.nm
    cfg = Config.load(L.p(L.config))
    check(nm.running(), "NetworkManager is running", "NetworkManager isn't running")
    lib = str(L.p(L.lib_dir))
    plugin_python = None
    if L.p(L.plugin_bin).exists():
        plugin_python = L.p(L.plugin_bin).read_text().splitlines()[0][2:]
    if cfg.backend == "grpc":
        check(plugin_python is None or has_grpc(plugin_python),
              f"talks to NetBird's daemon over gRPC ({cfg.daemon_addr})",
              f"{plugin_python} can't import grpcio (install python-grpcio, or reinstall "
              "with --backend cli)")
    else:
        check(os.access(cfg.netbird, os.X_OK), f"uses the netbird command at {cfg.netbird}",
              f"netbird not found at {cfg.netbird} (reinstall with --netbird PATH)")
    now = resolve_user(cfg)
    print("  ..  NetBird is controlled as: " + describe_run_as(cfg.run_as, now))
    try:
        nb = make_netbird(cfg, now, python=plugin_python, lib=lib)
        problem = inst.check_user_can_reach_netbird(nb)
    except KeyError:
        problem = "that user doesn't exist"
    who = now or "root"
    check(not problem, f"{who} can reach the NetBird daemon",
          f"NetBird status as {who} failed: {problem}")
    plugin = L.p(L.plugin_bin)
    check(plugin.exists(), f"plugin installed at {plugin}", "plugin missing; run install")
    if plugin.exists():
        py = plugin.read_text().splitlines()[0][2:]
        r = subprocess.run([py, "-c", 'import gi; gi.require_version("NM","1.0"); '
                            'from gi.repository import NM'], capture_output=True)
        check(r.returncode == 0, f"{py} can load libnm",
              f"{py} can't import the libnm bindings (python-gobject + libnm)")
    nf = inst.installed_name_file()
    check(nf is not None, f"plugin description in {nf.parent if nf else ''}",
          "plugin description (.name file) missing; run install")
    disp = dispatcher_service_enabled()
    check(disp is not False, "NetworkManager dispatcher can run hooks",
          "NetworkManager-dispatcher.service is disabled: "
          "sudo systemctl enable --now NetworkManager-dispatcher.service")
    uuids = nm.vpn_connections(SERVICE) if nm.running() else []
    check(bool(uuids), "VPN connection exists in NetworkManager",
          "no NetBird VPN connection; run install")

    if args.live and uuids:
        uuid = uuids[0]
        was_on = bool(nm.connection_state(uuid))
        print("  ..  live test: toggling the VPN" + (" off and on" if was_on else " on and off"))
        steps = (["down", "up"] if was_on else ["up", "down"])
        for step in steps:
            r = nm.up(uuid) if step == "up" else nm.down(uuid)
            msg = (r.stderr or r.stdout).strip()
            if r.returncode != 0 and "not installed" in msg:
                check(False, "", "NetworkManager doesn't know the plugin yet: "
                                 "sudo systemctl restart NetworkManager")
                break
            check(r.returncode == 0, f"'{step}' worked", f"'{step}' failed: {msg}")
            if step == "down":
                time.sleep(cfg.down_grace + 1)
    print()
    print("No problems found." if not problems else f"{problems} problem(s) found.")
    return 1 if problems else 0


# ------------------------------------------------------------------------ main ---

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="netbird-nm", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"netbird-nm {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    i = sub.add_parser("install", help="install the plugin and create the VPN connection")
    i.add_argument("--name", default="NetBird", help="connection name (default: NetBird)")
    i.add_argument("--iface", help="NetBird's interface (default: from /etc/netbird/config.json, else wt0)")
    i.add_argument("--netbird", help="path to the netbird binary (default: from PATH)")
    i.add_argument("--python", help="Python interpreter for the plugin (default: auto-detect)")
    who = i.add_mutually_exclusive_group()
    who.add_argument("--user", dest="run_as", metavar="NAME",
                     help="always run the netbird command as this user (default: whoever is "
                          "logged in on the desktop, decided at each connect)")
    who.add_argument("--as-root", dest="run_as", action="store_const", const="root",
                     help="always run the netbird command as root")
    who.add_argument("--session-user", dest="run_as", action="store_const", const="session",
                     help="go back to the default after --user or --as-root")
    i.add_argument("--no-deps", action="store_true", help="don't install missing Python packages")
    i.add_argument("--backend", choices=("grpc", "cli"),
                   help="talk to NetBird's daemon directly (grpc, needs python-grpcio) or run the "
                        "netbird command (cli). Default: grpc when grpcio is available")
    i.add_argument("--daemon-addr", metavar="ADDR",
                   help="NetBird daemon socket (default: unix:///var/run/netbird.sock)")
    i.add_argument("--vpn-dir", metavar="DIR",
                   help="NetworkManager VPN plugin directory (default: /usr/lib/NetworkManager/VPN "
                        "if it exists, else /etc/NetworkManager/VPN)")
    g = i.add_mutually_exclusive_group()
    g.add_argument("--restart-nm", dest="restart_nm", action="store_true", default=None,
                   help="restart NetworkManager if needed, without asking")
    g.add_argument("--no-restart-nm", dest="restart_nm", action="store_false",
                   help="never restart NetworkManager")
    i.add_argument("-y", "--yes", action="store_true", help="answer yes to questions")

    u = sub.add_parser("uninstall", help="remove everything netbird-nm installed")
    u.add_argument("--stop-netbird", action="store_true",
                   help="also run 'netbird down' (by default NetBird keeps running)")

    sub.add_parser("status", help="show what's installed and the connection state")
    d = sub.add_parser("doctor", help="check for common problems")
    d.add_argument("--live", action="store_true",
                   help="also toggle the VPN off/on to prove NetworkManager can drive it")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)
    if args.command in ("install", "uninstall"):
        ensure_root(argv)
    inst = Installer(Layout(), Nmcli(), say=say, warn=warn)
    handler = {"install": cmd_install, "uninstall": cmd_uninstall,
               "status": cmd_status, "doctor": cmd_doctor}[args.command]
    try:
        return handler(args, inst)
    except KeyboardInterrupt:
        return 130
