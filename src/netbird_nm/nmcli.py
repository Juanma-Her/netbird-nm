"""Thin wrapper around nmcli.

Everything goes through one ``runner`` callable (``subprocess.run`` by
default) so tests can substitute a fake.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Callable

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def split_terse(line: str) -> list[str]:
    """Split one line of ``nmcli -t`` output.

    nmcli escapes ``:`` and ``\\`` inside values with a backslash, so a VPN
    named ``a:b`` comes out as ``a\\:b``.
    """
    fields, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    fields.append("".join(cur))
    return fields


class Nmcli:
    def __init__(self, runner: Runner = subprocess.run, binary: str = "nmcli"):
        self.runner = runner
        self.binary = binary

    def run(self, *args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
        return self.runner([self.binary, *args], capture_output=True, text=True, timeout=timeout)

    def _out(self, *args: str) -> str:
        try:
            r = self.run(*args)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return r.stdout.strip() if r.returncode == 0 else ""

    # -- queries -------------------------------------------------------------
    def running(self) -> bool:
        try:
            return self.run("-t", "general", "status").returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def connections(self) -> list[tuple[str, str, str]]:
        """[(uuid, type, name)] for every saved connection."""
        rows = []
        for line in self._out("-t", "-f", "UUID,TYPE,NAME", "connection", "show").splitlines():
            parts = split_terse(line)
            if len(parts) >= 3:
                rows.append((parts[0], parts[1], ":".join(parts[2:])))
        return rows

    def field(self, uuid: str, name: str) -> str:
        return self._out("-g", name, "connection", "show", uuid)

    def vpn_connections(self, service: str) -> list[str]:
        """UUIDs of the VPN profiles that use ``service``."""
        return [uuid for uuid, ctype, _ in self.connections()
                if ctype == "vpn" and self.field(uuid, "vpn.service-type") == service]

    def connection_state(self, uuid: str) -> str:
        """'activated', 'activating', ... or '' when the profile isn't active."""
        return self._out("-g", "GENERAL.STATE", "connection", "show", uuid)

    def device_state(self, iface: str) -> str:
        """e.g. '100 (connected)'; '' if the device doesn't exist."""
        if not iface:
            return ""
        return self._out("-g", "GENERAL.STATE", "device", "show", iface)

    # -- actions -------------------------------------------------------------
    def up(self, uuid: str, timeout: float = 90) -> subprocess.CompletedProcess[str]:
        return self.run("connection", "up", "uuid", uuid, timeout=timeout)

    def down(self, uuid: str) -> subprocess.CompletedProcess[str]:
        return self.run("connection", "down", "uuid", uuid)

    def delete(self, uuid: str) -> subprocess.CompletedProcess[str]:
        return self.run("connection", "delete", "uuid", uuid)

    def modify(self, uuid: str, *settings: str) -> subprocess.CompletedProcess[str]:
        return self.run("connection", "modify", "uuid", uuid, *settings)

    def add_vpn(self, name: str, service: str, extra: Sequence[str] = ()) -> subprocess.CompletedProcess[str]:
        return self.run("connection", "add", "type", "vpn", "con-name", name,
                        "vpn-type", service, *extra)

    def reload_conf(self) -> bool:
        try:
            return self.run("general", "reload", "conf").returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
