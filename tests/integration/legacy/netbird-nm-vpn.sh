#!/usr/bin/env bash
# netbird-nm-vpn.sh — make NetBird show up as a VPN in NetworkManager / nm-applet.
#
# Installs a tiny NetworkManager VPN plugin whose "connect" runs `netbird up`
# and whose "disconnect" runs `netbird down`, plus a VPN connection profile.
#
#   sudo ./netbird-nm-vpn.sh install   [--name NetBird] [--iface wt0] [--no-deps]
#   sudo ./netbird-nm-vpn.sh uninstall
#        ./netbird-nm-vpn.sh status
set -euo pipefail

SERVICE="org.freedesktop.NetworkManager.netbird"
PLUGIN_BIN="/usr/local/libexec/nm-netbird-service"
NAME_FILE="/etc/NetworkManager/VPN/nm-netbird-service.name"
DBUS_CONF="/etc/dbus-1/system.d/nm-netbird-service.conf"
UNMANAGED_CONF="/etc/NetworkManager/conf.d/99-netbird-unmanaged.conf"
OLD_UNMANAGED_CONF="/etc/NetworkManager/conf.d/99-netbird.conf"     # from earlier manual setup
DISPATCHER="/etc/NetworkManager/dispatcher.d/90-netbird-vpn"
OLD_DISPATCHER="/etc/NetworkManager/dispatcher.d/90-netbird"        # from earlier manual setup
MARKER="# managed-by: netbird-nm-vpn.sh"

CON_NAME="NetBird"
IFACE=""
INSTALL_DEPS=1

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

need_root() {
    if [ "$(id -u)" -ne 0 ]; then
        command -v sudo >/dev/null || die "run this as root"
        exec sudo -- "$0" "$@"
    fi
}

# ---------------------------------------------------------------- helpers ---

find_python() {
    # Pick an interpreter that actually has the libnm GObject bindings.
    local py
    for py in /usr/bin/python3 /usr/bin/python3.* /usr/local/bin/python3; do
        [ -x "$py" ] || continue
        case "$py" in *-config) continue ;; esac
        if "$py" -c 'import gi; gi.require_version("NM","1.0"); from gi.repository import NM' 2>/dev/null; then
            echo "$py"; return 0
        fi
    done
    return 1
}

install_deps() {
    say "Installing Python libnm bindings"
    if   command -v apt-get >/dev/null; then DEBIAN_FRONTEND=noninteractive apt-get install -y python3-gi gir1.2-nm-1.0
    elif command -v dnf     >/dev/null; then dnf install -y python3-gobject NetworkManager-libnm
    elif command -v pacman  >/dev/null; then pacman -S --needed --noconfirm python-gobject libnm
    elif command -v zypper  >/dev/null; then zypper --non-interactive install python3-gobject typelib-1_0-NM-1_0
    elif command -v xbps-install >/dev/null; then xbps-install -y python3-gobject libnm
    else die "unknown package manager; install PyGObject + the NM typelib yourself and rerun with --no-deps"
    fi
}

