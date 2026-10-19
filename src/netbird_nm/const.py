"""Names shared by the plugin, the dispatcher hook and the installer."""

# D-Bus name of the VPN service; also the connection's vpn.service-type.
SERVICE = "org.freedesktop.NetworkManager.netbird"

# Every file the installer writes carries this marker, and uninstall only
# removes files that have it.
MARKER = "managed-by: netbird-nm"

# Marker used by the earlier single-file shell version; migrated on install.
LEGACY_MARKER = "managed-by: netbird-nm-vpn.sh"

CONFIG_PATH = "/etc/netbird-nm.json"
STATE_DIR = "/run/nm-netbird"
DEFAULT_IFACE = "wt0"
DEFAULT_CONNECTION_NAME = "NetBird"
