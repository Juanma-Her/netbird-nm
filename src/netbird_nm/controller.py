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
from .netbird import NetBird, RunAs, iface_exists, iface_ipv4, summarize
from .session import session_user
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


def resolve_user(cfg: Config, session: Callable[[], str | None] = session_user) -> str:
    """Who should run the netbird CLI right now; "" means root."""
    if cfg.run_as == "root":
        return ""
    if cfg.run_as == "session":
        return session() or ""
    return cfg.run_as


def make_netbird(cfg: Config, user: str, runner=subprocess.run) -> NetBird:
    """KeyError if ``user`` doesn't exist."""
    return NetBird(cfg.netbird, runner, run_as=RunAs.lookup(user) if user else None)


class Controller:
    def __init__(self, cfg: Config, *, netbird: NetBird | None = None,
                 state: StateDir | None = None, log: Log = print,
                 sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], float] = time.monotonic,
                 get_ipv4: Callable[[str], tuple[str, int] | None] = iface_ipv4,
                 exists: Callable[[str], bool] = iface_exists,
                 session: Callable[[], str | None] = session_user,
                 runner=subprocess.run, ip_runner=subprocess.run):
        self.cfg = cfg
        self.fixed_netbird = netbird          # tests pass one in; otherwise chosen per connect
        self.runner = runner
        self.netbird = netbird or NetBird(cfg.netbird, runner)
        self.state = state or StateDir(cfg.state_dir)
        self.log = log
        self.sleep = sleep
        self.now = now
        self.get_ipv4 = get_ipv4
        self.exists = exists
        self.session = session
        self.ip_runner = ip_runner

    @property
    def who(self) -> str:
        return self.netbird.run_as.name if self.netbird.run_as else "root"

    def _choose_netbird(self, user: str | None = None) -> None:
        """Point self.netbird at the user to act for. ``user`` None = resolve now."""
        if self.fixed_netbird:
            self.netbird = self.fixed_netbird
            return
        if user is None:
            user = resolve_user(self.cfg, self.session)
        try:
            self.netbird = make_netbird(self.cfg, user, self.runner)
        except KeyError:
            raise ConnectError(f"user {user!r} doesn't exist (run_as in /etc/netbird-nm.json)") from None

    # connect ----------------------------------------------------------------
    def bring_up(self) -> TunnelInfo:
        """Run 'netbird up' and wait for the interface. Blocking."""
        self._choose_netbird()
        self.state.set_user(self.who)         # the matching 'down' acts for the same user
        start = self.now()
        deadline = start + self.cfg.connect_timeout
        if not self.state.wait_down_done(timeout=40, sleep=self.sleep):
            self.log("a previous 'netbird down' is taking long; continuing anyway")
        with self.state.lock():
            self.log(f"running netbird up (as {self.who})")
            budget = max(1.0, min(self.cfg.up_timeout, deadline - self.now()))
            try:
                r = self.netbird.up(timeout=budget)
            except subprocess.TimeoutExpired as e:
                raise ConnectError(f"netbird up didn't finish within {budget:.0f}s; is NetBird "
                                   "logged in? Run 'netbird up' once in a terminal") from e
            except OSError as e:
                raise ConnectError(f"cannot run {self.cfg.netbird} as {self.who}: {e}") from e
        output = summarize((r.stdout or "") + "\n" + (r.stderr or ""))
        if r.returncode != 0:
            raise ConnectError(f"netbird up failed (exit {r.returncode}): {output or 'no output'}")
        if output:
            self.log(f"netbird up said: {output}")
        already = "already connected" in output.lower()
        up_done = self.now()
        restarted = False

        while True:
            ip = self.get_ipv4(self.cfg.iface)
            if not ip and already and not restarted and self.now() - up_done >= 3 \
                    and self.exists(self.cfg.iface):
                # NetworkManager removes the address it configured on the
                # tunnel when the VPN goes down. If NetBird kept running
                # (network change, uninstall, an earlier version), it's left
                # "connected" with no address, and 'netbird up' won't fix that.
                self.log(f"NetBird is connected but {self.cfg.iface} has no IPv4 address "
                         "(NetworkManager removes it when the VPN goes down); restarting NetBird")
                self.restart_netbird(deadline)
                restarted = True
                continue
            if ip:
                info = TunnelInfo(self.cfg.iface, *ip)
                self.log(f"up: {info.address}/{info.prefix} on {info.iface} "
                         f"after {self.now() - start:.1f}s")
                return info
            if self.now() >= deadline:
                break
            self.sleep(0.5)
        self.log_diagnostics()
        raise ConnectError(f"interface {self.cfg.iface} had no IPv4 address "
                           f"{self.now() - start:.0f}s after connecting started")

    def restart_netbird(self, deadline: float) -> None:
        with self.state.lock():
            try:
                self.netbird.down()
                budget = max(1.0, min(self.cfg.up_timeout, deadline - self.now()))
                r = self.netbird.up(timeout=budget)
            except (OSError, subprocess.SubprocessError) as e:
                raise ConnectError(f"restarting NetBird failed: {e}") from e
        if r.returncode != 0:
            out = summarize((r.stdout or "") + "\n" + (r.stderr or ""))
            raise ConnectError(f"netbird up failed after restarting NetBird: {out}")

    def log_diagnostics(self) -> None:
        """What NetBird and the system look like when the interface didn't come up."""
        try:
            st = self.netbird.status()
            self.log(f"netbird status: {summarize((st.stdout or '') + (st.stderr or ''))}")
        except (OSError, subprocess.SubprocessError) as e:
            self.log(f"netbird status failed: {e}")
        try:
            ip = self.ip_runner(["ip", "-brief", "-4", "addr"], capture_output=True, text=True, timeout=5)
            self.log(f"IPv4 addresses: {summarize(ip.stdout)}")
        except (OSError, subprocess.SubprocessError):
            pass

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
                remembered = self.state.get_user()
                try:
                    self._choose_netbird(None if remembered is None else
                                         ("" if remembered == "root" else remembered))
                except ConnectError:
                    self.netbird = NetBird(self.cfg.netbird, self.runner)    # fall back to root
                self.log(f"running netbird down (as {self.who})")
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
