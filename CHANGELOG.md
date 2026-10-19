# Changelog

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
