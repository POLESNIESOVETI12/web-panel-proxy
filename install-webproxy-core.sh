#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# The relay source is public and pinned below. Do not allow credential prompts
# or root-level Git URL rewrites to change where it is fetched from.
export GIT_TERMINAL_PROMPT=0
export GIT_CONFIG_NOSYSTEM=1
export GIT_CONFIG_GLOBAL=/dev/null

VERSION="2.1.0"
REPO_DIR="/root/tproxy-server"
SITE_INPUT="/opt/tproxy-site"
SITE_TARGET="/srv/tproxy-site"
REUSE_MT=0
REUSE_RELAY=0
REUSE_CADDY=0
PRESERVE_SITE=0
CADDY_MODE=""
CADDY_BIN=""
CADDY_SERVICE_EXISTS=0
MT_PORT=2398
CHANNEL_B64="aHR0cHM6Ly93d3cueW91dHViZS5jb20vQFBPTEVTTklFU09WRVRJMTI="
TPROXY_REF="52a5feb7fac38f68da5afef9cedd9b3bfc8473ca"

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

find_mtproxy_port() {
    if ss -lnt 2>/dev/null | grep -Eq ':2398\b'; then
        printf '2398'
        return 0
    fi
    return 1
}

wait_for_mtproxy() {
    local port=""
    for _ in $(seq 1 30); do
        if systemctl is-active --quiet mtproxy; then
            port="$(find_mtproxy_port || true)"
            if [[ -n "$port" ]]; then
                printf '%s' "$port"
                return 0
            fi
        fi
        sleep 1
    done
    return 1
}

repair_mtproxy() {
    echo "      MTProxy recovery: fixing permissions and restarting..."
    fix_mtproxy_permissions || true
    systemctl reset-failed mtproxy.service 2>/dev/null || true
    restart_service_reliably mtproxy.service 3 || true
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
    rm -f /etc/web-proxy-panel/install-credentials 2>/dev/null || true
    show_failure
    exit "$code"
}
trap on_error ERR

echo "Configuring WEB PANEL PROXY V 2.1.0..."

[[ $EUID -eq 0 ]] || die "Run this installer as root."
[[ "$(uname -m)" == "x86_64" ]] || die "x86_64 is required."

EXISTING_CADDY_CONF="/etc/systemd/system/caddy.service.d/tproxy.conf"
EXISTING_DOMAIN="$(sed -n 's/^Environment=TPROXY_HOSTNAME=//p' "$EXISTING_CADDY_CONF" 2>/dev/null | head -n1 || true)"
if ! valid_domain "$EXISTING_DOMAIN" && [[ -s /etc/tproxy-server/config.json ]]; then
    EXISTING_DOMAIN="$(sed -n 's/.*"public_hostname"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' /etc/tproxy-server/config.json | head -n1)"
fi
EXISTING_EMAIL="$(sed -n 's/^Environment=ACME_EMAIL=//p' "$EXISTING_CADDY_CONF" 2>/dev/null | head -n1 || true)"

if command -v caddy >/dev/null 2>&1; then
    REUSE_CADDY=1
    CADDY_BIN="$(command -v caddy)"
    if systemctl list-unit-files --no-legend caddy.service 2>/dev/null | grep -q '^caddy\.service'; then
        CADDY_SERVICE_EXISTS=1
    fi
    if [[ "$(cat /etc/web-proxy-panel/caddy-owned 2>/dev/null || true)" == "WEB_PANEL_PROXY_V2_CADDY_OWNER" ]]; then
        CADDY_MODE="owner"
    else
        CADDY_MODE="shared"
    fi
    echo "      Existing Caddy detected; its binary, service and unrelated site blocks will be preserved."
fi

if valid_domain "$EXISTING_DOMAIN"; then
    DOMAIN="${EXISTING_DOMAIN,,}"
    echo "      Existing WEB Proxy domain detected: $DOMAIN"
else
    while true; do
        echo
        read -r -p "Domain (example: proxy.example.com): " DOMAIN
        DOMAIN="$(trim "$DOMAIN")"
        DOMAIN="${DOMAIN,,}"
        valid_domain "$DOMAIN" && break
        echo "Invalid domain. Example: proxy.example.com"
    done
fi

if valid_email "$EXISTING_EMAIL"; then
    EMAIL="$EXISTING_EMAIL"
    echo "      Existing ACME email detected; reusing it."
else
    while true; do
        echo
        read -r -p "ACME email (example: admin@example.com): " EMAIL
        EMAIL="$(trim "$EMAIL")"
        valid_email "$EMAIL" && break
        echo "Invalid email. Example: admin@example.com"
    done
fi

install -d -m 0700 /etc/web-proxy-panel
if [[ -s /var/lib/tproxy-panel/data.json ]] &&
   [[ -f /etc/systemd/system/tproxy-panel.service ]]; then
    echo "      Existing panel account detected; login and password will be retained."
    rm -f /etc/web-proxy-panel/install-credentials
