from __future__ import annotations

import subprocess
from collections.abc import Callable

import pytest


class FakeRunner:
    """Stands in for subprocess.run. Rules map a command prefix to a result
    (CompletedProcess fields or a callable), and every call is recorded."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.rules: list[tuple[list[str], Callable | tuple]] = []

    def on(self, prefix: list[str], stdout: str = "", returncode: int = 0, stderr: str = "",
           action: Callable | None = None) -> FakeRunner:
        self.rules.insert(0, (prefix, action or (stdout, returncode, stderr)))
        return self

    def __call__(self, cmd, **kw):
        cmd = list(cmd)
        self.calls.append(cmd)
        for prefix, result in self.rules:
            if cmd[:len(prefix)] == prefix:
                if callable(result):
                    result = result(cmd, **kw)
                    if isinstance(result, subprocess.CompletedProcess):
                        return result
                stdout, rc, stderr = result
                return subprocess.CompletedProcess(cmd, rc, stdout, stderr)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def ran(self, *prefix: str) -> bool:
        return any(c[:len(prefix)] == list(prefix) for c in self.calls)


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


class Clock:
    """A fake sleep that just records how long we 'slept'."""

    def __init__(self, on_sleep: Callable[[float], None] | None = None):
        self.slept = 0.0
        self.on_sleep = on_sleep

    def __call__(self, seconds: float) -> None:
        self.slept += seconds
        if self.on_sleep:
            self.on_sleep(self.slept)


@pytest.fixture
def clock() -> Clock:
    return Clock()
