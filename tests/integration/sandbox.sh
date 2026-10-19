#!/bin/sh
# A throwaway NetworkManager for the integration tests.
#
# Run this only in a disposable root container or VM, never on your desktop:
# it starts its own system D-Bus and NetworkManager. Existing interfaces are
# marked unmanaged so the container's own networking is left alone, and a TUN
# device stands in for Wi-Fi/ethernet as the connection the VPN rides on.
# NetworkManager runs with the same sandboxing as its systemd unit (read-only
# home and /usr, reduced capabilities); NM_SANDBOX_HARDEN=0 turns that off.
#
#   sandbox.sh deps          install packages (Debian/Ubuntu)
#   sandbox.sh start         start D-Bus + NetworkManager + the base connection
#   sandbox.sh restart-nm    restart NetworkManager only
#   sandbox.sh stop          stop NetworkManager
set -eu

LOG=${NM_SANDBOX_LOG:-/tmp/nm-sandbox.log}
CONF=/etc/NetworkManager/conf.d/00-netbird-nm-sandbox.conf

start_dbus() {
    if [ ! -S /run/dbus/system_bus_socket ] || ! dbus-send --system --dest=org.freedesktop.DBus \
            --type=method_call --print-reply / org.freedesktop.DBus.ListNames >/dev/null 2>&1; then
        mkdir -p /run/dbus
        rm -f /run/dbus/system_bus_socket /run/dbus/pid
        dbus-daemon --system --fork
    fi
}

start_nm() {
    if [ "${NM_SANDBOX_HARDEN:-1}" = 1 ]; then
        # Run NetworkManager under the same limits as its systemd unit
        # (NetworkManager.service): ProtectHome=read-only, ProtectSystem=true
        # and its CapabilityBoundingSet. The VPN plugin and everything it
        # starts inherit these, exactly as on a desktop.
        setsid unshare --mount --propagation private sh -c '
            for d in /home /root /run/user /usr /boot; do
                [ -d "$d" ] || continue
                mount --bind "$d" "$d" && mount -o remount,bind,ro "$d"
            done
            exec setpriv --bounding-set -all,+net_admin,+dac_override,+net_raw,+net_bind_service,+setgid,+setuid,+sys_module,+audit_write,+kill,+sys_chroot \
                NetworkManager --no-daemon --log-level=DEBUG \
                --log-domains=VPN,VPN_PLUGIN,CORE,DEVICE,DISPATCH' >> "$LOG" 2>&1 < /dev/null &
    else
        setsid NetworkManager --no-daemon --log-level=DEBUG \
            --log-domains=VPN,VPN_PLUGIN,CORE,DEVICE,DISPATCH >> "$LOG" 2>&1 < /dev/null &
    fi
    i=0
    until nmcli -t general status >/dev/null 2>&1; do
        i=$((i + 1)); [ $i -gt 30 ] && { echo "NetworkManager didn't start; see $LOG" >&2; exit 1; }
        sleep 0.5
    done
}

stop_nm() {
    pkill -x NetworkManager 2>/dev/null || true
    i=0
    while pgrep -x NetworkManager >/dev/null; do
        i=$((i + 1)); [ $i -gt 20 ] && pkill -9 -x NetworkManager
        sleep 0.25
    done
    ip link del nmsbx-base 2>/dev/null || true
}

base_up() {
    nmcli -t connection show sandbox-base >/dev/null 2>&1 ||
        nmcli connection add type tun ifname nmsbx-base con-name sandbox-base tun.mode tun \
            ipv4.method manual ipv4.addresses 10.99.0.1/24 ipv4.gateway 10.99.0.254 \
            ipv4.route-metric 9999 ipv6.method disabled connection.autoconnect no >/dev/null
    nmcli connection up sandbox-base >/dev/null
}

case "${1:-}" in
    deps)
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq network-manager dbus iproute2 \
            python3-gi gir1.2-nm-1.0 python3-pytest >/dev/null ;;
    start)
        [ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
        mkdir -p /etc/NetworkManager/conf.d
        {
            echo "[main]"
            echo "no-auto-default=*"
            echo "[keyfile]"
            printf 'unmanaged-devices='
            ip -o link show | awk -F': ' '{print $2}' | cut -d@ -f1 | grep -v -e '^wt' -e '^nmsbx-' |
                sed 's/^/interface-name:/' | paste -sd';' -
        } > "$CONF"
        start_dbus
        if pgrep -x NetworkManager >/dev/null; then nmcli general reload conf; else start_nm; fi
        base_up ;;
    restart-nm)
        stop_nm
        start_nm
        base_up ;;
    base-up) base_up ;;
    stop)
        stop_nm
        rm -f "$CONF" ;;
    *) sed -n '2,13p' "$0"; exit 1 ;;
esac
