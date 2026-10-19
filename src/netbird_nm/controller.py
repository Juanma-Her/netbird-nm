"""What the VPN plugin actually does, without any GObject/NetworkManager code
so it can be tested on its own. ``service.py`` wires it into NM."""

from __future__ import annotations

import socket
import struct
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

from .config import Config
from .netbird import NetBird, iface_exists, iface_ipv4
from .state import StateDir

Log = Callable[[str], None]


class ConnectError(Exception):
    """'netbird up' failed; the message is shown in the logs."""


@dataclass
class TunnelInfo:
    iface: str
    address: str
    prefix: int


def ip_to_u32(addr: str) -> int:
    """An IPv4 address in the network-byte-order uint32 NM's D-Bus API wants."""
    return struct.unpack("=I", socket.inet_aton(addr))[0]


def vpn_config(t: TunnelInfo) -> tuple[dict, dict]:
    """The generic and IPv4 config dicts reported to NM (plain Python values)."""
    a32 = ip_to_u32(t.address)
    config = {
        # NM insists on a "VPN gateway". NetBird is a mesh with no single
        # gateway, so report our own tunnel address; the host route NM adds
        # for it is harmless because the kernel's local table wins.
        "gateway": ("u", a32),
        "tundev": ("s", t.iface),
        "has-ip4": ("b", True),
        "has-ip6": ("b", False),
    }
    ip4 = {
        "address": ("u", a32),
        "prefix": ("u", t.prefix),
        "never-default": ("b", True),   # NetBird manages its own routes
    }
    return config, ip4


class Controller:
    def __init__(self, cfg: Config, *, netbird: NetBird | None = None,
                 state: StateDir | None = None, log: Log = print,
                 sleep: Callable[[float], None] = time.sleep,
                 get_ipv4: Callable[[str], tuple[str, int] | None] = iface_ipv4,
                 exists: Callable[[str], bool] = iface_exists):
        self.cfg = cfg
        self.netbird = netbird or NetBird(cfg.netbird)
        self.state = state or StateDir(cfg.state_dir)
        self.log = log
        self.sleep = sleep
        self.get_ipv4 = get_ipv4
        self.exists = exists

    # connect ----------------------------------------------------------------
    def bring_up(self) -> TunnelInfo:
        """Run 'netbird up' and wait for the interface. Blocking."""
        if not self.state.wait_down_done(timeout=40, sleep=self.sleep):
            self.log("a previous 'netbird down' is taking long; continuing anyway")
        with self.state.lock():
            self.log("running netbird up")
            try:
                r = self.netbird.up(timeout=self.cfg.up_timeout)
            except subprocess.TimeoutExpired as e:
                raise ConnectError("netbird up timed out; is NetBird logged in? "
                                   "Run 'netbird up' once in a terminal") from e
            except OSError as e:
                raise ConnectError(f"cannot run {self.cfg.netbird}: {e}") from e
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "").strip() or f"exit code {r.returncode}"
            raise ConnectError(f"netbird up failed: {detail}")
        for _ in range(40):
            ip = self.get_ipv4(self.cfg.iface)
            if ip:
                info = TunnelInfo(self.cfg.iface, *ip)
                self.log(f"up: {info.address}/{info.prefix} on {info.iface}")
                return info
            self.sleep(0.5)
        raise ConnectError(f"interface {self.cfg.iface} never got an IPv4 address")

    # disconnect -------------------------------------------------------------
    def run_down(self) -> str:
        """The body of the down helper: stop NetBird unless it should keep
        running. Returns what it did ('down', 'resume' or 'keep')."""
        try:
            with self.state.lock():
                waited = 0.0
                while True:
                    # Set by the dispatcher hook when the network went away:
                    # it re-activates the VPN once a network is back.
                    if self.state.has_resume():
                        self.log("network change; keeping NetBird running")
                        return "resume"
                    if self.state.keep():
                        self.log("uninstalling; leaving NetBird as it is")
                        return "keep"
                    if waited >= self.cfg.down_grace:
                        break
                    self.sleep(0.25)
                    waited += 0.25
                self.log("running netbird down")
                try:
                    self.netbird.down()
                except (OSError, subprocess.TimeoutExpired) as e:
                    self.log(f"netbird down failed: {e}")
                return "down"
        finally:
            self.state.clear_down_pending()

    # watching ---------------------------------------------------------------
    def tunnel_alive(self) -> bool:
        return self.exists(self.cfg.iface)


class Watchdog:
    """Notices NetBird being stopped outside NM (CLI, tray app).

    ``tick()`` is called every few seconds; it returns False once the
    interface has been missing on two checks in a row."""

    def __init__(self, alive: Callable[[], bool], misses: int = 2):
        self.alive = alive
        self.misses = misses
        self.missing = 0

    def tick(self) -> bool:
        if self.alive():
            self.missing = 0
            return True
        self.missing += 1
        return self.missing < self.misses
