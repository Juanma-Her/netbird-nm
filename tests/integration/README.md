# Integration tests

`test_live.py` installs netbird-nm into a real NetworkManager and drives it
the way a user (or Quickshell, through `nmcli`) would, with
[`fake-netbird`](fake-netbird) standing in for NetBird: `up` creates a TUN
interface called `wt0` with a 100.x address, `down` removes it.

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

The suite takes about three and a half minutes.
