"""NetworkManager dispatcher hook.

NM drops a VPN whenever the connection under it goes away (switching Wi-Fi
networks, unplugging the cable). This hook tells that apart from you
switching the VPN off, keeps NetBird running across the gap, and re-activates
the VPN once a network is back. It also "adopts" a NetBird that is already
running when a network comes up (e.g. NetBird's own service connected at
boot), so NM shows the VPN as on.

NM runs the hook as ``<script> <iface> <action>`` with details in the
environment. Anything slow is handed to a detached copy of the hook
(``--resume UUID`` / ``--adopt``) so NM's dispatcher isn't held up.
"""

from __future__ import annotations

import os
import subprocess
import sys
import syslog
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Callable

from .config import Config
from .const import SERVICE
from .netbird import iface_exists
from .nmcli import Nmcli
from .state import StateDir


@dataclass(frozen=True)
class Decision:
    kind: str            # 'none', 'set-resume', 'clear-resume', 'resume', 'adopt'
    uuid: str = ""


def decide(action: str, env: Mapping[str, str], *, is_ours: Callable[[str], bool],
           device_state: Callable[[str], str], state: StateDir) -> Decision:
    """What to do for one dispatcher event. No side effects except reading."""
    uuid = env.get("CONNECTION_UUID", "")
    if action == "vpn-down":
        if not uuid or not is_ours(uuid):
            return Decision("none")
        # The VPN's parent device is still fully connected → you switched it
        # off. Anything else (deactivating, gone) → the network went away.
        if device_state(env.get("DEVICE_IFACE", "")).startswith("100"):
            return Decision("clear-resume")
        return Decision("set-resume", uuid)
    if action == "vpn-up":
        return Decision("clear-resume") if uuid and is_ours(uuid) else Decision("none")
    if action == "up":
        if state.has_resume():
            return Decision("resume")
        return Decision("adopt")
    return Decision("none")


def _log(msg: str) -> None:
    syslog.syslog(syslog.LOG_INFO, f"dispatcher: {msg}")


def _spawn(launcher: str, *args: str) -> None:
    subprocess.Popen([sys.executable, launcher, *args], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def handle(action: str, env: Mapping[str, str], *, nm: Nmcli, state: StateDir,
           launcher: str, spawn: Callable[..., None] = _spawn, log=_log) -> Decision:
    def is_ours(uuid: str) -> bool:
        return nm.field(uuid, "vpn.service-type") == SERVICE

    d = decide(action, env, is_ours=is_ours, device_state=nm.device_state, state=state)
    if d.kind == "set-resume":
        state.set_resume(d.uuid)
        log("VPN dropped by a network change; will bring it back")
    elif d.kind == "clear-resume":
        state.clear_resume()
    elif d.kind == "resume":
        uuid = state.pop_resume()
        if uuid:
            log(f"network back ({env.get('CONNECTION_ID', '?')}); re-activating the VPN")
            spawn(launcher, "--resume", uuid)
    elif d.kind == "adopt":
        spawn(launcher, "--adopt")
    return d


def resume(uuid: str, *, nm: Nmcli, cfg: Config, sleep=time.sleep) -> None:
    sleep(cfg.resume_delay)
    nm.up(uuid)


def adopt(*, nm: Nmcli, cfg: Config, state: StateDir, sleep=time.sleep,
          exists: Callable[[str], bool] = iface_exists, log=_log) -> bool:
    """Show an already-running NetBird as connected. True if it activated."""
    sleep(cfg.adopt_delay)
    uuids = nm.vpn_connections(SERVICE)
    if not uuids:
        return False
    uuid = uuids[0]
    # Must look "running but shown off" on two checks 3s apart with no
    # disconnect in progress, so this never fights a toggle in flight.
    for check in (1, 2):
        if not exists(cfg.iface) or state.down_pending() or nm.connection_state(uuid):
            return False
        if check == 1:
            sleep(3)
    log("NetBird is already running; showing it as connected")
    nm.up(uuid)
    return True


def main(argv: list[str] | None = None, launcher: str | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    launcher = launcher or os.path.abspath(sys.argv[0])
    syslog.openlog("nm-netbird", syslog.LOG_PID, syslog.LOG_DAEMON)
    cfg = Config.load()
    nm, state = Nmcli(), StateDir(cfg.state_dir)

    if argv[:1] == ["--resume"] and len(argv) == 2:
        resume(argv[1], nm=nm, cfg=cfg)
    elif argv == ["--adopt"]:
        adopt(nm=nm, cfg=cfg, state=state)
    elif len(argv) >= 2 and argv[1] in ("vpn-down", "vpn-up", "up"):   # cheap exit for everything else
        handle(argv[1], os.environ, nm=nm, state=state, launcher=launcher)
    return 0
