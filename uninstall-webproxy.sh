#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

MANIFEST="/etc/tproxy-webproxy-install.manifest"

die() { echo "ERROR: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root."

echo "============================================================"
echo "          TELEGRAM WEB PROXY FULL UNINSTALLER"
echo "============================================================"
echo
echo "This removes components installed by the Web Proxy installer."
echo "Existing components that were detected as pre-existing are kept."
echo
echo "Type REMOVE to continue:"
read -r CONFIRM
[[ "$CONFIRM" == "REMOVE" ]] || { echo "Cancelled."; exit 0; }

REUSED_CADDY=1
REUSED_MT=1
REUSED_RELAY=1

if [[ -r "$MANIFEST" ]]; then
    # The manifest contains only installer-state flags.
    while IFS='=' read -r k v; do
        case "$k" in
            reused_caddy) REUSED_CADDY="$v" ;;
            reused_mtproxy) REUSED_MT="$v" ;;
            reused_relay) REUSED_RELAY="$v" ;;
        esac
    done < "$MANIFEST"
else
    echo
    echo "WARNING: ownership manifest not found."
    echo "Conservative mode is enabled: pre-existing MTProxy, relay and Caddy are preserved."
fi

echo
echo "[1/7] Stopping installer services..."

for unit in \
    tproxy-firewall.service \
    refresh-mtproxy-config.timer \
    refresh-mtproxy-config.service
do
    systemctl stop "$unit" 2>/dev/null || true
done

[[ "$REUSED_RELAY" == "1" ]] || systemctl stop tproxy-server.service 2>/dev/null || true
[[ "$REUSED_MT" == "1" ]] || systemctl stop mtproxy.service 2>/dev/null || true
[[ "$REUSED_CADDY" == "1" ]] || systemctl stop caddy.service 2>/dev/null || true

echo "[2/7] Disabling installer services..."

for unit in \
    tproxy-firewall.service \
    refresh-mtproxy-config.timer \
    refresh-mtproxy-config.service
do
    systemctl disable "$unit" 2>/dev/null || true
done

[[ "$REUSED_RELAY" == "1" ]] || systemctl disable tproxy-server.service 2>/dev/null || true
[[ "$REUSED_MT" == "1" ]] || systemctl disable mtproxy.service 2>/dev/null || true
[[ "$REUSED_CADDY" == "1" ]] || systemctl disable caddy.service 2>/dev/null || true

echo "[3/7] Removing installer service files..."

rm -f \
    /etc/systemd/system/tproxy-firewall.service \
    /etc/systemd/system/refresh-mtproxy-config.service \
    /etc/systemd/system/refresh-mtproxy-config.timer \
    /usr/local/sbin/refresh-mtproxy-config

[[ "$REUSED_RELAY" == "1" ]] || rm -f /etc/systemd/system/tproxy-server.service
[[ "$REUSED_MT" == "1" ]] || rm -f /etc/systemd/system/mtproxy.service
[[ "$REUSED_CADDY" == "1" ]] || rm -f /etc/systemd/system/caddy.service

rm -f /etc/systemd/system/caddy.service.d/tproxy.conf

echo "[4/7] Removing proxy files..."

rm -rf \
    /root/tproxy-server \
    /opt/tproxy-site \
    /srv/tproxy-site \
    /etc/tproxy-server \
    /etc/caddy/caddy

if [[ "$REUSED_RELAY" != "1" ]]; then
    rm -f /usr/local/bin/tproxy-server
    rm -rf /opt/go1.26.5
fi

if [[ "$REUSED_MT" != "1" ]]; then
    rm -rf /opt/MTProxy
    rm -rf /etc/mtproxy
fi

echo "[5/7] Removing installer users..."

if [[ "$REUSED_RELAY" != "1" ]] && id tproxy >/dev/null 2>&1; then
    home="$(getent passwd tproxy | cut -d: -f6 || true)"
    shell="$(getent passwd tproxy | cut -d: -f7 || true)"
    if [[ "$home" == "/nonexistent" && "$shell" == "/usr/sbin/nologin" ]]; then
        userdel tproxy 2>/dev/null || true
    fi
fi

if [[ "$REUSED_MT" != "1" ]] && id mtproxy >/dev/null 2>&1; then
    home="$(getent passwd mtproxy | cut -d: -f6 || true)"
    shell="$(getent passwd mtproxy | cut -d: -f7 || true)"
    if [[ "$home" == "/nonexistent" && "$shell" == "/usr/sbin/nologin" ]]; then
        userdel mtproxy 2>/dev/null || true
    fi
fi

echo "[6/7] Cleaning firewall and runtime state..."
nft delete table inet tproxy_backend 2>/dev/null || true
rm -f /etc/tproxy-webproxy-install.manifest

systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true

echo "[7/7] Final verification..."

echo
echo "Remaining related units:"
systemctl list-unit-files | grep -E '^(mtproxy|tproxy-server|tproxy-firewall|refresh-mtproxy-config)\.' || true

echo
echo "============================================================"
echo "             TELEGRAM WEB PROXY REMOVED"
echo "============================================================"
echo
echo "The installer-owned proxy components have been removed."
echo "Shared OS packages were intentionally NOT removed."
echo "A pre-existing Caddy/MTProxy/relay was preserved."
echo
echo "Reboot is normally not required."
echo "============================================================"
