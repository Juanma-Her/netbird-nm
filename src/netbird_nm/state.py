"""Small state shared between the plugin, its down helper and the dispatcher
hook, kept as files under /run (gone after a reboot).

resume        the VPN was dropped by a network change; re-activate it when a
              network is back, and keep NetBird running meanwhile
keep-running  set by uninstall: don't stop NetBird while removing the profile
down-pending  a 'netbird down' is queued; a new 'up' must wait for it
lock          serializes 'netbird up' / 'netbird down'
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Callable


class StateDir:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _file(self, name: str) -> Path:
        return self.path / name

    def _touch(self, name: str, content: str = "") -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        self._file(name).write_text(content)

    def _remove(self, name: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._file(name).unlink()

    # resume -------------------------------------------------------------------
    def set_resume(self, uuid: str) -> None:
        self._touch("resume", uuid)

    def has_resume(self) -> bool:
        return self._file("resume").exists()

    def clear_resume(self) -> None:
        self._remove("resume")

    def pop_resume(self) -> str | None:
        try:
            uuid = self._file("resume").read_text().strip()
        except FileNotFoundError:
            return None
        self._remove("resume")
        return uuid or None

    # keep-running -------------------------------------------------------------
    def set_keep(self) -> None:
        self._touch("keep-running")

    def keep(self) -> bool:
        return self._file("keep-running").exists()

    # down-pending -------------------------------------------------------------
    def mark_down_pending(self) -> None:
        self._touch("down-pending")

    def down_pending(self) -> bool:
        return self._file("down-pending").exists()

    def clear_down_pending(self) -> None:
        self._remove("down-pending")

    def wait_down_done(self, timeout: float, sleep: Callable[[float], None] = time.sleep,
                       step: float = 0.25) -> bool:
        """Wait until no 'netbird down' is pending. False on timeout."""
        waited = 0.0
        while self.down_pending():
            if waited >= timeout:
                return False
            sleep(step)
            waited += step
        return True

    # lock ---------------------------------------------------------------------
    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        self.path.mkdir(parents=True, exist_ok=True)
        with open(self._file("lock"), "w") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)

    def wipe(self) -> None:
        for name in ("resume", "keep-running", "down-pending", "lock"):
            self._remove(name)
        with contextlib.suppress(OSError):
            os.rmdir(self.path)
