#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

VERSION="V 1.0"
REPO_DIR="/root/tproxy-server"
SITE_INPUT="/opt/tproxy-site"
SITE_TARGET="/srv/tproxy-site"
REUSE_MT=0
REUSE_RELAY=0
REUSE_CADDY=0
MT_PORT=2398
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

find_mtproxy_port() {
    local detected
    detected="$(ss -lntp 2>/dev/null |
        sed -n 's/.*:\([0-9]\+\)[[:space:]].*users:(("\mtproto-proxy".*/\1/p' |
        head -n1)"
    if [[ "$detected" =~ ^[0-9]+$ ]]; then
        printf '%s' "$detected"
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
    systemctl restart mtproxy.service
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

while true; do
    echo
    read -r -p "ACME email (example: admin@example.com): " EMAIL
    EMAIL="$(trim "$EMAIL")"
    valid_email "$EMAIL" && break
    echo "Invalid email. Example: admin@example.com"
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
if EXISTING_MTPROXY_PORT="$(find_mtproxy_port 2>/dev/null)"; then
    MT_PORT="$EXISTING_MTPROXY_PORT"
    echo "      Existing MTProxy detected on :${MT_PORT}; continuing."
else
    echo "      MTProxy is not running yet; its port will be detected after startup."
fi
check_install_port 8080 tproxy-server
check_install_port 8081 tproxy-server

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
        [[ -n "$EXISTING_EMAIL" ]] && EMAIL="$EXISTING_EMAIL"
        echo "      Existing HTTPS is already working; certificate/configuration will be reused."
    else
        echo "      Existing Caddy found, but HTTPS is not currently working."
        if [[ -n "$EXISTING_EMAIL" ]] && valid_email "$EXISTING_EMAIL"; then
            EMAIL="$EXISTING_EMAIL"
            echo "      Existing ACME email is valid; reusing it."
        else
            echo "      Existing ACME email is missing or invalid."
            while true; do
                read -r -p "ACME email (ASCII, e.g. admin@example.com): " EMAIL
                EMAIL="$(trim "$EMAIL")"
                valid_email "$EMAIL" && break
                echo "Invalid email. Use Latin characters, e.g. admin@example.com"
            done
        fi
    fi
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
rm -rf "$SITE_INPUT"
mkdir -p "$SITE_INPUT"