detect_iface() {
    local cfg=/etc/netbird/config.json iface=""
    if [ -r "$cfg" ]; then
        iface=$(sed -n 's/.*"WgIface"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$cfg" | head -n1)
    fi
    echo "${iface:-wt0}"
}

reload_dbus() {
    if command -v dbus-send >/dev/null; then
        dbus-send --system --type=method_call --dest=org.freedesktop.DBus \
            /org/freedesktop/DBus org.freedesktop.DBus.ReloadConfig >/dev/null 2>&1 && return 0
    fi
    if command -v busctl >/dev/null; then
        busctl call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus ReloadConfig >/dev/null 2>&1 && return 0
    fi
    systemctl reload dbus >/dev/null 2>&1 || warn "could not reload D-Bus config; reboot if connecting fails"
}

reload_nm() {
    # NM re-reads conf.d on reload; VPN .name files are picked up by a file watch.
    nmcli general reload conf >/dev/null 2>&1 \
        || systemctl reload NetworkManager >/dev/null 2>&1 \
        || true
}

our_connections() {
    # Print UUIDs of every NM connection that uses our VPN service.
    local uuid type
    while IFS=: read -r uuid type; do
        [ "$type" = "vpn" ] || continue
        [ "$(nmcli -g vpn.service-type connection show "$uuid" 2>/dev/null)" = "$SERVICE" ] && echo "$uuid"
    done < <(nmcli -t -f UUID,TYPE connection show 2>/dev/null)
}

cleanup_legacy() {
    # Leftovers from the earlier "dummy connection + dispatcher" workaround.
    if [ -f "$OLD_DISPATCHER" ] && grep -q 'netbird' "$OLD_DISPATCHER"; then
        say "Removing old dispatcher script $OLD_DISPATCHER"
        rm -f "$OLD_DISPATCHER"
    fi
    local uuid type name
    while IFS=: read -r uuid type name; do
        if [ "$type" = "dummy" ] && [ "$name" = "$CON_NAME" ]; then
            say "Removing old dummy connection '$name'"
            nmcli connection delete "$uuid" >/dev/null
        fi
    done < <(nmcli -t -f UUID,TYPE,NAME connection show 2>/dev/null)
    if [ -f "$OLD_UNMANAGED_CONF" ] && grep -q 'wt0\|netbird' "$OLD_UNMANAGED_CONF"; then
        say "Replacing old $OLD_UNMANAGED_CONF"
        rm -f "$OLD_UNMANAGED_CONF"
    fi
}

# ---------------------------------------------------------------- install ---

write_plugin() {
    local py="$1" netbird="$2"
    mkdir -p "$(dirname "$PLUGIN_BIN")"
    cat > "$PLUGIN_BIN" <<EOF
#!$py
$MARKER
# NetworkManager VPN service plugin for NetBird.
import fcntl, os, socket, struct, subprocess, sys, syslog, threading, time
import gi
gi.require_version("NM", "1.0")
from gi.repository import NM, GLib

BUS_NAME    = "$SERVICE"
NETBIRD     = "$netbird"
IFACE       = "$IFACE"
UP_TIMEOUT  = 50                          # seconds; NM gives a VPN 60s by default
WATCH_SECS  = 3
STATE_DIR   = "/run/nm-netbird"
RESUME_FLAG = STATE_DIR + "/resume"       # written by the dispatcher hook
LOCK_FILE   = STATE_DIR + "/lock"         # serializes netbird up/down
DOWN_MARK   = STATE_DIR + "/down-pending" # an up must wait for this down
KEEP_FLAG   = STATE_DIR + "/keep-running" # set by uninstall

os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

syslog.openlog("nm-netbird", syslog.LOG_PID, syslog.LOG_DAEMON)

def log(*a):
    msg = " ".join(str(x) for x in a)
    syslog.syslog(syslog.LOG_INFO, msg)    # shows up in journalctl -t nm-netbird
    try:
        print("nm-netbird:", msg, file=sys.stderr, flush=True)
    except Exception:
        pass

def lock():
    os.makedirs(STATE_DIR, exist_ok=True)
    fd = open(LOCK_FILE, "w")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd

def iface_exists():
    return os.path.isdir("/sys/class/net/" + IFACE)

def iface_ipv4():
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", IFACE],
                             capture_output=True, text=True, timeout=5).stdout.split()
    except Exception:
        return None
    if "inet" not in out:
        return None
    addr, prefix = out[out.index("inet") + 1].split("/")
    return addr, int(prefix)

def run_down():
    # Runs as a detached helper: NM kills the plugin right after a disconnect,
    # and 'netbird down' has to finish regardless.
    with lock():
        # If NM dropped the VPN because the network went away (Wi-Fi switch,
        # cable unplugged), the dispatcher hook leaves a flag and re-activates
        # the VPN once a network is back; keep NetBird running meanwhile.
        for _ in range(8):
            if os.path.exists(RESUME_FLAG):
                log("network change; keeping NetBird running")
                return
            if os.path.exists(KEEP_FLAG):
                log("uninstalling; leaving NetBird as it is")
                return
            time.sleep(0.25)
        log("running netbird down")
        try:
            subprocess.run([NETBIRD, "down"], timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            log("netbird down failed:", e)

def run_down_and_clear():
    try:
        run_down()
    finally:
        try:
            os.unlink(DOWN_MARK)
        except FileNotFoundError:
            pass

class NetbirdPlugin(NM.VpnServicePlugin):
    def __init__(self, loop, **kw):
        super().__init__(**kw)
        self.loop = loop
        self.watch_id = 0
        self.missing = 0
        self.stopping = False

    # need_secrets is deliberately not overridden: libnm then answers
    # "no secrets needed" itself (overriding it trips a PyGObject bug).

    def do_connect(self, connection):
        self.stopping = False
        threading.Thread(target=self._bring_up, daemon=True).start()
        return True

    def do_disconnect(self):
        self.stopping = True
        self._stop_watch()
        # Mark the down as pending *before* returning, so a quick reconnect
        # can't overtake it, then run it in a helper that outlives us.
        os.makedirs(STATE_DIR, exist_ok=True)
        open(DOWN_MARK, "w").close()
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--down"],
                         start_new_session=True, stdin=subprocess.DEVNULL)
        # Leave the bus right away so NM starts a fresh plugin for the next
        # connect instead of handing it to this exiting one.
        GLib.idle_add(self._quit, priority=GLib.PRIORITY_LOW)
        return True

    def _quit(self):
        self.loop.quit()
        return False

    def _bring_up(self):
        for _ in range(160):              # wait (max 40s) for a pending down
            if not os.path.exists(DOWN_MARK):
                break
            time.sleep(0.25)
        with lock():
            log("running netbird up")
            try:
                r = subprocess.run([NETBIRD, "up"], timeout=UP_TIMEOUT,
                                   capture_output=True, text=True)
            except subprocess.TimeoutExpired:
                GLib.idle_add(self._fail, "netbird up timed out (not logged in? run 'netbird up' once in a terminal)")
                return
            except FileNotFoundError:
                GLib.idle_add(self._fail, "netbird not found at " + NETBIRD)
                return
        if r.returncode != 0:
            GLib.idle_add(self._fail, "netbird up failed: " + (r.stderr or r.stdout).strip())
            return
        for _ in range(40):
            ip = iface_ipv4()
            if ip:
                GLib.idle_add(self._report, *ip)
                return
            time.sleep(0.5)
        GLib.idle_add(self._fail, "interface %s never got an IPv4 address" % IFACE)

    def _fail(self, msg):
        log(msg)
        self.failure(NM.VpnPluginFailure.CONNECT_FAILED)   # libnm stops the plugin
        return False

    def _report(self, addr, prefix):
        if self.stopping:
            return False
        log("up: %s/%d on %s" % (addr, prefix, IFACE))
        a32 = struct.unpack("=I", socket.inet_aton(addr))[0]
        self.set_config(GLib.Variant("a{sv}", {
            # NM insists on a "VPN gateway"; NetBird is a mesh with no single
            # gateway, so hand it our own tunnel address. NM adds a host route
            # for it, which is harmless: the kernel's local table wins.
            "gateway": GLib.Variant("u", a32),
            "tundev":  GLib.Variant("s", IFACE),
            "has-ip4": GLib.Variant("b", True),
            "has-ip6": GLib.Variant("b", False),
        }))
        self.set_ip4_config(GLib.Variant("a{sv}", {
            "address":       GLib.Variant("u", a32),
            "prefix":        GLib.Variant("u", prefix),
            "never-default": GLib.Variant("b", True),
        }))
        self.missing = 0
        self.watch_id = GLib.timeout_add_seconds(WATCH_SECS, self._watch)
        return False

    def _watch(self):
        # NetBird was taken down outside NM (CLI, tray app): mirror it in NM.
        if iface_exists():
            self.missing = 0
            return True
        self.missing += 1
        if self.missing < 2:
            return True
        log("%s disappeared; marking VPN as disconnected" % IFACE)
        self.watch_id = 0
        try:
            NM.VpnServicePlugin.disconnect(self)
        except GLib.Error as e:
            log("disconnect:", e.message)
        return False

    def _stop_watch(self):
        if self.watch_id:
            GLib.source_remove(self.watch_id)
            self.watch_id = 0

def main():
    if sys.argv[1:] == ["--down"]:
        run_down_and_clear()
        return
    loop = GLib.MainLoop()
    plugin = NetbirdPlugin(loop, service_name=BUS_NAME, watch_peer=True)
    plugin.init(None)
    plugin.connect("quit", lambda *_: loop.quit())
    loop.run()

if __name__ == "__main__":
    main()
EOF
    chmod 755 "$PLUGIN_BIN"
}

