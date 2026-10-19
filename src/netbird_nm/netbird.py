"""Talking to NetBird and looking at its interface."""

from __future__ import annotations

import os
import pwd
import subprocess
from dataclasses import dataclass
from typing import Callable

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


@dataclass(frozen=True)
class RunAs:
    """Who runs the netbird CLI. The CLI only talks to NetBird's own daemon
    (a separate root service), so it doesn't need root itself."""
    name: str
    uid: int
    gid: int
    groups: tuple[int, ...]
    home: str

    @classmethod
    def lookup(cls, name: str) -> RunAs:
        pw = pwd.getpwnam(name)          # KeyError if the user doesn't exist
        groups = tuple(os.getgrouplist(pw.pw_name, pw.pw_gid))
        return cls(pw.pw_name, pw.pw_uid, pw.pw_gid, groups, pw.pw_dir)

    def env(self) -> dict[str, str]:
        # A clean environment, the way a login would set it up. No SUDO_*
        # variables, so NetBird acts for this user and nobody else.
        return {"PATH": SAFE_PATH, "HOME": self.home, "USER": self.name,
                "LOGNAME": self.name, "LANG": os.environ.get("LANG", "C.UTF-8")}

    def subprocess_kwargs(self) -> dict:
        return {"user": self.uid, "group": self.gid, "extra_groups": list(self.groups),
                "env": self.env(), "cwd": "/"}


class NetBird:
    def __init__(self, binary: str, runner: Runner = subprocess.run, run_as: RunAs | None = None):
        self.binary = binary
        self.runner = runner
        self.run_as = run_as

    def _run(self, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        kw = self.run_as.subprocess_kwargs() if self.run_as else {}
        return self.runner([self.binary, *args], capture_output=True, text=True,
                           timeout=timeout, **kw)

    def up(self, timeout: float) -> subprocess.CompletedProcess[str]:
        return self._run("up", timeout=timeout)

    def down(self, timeout: float = 30) -> subprocess.CompletedProcess[str]:
        return self._run("down", timeout=timeout)

    def status(self, timeout: float = 10) -> subprocess.CompletedProcess[str]:
        return self._run("status", timeout=timeout)


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


def summarize(output: str, max_lines: int = 12) -> str:
    """Compact multi-line command output for a log line."""
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... ({len(lines) - max_lines} more lines)"]
    return " | ".join(lines)
