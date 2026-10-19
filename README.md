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
these Python packages, which the installer offers to install if missing:

| | Arch | Debian/Ubuntu | Fedora |
|---|---|---|---|
| libnm bindings (required) | `python-gobject` `libnm` | `python3-gi` `gir1.2-nm-1.0` | `python3-gobject` `NetworkManager-libnm` |
| gRPC (recommended) | `python-grpcio` | `python3-grpcio` | `python3-grpcio` |

With grpcio, netbird-nm talks to NetBird's daemon directly over its gRPC
socket, the same API the `netbird` command uses. Without it, it falls back to
running the `netbird` command (`--backend cli`).

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
| `--backend grpc\|cli` | talk to NetBird's daemon directly (default when grpcio is installed), or run the `netbird` command |
| `--daemon-addr ADDR` | NetBird's daemon socket (default `unix:///var/run/netbird.sock`) |
| `--vpn-dir DIR` | NetworkManager's VPN plugin directory (see below; normally automatic) |
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

### NetworkManager's VPN plugin directory

NetworkManager finds VPN plugins through small description files in a VPN
plugin directory, and it only notices new files in a directory that already
existed when it started. The installer picks one that does:

1. `/usr/lib/NetworkManager/VPN`, which NetworkManager's own package
   provides on Arch, Debian/Ubuntu, Fedora and openSUSE. This is the normal
   case on every distro; nothing to configure.
2. Otherwise `/etc/NetworkManager/VPN`, if it exists.
3. If neither exists, it creates the first and offers to restart
   NetworkManager once (it won't see the plugin until it restarts).

> **Note:** if your distribution keeps NetworkManager's VPN plugins somewhere
> else (some build NetworkManager with a different `--prefix`, e.g.
> `/usr/local/lib/NetworkManager/VPN`), point the installer at it with
> `--vpn-dir DIR`. The directory NetworkManager actually uses is wherever the
> `nm-*-service.name` files of other VPN plugins (OpenVPN, WireGuard…) are;
> `ls /usr/lib/NetworkManager/VPN /etc/NetworkManager/VPN` shows them. Arch
> needs nothing here: it ships `/usr/lib/NetworkManager/VPN` but no
> `/etc/NetworkManager/VPN`, which is exactly why netbird-nm uses the former.

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
  installed"**: NetworkManager hasn't seen the plugin description. Restart it
  once (`sudo systemctl restart NetworkManager`); if that doesn't help, your
  NetworkManager uses a different plugin directory: see
  [NetworkManager's VPN plugin directory](#networkmanagers-vpn-plugin-directory)
  and reinstall with `--vpn-dir`.
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
| `service.py` | `/usr/local/libexec/nm-netbird-service` | the VPN plugin NetworkManager starts: connects NetBird on connect, a detached helper disconnects it on disconnect, and a watchdog notices NetBird stopping on its own |
| `daemon.py` | (run by the plugin, as the logged-in user) | a minimal client for NetBird's daemon gRPC API: `Status`, `GetActiveProfile`, `Login`, `Up`, `Down`, `GetConfig`; protobuf encoded by hand, so grpcio is the only dependency |
| `nm-netbird-service.name` | `/usr/lib/NetworkManager/VPN/` (see above) | tells NetworkManager the plugin exists |
| D-Bus policy | `/etc/dbus-1/system.d/nm-netbird-service.conf` | lets the plugin own its bus name |
| `dispatcher.py` | `/etc/NetworkManager/dispatcher.d/90-netbird-vpn` | handles network changes and adopting an already running NetBird |
| keyfile conf | `/etc/NetworkManager/conf.d/99-netbird-unmanaged.conf` | hides `wt0` from NetworkManager |
| config | `/etc/netbird-nm.json` | backend, daemon socket, who runs it, interface, timings |

The plugin tells NetworkManager there is **no IP configuration** for it to
manage: NetBird owns its interface's addresses, routes and DNS. That also
means NetworkManager never touches NetBird's address, even when the VPN goes
down while NetBird keeps running (network switches, uninstall). As the VPN's
"gateway" it reports NetBird's management server, so NetworkManager keeps
that one route via your physical connection. (Versions before 0.4 reported
NetBird's address to NetworkManager, which then removed it whenever the VPN
went down; netbird-nm still repairs a NetBird left in that state.)

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

- NetworkManager shows no IP address for the VPN (it doesn't manage any);
  `netbird status` or `ip addr show wt0` show NetBird's address.
- It uses NetBird's existing login and active profile. Logging in (SSO or a
  setup key) is done with `netbird up` / `netbird login` in a terminal;
  connecting from NetworkManager while logged out fails with a message
  saying so.
- There is no GUI editor plugin, so "Configure VPN…" can't edit the
  connection; use `nmcli connection modify NetBird …` if you ever need to.
- Toggling from a script faster than about 50 ms can be rejected by
  NetworkManager itself ("VPN service stopped unexpectedly"). NetworkManager
  and NetBird still end up agreeing; clicking in a menu never hits this.
