from pathlib import Path

import pytest

from netbird_nm.daemon import ActiveProfile, DaemonError, Status, bring_up, decode, encode

REPO = Path(__file__).resolve().parents[2]


# -- protobuf codec ---------------------------------------------------------------------------

def test_roundtrip():
    data = encode([(1, "Connected"), (3, True), (30, "default"), (31, "juan"), (5, 300), (7, b"\x00\x01")])
    f = decode(data)
    assert f[1] == [b"Connected"] and f[3] == [1] and f[30] == [b"default"]
    assert f[31] == [b"juan"] and f[5] == [300] and f[7] == [b"\x00\x01"]


def test_unset_optional_fields_are_omitted():
    assert encode([(1, None), (2, "x")]) == encode([(2, "x")])
    assert encode([]) == b""


def test_known_bytes():
    # protoc's encoding of UpRequest{profileName: "default", username: "juan"}
    assert encode([(1, "default"), (2, "juan")]) == b"\n\x07default\x12\x04juan"
    # StatusRequest{waitForReady: true}
    assert encode([(3, True)]) == b"\x18\x01"


def test_decode_skips_unknown_wire_types():
    data = encode([(1, "a")]) + bytes([0x11]) + b"\x00" * 8 + bytes([0x1D]) + b"\x00" * 4
    assert decode(data)[1] == [b"a"]


def test_status_parse():
    mgmt = encode([(1, "https://api.netbird.io:443"), (2, True)])
    local = encode([(1, "100.101.161.35/16"), (4, "desktop.netbird.cloud")])
    st = Status.parse(encode([(1, "Connected"), (2, encode([(1, mgmt), (3, local)])), (3, "0.81.2")]))
    assert st == Status("Connected", "https://api.netbird.io:443", True, "100.101.161.35/16",
                        "desktop.netbird.cloud")


def test_status_parse_empty():
    assert Status.parse(b"") == Status("", "", False, "", "")


def test_active_profile_parse():
    assert ActiveProfile.parse(encode([(1, "default"), (2, "juan"), (3, "abc")])) == \
        ActiveProfile("default", "juan", "abc")


# -- the 'netbird up' sequence -------------------------------------------------------------

class FakeDaemon:
    def __init__(self, status="Idle", owner="", needs_sso=False):
        self.st, self.owner, self.needs_sso, self.calls = status, owner, needs_sso, []

    def status(self, wait_for_ready=False):
        self.calls.append(("status", wait_for_ready))
        return Status(self.st, "", False, "", "")

    def active_profile(self):
        self.calls.append(("active_profile",))
        return ActiveProfile("default", self.owner, "id-1")

    def login(self, profile, username):
        self.calls.append(("login", profile, username))
        return self.needs_sso

    def up(self, profile, username):
        self.calls.append(("up", profile, username))


def test_bring_up_sequence():
    d = FakeDaemon()
    assert bring_up(d, "juan") == "Connected"
    assert d.calls == [("status", True), ("active_profile",), ("login", "id-1", "juan"),
                       ("up", "id-1", "juan")]


def test_bring_up_already_connected():
    d = FakeDaemon(status="Connected")
    assert bring_up(d, "juan") == "Already connected"
    assert ("up", "id-1", "juan") not in d.calls


def test_bring_up_needs_login():
    with pytest.raises(DaemonError, match="needs juan to log in"):
        bring_up(FakeDaemon(needs_sso=True), "juan")


def test_bring_up_refuses_another_users_profile():
    with pytest.raises(DaemonError, match="belongs to 'maria'"):
        bring_up(FakeDaemon(owner="maria"), "juan")


def test_bring_up_own_profile_ok():
    assert bring_up(FakeDaemon(owner="juan"), "juan") == "Connected"
