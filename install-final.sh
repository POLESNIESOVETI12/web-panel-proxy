#!/usr/bin/env bash
set -Eeuo pipefail
BASE="$(cd "$(dirname "$0")" && pwd)"
umask 077

die() { echo "ERROR: $*" >&2; exit 1; }
for file in install-panel.sh install-webproxy-core.sh uninstall-web-proxy.sh update.sh panel-logo.png wpp_subscriptions.py wpp_panel_extras.py wpp_ui.py wpp_metrics.py wpp_update.py; do
    [[ -s "$BASE/$file" ]] || die "Package is incomplete: missing $file. Extract the complete archive."
done
command -v flock >/dev/null 2>&1 || die "flock is required (package: util-linux)."
exec 9>/run/lock/web-panel-proxy.lock
flock -n 9 || die "Another WEB PANEL PROXY install, update or removal is already running."
cleanup_credentials() {
    if [[ -f /etc/web-proxy-panel/install-credentials ]]; then
        command -v shred >/dev/null 2>&1 && shred -u /etc/web-proxy-panel/install-credentials 2>/dev/null || \
            rm -f /etc/web-proxy-panel/install-credentials
    fi
}
trap cleanup_credentials EXIT

echo "WEB PANEL PROXY V 2.1.0: preparing server..."

PANEL_UPDATE=0
if [[ -s /var/lib/tproxy-panel/data.json ]] &&
   [[ -f /etc/systemd/system/tproxy-panel.service ]] &&
   sed -n 's/^Environment=WEBPROXY_PANEL_PATH=//p' /etc/systemd/system/tproxy-panel.service |
       head -n1 | grep -Eq '^/panel-[a-z0-9-]{3,64}$'; then
    PANEL_UPDATE=1
    echo "Existing control panel detected; its users, password, address and site HTML will be preserved."
else
    echo "Installation/resume mode enabled. Existing compatible services will be reused and missing components installed."
fi

# Install the recovery command before making system changes so even an
# interrupted first installation can be cleaned up deterministically.
install -d -m 0700 /etc/web-proxy-panel
install -o root -g root -m 0755 \
    "$BASE/uninstall-web-proxy.sh" \
    /usr/local/sbin/web-panel-proxy-uninstall

echo "Installing proxy services..."
bash "$BASE/install-webproxy-core.sh"

echo "Installing control panel..."
if [[ "$PANEL_UPDATE" == 1 ]]; then
    WEB_PANEL_PROXY_UPDATE=1 bash "$BASE/install-panel.sh"
else
    bash "$BASE/install-panel.sh"
fi

for unit in caddy.service mtproxy.service tproxy-server.service tproxy-panel.service web-proxy-panel-firewall.service; do
    systemctl is-active --quiet "$unit" || { echo "Installation failed: $unit did not start."; exit 1; }
done
systemctl is-enabled --quiet web-proxy-panel-firewall.service ||
    die "Persistent user firewall is not enabled."
nft list table inet web_proxy_panel >/dev/null 2>&1 ||
    die "Persistent user firewall table is missing."
[[ -x /opt/web-panel-proxy/xray/xray ]] || die "Xray binary was not installed."
[[ -s /etc/web-panel-proxy-xray/config.json ]] || die "Xray configuration was not created."
[[ -x /usr/local/sbin/WPP ]] || die "WPP console menu was not installed."
systemctl is-active --quiet web-panel-proxy-sync-tls.timer ||
    die "The Xray TLS synchronization timer did not start."
echo "Installation complete."
printf '%s\n' '2.1.0' > /etc/web-proxy-panel/version
chmod 0600 /etc/web-proxy-panel/version
