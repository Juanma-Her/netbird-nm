import pwd

from netbird_nm.session import read_env_file, session_user

NOBODY = pwd.getpwnam("nobody")
DAEMON = pwd.getpwnam("daemon")


def logind(tmp_path, seat=None, users=None):
    """Lay out /run/systemd like systemd-logind does."""
    run = tmp_path / "systemd"
    (run / "seats").mkdir(parents=True)
    (run / "users").mkdir()
    if seat is not None:
        (run / "seats" / "seat0").write_text(seat)
    for uid, state in (users or {}).items():
        (run / "users" / str(uid)).write_text(
            f"# This is private data. Do not parse.\nNAME=x\nSTATE={state}\n")
    return run


def test_active_seat_user(tmp_path):
    run = logind(tmp_path, seat=f"IS_SEAT0=1\nACTIVE=c2\nACTIVE_UID={NOBODY.pw_uid}\n")
    assert session_user(run) == "nobody"


def test_seat_wins_over_other_logged_in_users(tmp_path):
    run = logind(tmp_path, seat=f"ACTIVE_UID={NOBODY.pw_uid}\n",
                 users={DAEMON.pw_uid: "active", NOBODY.pw_uid: "active"})
    assert session_user(run, uid_min=0) == "nobody"


def test_greeter_or_root_on_seat_is_ignored(tmp_path):
    run = logind(tmp_path, seat="ACTIVE_UID=0\n")
    assert session_user(run) is None


def test_no_seat_single_active_user(tmp_path):
    """Headless / SSH: one user with an active session."""
    run = logind(tmp_path, users={NOBODY.pw_uid: "active", DAEMON.pw_uid: "lingering"})
    assert session_user(run, uid_min=1) == "nobody"


def test_no_seat_ambiguous(tmp_path):
    run = logind(tmp_path, users={NOBODY.pw_uid: "active", DAEMON.pw_uid: "active"})
    assert session_user(run, uid_min=1) is None


def test_system_users_are_skipped(tmp_path):
    run = logind(tmp_path, users={DAEMON.pw_uid: "active"})      # uid 1, below UID_MIN
    assert session_user(run, uid_min=1000) is None


def test_nobody_logged_in(tmp_path):
    assert session_user(logind(tmp_path)) is None
    assert session_user(tmp_path / "no-logind") is None


def test_unknown_uid(tmp_path):
    run = logind(tmp_path, seat="ACTIVE_UID=4123456789\n")
    assert session_user(run) is None


def test_read_env_file(tmp_path):
    f = tmp_path / "f"
    f.write_text("# comment\nA=1\nB = two=2\n\nbroken\n")
    assert read_env_file(f) == {"A": "1", "B": "two=2"}
    assert read_env_file(tmp_path / "missing") == {}
