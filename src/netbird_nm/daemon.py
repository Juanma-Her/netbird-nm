"""A small client for NetBird's daemon gRPC API (client/proto/daemon.proto).

The plugin runs this module as a separate process, as the user it acts for
(``python -c ... netbird_nm.daemon up``), so the daemon sees the same caller
as it would for ``netbird up`` typed by that user. It replaces running the
netbird CLI and parsing its output.

Only the few messages needed are encoded/decoded, by hand, so the only
dependency is grpcio (no generated code, no protobuf version coupling).
Field numbers are from NetBird v0.81's daemon.proto.

    up        Status → GetActiveProfile → Login → Up, like 'netbird up'
    down      Down
    status    one JSON line: status, management_url, ip, fqdn, profile, username
"""

from __future__ import annotations

import json
import os
import pwd
import sys
from dataclasses import dataclass

DEFAULT_ADDR = "unix:///var/run/netbird.sock"
SERVICE = "/daemon.DaemonService/"


# -- protobuf wire format (just what we need) ------------------------------------------------

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def encode(fields: list[tuple[int, object]]) -> bytes:
    """Encode (field number, value) pairs; str/bytes, bool and int supported.
    None values are skipped (unset optional fields)."""
    out = bytearray()
    for num, value in fields:
        if value is None:
            continue
        if isinstance(value, (bool, int)):
            out += _varint(num << 3 | 0) + _varint(int(value))
        else:
            data = value.encode() if isinstance(value, str) else bytes(value)
            out += _varint(num << 3 | 2) + _varint(len(data)) + data
    return bytes(out)


def decode(data: bytes) -> dict[int, list]:
    """field number → list of raw values (int for varints, bytes otherwise)."""
    fields: dict[int, list] = {}
    i = 0

    def varint() -> int:
        nonlocal i
        shift = result = 0
        while True:
            b = data[i]
            i += 1
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                return result
            shift += 7

    while i < len(data):
        key = varint()
        num, wire = key >> 3, key & 7
        if wire == 0:
            value: object = varint()
        elif wire == 2:
            n = varint()
            value = data[i:i + n]
            i += n
        elif wire == 1:
            value = data[i:i + 8]
            i += 8
        elif wire == 5:
            value = data[i:i + 4]
            i += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        fields.setdefault(num, []).append(value)
    return fields


def _str(fields: dict, num: int) -> str:
    v = fields.get(num)
    return v[-1].decode(errors="replace") if v else ""


def _bool(fields: dict, num: int) -> bool:
    v = fields.get(num)
    return bool(v[-1]) if v else False


def _msg(fields: dict, num: int) -> dict:
    v = fields.get(num)
    return decode(v[-1]) if v else {}


# -- messages ----------------------------------------------------------------------------

@dataclass
class Status:
    status: str            # "Idle", "Connecting", "Connected", "NeedsLogin", ...
    management_url: str
    management_connected: bool
    ip: str                # NetBird address, e.g. "100.101.161.35/16"
    fqdn: str

    @classmethod
    def parse(cls, data: bytes) -> Status:
        f = decode(data)
        full = _msg(f, 2)                 # StatusResponse.fullStatus
        mgmt = _msg(full, 1)              # FullStatus.managementState
        local = _msg(full, 3)             # FullStatus.localPeerState
        return cls(status=_str(f, 1), management_url=_str(mgmt, 1),
                   management_connected=_bool(mgmt, 2), ip=_str(local, 1), fqdn=_str(local, 4))


@dataclass
class ActiveProfile:
    name: str
    username: str
    id: str

    @classmethod
    def parse(cls, data: bytes) -> ActiveProfile:
        f = decode(data)
        return cls(name=_str(f, 1), username=_str(f, 2), id=_str(f, 3))


class DaemonError(Exception):
    pass


class Daemon:
    def __init__(self, addr: str = DEFAULT_ADDR, timeout: float = 15):
        import grpc  # imported lazily: optional dependency
        self.grpc = grpc
        self.timeout = timeout
        self.channel = grpc.insecure_channel(addr)

    def _call(self, method: str, request: bytes, timeout: float | None = None) -> bytes:
        fn = self.channel.unary_unary(SERVICE + method, request_serializer=lambda b: b,
                                      response_deserializer=lambda b: b)
        try:
            return fn(request, timeout=timeout or self.timeout)
        except self.grpc.RpcError as e:
            detail = e.details() if hasattr(e, "details") else str(e)
            if e.code() == self.grpc.StatusCode.UNAVAILABLE:
                raise DaemonError(f"can't reach the NetBird daemon ({detail}); "
                                  "is the netbird service running?") from None
            raise DaemonError(f"{method}: {detail}") from None

    def status(self, wait_for_ready: bool = False) -> Status:
        return Status.parse(self._call("Status", encode([(3, True if wait_for_ready else None)])))

    def active_profile(self) -> ActiveProfile:
        return ActiveProfile.parse(self._call("GetActiveProfile", b""))

    def login(self, profile: str, username: str) -> bool:
        """True if NetBird needs an interactive (SSO) login first."""
        resp = decode(self._call("Login", encode([(30, profile), (31, username)]), timeout=60))
        return _bool(resp, 1)

    def up(self, profile: str, username: str, timeout: float = 50) -> None:
        self._call("Up", encode([(1, profile), (2, username)]), timeout=timeout)

    def down(self) -> None:
        self._call("Down", b"")

    def management_url(self, profile: str, username: str) -> str:
        return _str(decode(self._call("GetConfig", encode([(1, profile), (2, username)]))), 1)


def bring_up(d: Daemon, username: str) -> str:
    """What 'netbird up' does for a logged-in user. Returns the message to print."""
    st = d.status(wait_for_ready=True)
    if st.status == "Connected":
        return "Already connected"
    prof = d.active_profile()
    if prof.username and prof.username != username:
        raise DaemonError(f"NetBird's active profile {prof.name!r} belongs to {prof.username!r}, "
                          f"not {username!r}; switch it with 'netbird profile select' as {username}")
    profile = prof.id or prof.name or "default"
    if d.login(profile, username):
        raise DaemonError(f"NetBird needs {username} to log in first: run 'netbird up' in a terminal")
    d.up(profile, username)
    return "Connected"


def available() -> bool:
    try:
        import grpc  # noqa: F401
    except ImportError:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    addr = DEFAULT_ADDR
    if argv[:1] == ["--addr"]:
        addr, argv = argv[1], argv[2:]
    cmd = argv[0] if argv else ""
    username = pwd.getpwuid(os.getuid()).pw_name
    try:
        d = Daemon(addr)
        if cmd == "up":
            print(bring_up(d, username))
        elif cmd == "down":
            d.down()
            print("Disconnected")
        elif cmd == "status":
            st = d.status()
            info = {"status": st.status, "management_url": st.management_url,
                    "management_connected": st.management_connected, "ip": st.ip, "fqdn": st.fqdn}
            try:
                prof = d.active_profile()
                info.update(profile=prof.name, username=prof.username)
                if not info["management_url"]:
                    info["management_url"] = d.management_url(prof.id or prof.name, username)
            except DaemonError:
                pass
            print(json.dumps(info))
        else:
            print(f"usage: {sys.argv[0]} [--addr ADDR] up|down|status", file=sys.stderr)
            return 2
    except DaemonError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
