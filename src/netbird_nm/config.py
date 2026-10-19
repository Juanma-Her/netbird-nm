"""Runtime configuration, written by the installer and read by the plugin and
the dispatcher hook."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .const import CONFIG_PATH, DEFAULT_IFACE, MARKER, STATE_DIR


@dataclass
class Config:
    netbird: str = "/usr/bin/netbird"   # absolute path; NM runs us with a minimal PATH
    # Who runs the netbird CLI: "session" = the user of the active desktop
    # session at each connect (root if nobody is logged in), "root", or a
    # fixed user name. (0.2.1's "user" key is ignored, so it upgrades to "session".)
    run_as: str = "session"
    iface: str = DEFAULT_IFACE          # NetBird's WireGuard interface
    state_dir: str = STATE_DIR
    connect_timeout: float = 55.0       # 'netbird up' + the address showing up; NM gives up at 60s
    up_timeout: float = 50.0            # the 'netbird up' command alone
    down_grace: float = 2.0             # how long a disconnect waits for a "network change" flag
    watch_interval: int = 3             # seconds between checks that NetBird is still up
    adopt_delay: float = 8.0            # after a network comes up, before adopting a running NetBird
    resume_delay: float = 2.0           # after a network comes back, before re-activating the VPN

    @classmethod
    def load(cls, path: str | Path = CONFIG_PATH) -> Config:
        try:
            data = json.loads(Path(path).read_text())
        except FileNotFoundError:
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def dumps(self) -> str:
        return json.dumps({"_comment": MARKER, **asdict(self)}, indent=2) + "\n"


def detect_iface(netbird_config: str | Path = "/etc/netbird/config.json") -> str:
    """NetBird's interface name from its own config, falling back to wt0."""
    try:
        data = json.loads(Path(netbird_config).read_text())
    except (OSError, ValueError):
        return DEFAULT_IFACE
    iface = data.get("WgIface") if isinstance(data, dict) else None
    return iface if isinstance(iface, str) and iface else DEFAULT_IFACE
