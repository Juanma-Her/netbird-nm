# Integration tests

`test_live.py` installs netbird-nm into a real NetworkManager and drives it
the way a user (or Quickshell, through `nmcli`) would. NetBird is replaced by
two fakes with the same split as the real thing:
[`fake-netbirdd`](fake-netbirdd), a root daemon on a unix socket that creates
and removes a TUN interface `wt0` with a 100.x address, and
[`fake-netbird`](fake-netbird), a CLI that only sends it commands, so it can
run as an ordinary user. The daemon logs each caller's uid, which is how the
tests prove `netbird` runs as the logged-in user. Logins are emulated by
writing `/run/systemd/seats/seat0` the way systemd-logind does, switching
between two users (`nbtest`, `nbtest2`) the tests create. The daemon can also be told to fail `up`, or to bring the interface up
late, through files in `/tmp/fake-netbird`.

They start, stop and reconfigure NetworkManager, so **only run them in a
disposable root container or VM**, never on your desktop.

```sh
# in e.g. a privileged ubuntu:24.04 container, as root
sh tests/integration/sandbox.sh deps     # network-manager, dbus, python3-gi, pytest
sh tests/integration/sandbox.sh start    # own D-Bus + NetworkManager + a base connection
NETBIRD_NM_INTEGRATION=1 python3 -m pytest tests/integration -m integration -v
sh tests/integration/sandbox.sh stop
```

The sandbox marks every existing interface unmanaged, so the container's own
networking is left alone. A TUN device (`nmsbx-base`) plays the part of the
Wi-Fi/ethernet connection the VPN rides on; taking it down and up simulates
a network switch.

What's covered:

- the plugin directory problem from 0.1 (NetworkManager not watching
  `/etc/NetworkManager/VPN`), and restarting NM when no plugin directory exists
- connect, disconnect, reconnect; click-speed and scripted toggling
- NetBird stopped from its CLI while the VPN is on
- network switches, and switching the VPN off staying off across them
- adopting an already running NetBird when a network comes up
- `netbird up` failing (e.g. logged out)
- the exact `nmcli` calls the Quickshell bar makes, including `nmcli monitor`
- `status`, `doctor --live`, uninstall, and upgrading from the shell script
  in [`legacy/`](legacy/)
- `netbird` running as the logged-in user, following a different user logging
  in, disconnecting as the user who connected, root when nobody is logged in,
  and `--user` / `--as-root` / `--session-user`
- a slow first connection (address after 30s), and a failure that explains
  itself in the log
- NetBird left running without its address (NetworkManager removes it when
  the VPN goes down) being repaired on connect and after network changes

The suite takes about eight minutes.