write_name_file() {
    mkdir -p "$(dirname "$NAME_FILE")"
    cat > "$NAME_FILE" <<EOF
$MARKER
[VPN Connection]
name=netbird
service=$SERVICE
program=$PLUGIN_BIN
supports-multiple-connections=false

[libnm]

[GNOME]
supports-external-ui-mode=false
EOF
    chmod 644 "$NAME_FILE"
}

write_dbus_conf() {
    mkdir -p "$(dirname "$DBUS_CONF")"
    cat > "$DBUS_CONF" <<EOF
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<!-- managed-by: netbird-nm-vpn.sh -->
<busconfig>
  <policy user="root">
    <allow own_prefix="$SERVICE"/>
    <allow send_destination="$SERVICE"/>
  </policy>
  <policy context="default">
    <deny own_prefix="$SERVICE"/>
    <deny send_destination="$SERVICE"/>
  </policy>
</busconfig>
EOF
    chmod 644 "$DBUS_CONF"
}

write_dispatcher() {
    mkdir -p "$(dirname "$DISPATCHER")"
    cat > "$DISPATCHER" <<'EOF'
#!/bin/sh
# managed-by: netbird-nm-vpn.sh
# Brings the NetBird VPN back after a network change (NM drops VPNs when their
# underlying connection goes away), but not after you switched it off yourself.
SERVICE=org.freedesktop.NetworkManager.netbird
DIR=/run/nm-netbird
log() { logger -t nm-netbird "dispatcher: $*" 2>/dev/null || true; }
ours() {
    [ -n "$CONNECTION_UUID" ] &&
    [ "$(nmcli -g vpn.service-type connection show "$CONNECTION_UUID" 2>/dev/null)" = "$SERVICE" ]
}
case "$2" in
    vpn-down)
        ours || exit 0
        st=""
        [ -n "$DEVICE_IFACE" ] && st=$(nmcli -g GENERAL.STATE device show "$DEVICE_IFACE" 2>/dev/null)
        case "$st" in
            100*) rm -f "$DIR/resume" ;;                                    # switched off on purpose
            *)    mkdir -p "$DIR" && echo "$CONNECTION_UUID" > "$DIR/resume"     # network went away
                  log "VPN dropped by network change; will resume" ;;
        esac ;;
    vpn-up)
        ours && rm -f "$DIR/resume" ;;
    up)
        if [ -f "$DIR/resume" ]; then
            uuid=$(cat "$DIR/resume"); rm -f "$DIR/resume"
            log "network back ($CONNECTION_ID); re-activating VPN"
            ( sleep 2; nmcli connection up uuid "$uuid" ) </dev/null >/dev/null 2>&1 &
            exit 0
        fi
        # NetBird already running (e.g. its service auto-connected at boot) but
        # NM shows the VPN off: adopt it so the applet reflects reality.
        (
            sleep 8
            uuid=""
            for u in $(nmcli -g UUID,TYPE connection show | sed -n 's/:vpn$//p'); do
                [ "$(nmcli -g vpn.service-type connection show "$u")" = "$SERVICE" ] && uuid=$u && break
            done
            [ -n "$uuid" ] || exit 0
            # Must look "running but shown off" twice, 3s apart, with no
            # disconnect in progress, so we never fight a toggle in flight.
            for check in 1 2; do
                [ -d "/sys/class/net/$IFACE" ] || exit 0
                [ -e "$DIR/down-pending" ] && exit 0
                [ -n "$(nmcli -g GENERAL.STATE connection show "$uuid" 2>/dev/null)" ] && exit 0
                [ "$check" = 1 ] && sleep 3
            done
            log "NetBird already running; showing it as connected in NM"
            nmcli connection up uuid "$uuid"
        ) </dev/null >/dev/null 2>&1 &
        ;;