cat > "$SITE_INPUT/index.html" <<'EOF'
<!doctype html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">

    <title>Система подключения</title>
    <meta name="description" content="Страница загрузки">

    <style>
        :root {
            color-scheme: dark;
            --bg: #05070b;
            --bg-soft: #0a0e15;
            --card: rgba(15, 21, 31, 0.78);
            --line: rgba(255, 255, 255, 0.08);
            --text: #f5f7fb;
            --muted: #8490a3;
            --accent: #6ee7ff;
            --accent-2: #8b7cff;
            --success: #62f2ad;
        }

        * {
            box-sizing: border-box;
        }

        html,
        body {
            margin: 0;
            min-height: 100%;
        }

        body {
            min-height: 100vh;
            overflow-x: hidden;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 24px;
            color: var(--text);
            background:
                radial-gradient(circle at 20% 20%, rgba(110, 231, 255, 0.08), transparent 30%),
                radial-gradient(circle at 80% 80%, rgba(139, 124, 255, 0.10), transparent 30%),
                linear-gradient(135deg, var(--bg), var(--bg-soft));
            font-family:
                Inter,
                system-ui,
                -apple-system,
                BlinkMacSystemFont,
                "Segoe UI",
                sans-serif;
        }

        .grid {
            position: fixed;
            inset: 0;
            pointer-events: none;
            opacity: 0.18;
            background-image:
                linear-gradient(rgba(255,255,255,0.035) 1px, transparent 1px),
                linear-gradient(90deg, rgba(255,255,255,0.035) 1px, transparent 1px);
            background-size: 40px 40px;
            mask-image: linear-gradient(to bottom, transparent, black 20%, black 80%, transparent);
        }

        .glow {
            position: fixed;
            width: 280px;
            height: 280px;
            border-radius: 50%;
            filter: blur(80px);
            opacity: 0.15;
            pointer-events: none;
            animation: float 9s ease-in-out infinite;
        }

        .glow.one {
            top: -80px;
            left: -80px;
            background: var(--accent);
        }

        .glow.two {
            right: -90px;
            bottom: -80px;
            background: var(--accent-2);
            animation-delay: -4s;
        }

        @keyframes float {
            0%, 100% { transform: translate3d(0, 0, 0); }
            50% { transform: translate3d(20px, -20px, 0); }
        }

        .card {
            position: relative;
            width: min(100%, 650px);
            padding: 36px;
            border: 1px solid var(--line);
            border-radius: 28px;
            background: var(--card);
            backdrop-filter: blur(22px);
            box-shadow:
                0 30px 100px rgba(0, 0, 0, 0.45),
                inset 0 1px 0 rgba(255, 255, 255, 0.04);
            overflow: hidden;
        }

        .card::before {
            content: "";
            position: absolute;
            inset: 0;
            background: linear-gradient(135deg, rgba(255,255,255,0.05), transparent 40%);
            pointer-events: none;
        }

        .top {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 20px;
            margin-bottom: 34px;
        }

        .brand {
            display: flex;
            align-items: center;
            gap: 14px;
        }

        .logo {
            width: 48px;
            height: 48px;
            display: grid;
            place-items: center;
            border-radius: 15px;
            background: linear-gradient(
                135deg,
                rgba(110, 231, 255, 0.16),
                rgba(139, 124, 255, 0.16)
            );
            border: 1px solid var(--line);
            font-size: 21px;
            box-shadow: 0 0 30px rgba(110, 231, 255, 0.08);
        }

        .brand-title {
            font-size: 14px;
            font-weight: 700;
            letter-spacing: 0.08em;
            text-transform: uppercase;
        }

        .brand-subtitle {
            margin-top: 3px;
            font-size: 12px;
            color: var(--muted);
        }

        .status {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 8px 12px;
            border-radius: 999px;
            background: rgba(98, 242, 173, 0.06);
            border: 1px solid rgba(98, 242, 173, 0.12);
            color: #9bffca;
            font-size: 12px;
            font-weight: 600;
        }

        .status-dot {
            width: 7px;
            height: 7px;
            border-radius: 50%;
            background: var(--success);
            box-shadow: 0 0 12px var(--success);
            animation: pulse 1.8s ease-in-out infinite;
        }

        @keyframes pulse {
            0%, 100% { transform: scale(1); opacity: 0.75; }
            50% { transform: scale(1.35); opacity: 1; }
        }

        .content {
            text-align: center;
        }

        .icon {
            width: 82px;
            height: 82px;
            margin: 0 auto 22px;
            display: grid;
            place-items: center;
            border-radius: 24px;
            border: 1px solid var(--line);
            background: radial-gradient(
                circle,
                rgba(110, 231, 255, 0.10),
                rgba(139, 124, 255, 0.06)
            );
            font-size: 34px;
            box-shadow: 0 0 45px rgba(110, 231, 255, 0.06);
        }

        h1 {
            margin: 0;
            font-size: clamp(30px, 5vw, 46px);
            line-height: 1.05;
            letter-spacing: -0.04em;
        }

        .description {
            max-width: 480px;
            margin: 16px auto 0;
            color: var(--muted);
            font-size: 15px;
            line-height: 1.7;
        }

        .progress-wrap {
            margin-top: 34px;
        }

        .progress {
            position: relative;
            height: 10px;
            overflow: hidden;
            border-radius: 999px;
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid rgba(255,255,255,0.04);
        }

        .progress::before {
            content: "";
            position: absolute;
            inset: 0 auto 0 -35%;
            width: 35%;
            border-radius: inherit;
            background: linear-gradient(
                90deg,
                transparent,
                var(--accent),
                var(--accent-2),
                transparent
            );
            filter: blur(1px);
            animation: progress 1.7s ease-in-out infinite;
        }

        @keyframes progress {
            0% { transform: translateX(0); }
            100% { transform: translateX(380%); }
        }

        .loading-text {
            margin-top: 12px;
            color: #9ca8bb;
            font-size: 12px;
            letter-spacing: 0.08em;
            text-transform: uppercase;
        }

        .system {
            margin-top: 30px;
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 10px;
        }

        .system-item {
            padding: 14px 12px;
            text-align: left;
            border-radius: 14px;
            background: rgba(255, 255, 255, 0.025);
            border: 1px solid rgba(255, 255, 255, 0.05);
        }

        .system-label {
            color: var(--muted);
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.08em;
        }

        .system-value {
            margin-top: 5px;
            font-size: 13px;
            font-weight: 600;
            color: #e8edf5;
        }

        .system-value::before {
            content: "●";
            margin-right: 7px;
            color: var(--success);
            font-size: 8px;
            vertical-align: middle;
        }

        .terminal {
            margin-top: 22px;
            padding: 15px 16px;
            border-radius: 15px;
            background: #090c11;
            border: 1px solid rgba(255,255,255,0.05);
            text-align: left;
            font-family:
                ui-monospace,
                SFMono-Regular,
                Menlo,
                Monaco,
                Consolas,
                monospace;
            font-size: 12px;
            color: #8997aa;
        }

        .terminal-line {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 7px;
        }

        .terminal-line:last-child {
            margin-bottom: 0;
        }

        .prompt {
            color: var(--accent);
        }

        .ok {
            color: var(--success);
        }

        .cursor {
            display: inline-block;
            width: 7px;
            height: 14px;
            margin-left: 2px;
            background: #dbe4ef;
            vertical-align: middle;
            animation: blink 0.9s step-end infinite;
        }

        @keyframes blink {
            0%, 100% { opacity: 1; }
            50% { opacity: 0; }
        }

        .footer {
            margin-top: 22px;
            text-align: center;
            color: #5f6b7d;
            font-size: 11px;
        }

        @media (max-width: 640px) {
            .card {
                padding: 24px 18px;
                border-radius: 22px;
            }

            .top {
                align-items: flex-start;
                flex-direction: column;
            }

            .status {
                align-self: flex-start;
            }

            .system {
                grid-template-columns: 1fr;
            }
        }

        @media (prefers-reduced-motion: reduce) {
            *,
            *::before,
            *::after {
                animation: none !important;
                scroll-behavior: auto !important;
            }
        }
    </style>
