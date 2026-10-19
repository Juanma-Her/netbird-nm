import json

from netbird_nm.config import Config, detect_iface
from netbird_nm.const import MARKER
from netbird_nm.netbird import NetBird, iface_exists, iface_ipv4, parse_ip_addr

IP_OUTPUT = "11: wt0    inet 100.92.1.5/16 scope global wt0\\       valid_lft forever preferred_lft forever\n"


def test_parse_ip_addr():
    assert parse_ip_addr(IP_OUTPUT) == ("100.92.1.5", 16)


def test_parse_ip_addr_no_address():
    assert parse_ip_addr("") is None
    assert parse_ip_addr("11: wt0 inet6 fe80::1/64 scope link") is None


def test_iface_ipv4_uses_ip_command(runner):
    runner.on(["ip", "-4", "-o", "addr", "show", "dev", "wt0"], stdout=IP_OUTPUT)
    assert iface_ipv4("wt0", runner) == ("100.92.1.5", 16)


def test_iface_ipv4_missing_device(runner):
    runner.on(["ip"], returncode=1, stderr='Device "wt0" does not exist.')
    assert iface_ipv4("wt0", runner) is None


def test_iface_exists(tmp_path):
    (tmp_path / "wt0").mkdir()
    assert iface_exists("wt0", str(tmp_path))
    assert not iface_exists("wt1", str(tmp_path))


def test_netbird_commands(runner):
    nb = NetBird("/opt/netbird", runner)
    nb.up(timeout=5)
    nb.down()
    assert runner.calls == [["/opt/netbird", "up"], ["/opt/netbird", "down"]]


def test_config_roundtrip_and_marker(tmp_path):
    cfg = Config(netbird="/x/netbird", iface="nb0")
    path = tmp_path / "c.json"
    path.write_text(cfg.dumps())
    assert MARKER in path.read_text()
    assert Config.load(path) == cfg


def test_config_missing_file_gives_defaults(tmp_path):
    assert Config.load(tmp_path / "nope.json") == Config()


def test_config_ignores_unknown_keys(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"iface": "nb1", "future_option": 1}))
    assert Config.load(path).iface == "nb1"


def test_detect_iface(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"WgIface": "nb7", "WgPort": 51820}))
    assert detect_iface(cfg) == "nb7"


def test_detect_iface_fallbacks(tmp_path):
    assert detect_iface(tmp_path / "missing.json") == "wt0"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert detect_iface(bad) == "wt0"
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"WgIface": ""}))
    assert detect_iface(empty) == "wt0"
