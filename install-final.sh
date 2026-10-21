#!/usr/bin/env bash
set -Eeuo pipefail
BASE="$(cd "$(dirname "$0")" && pwd)"
umask 077

WPP_PROGRESS_TTY=0 WPP_PROGRESS_ROWS=0 WPP_PROGRESS_LAST=-1 WPP_PROGRESS_ACTIVE=0
wpp_progress_start() {
    WPP_PROGRESS_ACTIVE=1
    if [[ -t 1 && "${TERM:-dumb}" != "dumb" ]] && command -v tput >/dev/null 2>&1; then
        WPP_PROGRESS_ROWS="$(tput lines 2>/dev/null || echo 0)"
        if (( WPP_PROGRESS_ROWS >= 6 )); then
            WPP_PROGRESS_TTY=1
            # Reserve the last row and move the command cursor back inside the
            # scrolling region.  Leaving it on the reserved row makes apt/curl
            # overwrite the progress bar on many SSH terminals.
            printf '\033[1;%dr\033[%d;1H' "$((WPP_PROGRESS_ROWS-1))" "$((WPP_PROGRESS_ROWS-1))"
        fi
    fi
    wpp_progress 0 "$1"
}
wpp_progress() {
    local percent="$1" label="$2" width=28 filled empty bar
    (( percent < 0 )) && percent=0; (( percent > 100 )) && percent=100
    filled=$((percent*width/100)); empty=$((width-filled))
    printf -v bar '%*s' "$filled" ''; bar="${bar// /#}"
    printf -v empty '%*s' "$empty" ''; bar+="${empty// /-}"
    if (( WPP_PROGRESS_TTY )); then
        printf '\0337\033[%d;1H\033[2K[%s] %3d%%  %s\0338' "$WPP_PROGRESS_ROWS" "$bar" "$percent" "$label"
    elif (( percent != WPP_PROGRESS_LAST )); then
        printf '[%s] %3d%%  %s\n' "$bar" "$percent" "$label"
    fi
    WPP_PROGRESS_LAST="$percent"
}
wpp_progress_finish() {
    local code="$1"
    (( WPP_PROGRESS_ACTIVE )) || return 0
    if (( code == 0 )); then wpp_progress 100 "Установка завершена"; else wpp_progress "$WPP_PROGRESS_LAST" "Установка прервана"; fi
    if (( WPP_PROGRESS_TTY )); then
        printf '\0337\033[%d;1H\033[2K\0338\033[r\033[%d;1H' "$WPP_PROGRESS_ROWS" "$WPP_PROGRESS_ROWS"
        printf '[############################] %3d%%  %s\n' "$([[ $code == 0 ]] && echo 100 || echo "$WPP_PROGRESS_LAST")" "$([[ $code == 0 ]] && echo 'Установка завершена' || echo 'Установка прервана')"
    fi
}

die() { echo "ERROR: $*" >&2; exit 1; }
for file in install-panel.sh install-webproxy-core.sh uninstall-web-proxy.sh update.sh panel-logo.png wpp_subscriptions.py wpp_panel_extras.py wpp_ui.py wpp_metrics.py wpp_update.py wpp_nodes.py wpp_openflux.py wpp_awg.py wpp_firewall.py wpp_components.py wpp_cdn.py wpp_cdn_doctor.py wpp_connections.py wpp_backup.py wpp_routing.py; do
    [[ -s "$BASE/$file" ]] || die "Package is incomplete: missing $file. Extract the complete archive."
done
[[ -s "$BASE/assets/OpenFlux-linux-amd64" || -s "$BASE/OpenFlux-linux-amd64" ]] ||
    die "Package is incomplete: missing OpenFlux-linux-amd64. Extract the complete archive."
for file in amneziawg-go-linux-amd64 awg-linux-amd64 awg-quick-linux-amd64; do
    [[ -s "$BASE/assets/$file" ]] ||
        die "Package is incomplete: missing assets/$file. Extract the complete archive."
done
[[ -s "$BASE/wpp-panel/flags.tar.gz" ]] ||
    die "Package is incomplete: wpp-panel/flags.tar.gz is missing. Extract the complete archive."
command -v flock >/dev/null 2>&1 || die "flock is required (package: util-linux)."
exec 9>/run/lock/web-panel-proxy.lock
flock -n 9 || die "Another WEB PANEL PROXY install, update or removal is already running."
cleanup_credentials() {
    if [[ -f /etc/web-proxy-panel/install-credentials ]]; then
        command -v shred >/dev/null 2>&1 && shred -u /etc/web-proxy-panel/install-credentials 2>/dev/null || \
            rm -f /etc/web-proxy-panel/install-credentials
    fi
}
finish_install() { local code=$?; cleanup_credentials; wpp_progress_finish "$code"; }
trap finish_install EXIT

wpp_progress_start "Проверка пакета"
wpp_progress 5 "Подготовка сервера"

echo "WEB PANEL PROXY V 2.6.0: preparing server..."

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
wpp_progress 10 "Установка прокси-служб"
WEB_PANEL_PROXY_PACKAGE_VERSION="2.6.0" bash "$BASE/install-webproxy-core.sh"
wpp_progress 55 "Прокси-службы установлены"

echo "Installing control panel..."
wpp_progress 60 "Установка панели"
if [[ "$PANEL_UPDATE" == 1 ]]; then
    WEB_PANEL_PROXY_UPDATE=1 bash "$BASE/install-panel.sh"
else
    bash "$BASE/install-panel.sh"
fi
wpp_progress 92 "Проверка служб"

for unit in caddy.service mtproxy.service tproxy-server.service tproxy-panel.service web-proxy-panel-firewall.service; do
    systemctl is-active --quiet "$unit" || { echo "Installation failed: $unit did not start."; exit 1; }
done
systemctl is-enabled --quiet web-proxy-panel-firewall.service ||
    die "Persistent user firewall is not enabled."
nft list table inet web_proxy_panel >/dev/null 2>&1 ||
    die "Persistent user firewall table is missing."
nft list table ip web_proxy_awg >/dev/null 2>&1 ||
    die "AmneziaWG forwarding table is missing."
for file in /usr/local/bin/amneziawg-go /usr/local/bin/awg /usr/local/bin/awg-quick; do
    [[ -x "$file" ]] || die "AmneziaWG component was not installed: $file"
done
[[ -s /etc/systemd/system/web-panel-proxy-awg@.service ]] ||
    die "AmneziaWG service template was not installed."
[[ -x /opt/web-panel-proxy/xray/xray ]] || die "Xray binary was not installed."
[[ -s /etc/web-panel-proxy-xray/config.json ]] || die "Xray configuration was not created."
[[ -x /usr/local/sbin/WPP ]] || die "WPP console menu was not installed."
systemctl is-active --quiet web-panel-proxy-sync-tls.timer ||
    die "The Xray TLS synchronization timer did not start."
echo "Checking the latest Xray and OpenFlux releases..."
wpp_progress 96 "Обновление Xray и OpenFlux"
flock -u 9
if ! systemctl start web-panel-proxy-component-auto-update.service; then
    echo "WARNING: Latest Xray/OpenFlux check failed; the automatic timer will retry." >&2
fi
echo "Installation complete."
wpp_progress 100 "Установка завершена"
printf '%s\n' '2.6.0' > /etc/web-proxy-panel/version
chmod 0600 /etc/web-proxy-panel/version