</head>

<body>
    <div class="grid"></div>
    <div class="glow one"></div>
    <div class="glow two"></div>

    <main class="card">
        <header class="top">
            <div class="brand">
                <div class="logo">◈</div>

                <div>
                    <div class="brand-title">Connection Service</div>
                    <div class="brand-subtitle">Secure infrastructure</div>
                </div>
            </div>

            <div class="status">
                <span class="status-dot"></span>
                SYSTEM ONLINE
            </div>
        </header>

        <section class="content">
            <div class="icon">⚡</div>

            <h1>Подключение</h1>

            <p class="description">
                Система подготавливает соединение.
                Пожалуйста, оставайтесь на этой странице.
            </p>

            <div class="progress-wrap">
                <div class="progress"></div>

                <div class="loading-text">
                    Initializing secure connection
                </div>
            </div>
        </section>

        <section class="system">
            <div class="system-item">
                <div class="system-label">Network</div>
                <div class="system-value">Online</div>
            </div>

            <div class="system-item">
                <div class="system-label">Security</div>
                <div class="system-value">Protected</div>
            </div>

            <div class="system-item">
                <div class="system-label">Status</div>
                <div class="system-value">Ready</div>
            </div>
        </section>

        <section class="terminal">
            <div class="terminal-line">
                <span class="prompt">$</span>
                <span>initializing connection...</span>
                <span class="ok">OK</span>
            </div>

            <div class="terminal-line">
                <span class="prompt">$</span>
                <span>checking secure channel...</span>
                <span class="ok">OK</span>
            </div>

            <div class="terminal-line">
                <span class="prompt">$</span>
                <span>waiting for response...</span>
                <span class="cursor"></span>
            </div>
        </section>

        <footer class="footer">
            Please wait while the connection is being established.
        </footer>
    </main>