else
    echo
    read -r -p "Panel administrator login [admin]: " PANEL_ADMIN
    PANEL_ADMIN="${PANEL_ADMIN:-admin}"
    while true; do
        read -r -s -p "Panel administrator password: " PANEL_PASS
        echo
        if [[ ${#PANEL_PASS} -lt 8 ]]; then
            echo "Password must contain at least 8 characters."
            continue
        fi
        break
    done
    printf '%s\n%s\n' "$PANEL_ADMIN" "$PANEL_PASS" > /etc/web-proxy-panel/install-credentials
    chmod 0600 /etc/web-proxy-panel/install-credentials
    unset PANEL_PASS
fi

echo
echo "      Preparing primary secret..."
command -v openssl >/dev/null 2>&1 || {
    export DEBIAN_FRONTEND=noninteractive
    apt-get -o DPkg::Lock::Timeout=600 update
    apt-get -o DPkg::Lock::Timeout=600 install -y --no-install-recommends openssl
}
SECRET="$(cat /etc/web-proxy-panel/primary-secret 2>/dev/null || true)"
if ! valid_secret "$SECRET" && [[ -s /etc/tproxy-server/profiles.json ]]; then
    SECRET="$(sed -n 's/.*"secret"[[:space:]]*:[[:space:]]*"\([0-9a-f]*\)".*/\1/p' /etc/tproxy-server/profiles.json | head -n1)"
fi
if ! valid_secret "$SECRET" && [[ -s /etc/mtproxy/mtproxy.env ]]; then
    SECRET="$(sed -n 's/^MTPROXY_SECRET=//p' /etc/mtproxy/mtproxy.env | head -n1)"
fi
if ! valid_secret "$SECRET"; then
    SECRET="$(systemctl cat mtproxy.service 2>/dev/null |
        sed -n 's/.*[[:space:]]-S[[:space:]]\([0-9a-f]*\).*/\1/p' | head -n1 || true)"
fi
if valid_secret "$SECRET"; then
    echo "      Existing primary secret detected; reusing it."
else
    SECRET="$(openssl rand -hex 16)"
    echo "      New primary secret generated."
fi
valid_secret "$SECRET" || die "Secret generation failed."

echo
echo "[1/10] Checking system..."
. /etc/os-release
case "${ID:-}" in
    ubuntu)
        dpkg --compare-versions "${VERSION_ID:-0}" ge "22.04" ||
            die "Ubuntu 22.04 or newer is required."
        echo "      Ubuntu ${VERSION_ID} / x86_64"
        ;;
    debian)
        dpkg --compare-versions "${VERSION_ID:-0}" ge "12" ||
            die "Debian 12 or newer is required."
        echo "      Debian ${VERSION_ID} / x86_64"
        ;;
    *)
        die "Supported systems: Ubuntu 22.04+ or Debian 12+ (found ${ID:-unknown})."
        ;;
esac

echo
echo "[2/10] Installing dependencies..."
export DEBIAN_FRONTEND=noninteractive
apt-get -o DPkg::Lock::Timeout=600 update
apt-get -o DPkg::Lock::Timeout=600 install -y --no-install-recommends \
    ca-certificates curl git openssl dnsutils nftables \
    build-essential libssl-dev util-linux zlib1g-dev
echo "      OK"

