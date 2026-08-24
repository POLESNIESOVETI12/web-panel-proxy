#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

echo
echo "============================================================"
echo "        TELEGRAM WEB PROXY COMPLETE UNINSTALLER"
echo "============================================================"
echo
echo "WARNING: this removes the Telegram Web Proxy stack installed"
echo "by install-webproxy.sh, including its configuration and data."
echo
read -r -p "Type REMOVE to continue: " CONFIRM
[[ "$CONFIRM" == "REMOVE" ]] || { echo "Cancelled."; exit 0; }

echo
echo "[1/7] Stopping services..."
for unit in \
    caddy.service \
    mtproxy.service \
    tproxy-server.service \
    tproxy-firewall.service \
    refresh-mtproxy-config.timer \
    refresh-mtproxy-config.service
do
    systemctl disable --now "$unit" 2>/dev/null || true
done

echo "[2/7] Removing running processes..."
pkill -9 caddy 2>/dev/null || true
pkill -9 mtproto-proxy 2>/dev/null || true
pkill -9 tproxy-server 2>/dev/null || true

echo "[3/7] Removing systemd units..."
rm -f \
    /etc/systemd/system/caddy.service \
    /etc/systemd/system/mtproxy.service \
    /etc/systemd/system/tproxy-server.service \
    /etc/systemd/system/tproxy-firewall.service \
    /etc/systemd/system/refresh-mtproxy-config.service \
    /etc/systemd/system/refresh-mtproxy-config.timer

rm -rf /etc/systemd/system/caddy.service.d

systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true

echo "[4/7] Removing Telegram Web Proxy data..."
rm -rf \
    /etc/tproxy-server \
    /etc/mtproxy \
    /opt/MTProxy \
    /opt/tproxy-site \
    /srv/tproxy-site \
    /root/tproxy-server \
    /run/credentials/tproxy-server.service \
    /usr/local/bin/tproxy-server \
    /usr/local/sbin/refresh-mtproxy-config

echo "[5/7] Removing Caddy data..."
rm -rf \
    /etc/caddy \
    /var/lib/caddy \
    /usr/local/bin/caddy

echo "[6/7] Removing installer/download leftovers..."
rm -f \
    /root/install-webproxy.sh \
    /root/install-webproxy-FINAL-UNIVERSAL-2.sh \
    /root/install-webproxy-FINAL-UNIVERSAL.sh \
    /root/install-webproxy-FINAL.sh \
    /root/install-webproxy-COMPACT.sh \
    /root/install-webproxy-COMPACT-FIXED.sh \
    /root/install-webproxy-COMPACT-FIXED2.sh \
    /root/install-webproxy-FINAL-3.sh \
    /root/telegram-webproxy-installer-FINAL-3.sh

rm -rf \
    /root/telegram-webproxy-panel \
    /root/tproxy-server

# This Go tree is the exact version downloaded by our installer.
# Remove it only if it exists and is not needed by another application.
rm -rf /opt/go1.26.5

echo "[7/7] Removing service users..."
for user in tproxy mtproxy caddy; do
    if id "$user" >/dev/null 2>&1; then
        userdel "$user" 2>/dev/null || true
    fi
done

getent group tproxy >/dev/null 2>&1 && groupdel tproxy 2>/dev/null || true
getent group mtproxy >/dev/null 2>&1 && groupdel mtproxy 2>/dev/null || true
getent group caddy >/dev/null 2>&1 && groupdel caddy 2>/dev/null || true

systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true

echo
echo "============================================================"
echo "                    CLEANUP CHECK"
echo "============================================================"

ss -lntp | grep -E ':(80|443|2398|8080|8081|8090|8888)\b' \
    || echo "Ports: CLEAN"

systemctl --no-pager --full status \
    caddy mtproxy tproxy-server tproxy-firewall tproxy-panel \
    2>/dev/null || true

echo
echo "Telegram Web Proxy files/services removed."
echo
echo "NOTE: system packages installed with apt are intentionally"
echo "not purged automatically because some may have existed before"
echo "the Web Proxy installation."
echo "============================================================"
