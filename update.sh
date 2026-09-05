#!/usr/bin/env bash
# Safe in-place updater for WEB PANEL PROXY V 2.1.0.
set -Eeuo pipefail
umask 077

# Use the fixed public repository without interactive credentials or local
# Git URL substitutions.
export GIT_TERMINAL_PROMPT=0
export GIT_CONFIG_NOSYSTEM=1
export GIT_CONFIG_GLOBAL=/dev/null

REPOSITORY="https://github.com/POLESNIESOVETI12/web-panel-proxy.git"
REQUESTED_REF="${WEB_PANEL_PROXY_REF:-}"
RELEASE_REF="$REQUESTED_REF"
LOCAL_SOURCE=""
if [[ "${1:-}" == "--local" ]]; then
    LOCAL_SOURCE="$(cd "$(dirname "$0")" && pwd)"
    RELEASE_REF="v2.1.0"
    for file in install-panel.sh update.sh uninstall-web-proxy.sh repair-landing-pages.sh panel-logo.png wpp_subscriptions.py wpp_panel_extras.py wpp_ui.py wpp_metrics.py wpp_update.py; do
        [[ -s "$LOCAL_SOURCE/$file" ]] || { echo "Incomplete local archive: $file is missing." >&2; exit 1; }
    done
elif [[ $# != 0 ]]; then
    echo "Usage: bash update.sh [--local]" >&2; exit 1
fi
SERVICE="/etc/systemd/system/tproxy-panel.service"
LEGACY_SERVICE="/etc/systemd/system/web-proxy-panel.service"
DATA_FILE="/var/lib/tproxy-panel/data.json"
PRIMARY_SECRET="/etc/web-proxy-panel/primary-secret"

die() { echo "ERROR: $*" >&2; exit 1; }
[[ ${EUID} -eq 0 ]] || die "Run as root: sudo -i"
command -v flock >/dev/null 2>&1 || die "flock is required (package: util-linux)."
exec 9>/run/lock/web-panel-proxy.lock
flock -n 9 || die "Another WEB PANEL PROXY install, update or removal is already running."

echo "============================================================"
echo "      WEB PANEL PROXY V 2.1.0 — SAFE UPDATE"
echo "============================================================"
echo "Users, administrator password, panel URL and site HTML will be retained."

MIGRATING_LEGACY=0
PANEL_PATH=""
LEGACY_SECRET=""

if [[ -s "$DATA_FILE" ]] &&
   { [[ -f "$SERVICE" ]] || [[ -f "$LEGACY_SERVICE" ]]; }; then
    echo "Current control panel detected; account and data will be preserved."
else
    for legacy_item in /etc/tproxy-server/config.json /etc/tproxy-server/profiles.json /etc/mtproxy /opt/MTProxy /etc/systemd/system/mtproxy.service /etc/systemd/system/tproxy-server.service; do
        if [[ -e "$legacy_item" ]]; then
            MIGRATING_LEGACY=1
            break
        fi
    done
    [[ "$MIGRATING_LEGACY" == 1 ]] || die "No supported WEB Proxy installation was found."
    echo "First-generation WEB Proxy detected (without a control panel)."
    echo "The updater will add the panel and ask for a new administrator login and password."
fi

# Some early builds used this service name. Copying it only supplies the
# existing secret panel address; install-panel.sh replaces the definition.
if [[ "$MIGRATING_LEGACY" != 1 && ! -f "$SERVICE" && -f "$LEGACY_SERVICE" ]]; then
    cp -a "$LEGACY_SERVICE" "$SERVICE"
fi
if [[ "$MIGRATING_LEGACY" != 1 ]]; then
    [[ -f "$SERVICE" ]] || die "Panel service file was not found."
    PANEL_PATH="$(sed -n 's/^Environment=WEBPROXY_PANEL_PATH=//p' "$SERVICE" | head -n1 || true)"
    [[ "$PANEL_PATH" =~ ^/panel-[a-z0-9-]{3,64}$ ]] || die "Could not read the existing panel address."
fi

if [[ "$MIGRATING_LEGACY" == 1 ]]; then
    LEGACY_SECRET="$(cat "$PRIMARY_SECRET" 2>/dev/null || true)"
    if ! [[ "$LEGACY_SECRET" =~ ^([0-9a-f]{32}|dd[0-9a-f]{32})$ ]] && [[ -s /etc/tproxy-server/profiles.json ]]; then
        LEGACY_SECRET="$(sed -n 's/.*"secret"[[:space:]]*:[[:space:]]*"\([0-9a-f]*\)".*/\1/p' /etc/tproxy-server/profiles.json | head -n1)"
    fi
    if ! [[ "$LEGACY_SECRET" =~ ^([0-9a-f]{32}|dd[0-9a-f]{32})$ ]] && [[ -s /etc/mtproxy/mtproxy.env ]]; then
        LEGACY_SECRET="$(sed -n 's/^MTPROXY_SECRET=//p' /etc/mtproxy/mtproxy.env | head -n1)"
    fi
    if ! [[ "$LEGACY_SECRET" =~ ^([0-9a-f]{32}|dd[0-9a-f]{32})$ ]]; then
        # The first public installer stored the Secret only in ExecStart=-S.
        LEGACY_SECRET="$(systemctl cat mtproxy.service 2>/dev/null |
            sed -n 's/.*[[:space:]]-S[[:space:]]\([0-9a-f]*\).*/\1/p' | head -n1 || true)"
    fi
    [[ "$LEGACY_SECRET" =~ ^([0-9a-f]{32}|dd[0-9a-f]{32})$ ]] ||
        die "Could not recover the primary Secret from the first-generation installation."
else
    [[ -s "$PRIMARY_SECRET" ]] || die "Primary Secret is missing from the existing panel installation."
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
# Pause the HTTP writer before taking a snapshot of subscription slots/keys.
# Proxy services keep running. Recover the panel if snapshot/download fails.
PANEL_WAS_RUNNING=0
if systemctl is-active --quiet tproxy-panel.service; then
    PANEL_WAS_RUNNING=1
    systemctl stop tproxy-panel.service
fi
trap 'if [[ "$PANEL_WAS_RUNNING" == 1 ]]; then systemctl start tproxy-panel.service || true; fi' EXIT
BACKUP="/root/web-panel-proxy-update-backup-${STAMP}"
install -d -m 0700 "$BACKUP"
BACKUP_ITEMS=()
for item in /opt/tproxy-panel /opt/web-panel-proxy /opt/MTProxy /usr/local/bin/caddy /usr/local/bin/tproxy-server /usr/local/sbin/web-proxy-panelctl /usr/local/sbin/web-proxy-panel-user-firewall /usr/local/sbin/web-panel-proxy-sync-tls /usr/local/sbin/web-panel-proxy-update /usr/local/sbin/web-panel-proxy-uninstall /usr/local/sbin/WPP /usr/local/sbin/wpp /etc/systemd/system/tproxy-panel.service /etc/systemd/system/web-proxy-panel-firewall.service /etc/systemd/system/web-proxy-panel-traffic.service /etc/systemd/system/web-proxy-panel-traffic.timer /etc/systemd/system/web-panel-proxy-xray.service /etc/systemd/system/web-panel-proxy-sync-tls.service /etc/systemd/system/web-panel-proxy-sync-tls.timer /etc/systemd/system/tproxy-server.service /etc/systemd/system/mtproxy.service /etc/systemd/system/caddy.service.d/tproxy.conf /etc/caddy/Caddyfile /etc/tproxy-server /etc/mtproxy /etc/mita /etc/web-panel-proxy-xray /var/lib/web-panel-proxy-xray /var/lib/tproxy-panel /etc/web-proxy-panel /srv/tproxy-site; do
    [[ -e "$item" ]] && BACKUP_ITEMS+=("$item")
    [[ -e "$item" ]] && cp -a --parents "$item" "$BACKUP"
done
shopt -s nullglob
for item in /etc/systemd/system/web-proxy-user-*.service /etc/systemd/system/web-panel-proxy-web-update.service /etc/systemd/system/web-panel-proxy-metrics.service /etc/systemd/system/web-panel-proxy-metrics.timer; do
    [[ -e "$item" ]] || continue
    BACKUP_ITEMS+=("$item")
    cp -a --parents "$item" "$BACKUP"
done
shopt -u nullglob
tar --numeric-owner -cpf "$BACKUP/state.tar" "${BACKUP_ITEMS[@]}"
echo "Backup created: $BACKUP"

HAD_FIREWALL_SERVICE=0
[[ -e /etc/systemd/system/web-proxy-panel-firewall.service ]] && HAD_FIREWALL_SERVICE=1
HAD_TRAFFIC_TIMER=0
[[ -e /etc/systemd/system/web-proxy-panel-traffic.timer ]] && HAD_TRAFFIC_TIMER=1
HAD_METRICS_TIMER=0
[[ -e /etc/systemd/system/web-panel-proxy-metrics.timer ]] && HAD_METRICS_TIMER=1
HAD_PANEL_DATA=0
HAD_PANEL_SERVICE=0
HAD_PRIMARY_SECRET=0
HAD_CADDY_DROPIN=0
HAD_PANEL_STATE_DIR=0
HAD_XRAY_STATE=0
HAD_XRAY_USER=0
HAD_XRAY_GROUP=0
HAD_WPP_MENU=0
HAD_WEB_UPDATE_UNIT=0
[[ -e /etc/systemd/system/web-panel-proxy-web-update.service ]] && HAD_WEB_UPDATE_UNIT=1
[[ -e "$DATA_FILE" ]] && HAD_PANEL_DATA=1
[[ -e "$SERVICE" ]] && HAD_PANEL_SERVICE=1
[[ -e "$PRIMARY_SECRET" ]] && HAD_PRIMARY_SECRET=1
[[ -e /etc/systemd/system/caddy.service.d/tproxy.conf ]] && HAD_CADDY_DROPIN=1
[[ -d /etc/web-proxy-panel ]] && HAD_PANEL_STATE_DIR=1
{ [[ -e /opt/web-panel-proxy ]] || [[ -e /etc/web-panel-proxy-xray ]] || [[ -e /etc/systemd/system/web-panel-proxy-xray.service ]]; } && HAD_XRAY_STATE=1
id xray >/dev/null 2>&1 && HAD_XRAY_USER=1
getent group xray >/dev/null 2>&1 && HAD_XRAY_GROUP=1
[[ -e /usr/local/sbin/WPP ]] && HAD_WPP_MENU=1
UPDATE_COMMITTED=0
rollback_update() {
    local code="$1"
    [[ "$UPDATE_COMMITTED" == 1 || "$code" == 0 ]] && return 0
    echo "Update failed; restoring the previous working state..." >&2
    systemctl stop tproxy-panel.service web-proxy-panel-firewall.service web-proxy-panel-traffic.timer web-proxy-panel-traffic.service web-panel-proxy-metrics.timer web-panel-proxy-metrics.service web-panel-proxy-xray.service web-panel-proxy-sync-tls.timer 2>/dev/null || true
    tar --numeric-owner -xpf "$BACKUP/state.tar" -C / 2>/dev/null || true
    if [[ "$HAD_FIREWALL_SERVICE" == 0 ]]; then
        rm -f /etc/systemd/system/web-proxy-panel-firewall.service
        rm -f /usr/local/sbin/web-proxy-panel-user-firewall
    fi
    if [[ "$HAD_TRAFFIC_TIMER" == 0 ]]; then
        rm -f /etc/systemd/system/web-proxy-panel-traffic.service /etc/systemd/system/web-proxy-panel-traffic.timer
    fi
    if [[ "$HAD_METRICS_TIMER" == 0 ]]; then
        systemctl disable web-panel-proxy-metrics.timer 2>/dev/null || true
        rm -f /etc/systemd/system/web-panel-proxy-metrics.service /etc/systemd/system/web-panel-proxy-metrics.timer
    fi
    if [[ "$HAD_WPP_MENU" == 0 ]]; then
        rm -f /usr/local/sbin/WPP /usr/local/sbin/wpp /usr/local/sbin/web-panel-proxy-update
    fi
    if [[ "$HAD_WEB_UPDATE_UNIT" == 0 ]]; then
        rm -f /etc/systemd/system/web-panel-proxy-web-update.service
    fi
    if [[ "$HAD_PANEL_SERVICE" == 0 ]]; then
        rm -f "$SERVICE"
        rm -rf /opt/tproxy-panel
    fi
    if [[ "$HAD_PANEL_DATA" == 0 ]]; then
        rm -rf /var/lib/tproxy-panel
    fi
    if [[ "$HAD_PRIMARY_SECRET" == 0 ]]; then
        rm -f "$PRIMARY_SECRET"
    fi
    if [[ "$HAD_PANEL_STATE_DIR" == 0 ]]; then
        rm -rf /etc/web-proxy-panel
    fi
    if [[ "$HAD_CADDY_DROPIN" == 0 ]]; then
        rm -f /etc/systemd/system/caddy.service.d/tproxy.conf
    fi
    if [[ "$HAD_XRAY_STATE" == 0 ]]; then
        rm -rf /opt/web-panel-proxy /etc/web-panel-proxy-xray /var/lib/web-panel-proxy-xray
        rm -f /etc/web-proxy-panel/xray-path /etc/web-proxy-panel/xray-user-owned /etc/web-proxy-panel/xray-group-owned \
            /etc/web-proxy-panel/hysteria-ufw-owned
        rm -f /usr/local/sbin/web-panel-proxy-sync-tls \
            /etc/systemd/system/web-panel-proxy-xray.service \
            /etc/systemd/system/web-panel-proxy-sync-tls.service \
            /etc/systemd/system/web-panel-proxy-sync-tls.timer
    fi
    if [[ "$HAD_XRAY_USER" == 0 ]]; then
        userdel xray 2>/dev/null || true
    fi
    if [[ "$HAD_XRAY_GROUP" == 0 ]]; then
        groupdel xray 2>/dev/null || true
    fi
    systemctl daemon-reload
    systemctl restart mtproxy.service tproxy-server.service caddy.service tproxy-panel.service 2>/dev/null || true
    [[ "$HAD_FIREWALL_SERVICE" == 1 ]] && systemctl restart web-proxy-panel-firewall.service 2>/dev/null || true
    [[ "$HAD_XRAY_STATE" == 1 ]] && systemctl restart web-panel-proxy-xray.service 2>/dev/null || true
    [[ "$HAD_TRAFFIC_TIMER" == 1 ]] && systemctl restart web-proxy-panel-traffic.timer 2>/dev/null || true
    [[ "$HAD_METRICS_TIMER" == 1 ]] && systemctl restart web-panel-proxy-metrics.timer 2>/dev/null || true
    echo "Previous files restored from: $BACKUP" >&2
}

if ! command -v git >/dev/null 2>&1; then
    echo "Installing Git (required to download the update)..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get -o DPkg::Lock::Timeout=600 update
    apt-get -o DPkg::Lock::Timeout=600 install -y --no-install-recommends git
fi

if [[ -z "$LOCAL_SOURCE" ]]; then
if [[ -z "$RELEASE_REF" ]]; then
    echo "Checking the latest published WEB PANEL PROXY version..."
    RELEASE_REF="$(git ls-remote --tags --refs "$REPOSITORY" 'v[0-9]*' |
        awk -F/ '{print $3}' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -n1)"
