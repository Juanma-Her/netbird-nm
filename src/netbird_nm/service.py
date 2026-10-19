"""The NetworkManager VPN service plugin (org.freedesktop.NetworkManager.netbird).

NetworkManager starts this when the "NetBird" VPN connection is activated:
connect runs 'netbird up', disconnect runs 'netbird down'. All the logic lives
in ``controller.py``; this module only adapts it to libnm's VpnServicePlugin.

Run with ``--down`` it is the detached helper that performs 'netbird down':
NM kills the plugin right after a disconnect, so that work has to live in a
process of its own.
"""

from __future__ import annotations

import os
import subprocess
import sys
import syslog
import threading

from .config import Config
from .const import SERVICE
from .controller import ConnectError, Controller, TunnelInfo, Watchdog, vpn_config

os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def log(msg: str) -> None:
    syslog.syslog(syslog.LOG_INFO, msg)          # journalctl -t nm-netbird
    try:
        print(f"nm-netbird: {msg}", file=sys.stderr, flush=True)
    except OSError:
        pass


def _make_plugin_class():
    import gi
    gi.require_version("NM", "1.0")
    from gi.repository import NM, GLib

    def variant_dict(d: dict) -> GLib.Variant:
        return GLib.Variant("a{sv}", {k: GLib.Variant(t, v) for k, (t, v) in d.items()})

    class NetbirdPlugin(NM.VpnServicePlugin):
        def __init__(self, loop, controller: Controller, launcher: str, **kw):
            super().__init__(**kw)
            self.loop = loop
            self.ctl = controller
            self.launcher = launcher
            self.watch_id = 0
            self.stopping = False

        # need_secrets is deliberately not overridden: libnm then answers
        # "no secrets needed" itself (overriding it trips a PyGObject bug).

        def do_connect(self, connection):
            self.stopping = False
            threading.Thread(target=self._bring_up, daemon=True).start()
            return True

        def do_disconnect(self):
            self.stopping = True
            self._stop_watch()
            # Mark the down as pending *before* returning so a quick reconnect
            # can't overtake it, then run it in a helper that outlives us.
            self.ctl.state.mark_down_pending()
            subprocess.Popen([sys.executable, self.launcher, "--down"],
                             start_new_session=True, stdin=subprocess.DEVNULL)
            # Leave the bus right away so NM starts a fresh plugin for the
            # next connect instead of handing it to this exiting one.
            GLib.idle_add(self._quit, priority=GLib.PRIORITY_LOW)
            return True

        def _quit(self):
            self.loop.quit()
            return False

        def _bring_up(self):
            try:
                info = self.ctl.bring_up()
            except ConnectError as e:
                GLib.idle_add(self._fail, str(e))
            except Exception as e:                       # never leave NM hanging
                GLib.idle_add(self._fail, f"unexpected error: {e!r}")
            else:
                GLib.idle_add(self._report, info)

        def _fail(self, msg: str):
            log(msg)
            self.failure(NM.VpnPluginFailure.CONNECT_FAILED)   # libnm stops the plugin
            return False

        def _report(self, info: TunnelInfo):
            if self.stopping:
                return False
            config, ip4 = vpn_config(info)
            self.set_config(variant_dict(config))
            self.set_ip4_config(variant_dict(ip4))
            dog = Watchdog(self.ctl.tunnel_alive)
            self.watch_id = GLib.timeout_add_seconds(self.ctl.cfg.watch_interval,
                                                     self._watch, dog)
            return False

        def _watch(self, dog: Watchdog):
            if dog.tick():
                return True
            log(f"{self.ctl.cfg.iface} disappeared; marking the VPN as disconnected")
            self.watch_id = 0
            try:
                NM.VpnServicePlugin.disconnect(self)
            except GLib.Error as e:
                log(f"disconnect: {e.message}")
            return False

        def _stop_watch(self):
            if self.watch_id:
                GLib.source_remove(self.watch_id)
                self.watch_id = 0

    return GLib, NetbirdPlugin


def main(argv: list[str] | None = None, launcher: str | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    launcher = launcher or os.path.abspath(sys.argv[0])
    syslog.openlog("nm-netbird", syslog.LOG_PID, syslog.LOG_DAEMON)
    controller = Controller(Config.load(), log=log)

    if argv == ["--down"]:
        controller.run_down()
        return 0

    GLib, NetbirdPlugin = _make_plugin_class()
    loop = GLib.MainLoop()
    plugin = NetbirdPlugin(loop, controller, launcher, service_name=SERVICE, watch_peer=True)
    plugin.init(None)
    plugin.connect("quit", lambda *_: loop.quit())
    loop.run()
    return 0
