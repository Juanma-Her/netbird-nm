import os
import pwd
import shutil
import tempfile

import pytest

from netbird_nm.netbird import NetBird, RunAs, summarize

NOBODY = pwd.getpwnam("nobody")


def test_lookup():
    ra = RunAs.lookup("nobody")
    assert (ra.uid, ra.gid, ra.home) == (NOBODY.pw_uid, NOBODY.pw_gid, NOBODY.pw_dir)
    assert NOBODY.pw_gid in ra.groups


def test_lookup_unknown_user():
    with pytest.raises(KeyError):
        RunAs.lookup("no-such-user-xyz")


def test_env_is_clean(monkeypatch):
    monkeypatch.setenv("SUDO_USER", "someone")
    monkeypatch.setenv("SECRET_TOKEN", "x")
    env = RunAs.lookup("nobody").env()
    assert env["HOME"] == NOBODY.pw_dir and env["USER"] == "nobody"
    assert not any(k.startswith("SUDO_") for k in env)
    assert "SECRET_TOKEN" not in env


def test_netbird_passes_identity_to_subprocess(runner):
    seen = {}
    runner.on(["/bin/netbird", "up"], action=lambda cmd, **kw: seen.update(kw) or ("", 0, ""))
    NetBird("/bin/netbird", runner, RunAs.lookup("nobody")).up(timeout=5)
    assert seen["user"] == NOBODY.pw_uid and seen["group"] == NOBODY.pw_gid
    assert seen["env"]["USER"] == "nobody" and seen["cwd"] == "/"


def test_netbird_as_root_passes_no_identity(runner):
    seen = {}
    runner.on(["/bin/netbird"], action=lambda cmd, **kw: seen.update(kw) or ("", 0, ""))
    NetBird("/bin/netbird", runner).down()
    assert not {"user", "group", "env"} & seen.keys()


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to switch users")
def test_really_drops_privileges():
    # pytest's tmp_path is private to root, so the probe goes somewhere public
    d = tempfile.mkdtemp(prefix="netbird-nm-test-")
    try:
        os.chmod(d, 0o755)
        probe = os.path.join(d, "probe")
        with open(probe, "w") as f:
            f.write('#!/bin/sh\necho "$(id -u) $(id -g) $HOME $1"\n')
        os.chmod(probe, 0o755)
        out = NetBird(probe, run_as=RunAs.lookup("nobody")).status().stdout.split()
    finally:
        shutil.rmtree(d)
    assert out == [str(NOBODY.pw_uid), str(NOBODY.pw_gid), NOBODY.pw_dir, "status"]


def test_summarize():
    assert summarize("a\n\n b \nc\n") == "a | b | c"
    long = "\n".join(str(i) for i in range(20))
    assert summarize(long, max_lines=3) == "0 | 1 | 2 | ... (17 more lines)"


def test_no_switch_when_already_that_user(monkeypatch):
    """'netbird-nm doctor' run by the user itself, without sudo (was EPERM)."""
    me = RunAs.lookup(pwd.getpwuid(os.geteuid()).pw_name)
    kw = me.subprocess_kwargs()
    assert not {"user", "group", "extra_groups"} & kw.keys()
    assert kw["env"]["USER"] == me.name


def test_switch_when_different_user(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    kw = RunAs.lookup("nobody").subprocess_kwargs()
    assert kw["user"] == NOBODY.pw_uid and kw["group"] == NOBODY.pw_gid