fi
[[ "$RELEASE_REF" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?$ ]] ||
    die "Could not determine a valid published release tag."
echo "Selected release: $RELEASE_REF"
CURRENT_VERSION="$(cat /etc/web-proxy-panel/version 2>/dev/null || true)"
if [[ -z "$REQUESTED_REF" && "$CURRENT_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.+~-][A-Za-z0-9.+~-]+)?$ ]] &&
   dpkg --compare-versions "$CURRENT_VERSION" ge "${RELEASE_REF#v}"; then
    echo "WEB PANEL PROXY ${CURRENT_VERSION} is already the latest published stable version."
    exit 0
fi
fi

TEMP_DIR="$(mktemp -d /tmp/web-panel-proxy-update.XXXXXX)"
finish() {
    local code=$?
    trap - EXIT
    rollback_update "$code"
    rm -rf "$TEMP_DIR"
    exit "$code"
}
trap finish EXIT

echo "Downloading the current WEB PANEL PROXY V 2.1.0 files..."
if [[ -n "$LOCAL_SOURCE" ]]; then
    install -d -m 0700 "$TEMP_DIR/source"
    for file in install-panel.sh update.sh uninstall-web-proxy.sh repair-landing-pages.sh panel-logo.png wpp_subscriptions.py wpp_panel_extras.py wpp_ui.py wpp_metrics.py wpp_update.py; do
        cp -a "$LOCAL_SOURCE/$file" "$TEMP_DIR/source/$file"
    done
else
git clone --depth 1 --branch "$RELEASE_REF" "$REPOSITORY" "$TEMP_DIR/source"
fi
[[ -f "$TEMP_DIR/source/install-panel.sh" ]] || die "Update package is incomplete."
chmod 0700 "$TEMP_DIR/source/install-panel.sh"

if [[ "$MIGRATING_LEGACY" == 1 ]]; then
    DOMAIN="$(sed -n 's/^Environment=TPROXY_HOSTNAME=//p' /etc/systemd/system/caddy.service.d/tproxy.conf 2>/dev/null | head -n1 || true)"
    if ! [[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] && [[ -s /etc/tproxy-server/config.json ]]; then
        DOMAIN="$(sed -n 's/.*"public_hostname"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' /etc/tproxy-server/config.json | head -n1)"
    fi
    if ! [[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] && [[ -s /etc/caddy/Caddyfile ]]; then
        DOMAIN="$(sed -n 's/^[[:space:]]*\([a-z0-9][a-z0-9.-]*\)[[:space:]]*{[[:space:]]*$/\1/p' /etc/caddy/Caddyfile |
            grep -vE '^(http|https|localhost)$' | head -n1 || true)"
    fi
    [[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ && "$DOMAIN" == *.* ]] ||
        die "Could not recover the domain from the first-generation installation."

    ACME_EMAIL="$(sed -n 's/^Environment=ACME_EMAIL=//p' /etc/systemd/system/caddy.service.d/tproxy.conf 2>/dev/null | head -n1 || true)"
    if ! [[ "$ACME_EMAIL" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; then
        while true; do
            read -r -p "ACME email for the existing domain: " ACME_EMAIL
            [[ "$ACME_EMAIL" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] && break
            echo "Invalid email."
        done
    fi

    install -d -m 0700 /etc/web-proxy-panel
    printf '%s\n' "$LEGACY_SECRET" > "$PRIMARY_SECRET"
    chmod 0600 "$PRIMARY_SECRET"
    if [[ ! -s /etc/web-proxy-panel/mtproto-host ]]; then
        MTPROTO_HOST="$(curl -4fsS --max-time 10 https://api.ipify.org 2>/dev/null || true)"
        printf '%s\n' "${MTPROTO_HOST:-$DOMAIN}" > /etc/web-proxy-panel/mtproto-host
        chmod 0600 /etc/web-proxy-panel/mtproto-host
    fi
    if [[ ! -s /etc/web-proxy-panel/caddy-owned ]]; then
        printf '%s\n' 'WEB_PANEL_PROXY_V2_CADDY_SHARED' > /etc/web-proxy-panel/caddy-owned
        chmod 0600 /etc/web-proxy-panel/caddy-owned
    fi
    install -d -m 0755 /etc/systemd/system/caddy.service.d
    cat > /etc/systemd/system/caddy.service.d/tproxy.conf <<EOF
[Service]
Environment=TPROXY_HOSTNAME=$DOMAIN
Environment=TPROXY_SITE_ROOT=/srv/tproxy-site
Environment=ACME_EMAIL=$ACME_EMAIL
ReadWritePaths=/etc/caddy
EOF
    chmod 0644 /etc/systemd/system/caddy.service.d/tproxy.conf
    systemctl daemon-reload
fi

# Keep the relay binary current as part of the same public update command.
# Configuration, users, secrets and public-site files are not replaced.
if [[ -f "$TEMP_DIR/source/repair-landing-pages.sh" ]]; then
    chmod 0700 "$TEMP_DIR/source/repair-landing-pages.sh"
    bash "$TEMP_DIR/source/repair-landing-pages.sh"
fi

if [[ "$MIGRATING_LEGACY" == 1 ]]; then
    # New panel bootstrap: install-panel asks for a login and one password,
    # creates the private URL and leaves the old core proxy data in place.
    bash "$TEMP_DIR/source/install-panel.sh"
else
    WEB_PANEL_PROXY_UPDATE=1 bash "$TEMP_DIR/source/install-panel.sh"
fi
install -o root -g root -m 0755 \
    "$TEMP_DIR/source/uninstall-web-proxy.sh" \
    /usr/local/sbin/web-panel-proxy-uninstall

if [[ -f "$LEGACY_SERVICE" ]]; then
    systemctl disable --now web-proxy-panel.service 2>/dev/null || true
    rm -f "$LEGACY_SERVICE"
    systemctl daemon-reload
fi

DOMAIN="$(sed -n 's/^Environment=TPROXY_HOSTNAME=//p' /etc/systemd/system/caddy.service.d/tproxy.conf | head -n1)"
PANEL_PATH="$(sed -n 's/^Environment=WEBPROXY_PANEL_PATH=//p' "$SERVICE" | head -n1 || true)"
[[ "$PANEL_PATH" =~ ^/panel-[a-z0-9-]{3,64}$ ]] || die "The updated panel address could not be read."
systemctl is-active --quiet web-proxy-panel-firewall.service ||
    die "Persistent user firewall did not start after the update."
nft list table inet web_proxy_panel >/dev/null 2>&1 ||
    die "Persistent user firewall table is missing after the update."
[[ -x /opt/web-panel-proxy/xray/xray ]] || die "Xray binary is missing after the update."
[[ -s /etc/web-panel-proxy-xray/config.json ]] || die "Xray configuration is missing after the update."
command -v caddy >/dev/null 2>&1 || die "Caddy is missing after the update."
[[ -x /opt/MTProxy/objs/bin/mtproto-proxy ]] || die "MTProxy is missing after the update."
systemctl is-active --quiet caddy.service || die "Caddy did not start after the update."
systemctl is-active --quiet web-panel-proxy-sync-tls.timer ||
    die "The Xray TLS synchronization timer did not start after the update."
if [[ ! -s /etc/web-proxy-panel/caddy-owned ]]; then
    printf '%s\n' 'WEB_PANEL_PROXY_V2_CADDY_SHARED' > /etc/web-proxy-panel/caddy-owned
fi
printf '%s\n' "${RELEASE_REF#v}" > /etc/web-proxy-panel/version
chmod 0600 /etc/web-proxy-panel/caddy-owned /etc/web-proxy-panel/version
UPDATE_COMMITTED=1
echo
echo "Update completed. Open the panel at: https://${DOMAIN}${PANEL_PATH}/login"