esac
exit 0
EOF
    sed -i "s|^DIR=/run/nm-netbird\$|DIR=/run/nm-netbird\nIFACE=$IFACE|" "$DISPATCHER"
    chmod 755 "$DISPATCHER"
}

write_unmanaged_conf() {
    cat > "$UNMANAGED_CONF" <<EOF
$MARKER
# Keep NM from treating NetBird's interface as a separate "external" connection
# (that's what breaks reconnecting from the applet).
[keyfile]
unmanaged-devices=interface-name:$IFACE
EOF
    chmod 644 "$UNMANAGED_CONF"
}

do_install() {
    command -v nmcli >/dev/null || die "NetworkManager (nmcli) not found"
    nmcli -t general status >/dev/null 2>&1 || die "NetworkManager is not running"

    local netbird
    netbird=$(command -v netbird || true)
    [ -n "$netbird" ] || { [ -x /usr/bin/netbird ] && netbird=/usr/bin/netbird; }
    [ -n "$netbird" ] || die "netbird not found in PATH; install NetBird first"

    local py
    if ! py=$(find_python); then
        [ "$INSTALL_DEPS" -eq 1 ] || die "no Python with libnm bindings found (drop --no-deps to install them)"
        install_deps
        py=$(find_python) || die "libnm Python bindings still not importable after install"
    fi

    [ -n "$IFACE" ] || IFACE=$(detect_iface)
    say "Using netbird=$netbird, interface=$IFACE, python=$py"

    cleanup_legacy

    say "Writing plugin files"
    write_plugin "$py" "$netbird"
    write_name_file
    write_dbus_conf
    write_dispatcher
    write_unmanaged_conf

    reload_dbus
    reload_nm
    sleep 1

    local existing; existing=$(our_connections)
    if [ -n "$existing" ]; then
        say "VPN connection already exists, keeping it"
        # persistent=yes would make NM restart NetBird after you stop it yourself
        for u in $existing; do nmcli connection modify "$u" vpn.persistent no >/dev/null; done
    else
        say "Creating VPN connection '$CON_NAME'"
        nmcli connection add type vpn con-name "$CON_NAME" \
            vpn-type "$SERVICE" vpn.data "iface=$IFACE" \
            connection.autoconnect no vpn.persistent no \
            ipv4.never-default yes ipv6.method ignore >/dev/null
    fi

    say "Done. '$CON_NAME' is now under VPN connections in nm-applet."
    echo "    Test from a terminal:  nmcli connection up '$CON_NAME'  /  nmcli connection down '$CON_NAME'"
    echo "    Logs:                  journalctl -t nm-netbird -f"
}