# Direct MTProto must bypass any HTTPS/CDN proxy in front of the site domain.
# Store the public IPv4 for MTProto links; WEB Proxy continues to use DOMAIN.
MTPROTO_HOST="$(curl -4fsS --max-time 10 https://api.ipify.org 2>/dev/null || true)"
if [[ ! "$MTPROTO_HOST" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    MTPROTO_HOST="$DOMAIN"
fi
install -d -m 0700 /etc/web-proxy-panel
printf '%s\n' "$MTPROTO_HOST" > /etc/web-proxy-panel/mtproto-host
chmod 0600 /etc/web-proxy-panel/mtproto-host

echo
echo "[3/10] Checking ports..."
echo "      :80 is not required by WEB PANEL PROXY and may be used by another application."
check_install_port 443 caddy
if EXISTING_MTPROXY_PORT="$(find_mtproxy_port 2>/dev/null)"; then
    MT_PORT="$EXISTING_MTPROXY_PORT"
    echo "      Existing MTProxy detected on :${MT_PORT}; continuing."
else
    echo "      MTProxy is not running yet; its port will be detected after startup."
fi
check_install_port 8080 tproxy-server
check_install_port 8081 tproxy-server

REUSE_EXISTING_HTTPS=0

if [[ "$REUSE_CADDY" == "1" ]] &&
   [[ "$EXISTING_DOMAIN" == "$DOMAIN" ]] &&
   curl -fsSI --max-time 10 "https://${DOMAIN}/" >/dev/null 2>&1; then
        REUSE_EXISTING_HTTPS=1
        echo "      Existing HTTPS is already working; certificate/configuration will be reused."
elif [[ "$REUSE_CADDY" == "1" ]]; then
    echo "      Existing Caddy will be extended with the WEB Proxy site block."
fi

echo
echo "[4/10] Checking DNS..."
if [[ "$REUSE_EXISTING_HTTPS" == "1" ]]; then
    DNS_IP="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}')"
    [[ -n "$DNS_IP" ]] || die "Existing HTTPS works, but DNS lookup failed for $DOMAIN."
    echo "      Existing HTTPS verified: $DOMAIN -> $DNS_IP"
else
    DNS_IP="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}')"
    [[ -n "$DNS_IP" ]] || die "No IPv4 A record found for $DOMAIN."

    VPS_IP="$(curl -4fsS --max-time 10 https://api.ipify.org || true)"
    if [[ -n "$VPS_IP" && "$DNS_IP" != "$VPS_IP" ]]; then
        echo "      DNS: $DNS_IP"
        echo "      VPS: $VPS_IP"
        die "DNS does not point to this VPS."
    fi
    echo "      $DOMAIN -> $DNS_IP"
fi

echo "[5/10] Creating public site..."
if [[ -s "$SITE_TARGET/index.html" ]]; then
    PRESERVE_SITE=1
    echo "      Existing public site detected; preserving its HTML, CSS and JavaScript."
else
    rm -rf "$SITE_INPUT"
    mkdir -p "$SITE_INPUT"

cat > "$SITE_INPUT/index.html" <<'EOF'
<!doctype html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#05070d">
<title>Система подключения</title>
<style>
*{box-sizing:border-box}
html,body{margin:0;min-height:100%;font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
body{min-height:100vh;display:grid;place-items:center;overflow:hidden;color:#f5f7ff;background:#05070d}
.bg{position:fixed;inset:0;overflow:hidden;background:
radial-gradient(circle at 15% 20%,rgba(72,190,255,.16),transparent 30%),
radial-gradient(circle at 85% 80%,rgba(125,92,255,.18),transparent 32%),
linear-gradient(135deg,#04060b,#090e18 50%,#05070d)}
.bg:before{content:"";position:absolute;inset:-45%;
background:conic-gradient(from 90deg,transparent,rgba(75,210,255,.09),transparent 30%,rgba(139,92,246,.09),transparent 65%);
animation:spin 18s linear infinite}
.bg:after{content:"";position:absolute;inset:0;background-image:radial-gradient(rgba(255,255,255,.35) 1px,transparent 1px);background-size:42px 42px;opacity:.12;animation:drift 20s linear infinite}
.card{position:relative;width:min(680px,calc(100% - 32px));padding:44px 34px 30px;text-align:center;border:1px solid rgba(255,255,255,.11);border-radius:28px;background:rgba(10,14,24,.72);backdrop-filter:blur(22px);box-shadow:0 30px 90px rgba(0,0,0,.48),inset 0 1px rgba(255,255,255,.07);animation:enter .8s cubic-bezier(.2,.8,.2,1) both}
.logo{width:78px;height:78px;margin:0 auto 22px;border-radius:24px;display:grid;place-items:center;font-size:34px;background:linear-gradient(135deg,#56d7ff,#7568ff);box-shadow:0 0 45px rgba(86,180,255,.3);animation:float 4s ease-in-out infinite}
h1{margin:0;font-size:clamp(30px,6vw,48px);letter-spacing:-1.8px}
p{margin:14px auto 0;max-width:520px;color:#98a3b7;font-size:16px;line-height:1.65}
.status{display:inline-flex;align-items:center;gap:9px;margin-top:24px;padding:10px 15px;border:1px solid rgba(255,255,255,.08);border-radius:999px;background:rgba(255,255,255,.035);color:#cdd5e5;font-size:14px}
.dot{width:8px;height:8px;border-radius:50%;background:#63f5b0;box-shadow:0 0 15px #63f5b0;animation:pulse 1.7s infinite}
.line{height:1px;margin:28px 0 20px;background:linear-gradient(90deg,transparent,rgba(255,255,255,.12),transparent)}
.footer{font-size:12px;color:#596579}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes drift{to{transform:translate3d(42px,42px,0)}}
@keyframes enter{from{opacity:0;transform:translateY(24px) scale(.97)}to{opacity:1;transform:none}}
@keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-8px)}}
@keyframes pulse{0%,100%{transform:scale(1);opacity:1}50%{transform:scale(1.35);opacity:.65}}
@media(max-width:520px){.card{padding:34px 22px 26px;border-radius:22px}.logo{width:68px;height:68px}}
</style>
</head>
<body>
<div class="bg"></div>
<main class="card">
  <div class="logo">⚡</div>
  <h1>Система подключения</h1>
  <p>Безопасный доступ активен. Соединение проверяется автоматически.</p>
  <div class="status"><span class="dot"></span> Система работает</div>
  <div class="line"></div>
  <div class="footer">Защищённое соединение • Автоматическая проверка</div>
</main>
</body>
</html>
EOF

# The relay's public-site policy deliberately blocks inline style/script tags.
# Keep the bundled first-run page compliant too, otherwise it would render as
# unstyled text before the owner opens the panel and selects a preset.
python3 - "$SITE_INPUT/index.html" "$SITE_INPUT/styles.css" <<'PY'
import re, sys
index, stylesheet = sys.argv[1:]
s = open(index, encoding="utf-8").read()
m = re.search(r"<style\b[^>]*>(.*?)</style\s*>", s, flags=re.I | re.S)
if not m:
    raise SystemExit("Default public page has no style block")
css = "/* WEB PANEL PROXY default public CSS */\n" + m.group(1).strip() + "\n"
s = s[:m.start()] + '<link rel="stylesheet" href="/styles.css">' + s[m.end():]
open(stylesheet, "w", encoding="utf-8").write(css)
open(index, "w", encoding="utf-8").write(s)
PY

chmod 0755 "$SITE_INPUT"
chmod 0644 "$SITE_INPUT/index.html" "$SITE_INPUT/styles.css"
echo "      OK"
fi

echo
echo "[6/10] Installing Telegram Web Proxy components..."


if [[ -x /opt/MTProxy/objs/bin/mtproto-proxy ]] &&
   systemctl list-unit-files mtproxy.service >/dev/null 2>&1; then
    if EXISTING_MTPROXY_PORT="$(find_mtproxy_port 2>/dev/null)"; then
        MT_PORT="$EXISTING_MTPROXY_PORT"
        REUSE_MT=1
        echo "      Existing MTProxy detected on :${MT_PORT}; reusing it."
    fi
fi

if [[ -x /usr/local/bin/tproxy-server ]] &&
   systemctl list-unit-files tproxy-server.service >/dev/null 2>&1 &&
   port_has_expected_process 8080 tproxy-server &&
   port_has_expected_process 8081 tproxy-server; then
    REUSE_RELAY=1
    echo "      Existing tproxy-server detected; reusing it."
fi

if [[ ! -d "$REPO_DIR/.git" ]]; then
    rm -rf "$REPO_DIR"
    mkdir -p "$REPO_DIR"
    git -C "$REPO_DIR" init
    git -C "$REPO_DIR" remote add origin https://github.com/telegramdesktop/tproxy-server.git
fi
echo "      Fetching pinned tproxy-server release source..."
git -C "$REPO_DIR" fetch --depth 1 origin "$TPROXY_REF"
git -C "$REPO_DIR" checkout --detach --force FETCH_HEAD
[[ "$(git -C "$REPO_DIR" rev-parse HEAD)" == "$TPROXY_REF" ]] ||
    die "Pinned tproxy-server source verification failed."
cd "$REPO_DIR"

if [[ "$REUSE_CADDY" == "1" ]]; then
    echo "      Caddy already installed; keeping its binary and systemd service."
else
    echo "      Installing Caddy..."
    caddy_version="2.11.4"
    caddy_sha512="8220d1f013b6f27510247b2360c9e0ca9f018feebd82515f07635318b34ff9777ccc8fd0b6e6f2486ce3a33fe389fbb7db12d05baa474f4587509fb4f5ebf1c9"

    caddy_archive="$(mktemp /tmp/caddy-linux-amd64.XXXXXX.tar.gz)"
    caddy_directory="$(mktemp -d /tmp/caddy-linux-amd64.XXXXXX)"

    curl --fail --silent --show-error --location \
        --proto '=https' --proto-redir '=https' --tlsv1.2 \
        --output "$caddy_archive" \
        "https://github.com/caddyserver/caddy/releases/download/v${caddy_version}/caddy_${caddy_version}_linux_amd64.tar.gz"

    test "$(sha512sum "$caddy_archive" | awk '{print $1}')" = "$caddy_sha512" ||
        die "Caddy checksum verification failed."

    tar -C "$caddy_directory" -xzf "$caddy_archive"
    install -m 0755 "$caddy_directory/caddy" /usr/local/bin/caddy
    rm -f "$caddy_archive"
    rm -rf "$caddy_directory"

    if ! id caddy >/dev/null 2>&1; then
        useradd --system --home /var/lib/caddy --shell /usr/sbin/nologin caddy
    fi
    install -d -o root -g caddy -m 0750 /etc/caddy
    install -d -o caddy -g caddy -m 0750 /var/lib/caddy
    CADDY_BIN="/usr/local/bin/caddy"
    CADDY_MODE="owner"
fi

[[ -n "$CADDY_BIN" && -x "$CADDY_BIN" ]] || die "Caddy executable was not found."
if ! id caddy >/dev/null 2>&1; then
    useradd --system --home /var/lib/caddy --shell /usr/sbin/nologin caddy
fi
install -d -o root -g caddy -m 0750 /etc/caddy
install -d -o caddy -g caddy -m 0750 /var/lib/caddy

echo "      Installing official MTProxy..."
if [[ "$REUSE_MT" != "1" ]]; then
    # The pinned upstream installer invokes apt-get without a lock timeout.
    # Ubuntu may start unattended-upgrades between our dependency step and
    # this call, so add the same bounded wait used by the main installer.
    MT_INSTALLER="$REPO_DIR/deploy/install-mtproxy.sh"
    grep -Fxq 'apt-get update' "$MT_INSTALLER" ||
        die "Unexpected pinned MTProxy installer: apt update command not found."
    grep -Fxq 'apt-get install -y --no-install-recommends ca-certificates curl build-essential libssl-dev util-linux zlib1g-dev' "$MT_INSTALLER" ||
        die "Unexpected pinned MTProxy installer: apt install command not found."
    sed -i \
        -e 's/^apt-get update$/apt-get -o DPkg::Lock::Timeout=600 update/' \
        -e 's/^apt-get install /apt-get -o DPkg::Lock::Timeout=600 install /' \
        "$MT_INSTALLER"
    "$MT_INSTALLER"
else
    echo "      MTProxy installation skipped; existing instance is already listening on :2398."
fi

if ! id tproxy >/dev/null 2>&1; then
    useradd --system --home /nonexistent --shell /usr/sbin/nologin tproxy
fi

# The official deployment leaves the MTProxy build tree root-only.
# Make the complete executable path traversable before systemd starts it.
fix_mtproxy_permissions() {
    if ! id mtproxy >/dev/null 2>&1; then
        echo "      MTProxy user missing; creating it..."
        useradd --system             --home /nonexistent             --no-create-home             --shell /usr/sbin/nologin             mtproxy
    fi

    chmod 0755 /opt/MTProxy 2>/dev/null || true
    chmod 0755 /opt/MTProxy/objs 2>/dev/null || true
    chmod 0755 /opt/MTProxy/objs/bin 2>/dev/null || true
    chmod 0755 /opt/MTProxy/objs/bin/mtproto-proxy 2>/dev/null || true

    chown root:root /opt/MTProxy/objs/bin/mtproto-proxy 2>/dev/null || true
    chmod 0755 /opt/MTProxy/objs/bin/mtproto-proxy

    # Verify path traversal and execution as the service user.
    if ! runuser -u mtproxy -- /bin/test -x /opt/MTProxy/objs/bin/mtproto-proxy; then
        echo "      MTProxy permission check failed; attempting automatic repair..."
        namei -l /opt/MTProxy/objs/bin/mtproto-proxy || true

        chmod 0755 /opt /opt/MTProxy /opt/MTProxy/objs /opt/MTProxy/objs/bin
        chown -R root:root /opt/MTProxy/objs/bin

        runuser -u mtproxy -- /bin/test -x /opt/MTProxy/objs/bin/mtproto-proxy ||
            die "MTProxy binary is not executable by the mtproxy user after automatic repair."
    fi
}

echo "      Installing Go relay..."
if [[ "$REUSE_RELAY" == "1" ]]; then
    echo "      Healthy tproxy-server already exists; binary build skipped."
else
go_version="1.26.5"
go_sha256="5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053"

if [[ -x "/opt/go${go_version}/bin/go" ]]; then
    go_binary="/opt/go${go_version}/bin/go"
else
    go_archive="$(mktemp /tmp/go-linux-amd64.XXXXXX.tar.gz)"
    go_directory="$(mktemp -d /tmp/go-linux-amd64.XXXXXX)"

    curl --fail --silent --show-error --location \
        --proto '=https' --proto-redir '=https' --tlsv1.2 \
        --output "$go_archive" \
        "https://go.dev/dl/go${go_version}.linux-amd64.tar.gz"

    test "$(sha256sum "$go_archive" | awk '{print $1}')" = "$go_sha256" ||
        die "Go checksum verification failed."

    tar -C "$go_directory" -xzf "$go_archive"
    mv "$go_directory/go" "/opt/go${go_version}"
    rm -f "$go_archive"
    rm -rf "$go_directory"
    go_binary="/opt/go${go_version}/bin/go"
fi

echo "      Building current relay..."
(
    cd "$REPO_DIR"
    "$go_binary" build -trimpath -ldflags='-s -w' \
        -o /usr/local/bin/tproxy-server ./cmd/tproxy-server
)
chown root:root /usr/local/bin/tproxy-server
chmod 0755 /usr/local/bin/tproxy-server
fi

echo "      Preparing site..."
install -d -o root -g tproxy -m 0750 "$SITE_TARGET"
if [[ "$PRESERVE_SITE" != "1" ]]; then
    rm -rf "$SITE_TARGET"/*
    cp -a "$SITE_INPUT/." "$SITE_TARGET/"
fi
chown -R root:tproxy "$SITE_TARGET"
find "$SITE_TARGET" -type d -exec chmod 0750 {} +
find "$SITE_TARGET" -type f -exec chmod 0640 {} +

runuser -u tproxy -- test -x "$SITE_TARGET" ||
    die "tproxy user cannot traverse public site."
runuser -u tproxy -- test -r "$SITE_TARGET/index.html" ||
    die "tproxy user cannot read public site index.html."

echo "      Preparing configuration..."
install -d -o root -g tproxy -m 0750 /etc/tproxy-server

cat > /etc/tproxy-server/config.json <<EOF
{
  "public_hostname": "$DOMAIN",
  "listen": "127.0.0.1:8080",
  "admin_listen": "127.0.0.1:8081",
  "public_dir": "/srv/tproxy-site",
  "profiles_file": "/run/credentials/tproxy-server.service/profiles.json"
}
EOF

cat > /etc/tproxy-server/profiles.json <<EOF
{"profiles":[{"name":"default","secret":"$SECRET","backend":"127.0.0.1:$MT_PORT","carrier_mode":"https"}]}
EOF
install -d -m 0700 /etc/web-proxy-panel
printf '%s\n' "$SECRET" > /etc/web-proxy-panel/primary-secret
if [[ "$CADDY_MODE" == "owner" ]]; then
    printf '%s\n' 'WEB_PANEL_PROXY_V2_CADDY_OWNER' > /etc/web-proxy-panel/caddy-owned
else
    printf '%s\n' 'WEB_PANEL_PROXY_V2_CADDY_SHARED' > /etc/web-proxy-panel/caddy-owned
fi
printf '%s\n' '2.1.0' > /etc/web-proxy-panel/version
chmod 0600 /etc/web-proxy-panel/primary-secret
chmod 0600 /etc/web-proxy-panel/caddy-owned
chmod 0600 /etc/web-proxy-panel/version

chown root:tproxy /etc/tproxy-server/config.json /etc/tproxy-server/profiles.json
chmod 0640 /etc/tproxy-server/config.json
chmod 0400 /etc/tproxy-server/profiles.json

backend_secret="$SECRET"
if [[ "$backend_secret" == dd* ]] && [[ ${#backend_secret} -eq 34 ]]; then
    backend_secret="${backend_secret:2}"
fi

cat > /etc/mtproxy/mtproxy.env <<EOF
MTPROXY_SECRET=$backend_secret
MTPROXY_WORKERS=1
MTPROXY_MAX_CONNECTIONS=4096
EOF

chown root:mtproxy /etc/mtproxy/mtproxy.env
chmod 0640 /etc/mtproxy/mtproxy.env

echo "      Installing service files..."
CANONICAL_CADDY="$(mktemp /tmp/web-panel-proxy-caddy.XXXXXX)"
install -m 0600 "$REPO_DIR/deploy/Caddyfile" "$CANONICAL_CADDY"
sed -i \
    -e "s#{\$TPROXY_HOSTNAME}#$DOMAIN#g" \
    -e "s#{\$ACME_EMAIL}#$EMAIL#g" \
    "$CANONICAL_CADDY"

if [[ "$REUSE_CADDY" == "1" ]]; then
    # Preserve global options and every unrelated site. Replace only an exact
    # block for this domain, or append it when Caddy belonged to another app.
    install -d -m 0755 /etc/caddy
    if [[ ! -e /etc/caddy/Caddyfile ]]; then
        install -o root -g caddy -m 0640 /dev/null /etc/caddy/Caddyfile
    fi
    cp -a /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.before-web-panel-proxy"
    python3 - /etc/caddy/Caddyfile "$CANONICAL_CADDY" "$DOMAIN" <<'PY'
import re,sys
target,canonical,domain=sys.argv[1:]
existing=open(target,encoding="utf-8").read()
source=open(canonical,encoding="utf-8").read()

def block(text, name):
    lines=text.splitlines(keepends=True)
    pattern=re.compile(r"^\s*"+re.escape(name)+r"\s*\{\s*$")
    start=next((i for i,line in enumerate(lines) if pattern.match(line)),None)
    if start is None: return None,None,None
    depth=0
    for i in range(start,len(lines)):
        depth+=lines[i].count("{")-lines[i].count("}")
        if depth==0:
            return lines,start,i
    raise SystemExit("Unclosed Caddy site block for "+name)

canonical_lines,canonical_start,canonical_end=block(source,domain)
if canonical_lines is None:
    raise SystemExit("Canonical Caddy site block was not found")
site="".join(canonical_lines[canonical_start:canonical_end+1]).strip()+"\n"
lines,start,end=block(existing,domain)
if lines is not None:
    existing="".join(lines[:start]+lines[end+1:]).rstrip()+"\n"
open(target,"w",encoding="utf-8").write(existing.rstrip()+"\n\n"+site)
PY
    if [[ "$CADDY_SERVICE_EXISTS" != 1 ]]; then
        install -m 0644 "$REPO_DIR/deploy/caddy.service" /etc/systemd/system/caddy.service
        sed -i "s#^ExecStart=.*#ExecStart=$CADDY_BIN run --environ --config /etc/caddy/Caddyfile#" /etc/systemd/system/caddy.service
        CADDY_SERVICE_EXISTS=1
        echo "      Missing caddy.service installed for the existing Caddy binary."
    fi
    echo "      Existing Caddyfile preserved; WEB Proxy domain block applied."
else
    install -o root -g caddy -m 0640 "$CANONICAL_CADDY" /etc/caddy/Caddyfile
    install -m 0644 "$REPO_DIR/deploy/caddy.service" /etc/systemd/system/caddy.service
fi
rm -f "$CANONICAL_CADDY"

# Keep TCP/80 free. Automatic certificates stay enabled, but the permanent
# HTTP redirect listener is disabled and the WPP domain uses ACME TLS-ALPN-01
# on TCP/443 instead of HTTP-01 on TCP/80.
python3 - /etc/caddy/Caddyfile "$DOMAIN" <<'PY'
import re, sys
from pathlib import Path

path, domain = sys.argv[1:]
text = Path(path).read_text(encoding="utf-8")
text = re.sub(
    r'\n?\s*# WPP TLS WITHOUT PORT 80 BEGIN\n.*?\n\s*# WPP TLS WITHOUT PORT 80 END\n?',
    '\n', text, flags=re.S,
)

def block_end(lines, start):
    depth = 0
    for index in range(start, len(lines)):
        depth += lines[index].count("{") - lines[index].count("}")
        if depth == 0:
            return index
    raise SystemExit("Unclosed Caddy block")

lines = text.splitlines(keepends=True)
global_start = next((i for i, line in enumerate(lines) if line.strip() == "{"), None)
if global_start is None:
    lines[0:0] = ["{\n", "\tauto_https disable_redirects\n", "}\n", "\n"]
else:
    global_end = block_end(lines, global_start)
    body = [
        line for line in lines[global_start + 1:global_end]
        if not re.match(r'^\s*auto_https\b', line)
    ]
    lines[global_start + 1:global_end] = ["\tauto_https disable_redirects\n"] + body

site_pattern = re.compile(r"^\s*" + re.escape(domain) + r"\s*\{\s*$")
site_start = next((i for i, line in enumerate(lines) if site_pattern.match(line)), None)
if site_start is None:
    raise SystemExit("WEB PANEL PROXY Caddy site block was not found")
tls_block = [
    "\t# WPP TLS WITHOUT PORT 80 BEGIN\n",
    "\ttls {\n",
    "\t\tissuer acme {\n",
    "\t\t\tdisable_http_challenge\n",
    "\t\t}\n",
    "\t}\n",
    "\t# WPP TLS WITHOUT PORT 80 END\n",
    "\n",
]
lines[site_start + 1:site_start + 1] = tls_block
Path(path).write_text("".join(lines), encoding="utf-8")
PY
test -s /etc/caddy/Caddyfile || die "Caddyfile was not configured."

install -d -m 0755 /etc/systemd/system/caddy.service.d
cat > /etc/systemd/system/caddy.service.d/tproxy.conf <<EOF
[Service]
Environment=TPROXY_HOSTNAME=$DOMAIN
Environment=TPROXY_SITE_ROOT=/srv/tproxy-site
Environment=ACME_EMAIL=$EMAIL
ReadWritePaths=/etc/caddy
EOF

install -d -o caddy -g caddy -m 0750 /etc/caddy/caddy

install -m 0644 "$REPO_DIR/deploy/tproxy-server.service" /etc/systemd/system/tproxy-server.service
install -m 0644 "$REPO_DIR/deploy/mtproxy.service" /etc/systemd/system/mtproxy.service
# Enable local MTProxy HTTP statistics for dashboard traffic accounting.
# Keep the MTProxy statistics listener on localhost for the panel. The old
# empty second substitution reused the preceding pattern and removed -p 8888.
sed -i 's# -p [0-9][0-9]*# -p 8888#' /etc/systemd/system/mtproxy.service
install -m 0644 "$REPO_DIR/deploy/tproxy-firewall.service" /etc/systemd/system/tproxy-firewall.service
install -m 0644 "$REPO_DIR/deploy/refresh-mtproxy-config.service" /etc/systemd/system/refresh-mtproxy-config.service
install -m 0644 "$REPO_DIR/deploy/refresh-mtproxy-config.timer" /etc/systemd/system/refresh-mtproxy-config.timer

install -m 0644 "$REPO_DIR/deploy/firewall.nft" /etc/tproxy-server/firewall.nft
install -m 0755 "$REPO_DIR/deploy/refresh-mtproxy-config.sh" /usr/local/sbin/refresh-mtproxy-config


start_service_reliably() {
    local unit="$1"
    local tries="${2:-3}"

    systemctl daemon-reload
    systemctl reset-failed "$unit" 2>/dev/null || true

    for _ in $(seq 1 "$tries"); do
        if systemctl is-active --quiet "$unit"; then
            return 0
        fi

        systemctl start "$unit" 2>/dev/null || true

        if systemctl is-active --quiet "$unit"; then
            return 0
        fi

        sleep 2
    done

    return 1
}

restart_service_reliably() {
    local unit="$1"
    local tries="${2:-3}"

    # Never stop an already healthy unit merely to reapply the same state.
    # Only attempt a restart when the unit is inactive/failed.
    if systemctl is-active --quiet "$unit"; then
        return 0
    fi

    systemctl reset-failed "$unit" 2>/dev/null || true

    for _ in $(seq 1 "$tries"); do
        systemctl start "$unit" 2>/dev/null || true

        if systemctl is-active --quiet "$unit"; then
            return 0
        fi

        sleep 2
        systemctl reset-failed "$unit" 2>/dev/null || true
    done

    return 1
}

echo "      Preflight validation..."
fix_mtproxy_permissions
runuser -u tproxy -- test -r "$SITE_TARGET/index.html" ||
    die "Public site is not readable by tproxy."

/usr/local/bin/tproxy-server \
    -config /etc/tproxy-server/config.json \
    -profiles-file /etc/tproxy-server/profiles.json \
    -check

TPROXY_HOSTNAME="$DOMAIN" \
TPROXY_SITE_ROOT=/srv/tproxy-site \
ACME_EMAIL="$EMAIL" \
"$CADDY_BIN" validate \
    --config /etc/caddy/Caddyfile \
    --adapter caddyfile

systemctl daemon-reload

echo "      Starting firewall..."
systemctl enable tproxy-firewall.service 2>/dev/null || true
if ! start_service_reliably tproxy-firewall.service 3; then
    echo "      Firewall start failed; retrying after nftables cleanup..."
    nft delete table inet tproxy_backend 2>/dev/null || true
    start_service_reliably tproxy-firewall.service 3 ||
        die "tproxy-firewall could not be started."
fi

echo "      Checking MTProxy service overrides..."
rm -f /etc/systemd/system/mtproxy.service.d/web-proxy-panel.conf
rm -f /usr/local/sbin/web-proxy-panel-mtproxy
systemctl daemon-reload

echo "      Starting MTProxy..."
fix_mtproxy_permissions
systemctl enable mtproxy.service 2>/dev/null || true

MT_PORT="$(find_mtproxy_port 2>/dev/null || true)"

if [[ -n "$MT_PORT" ]]; then
    echo "      Existing MTProxy is already listening on :${MT_PORT}; keeping it running."
else
    if ! start_service_reliably mtproxy.service 3; then
        echo "      MTProxy did not start; attempting automatic recovery..."
        repair_mtproxy || true
    fi
    MT_PORT="$(wait_for_mtproxy || true)"
fi

if [[ -z "$MT_PORT" ]]; then
    echo "      MTProxy still has no detected listening port; attempting final recovery..."
    repair_mtproxy || true
    MT_PORT="$(wait_for_mtproxy || true)"
fi

if [[ -z "$MT_PORT" ]]; then
    echo
    echo "      MTProxy diagnostic:"
    systemctl --no-pager --full status mtproxy.service 2>/dev/null || true
    journalctl -u mtproxy -n 60 --no-pager 2>/dev/null || true
    die "MTProxy did not become ready on any listening port."
fi

echo "      MTProxy :${MT_PORT} OK"

if ! timeout 3 bash -c "</dev/tcp/127.0.0.1/${MT_PORT}" 2>/dev/null; then
    echo "      MTProxy TCP check failed on :${MT_PORT}; attempting recovery..."
    repair_mtproxy || true
    MT_PORT="$(wait_for_mtproxy || true)"
    [[ -n "$MT_PORT" ]] || die "MTProxy did not expose a usable TCP port."
fi


# Synchronize the relay backend with the port that MTProxy actually uses.
# Keep tproxy-server listen/admin_listen unchanged.
# The MTProxy backend port belongs only in profiles.json.
grep -Eq '"listen"[[:space:]]*:[[:space:]]*"127\.0\.0\.1:8080"' /etc/tproxy-server/config.json ||
    die "tproxy-server listen must remain 127.0.0.1:8080."
grep -Eq '"admin_listen"[[:space:]]*:[[:space:]]*"127\.0\.0\.1:8081"' /etc/tproxy-server/config.json ||
    die "tproxy-server admin_listen must remain 127.0.0.1:8081."
cat > /etc/tproxy-server/profiles.json <<EOF
{"profiles":[{"name":"default","secret":"$SECRET","backend":"127.0.0.1:$MT_PORT","carrier_mode":"https"}]}
EOF
chown root:tproxy /etc/tproxy-server/config.json /etc/tproxy-server/profiles.json
chmod 0640 /etc/tproxy-server/config.json
chmod 0400 /etc/tproxy-server/profiles.json

grep -q '"carrier_mode":"https"' /etc/tproxy-server/profiles.json ||
    die "WEB carrier profile was not configured correctly."

echo "      WEB carrier mode: https"

# Final relay config invariant: the two listener addresses must differ.
if ! grep -Eq '"listen"[[:space:]]*:[[:space:]]*"127\.0\.0\.1:8080"' /etc/tproxy-server/config.json ||
   ! grep -Eq '"admin_listen"[[:space:]]*:[[:space:]]*"127\.0\.0\.1:8081"' /etc/tproxy-server/config.json; then
    echo "      ERROR: invalid tproxy-server listener configuration:"
    sed -n '1,30p' /etc/tproxy-server/config.json >&2
    die "listen/admin_listen configuration is invalid."
fi

echo "      Starting relay..."
runuser -u tproxy -- test -r "$SITE_TARGET/index.html" ||
    die "tproxy user cannot read site before relay start."

systemctl enable tproxy-server.service 2>/dev/null || true
if systemctl is-active --quiet tproxy-server.service; then
    echo "      Existing relay is already active; keeping it running."
else
    if ! start_service_reliably tproxy-server.service 3; then
        echo "      Relay start failed; attempting automatic recovery..."
        restart_service_reliably tproxy-server.service 3 ||
            die "tproxy-server could not be started."
    fi
fi

RELAY_READY=0

check_relay() {
    local health ready
    if ! systemctl is-active --quiet tproxy-server.service; then
        return 1
    fi

    health="$(curl -fsS --max-time 2 http://127.0.0.1:8081/healthz 2>/dev/null || true)"
    ready="$(curl -fsS --max-time 2 http://127.0.0.1:8081/readyz 2>/dev/null || true)"

    [[ "$health" == "ok" && "$ready" == "ready" ]]
}

for _ in $(seq 1 30); do
    if check_relay; then
        RELAY_READY=1
        break
    fi
    sleep 1
done

if [[ "$RELAY_READY" != "1" ]]; then
    echo "      Relay not ready; collecting diagnostics and repairing..."

    echo "      --- relay status ---"
    systemctl --no-pager --full status tproxy-server.service 2>/dev/null || true

    echo "      --- relay log ---"
    journalctl -u tproxy-server -n 80 --no-pager 2>/dev/null || true

    echo "      --- healthz ---"
    curl -i --max-time 5 http://127.0.0.1:8081/healthz 2>/dev/null || true

    echo "      --- readyz ---"
    curl -i --max-time 5 http://127.0.0.1:8081/readyz 2>/dev/null || true

    echo "      --- backend sockets ---"
    ss -lntp 2>/dev/null | grep -E ':(2398|8888|8080|8081)\b' || true

    fix_mtproxy_permissions || true

    # Detect the actual MTProxy port again before rewriting the relay profile.
    NEW_MT_PORT="$(find_mtproxy_port 2>/dev/null || true)"
    if [[ -n "$NEW_MT_PORT" ]]; then
        MT_PORT="$NEW_MT_PORT"
    fi

    cat > /etc/tproxy-server/profiles.json <<EOF
{"profiles":[{"name":"default","secret":"$SECRET","backend":"127.0.0.1:$MT_PORT","carrier_mode":"https"}]}
EOF

    chown root:tproxy /etc/tproxy-server/profiles.json
    chmod 0400 /etc/tproxy-server/profiles.json

    # Verify the relay configuration before touching the service.
    if ! /usr/local/bin/tproxy-server \
        -config /etc/tproxy-server/config.json \
        -profiles-file /etc/tproxy-server/profiles.json \
        -check; then
        echo "      Relay config check failed; reloading profile permissions/configuration..."
        chown root:tproxy /etc/tproxy-server/config.json /etc/tproxy-server/profiles.json
        chmod 0640 /etc/tproxy-server/config.json
        chmod 0400 /etc/tproxy-server/profiles.json
    fi

    if ! systemctl is-active --quiet tproxy-server.service; then
        restart_service_reliably tproxy-server.service 3 || true
    fi

    for _ in $(seq 1 30); do
        if check_relay; then
            RELAY_READY=1
            break
        fi
        sleep 1
    done
fi

if [[ "$RELAY_READY" != "1" ]]; then
    echo
    echo "============================================================"
    echo "              RELAY НЕ ГОТОВ"
    echo "============================================================"
    echo
    echo "tproxy-server запущен, но не прошёл /healthz или /readyz."
    echo
    echo "Последнее состояние:"
    systemctl --no-pager --full status tproxy-server.service 2>/dev/null || true
    echo
    echo "Проверка /healthz:"
    curl -i --max-time 5 http://127.0.0.1:8081/healthz 2>/dev/null || true
    echo
    echo "Проверка /readyz:"
    curl -i --max-time 5 http://127.0.0.1:8081/readyz 2>/dev/null || true
    echo
    echo "============================================================"
    die "ПОПРОБУЙТЕ ЗАНОВО"
fi

echo "      Relay /healthz and /readyz OK"

echo "      Starting refresh timer..."
systemctl enable refresh-mtproxy-config.timer 2>/dev/null || true
systemctl start refresh-mtproxy-config.timer 2>/dev/null || true

echo "      Starting Caddy..."
systemctl enable caddy.service 2>/dev/null || true

if systemctl is-active --quiet caddy.service; then
    if systemctl reload caddy.service 2>/dev/null; then
        echo "      Active Caddy reloaded with the WEB Proxy route."
    else
        systemctl restart caddy.service
    fi
else
    if ! start_service_reliably caddy.service 3; then
        echo "      Caddy is not healthy; attempting clean recovery..."
        systemctl kill --kill-who=main --signal=SIGKILL caddy.service 2>/dev/null || true
        systemctl reset-failed caddy.service 2>/dev/null || true
        start_service_reliably caddy.service 3 ||
            die "Caddy could not be started."
    fi
fi

echo
echo "[9/10] Running health checks..."
curl -fsS --max-time 5 http://127.0.0.1:8081/healthz >/dev/null ||
    die "tproxy-server healthz failed."

echo "      healthz OK"

if ! systemctl is-active --quiet caddy.service; then
    echo "      Caddy is not active; attempting recovery..."
    start_service_reliably caddy.service 3 ||
        die "Caddy is not running."
fi

HTTPS_READY=0

if [[ "$REUSE_EXISTING_HTTPS" == "1" ]] &&
   curl -fsSI --max-time 10 "https://${DOMAIN}/" >/dev/null 2>&1; then
    HTTPS_READY=1
    echo "      Existing HTTPS certificate/config is already working."
else
    for _ in $(seq 1 120); do
        if curl -fsSI --max-time 5 "https://${DOMAIN}/" >/dev/null 2>&1; then
            HTTPS_READY=1
            break
        fi
        sleep 5
    done
fi

if [[ "$HTTPS_READY" != "1" ]]; then
    echo "      Caddy diagnostic:"
    journalctl -u caddy -n 60 --no-pager 2>/dev/null || true
    die "HTTPS did not become ready within 600 seconds. Check Caddy/ACME/DNS."
fi

echo "      HTTPS OK"
echo "      Backend MTProxy port: ${MT_PORT}"
echo "      Services: mtproxy=$(systemctl is-active mtproxy 2>/dev/null || true) relay=$(systemctl is-active tproxy-server 2>/dev/null || true) caddy=$(systemctl is-active caddy 2>/dev/null || true)"


echo
echo "[10/10] Checking persistence and ports..."
for unit in mtproxy tproxy-server caddy; do
    systemctl is-active --quiet "$unit" || {
        echo "      $unit is not active; attempting final start..."
        start_service_reliably "$unit" 3 ||
            die "$unit is not active."
    }
done

systemctl is-active --quiet tproxy-firewall || {
    echo "      tproxy-firewall is not active; attempting final start..."
    start_service_reliably tproxy-firewall.service 3 ||
        die "tproxy-firewall is not active."
}

systemctl enable refresh-mtproxy-config.timer 2>/dev/null || true
start_service_reliably refresh-mtproxy-config.timer 2 >/dev/null 2>&1 || true

runuser -u mtproxy -- test -x /opt/MTProxy/objs/bin/mtproto-proxy ||
    die "Final MTProxy permission check failed."

runuser -u tproxy -- test -r /srv/tproxy-site/index.html ||
    die "Final site permission check failed."

for p in "$MT_PORT" 8080 8081 443; do
    if ! ss -lnt | grep -Eq ":(${p})\b"; then
        echo "      Missing expected listening port: ${p}" >&2
        ss -lntp || true
        die "Expected port ${p} is not listening."
    fi
done

# Port 80 is intentionally outside WPP. Another application may listen there,
# but Caddy itself must not re-enable its automatic HTTP listener.
if ss -lntp 2>/dev/null | grep -E ':80[[:space:]]' | grep -q 'caddy'; then
    die "Caddy unexpectedly listens on port 80; the no-HTTP-listener policy was not applied."
fi

echo
echo "Core services configured successfully. Continuing to panel setup..."
