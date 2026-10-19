# Changelog

## 0.4.0

Two ideas borrowed from
[groundbasesoft/netbird-network-manager-vpn-plugin](https://github.com/groundbasesoft/netbird-network-manager-vpn-plugin):

- **Talks to NetBird's daemon directly** over its gRPC socket (`Status`,
  `GetActiveProfile`, `Login`, `Up`, `Down`, `GetConfig`) instead of running
  the `netbird` command and parsing its output. Still done as the logged-in
  user, in a separate process, so the daemon sees the same caller as before.
  Needs Python's grpcio (`python-grpcio`); without it, `--backend cli` keeps
  using the `netbird` command. Verified against the real NetBird 0.81.2 daemon.
- **No IP configuration is reported to NetworkManager.** NetBird owns its
  addresses, routes and DNS; NetworkManager now leaves them alone, so it no
  longer strips NetBird's address when the VPN goes down. The VPN's gateway
  is NetBird's management server instead of our own tunnel address.
- New install options: `--backend`, `--daemon-addr`, `--vpn-dir`. The README
  explains how the VPN plugin directory is chosen.

## 0.3.0

- The `netbird` command now runs as **whoever is logged in on the desktop
  when the VPN is turned on** (the active systemd-logind session), instead of
  the user who ran `install`. The matching disconnect acts for the same user,
  and with nobody logged in it runs as root. Nothing is pinned at install
  time; `--user NAME` / `--as-root` still pin it, `--session-user` undoes that.
  Upgrading from 0.2.1 switches to the new default.

## 0.2.1

- **Fixed**: connecting failed with "interface wt0 never got an IPv4 address"
  when NetBird was already running. Taking the VPN down makes NetworkManager
  remove NetBird's address from its interface; if NetBird stayed up (network
  switch, uninstall, an earlier version), the next `netbird up` only said
  "Already connected" and the address never came back. The plugin now
  notices this and restarts NetBird, and uninstall restores the address when
  it leaves NetBird running.
- The `netbird` command now runs as your user instead of root (`--user NAME`,
  default: whoever ran `install` through sudo; `--as-root` keeps the old
  behavior). It only talks to NetBird's own daemon, so it doesn't need root,
  and it now behaves exactly like `netbird` in your terminal. Install and
  `doctor` check that this user can reach the daemon.
- Connecting waits for NetBird's address for the whole time NetworkManager
  allows (about 55 seconds) instead of 20.
- Failures explain themselves in `journalctl -t nm-netbird`: what `netbird up`
  printed and, when the interface doesn't come up, `netbird status` and the
  system's IPv4 addresses.

## 0.2.0

Rewritten in Python as a proper project.

- **Fixed**: "The VPN service 'org.freedesktop.NetworkManager.netbird' was not
  installed" on Arch and anywhere `/etc/NetworkManager/VPN` didn't exist when
  NetworkManager started. The plugin description now goes into
  `/usr/lib/NetworkManager/VPN`, which NetworkManager watches; when no plugin
  directory exists at all, the installer restarts NetworkManager (asking first).
- **Fixed**: `vpn.persistent` is now off. With it on, NetworkManager restarted
  NetBird right after you stopped it from its CLI.
- Enables `NetworkManager-dispatcher.service` when it's disabled (needed for
  bringing the VPN back after a network switch).
- Uninstall leaves NetBird running unless `--stop-netbird` is given.
- New `status` and `doctor [--live]` commands; logs go to `journalctl -t nm-netbird`.
- Upgrading from 0.1 is automatic: its files are replaced and the existing
  connection is kept.
- Unit tests for all the logic, and an integration suite against a real
  NetworkManager.

## 0.1.0

Single shell script (`netbird-nm-vpn.sh`).
