"""Talking to NetBird and looking at its interface."""

from __future__ import annotations

import os
import subprocess
from typing import Callable

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class NetBird:
    def __init__(self, binary: str, runner: Runner = subprocess.run):
        self.binary = binary
        self.runner = runner

    def up(self, timeout: float) -> subprocess.CompletedProcess[str]:
        return self.runner([self.binary, "up"], capture_output=True, text=True, timeout=timeout)

    def down(self, timeout: float = 30) -> subprocess.CompletedProcess[str]:
        return self.runner([self.binary, "down"], capture_output=True, text=True, timeout=timeout)


def iface_exists(iface: str, sysfs: str = "/sys/class/net") -> bool:
    return os.path.isdir(os.path.join(sysfs, iface))


def parse_ip_addr(output: str) -> tuple[str, int] | None:
    """First IPv4 address in ``ip -4 -o addr show`` output."""
    for line in output.splitlines():
        words = line.split()
        if "inet" in words:
            i = words.index("inet")
            if i + 1 < len(words) and "/" in words[i + 1]:
                addr, prefix = words[i + 1].split("/", 1)
                try:
                    return addr, int(prefix)
                except ValueError:
                    continue
    return None


def iface_ipv4(iface: str, runner: Runner = subprocess.run) -> tuple[str, int] | None:
    try:
        r = runner(["ip", "-4", "-o", "addr", "show", "dev", iface],
                   capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_ip_addr(r.stdout) if r.returncode == 0 else None
