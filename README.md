# netbird-nm

Control [NetBird](https://netbird.io) from NetworkManager as a regular VPN
connection: it shows up in nm-applet, GNOME/KDE network menus, Quickshell or
Waybar bars that go through `nmcli`, and `nmcli connection up/down` itself.

```
$ nmcli connection show
NAME     UUID                                  TYPE  DEVICE
Home     8c1e…                                 wifi  wlan0
NetBird  f1b1c765-5607-47b7-99b6-446628f2baf3  vpn   wlan0
```

## Why

NetBird manages its own WireGuard interface (`wt0`). NetworkManager notices it
and shows it as an "externally connected" device. If you switch that off from
NetworkManager it tears the interface down, but has no way to bring it back:
only `netbird down && netbird up` recovers.

netbird-nm adds a small NetworkManager **VPN plugin** for NetBird instead.
Turning the "NetBird" VPN connection on runs `netbird up`; turning it off runs
`netbird down`. NetBird's own interface is hidden from NetworkManager so the
broken toggle disappears.

## Install

Requirements: NetworkManager, NetBird (logged in once with `netbird up`), and
the Python GObject bindings for libnm (`python-gobject` + `libnm` on Arch,
`python3-gi` + `gir1.2-nm-1.0` on Debian/Ubuntu). The installer offers to
install the bindings if they're missing.

```sh
git clone <this repo> && cd netbird-nm
./netbird-nm install          # asks for sudo
```

That's all: "NetBird" now appears among your VPN connections. No pip install
is needed; the installer copies the package to `/usr/local/lib/netbird-nm`.

Options:

| option | |
|---|---|
| `--name NAME` | connection name (default `NetBird`) |
| `--iface IFACE` | NetBird's interface (default: read from `/etc/netbird/config.json`, else `wt0`) |
| `--netbird PATH` | the netbird binary (default: from `PATH`) |
| `--user NAME` | always run the `netbird` command as this user |
| `--as-root` | always run the `netbird` command as root |
| `--session-user` | back to the default: whoever is logged in, at each connect |
| `--restart-nm` / `--no-restart-nm` | restart NetworkManager if it's needed, without asking / never |
| `--no-deps` | don't install missing Python bindings |
| `-y` | answer yes to questions |

### Which user runs `netbird`

NetworkManager starts every VPN plugin as root, but the plugin doesn't run
the `netbird` command as root. Each time the VPN is turned on, it runs it as
**whoever is logged in on the desktop at that moment**, the person clicking
the toggle, and the matching disconnect acts for that same user. That command
only asks NetBird's own daemon, a separate root service, to connect or
disconnect, so it needs no privileges, and this way it acts on the same
NetBird profile as `netbird` in that user's terminal.

NetworkManager doesn't tell VPN plugins which user asked for a connection,
so "logged in" means the active session on the main seat according to
systemd-logind (or, without a seat, the only user with an active session).
When nobody is logged in, e.g. at boot, `netbird` runs as root. Nothing is
decided at install time; `--user` or `--as-root` pin it if you prefer.
`install` and `doctor` show who it would be right now and check that this
user can reach NetBird's daemon.

## Use

Toggle "NetBird" in your network menu, or:

```sh
nmcli connection up NetBird
nmcli connection down NetBird
```

What happens around that:

- **NetBird stopped outside NetworkManager** (CLI, tray app): the VPN is shown
  as off within a few seconds.
- **Switching Wi-Fi networks or unplugging the cable**: NetworkManager drops
  every VPN when the connection under it goes away. NetBird keeps running
  through the gap and the VPN is shown as on again once the new network is up.
- **Switching it off yourself** sticks, even across network changes.
- **At boot**, if NetBird's own service already connected, the VPN is shown
  as on about ten seconds after the network comes up.

## Uninstall

```sh
./netbird-nm uninstall              # NetBird stays connected if it was
./netbird-nm uninstall --stop-netbird
```

Uninstall only removes files that carry netbird-nm's marker, and also cleans
up files from the earlier single-script version (`netbird-nm-vpn.sh`).

## Troubleshooting

```sh
./netbird-nm status          # what's installed, connection state
./netbird-nm doctor          # checks for common problems
./netbird-nm doctor --live   # also toggles the VPN to prove NM can drive it
journalctl -t nm-netbird -f  # plugin and dispatcher logs
```

- **"The VPN service 'org.freedesktop.NetworkManager.netbird' was not
  installed"**: NetworkManager only watches VPN plugin directories that
  existed when it started. netbird-nm puts its plugin description in
  `/usr/lib/NetworkManager/VPN`, which NetworkManager packages ship; if your
  system has neither that nor `/etc/NetworkManager/VPN`, the installer offers
  to restart NetworkManager. `sudo systemctl restart NetworkManager` also fixes it.
- **The VPN doesn't come back after a Wi-Fi switch**: the dispatcher service
  must be enabled. The installer does this; check with
  `systemctl is-enabled NetworkManager-dispatcher.service`.
- **Connecting times out**: NetBird probably isn't logged in. Run `netbird up`
  once in a terminal. The log shows what `netbird up` and `netbird status`
  printed.
- **"'netbird status' failed as <user>"** during install or in `doctor`:
  that user can't control NetBird's daemon. Reinstall with `--user` someone
  who can, or `--as-root`.

## How it works

| piece | installed as | role |
|---|---|---|
| `service.py` | `/usr/local/libexec/nm-netbird-service` | the VPN plugin NetworkManager starts: `netbird up` on connect, a detached helper runs `netbird down` on disconnect, and a watchdog notices NetBird stopping on its own |
| `nm-netbird-service.name` | `/usr/lib/NetworkManager/VPN/` | tells NetworkManager the plugin exists |
| D-Bus policy | `/etc/dbus-1/system.d/nm-netbird-service.conf` | lets the plugin own its bus name |
| `dispatcher.py` | `/etc/NetworkManager/dispatcher.d/90-netbird-vpn` | handles network changes and adopting an already running NetBird |
| keyfile conf | `/etc/NetworkManager/conf.d/99-netbird-unmanaged.conf` | hides `wt0` from NetworkManager |
| config | `/etc/netbird-nm.json` | netbird path, interface and timings |

The plugin reports NetBird's IPv4 address to NetworkManager with
`never-default`, and no DNS, so NetBird keeps managing its own routes and DNS.

One quirk of that: when the VPN goes down, NetworkManager removes the address
it "configured" from NetBird's interface. Whenever NetBird is meant to keep
running across that (network switches, uninstall), netbird-nm restarts it so
the address comes back.

The connection logic (`controller.py`, `dispatcher.py`, `state.py`) has no
GObject dependency and is unit-tested on its own; `service.py` is the thin
libnm adapter.

## Development

```sh
pip install -e '.[dev]'      # or just have pytest and ruff around
make lint test               # ruff + unit tests, no root needed
```

Integration tests run the real installer against a real NetworkManager with
a fake `netbird`. They start their own NetworkManager, so only run them in a
throwaway root container or VM; see [tests/integration/README.md](tests/integration/README.md).

## Limitations

- IPv4 only is reported to NetworkManager; NetBird's IPv6 setup is untouched.
- There is no GUI editor plugin, so "Configure VPN…" can't edit the
  connection; use `nmcli connection modify NetBird …` if you ever need to.
- Toggling from a script faster than about 50 ms can be rejected by
  NetworkManager itself ("VPN service stopped unexpectedly"). NetworkManager
  and NetBird still end up agreeing; clicking in a menu never hits this.