# -------------------------------------------------------------- uninstall ---

do_uninstall() {
    local uuid removed=0
    if command -v nmcli >/dev/null && nmcli -t general status >/dev/null 2>&1; then
        mkdir -p /run/nm-netbird && touch /run/nm-netbird/keep-running   # don't cut NetBird
        for uuid in $(our_connections); do
            say "Removing connection $(nmcli -g connection.id connection show "$uuid")"
            nmcli connection down "$uuid" >/dev/null 2>&1 || true
            nmcli connection delete "$uuid" >/dev/null
            removed=1
        done
    else
        warn "NetworkManager not running; connection profiles were not removed"
    fi
    [ "$removed" -eq 1 ] || say "No NetBird VPN connections found"

    # Let an in-flight 'netbird down' finish before removing the plugin.
    local i
    for i in $(seq 1 40); do
        pgrep -f "$PLUGIN_BIN" >/dev/null 2>&1 || break
        sleep 0.5
    done
    pkill -f "$PLUGIN_BIN" 2>/dev/null || true

    local f
    for f in "$PLUGIN_BIN" "$NAME_FILE" "$DBUS_CONF" "$DISPATCHER" "$UNMANAGED_CONF"; do
        if [ -e "$f" ]; then
            if grep -q 'managed-by: netbird-nm-vpn.sh' "$f"; then
                say "Removing $f"; rm -f "$f"
            else
                warn "$f wasn't created by this script; leaving it"
            fi
        fi
    done
    rm -rf /run/nm-netbird
    reload_dbus
    reload_nm
    say "Uninstalled. NetBird itself is untouched (it's still connected if it was)."
}

# ----------------------------------------------------------------- status ---

do_status() {
    local f
    for f in "$PLUGIN_BIN" "$NAME_FILE" "$DBUS_CONF" "$DISPATCHER" "$UNMANAGED_CONF"; do
        [ -e "$f" ] && echo "present  $f" || echo "missing  $f"
    done
    local uuids; uuids=$(our_connections || true)
    if [ -n "$uuids" ]; then
        for u in $uuids; do
            local st; st=$(nmcli -g GENERAL.STATE connection show "$u" 2>/dev/null || true)
            printf 'connection  %s  (%s)\n' "$(nmcli -g connection.id connection show "$u")" "${st:-inactive}"
        done
    else
        echo "connection  none"
    fi
}

# ------------------------------------------------------------------- main ---

cmd="${1:-}"; shift || true
while [ $# -gt 0 ]; do
    case "$1" in
        --name)    CON_NAME="${2:?}"; shift 2 ;;
        --iface)   IFACE="${2:?}"; shift 2 ;;
        --no-deps) INSTALL_DEPS=0; shift ;;
        -h|--help) cmd=help; shift ;;
        *) die "unknown option: $1" ;;
    esac
done

case "$cmd" in
    install)   need_root install ${CON_NAME:+--name "$CON_NAME"} ${IFACE:+--iface "$IFACE"} $([ "$INSTALL_DEPS" -eq 0 ] && echo --no-deps); do_install ;;
    uninstall) need_root uninstall; do_uninstall ;;
    status)    do_status ;;
    *) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