</body>
</html>
EOF

chmod 0755 "$SITE_INPUT"
chmod 0644 "$SITE_INPUT/index.html"
echo "      OK"

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

if [[ -x /usr/local/bin/caddy ]] &&
   systemctl list-unit-files caddy.service >/dev/null 2>&1 &&
   port_has_expected_process 80 caddy &&
   port_has_expected_process 443 caddy; then
    REUSE_CADDY=1
    echo "      Existing Caddy detected; reusing it."
fi
if [[ ! -d "$REPO_DIR/.git" ]]; then
    rm -rf "$REPO_DIR"
    git clone --depth 1 https://github.com/telegramdesktop/tproxy-server.git "$REPO_DIR"
else
    echo "      Existing tproxy-server source tree detected; reusing it."
fi
cd "$REPO_DIR"

if [[ "$REUSE_CADDY" == "1" ]]; then
    echo "      Caddy already installed; reusing it."
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

fi

echo "      Installing official MTProxy..."
if [[ "$REUSE_MT" != "1" ]]; then
    "$REPO_DIR/deploy/install-mtproxy.sh"
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

if [[ "$REUSE_RELAY" == "1" ]]; then
    echo "      Existing tproxy-server binary is already active; reusing it."
else
    echo "      Building relay..."
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
rm -rf "$SITE_TARGET"/*
cp -a "$SITE_INPUT/." "$SITE_TARGET/"
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
  "profiles_file": "/run/credentials/tproxy-server.service/profiles.json",
  "backend": "127.0.0.1:$MT_PORT"
}
EOF

cat > /etc/tproxy-server/profiles.json <<EOF
{"profiles":[{"name":"default","secret":"$SECRET","backend":"127.0.0.1:$MT_PORT"}]}
EOF

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
if [[ "$REUSE_CADDY" != "1" ]]; then
    install -m 0644 "$REPO_DIR/deploy/Caddyfile" /etc/caddy/Caddyfile
    install -m 0644 "$REPO_DIR/deploy/caddy.service" /etc/systemd/system/caddy.service
else
    echo "      Preserving existing Caddyfile and Caddy service."
fi

install -d -m 0755 /etc/systemd/system/caddy.service.d
if [[ "$REUSE_EXISTING_HTTPS" == "1" ]]; then
    echo "      Preserving existing working Caddy environment."
else
    cat > /etc/systemd/system/caddy.service.d/tproxy.conf <<EOF
[Service]
Environment=TPROXY_HOSTNAME=$DOMAIN
Environment=TPROXY_SITE_ROOT=/srv/tproxy-site
Environment=ACME_EMAIL=$EMAIL
ReadWritePaths=/etc/caddy
EOF
fi

install -d -o caddy -g caddy -m 0750 /etc/caddy/caddy

install -m 0644 "$REPO_DIR/deploy/tproxy-server.service" /etc/systemd/system/tproxy-server.service
install -m 0644 "$REPO_DIR/deploy/mtproxy.service" /etc/systemd/system/mtproxy.service
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

    systemctl reset-failed "$unit" 2>/dev/null || true

    for _ in $(seq 1 "$tries"); do
        # stop may time out for Caddy because it uses a graceful shutdown.
        # Never leave the unit dead just because the graceful stop exceeded TimeoutStopSec.
        systemctl stop "$unit" 2>/dev/null || true
        systemctl reset-failed "$unit" 2>/dev/null || true
        systemctl start "$unit" 2>/dev/null || true

        if systemctl is-active --quiet "$unit"; then
            return 0
        fi

        sleep 2
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
/usr/local/bin/caddy validate \
    --config /etc/caddy/Caddyfile \
    --adapter caddyfile

systemctl daemon-reload

echo "      Starting firewall..."
systemctl enable tproxy-firewall.service
if ! start_service_reliably tproxy-firewall.service 3; then
    echo "      Firewall start failed; retrying after nftables cleanup..."
    nft delete table inet tproxy_backend 2>/dev/null || true
    start_service_reliably tproxy-firewall.service 3 ||
        die "tproxy-firewall could not be started."
fi

echo "      Starting MTProxy..."
fix_mtproxy_permissions
systemctl enable mtproxy.service
systemctl reset-failed mtproxy.service 2>/dev/null || true

MT_PORT="$(find_mtproxy_port 2>/dev/null || true)"

if [[ -z "$MT_PORT" ]]; then
    restart_service_reliably mtproxy.service 3 || true
    MT_PORT="$(wait_for_mtproxy || true)"
fi

if [[ -z "$MT_PORT" ]]; then
    echo "      MTProxy did not expose a listening port; attempting automatic recovery..."
    repair_mtproxy
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

# Synchronize the relay backend with the port that MTProxy actually uses.
sed -i -E "s#127\.0\.0\.1:[0-9]+#127.0.0.1:${MT_PORT}#g" /etc/tproxy-server/config.json
cat > /etc/tproxy-server/profiles.json <<EOF
{"profiles":[{"name":"default","secret":"$SECRET","backend":"127.0.0.1:$MT_PORT"}]}
EOF
chown root:tproxy /etc/tproxy-server/config.json /etc/tproxy-server/profiles.json
chmod 0640 /etc/tproxy-server/config.json
chmod 0400 /etc/tproxy-server/profiles.json

echo "      Starting relay..."
runuser -u tproxy -- test -r "$SITE_TARGET/index.html" ||
    die "tproxy user cannot read site before relay start."

systemctl enable tproxy-server.service
if ! start_service_reliably tproxy-server.service 3; then
    echo "      Relay start failed; attempting automatic recovery..."
    systemctl reset-failed tproxy-server.service 2>/dev/null || true
    restart_service_reliably tproxy-server.service 3 ||
        die "tproxy-server could not be started."
fi

RELAY_READY=0
for _ in $(seq 1 30); do
    if systemctl is-active --quiet tproxy-server &&
       curl -fsS --max-time 2 http://127.0.0.1:8081/readyz >/dev/null 2>&1; then
        RELAY_READY=1
        break
    fi
    sleep 1
done

if [[ "$RELAY_READY" != "1" ]]; then
    echo "      Relay not ready; running automatic recovery..."
    fix_mtproxy_permissions || true
    chown -R root:tproxy "$SITE_TARGET"
    find "$SITE_TARGET" -type d -exec chmod 0750 {} +
    find "$SITE_TARGET" -type f -exec chmod 0640 {} +

    # Re-detect MTProxy in case its listening port changed after restart.
    NEW_MT_PORT="$(find_mtproxy_port 2>/dev/null || true)"
    if [[ -n "$NEW_MT_PORT" ]]; then
        MT_PORT="$NEW_MT_PORT"
    fi

    sed -i -E "s#127\.0\.0\.1:[0-9]+#127.0.0.1:${MT_PORT}#g" /etc/tproxy-server/config.json
    cat > /etc/tproxy-server/profiles.json <<EOF
{"profiles":[{"name":"default","secret":"$SECRET","backend":"127.0.0.1:$MT_PORT"}]}
EOF
    chown root:tproxy /etc/tproxy-server/config.json /etc/tproxy-server/profiles.json
    chmod 0640 /etc/tproxy-server/config.json
    chmod 0400 /etc/tproxy-server/profiles.json

    systemctl reset-failed mtproxy tproxy-server 2>/dev/null || true
    restart_service_reliably mtproxy.service 3 || true
    sleep 2

    NEW_MT_PORT="$(wait_for_mtproxy || true)"
    if [[ -n "$NEW_MT_PORT" ]]; then
        MT_PORT="$NEW_MT_PORT"
        sed -i -E "s#127\.0\.0\.1:[0-9]+#127.0.0.1:${MT_PORT}#g" /etc/tproxy-server/config.json
        sed -i -E "s#127\.0\.0\.1:[0-9]+#127.0.0.1:${MT_PORT}#g" /etc/tproxy-server/profiles.json
    fi

    restart_service_reliably tproxy-server.service 3 ||
        true

    for _ in $(seq 1 30); do
        if systemctl is-active --quiet tproxy-server &&
           curl -fsS --max-time 2 http://127.0.0.1:8081/readyz >/dev/null 2>&1; then
            RELAY_READY=1
            break
        fi
        sleep 1
    done
fi

[[ "$RELAY_READY" == "1" ]] || die "tproxy-server did not become ready."
echo "      Relay /readyz OK"

echo "      Starting refresh timer..."
systemctl enable --now refresh-mtproxy-config.timer

echo "      Starting Caddy..."
systemctl enable caddy.service

if [[ "$REUSE_EXISTING_HTTPS" == "1" ]] &&
   curl -fsSI --max-time 10 "https://${DOMAIN}/" >/dev/null 2>&1; then
    echo "      Existing HTTPS is healthy; keeping Caddy running."
else
    if ! restart_service_reliably caddy.service 3; then
        echo "      Caddy restart did not complete normally; forcing a clean service recovery..."
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

if [[ "$REUSE_EXISTING_HTTPS" == "1" ]]; then
    HTTPS_READY=1
    echo "      Existing HTTPS certificate/config is already working."
else
    for _ in $(seq 1 90); do
        if curl -fsSI --max-time 5 "https://${DOMAIN}/" >/dev/null 2>&1; then
            HTTPS_READY=1
            break
        fi
        sleep 2
    done
fi

if [[ "$HTTPS_READY" != "1" ]]; then
    echo "      Caddy diagnostic:"
    journalctl -u caddy -n 60 --no-pager 2>/dev/null || true
    die "HTTPS did not become ready within 180 seconds. Check Caddy/ACME/DNS."
fi

echo "      HTTPS OK"
echo "      Backend MTProxy port: ${MT_PORT}"


echo
echo "[10/10] Checking persistence and ports..."
for unit in mtproxy tproxy-server caddy; do
    systemctl is-active --quiet "$unit" || die "$unit is not active."
    systemctl is-enabled --quiet "$unit" || die "$unit is not enabled."
done

systemctl is-active --quiet tproxy-firewall || die "tproxy-firewall is not active."
systemctl is-enabled --quiet refresh-mtproxy-config.timer || die "refresh timer is not enabled."

runuser -u mtproxy -- test -x /opt/MTProxy/objs/bin/mtproto-proxy ||
    die "Final MTProxy permission check failed."

runuser -u tproxy -- test -r /srv/tproxy-site/index.html ||
    die "Final site permission check failed."

for p in 2398 8080 8081 80 443; do
    ss -lnt | grep -Eq ":(${p})\b" || die "Expected port ${p} is not listening."
done

TELEGRAM_SECRET="${SECRET#dd}"
CHANNEL_URL="$(printf "%s" "$CHANNEL_B64" | base64 -d)"

echo
echo "============================================================"
echo "             TELEGRAM WEB PROXY IS READY"
echo "============================================================"
echo
echo "Domain:"
echo "  https://${DOMAIN}/"
echo
echo "Secret:"
echo "  ${SECRET}"
echo
echo "Telegram Web Proxy:"
echo "  https://t.me/webproxy?server=${DOMAIN}&secret=${TELEGRAM_SECRET}"
echo
echo "YouTube channel:"
echo "  ${CHANNEL_URL}"
echo
echo "Status:"
echo "  HTTPS          OK"
echo "  MTProxy        ACTIVE"
echo "  Relay          READY"
echo "  Firewall       ACTIVE"
echo
echo "IMPORTANT: keep the secret private."
echo "============================================================"
