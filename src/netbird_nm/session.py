"""Who is using the computer right now, from systemd-logind's state files.

NetworkManager doesn't tell VPN plugins which user asked for a connection,
so the plugin uses the next best thing: the user of the active session on
the main seat (the person at the desktop clicking the toggle). This reads the
same files as sd_seat_get_active() / sd_uid_get_state() in libsystemd:

  /run/systemd/seats/seat0   ACTIVE_UID=<uid of the foreground session>
  /run/systemd/users/<uid>   STATE=active|online|lingering|...

Without a seat (SSH, headless), a single user with an active session counts.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

LOGIND_RUN = "/run/systemd"


def read_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return data
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    return data


def _name(uid: int) -> str | None:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return None


def _uid_min(login_defs: str = "/etc/login.defs") -> int:
    try:
        for line in Path(login_defs).read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "UID_MIN":
                return int(parts[1])
    except (OSError, ValueError):
        pass
    return 1000


def session_user(run_dir: str | Path = LOGIND_RUN, uid_min: int | None = None) -> str | None:
    """The logged-in user to act for, or None (nobody / can't tell)."""
    run = Path(run_dir)
    uid_min = _uid_min() if uid_min is None else uid_min

    active = read_env_file(run / "seats" / "seat0").get("ACTIVE_UID", "")
    if active.isdigit() and int(active) != 0:
        return _name(int(active))

    users = run / "users"
    try:
        entries = os.listdir(users)
    except OSError:
        return None
    candidates = [int(e) for e in entries if e.isdigit() and int(e) >= uid_min
                  and read_env_file(users / e).get("STATE") == "active"]
    if len(candidates) == 1:
        return _name(candidates[0])
    return None
