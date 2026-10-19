import subprocess

from netbird_nm.const import SERVICE
from netbird_nm.nmcli import Nmcli, split_terse


def test_split_terse_plain():
    assert split_terse("uuid-1:vpn:NetBird") == ["uuid-1", "vpn", "NetBird"]


def test_split_terse_unescapes_colons_and_backslashes():
    assert split_terse(r"u:vpn:office\:vpn") == ["u", "vpn", "office:vpn"]
    assert split_terse(r"u:vpn:back\\slash") == ["u", "vpn", r"back\slash"]


def test_split_terse_empty_fields():
    assert split_terse("::") == ["", "", ""]


def test_connections_parses_names_with_colons(runner):
    runner.on(["nmcli", "-t", "-f", "UUID,TYPE,NAME"],
              stdout="a:802-11-wireless:Home\nb:vpn:NetBird\nc:vpn:work\\:vpn\n")
    assert Nmcli(runner).connections() == [
        ("a", "802-11-wireless", "Home"), ("b", "vpn", "NetBird"), ("c", "vpn", "work:vpn")]


def test_vpn_connections_filters_by_service_type(runner):
    runner.on(["nmcli", "-t", "-f", "UUID,TYPE,NAME"],
              stdout="a:802-11-wireless:Home\nb:vpn:NetBird\nc:vpn:OpenVPN\n")
    runner.on(["nmcli", "-g", "vpn.service-type", "connection", "show", "b"], stdout=SERVICE + "\n")
    runner.on(["nmcli", "-g", "vpn.service-type", "connection", "show", "c"],
              stdout="org.freedesktop.NetworkManager.openvpn\n")
    assert Nmcli(runner).vpn_connections(SERVICE) == ["b"]


def test_inactive_connection_state_is_empty(runner):
    runner.on(["nmcli", "-g", "GENERAL.STATE"], stdout="")
    assert Nmcli(runner).connection_state("x") == ""


def test_failed_query_returns_empty(runner):
    runner.on(["nmcli", "-g"], stdout="garbage", returncode=10)
    assert Nmcli(runner).field("x", "connection.id") == ""


def test_device_state_without_device_does_not_call_nmcli(runner):
    assert Nmcli(runner).device_state("") == ""
    assert runner.calls == []


def test_running_handles_missing_binary():
    def boom(*a, **k):
        raise FileNotFoundError("nmcli")
    assert Nmcli(boom).running() is False


def test_running_handles_timeout():
    def slow(cmd, **k):
        raise subprocess.TimeoutExpired(cmd, 1)
    assert Nmcli(slow).running() is False


def test_add_vpn_command(runner):
    Nmcli(runner).add_vpn("NetBird", SERVICE, ["connection.autoconnect", "no"])
    assert runner.calls[-1] == ["nmcli", "connection", "add", "type", "vpn", "con-name", "NetBird",
                                "vpn-type", SERVICE, "connection.autoconnect", "no"]


def test_up_and_down_use_uuid(runner):
    nm = Nmcli(runner)
    nm.up("u1")
    nm.down("u1")
    assert runner.calls == [["nmcli", "connection", "up", "uuid", "u1"],
                            ["nmcli", "connection", "down", "uuid", "u1"]]
