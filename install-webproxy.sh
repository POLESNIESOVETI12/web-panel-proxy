#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

VERSION="FINAL-EMAIL-FIRST"
REPO_DIR="/root/tproxy-server"
SITE_INPUT="/opt/tproxy-site"
SITE_TARGET="/srv/tproxy-site"
REUSE_MT=0
REUSE_RELAY=0
REUSE_CADDY=0
CHANNEL_B64="aHR0cHM6Ly93d3cueW91dHViZS5jb20vQFBPTEVTTklFU09WRVRJMTI="

die() {
    echo
    echo "ERROR: $*" >&2
    exit 1
}

trim() {
    local s="$1"
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    printf '%s' "$s"
}

valid_domain() {
    [[ "$1" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] &&
    [[ "$1" == *.* ]] &&
    [[ "$1" != *..* ]]
}

valid_email() {
    [[ "$1" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]
}

valid_secret() {
    [[ "$1" =~ ^([0-9a-f]{32}|dd[0-9a-f]{32})$ ]]
}

port_is_listening() {
    local port="$1"
    ss -lnt | grep -Eq ":${port}\b"
}

port_has_expected_process() {
    local port="$1"
    local process="$2"
    ss -lntp 2>/dev/null |
        grep -Eq ":${port}\b.*users:\(\(\"${process}\""
}

check_install_port() {
    local port="$1"
    local process="$2"

    if ! port_is_listening "$port"; then
        echo "      :${port} free"
        return 0
    fi

    if port_has_expected_process "$port" "$process"; then
        echo "      :${port} already used by ${process}; continuing."
        return 0
    fi

    ss -lntp | grep -E ":${port}\b" || true
    die "Port ${port} is occupied by an unexpected process."
}

show_failure() {
    echo
    echo "============================================================"
    echo "                    INSTALLATION FAILED"
    echo "============================================================"
    echo
    echo "--- services ---"
    systemctl --no-pager --full status mtproxy tproxy-server caddy tproxy-firewall 2>/dev/null || true
    echo
    echo "--- MTProxy log ---"
    journalctl -u mtproxy -n 40 --no-pager 2>/dev/null || true
    echo
    echo "--- relay log ---"
    journalctl -u tproxy-server -n 40 --no-pager 2>/dev/null || true
    echo
    echo "--- site permissions ---"
    namei -l "$SITE_TARGET/index.html" 2>/dev/null || true
    echo
    echo "--- MTProxy permissions ---"
    namei -l /opt/MTProxy/objs/bin/mtproto-proxy 2>/dev/null || true
}

on_error() {
    local code=$?
    trap - ERR
    show_failure
    exit "$code"
}
trap on_error ERR

clear 2>/dev/null || true

cat <<EOF
============================================================
      TELEGRAM WEB PROXY INSTALLER ${VERSION}
============================================================

One-file installer.
No repository or hosting is required for this installer.

The official tproxy-server source is downloaded during setup.
============================================================
EOF

[[ $EUID -eq 0 ]] || die "Run this installer as root."
[[ "$(uname -m)" == "x86_64" ]] || die "x86_64 is required."

while true; do
    echo
    read -r -p "Domain (example: proxy.example.com): " DOMAIN
    DOMAIN="$(trim "$DOMAIN")"
    DOMAIN="${DOMAIN,,}"
    valid_domain "$DOMAIN" && break
    echo "Invalid domain. Example: proxy.example.com"
done

echo
while true; do
    read -r -p "ACME email (example: admin@example.com): " EMAIL
    EMAIL="$(trim "$EMAIL")"
    valid_email "$EMAIL" && break
    echo "Неверный email. Используйте латинские символы, например admin@example.com"
done

echo
read -r -p "Generate a secure secret automatically? [Y/n]: " MODE
MODE="$(trim "${MODE:-Y}")"

if [[ -z "$MODE" || "$MODE" =~ ^[Yy]$ ]]; then
    command -v openssl >/dev/null 2>&1 || {
        export DEBIAN_FRONTEND=noninteractive
        apt-get update
        apt-get install -y --no-install-recommends openssl
    }
    SECRET="$(openssl rand -hex 16)"
else
    while true; do
        echo
        read -r -s -p "WEB proxy secret (32 lowercase hex, optionally dd + 32 hex): " SECRET
        echo
        valid_secret "$SECRET" && break
        echo "Invalid secret."
    done
fi

valid_secret "$SECRET" || die "Secret is invalid."

echo
echo "[1/10] Checking system..."
. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || die "Ubuntu is required."
dpkg --compare-versions "${VERSION_ID:-0}" ge "22.04" ||
    die "Ubuntu 22.04 or newer is required."
echo "      Ubuntu ${VERSION_ID} / x86_64"

echo
echo "[2/10] Installing dependencies..."
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates curl git openssl dnsutils nftables \
    build-essential libssl-dev util-linux zlib1g-dev
echo "      OK"

echo
echo "[3/10] Checking ports..."
check_install_port 80 caddy
check_install_port 443 caddy
check_install_port 2398 mtproto-proxy
check_install_port 8080 tproxy-server
check_install_port 8081 tproxy-server
if [[ -x /opt/MTProxy/objs/bin/mtproto-proxy ]] &&
   systemctl list-unit-files mtproxy.service >/dev/null 2>&1 &&
   port_has_expected_process 2398 mtproto-proxy; then
    REUSE_MT=1
    echo "      Existing MTProxy detected; reusing it."
fi

if [[ -x /usr/local/bin/tproxy-server ]] &&
   systemctl list-unit-files tproxy-server.service >/dev/null 2>&1 &&
   port_has_expected_process 8080 tproxy-server &&
   port_has_expected_process 8081 tproxy-server; then
    REUSE_RELAY=1
    echo "      Existing tproxy-server detected; reusing it."
fi

if [[ -x /usr/local/bin/caddy ]] &&
   systemctl list-unit-files caddy.service >/dev/null 2>&1 &&
   port_has_expected_process 80 caddy &&
   port_has_expected_process 443 caddy; then
    REUSE_CADDY=1
    echo "      Existing Caddy detected; reusing it."
fi

EXISTING_CADDY_CONF="/etc/systemd/system/caddy.service.d/tproxy.conf"
EXISTING_DOMAIN=""
EXISTING_EMAIL=""
REUSE_EXISTING_HTTPS=0

if [[ "$REUSE_CADDY" == "1" && -f "$EXISTING_CADDY_CONF" ]]; then
    EXISTING_DOMAIN="$(sed -n 's/^Environment=TPROXY_HOSTNAME=//p' "$EXISTING_CADDY_CONF" | head -n1)"
    EXISTING_EMAIL="$(sed -n 's/^Environment=ACME_EMAIL=//p' "$EXISTING_CADDY_CONF" | head -n1)"

    if [[ -n "$EXISTING_DOMAIN" && "$EXISTING_DOMAIN" != "$DOMAIN" ]]; then
        die "Existing Web Proxy uses domain ${EXISTING_DOMAIN}. Use that domain or uninstall first."
    fi

    if [[ -n "$EXISTING_DOMAIN" ]] &&
       curl -fsSI --max-time 10 "https://${EXISTING_DOMAIN}/" >/dev/null 2>&1; then
        REUSE_EXISTING_HTTPS=1
        DOMAIN="$EXISTING_DOMAIN"
        echo "      Existing HTTPS is already working; certificate/configuration will be reused."
        echo "      The email entered above will be kept for this run."
    else
        echo "      Existing Caddy found, but HTTPS is not currently working."
        if [[ -n "$EXISTING_EMAIL" ]] && valid_email "$EXISTING_EMAIL"; then
            echo "      Existing ACME email is valid."
        else
            echo "      Existing ACME email is missing or invalid."
            echo "      The email entered above will be used."
        fi
    fi
fi

echo "[4/10] Checking DNS..."

if [[ "$REUSE_EXISTING_HTTPS" == "1" ]]; then
    DNS_IP="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}')"
    if [[ -z "$DNS_IP" ]]; then
        echo
        echo "============================================================"
        echo "                DNS НЕ ПРИВЯЗАН"
        echo "============================================================"
        echo
        echo "Домен не привязан к этому VPS."
        echo
        echo "Проверьте A-запись домена и попробуйте заново."
        echo
        echo "============================================================"
        die "ПОПРОБУЙТЕ ЗАНОВО"
    fi
    echo "      Existing HTTPS verified: $DOMAIN -> $DNS_IP"
else
    DNS_IP="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}')"
    if [[ -z "$DNS_IP" ]]; then
        echo
        echo "============================================================"
        echo "                DNS НЕ ПРИВЯЗАН"
        echo "============================================================"
        echo
        echo "Домен не привязан к этому VPS."
        echo
        echo "Проверьте A-запись домена и попробуйте заново."
        echo
        echo "============================================================"
        die "ПОПРОБУЙТЕ ЗАНОВО"
    fi

    VPS_IP="$(curl -4fsS --max-time 10 https://api.ipify.org || true)"
    if [[ -n "$VPS_IP" && "$DNS_IP" != "$VPS_IP" ]]; then
        echo
        echo "============================================================"
        echo "                DNS НЕ ПРИВЯЗАН"
        echo "============================================================"
        echo
        echo "Домен не привязан к этому VPS."
        echo
        echo "DNS: $DNS_IP"
        echo "VPS: $VPS_IP"
        echo
        echo "Проверьте A-запись домена и попробуйте заново."
        echo
        echo "============================================================"
        die "ПОПРОБУЙТЕ ЗАНОВО"
    fi
    echo "      $DOMAIN -> $DNS_IP"
fi



if [[ "$REUSE_EXISTING_HTTPS" == "1" ]]; then
    DNS_IP="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}')"
    if [[ -z "$DNS_IP" ]]; then
        echo
        echo "============================================================"
        echo "                DNS НЕ ПРИВЯЗАН"
        echo "============================================================"
        echo
        echo "Домен не привязан к этому VPS."
        echo
        echo "Проверьте A-запись домена и попробуйте заново."
        echo
        echo "============================================================"
        die "ПОПРОБУЙТЕ ЗАНОВО"
    fi
    echo "      Existing HTTPS verified: $DOMAIN -> $DNS_IP"
else
    DNS_IP="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}')"
    if [[ -z "$DNS_IP" ]]; then
        echo
        echo "============================================================"
        echo "                DNS НЕ ПРИВЯЗАН"
        echo "============================================================"
        echo
        echo "Домен не привязан к этому VPS."
        echo
        echo "Проверьте A-запись домена и попробуйте заново."
        echo
        echo "============================================================"
        die "ПОПРОБУЙТЕ ЗАНОВО"
    fi

    VPS_IP="$(curl -4fsS --max-time 10 https://api.ipify.org || true)"
    if [[ -n "$VPS_IP" && "$DNS_IP" != "$VPS_IP" ]]; then
        echo
        echo "============================================================"
        echo "                DNS НЕ ПРИВЯЗАН"
        echo "============================================================"
        echo
        echo "Домен не привязан к этому VPS."
        echo
        echo "DNS: $DNS_IP"
        echo "VPS: $VPS_IP"
        echo
        echo "Проверьте A-запись домена и попробуйте заново."
        echo
        echo "============================================================"
        die "ПОПРОБУЙТЕ ЗАНОВО"
    fi
    echo "      $DOMAIN -> $DNS_IP"
fi
