#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

RAW="https://raw.githubusercontent.com/POLESNIESOVETI12/webtelegram/main/panel"
TMP="$(mktemp -d /tmp/tproxy-panel.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

ETC="/etc/tproxy-panel"
SRC="/root/tproxy-server/panel"
BIN="/usr/local/bin/tproxy-panel"
UNIT="/etc/systemd/system/tproxy-panel.service"
ROUTE="/etc/caddy/panel-route.caddy"
CF="/etc/caddy/Caddyfile"
CD="/etc/systemd/system/caddy.service.d/tproxy.conf"

die(){ echo "ERROR: $*" >&2; exit 1; }

echo "============================================================"
echo "       TELEGRAM WEB PROXY MULTI-USER PANEL"
echo "============================================================"
echo

[[ $EUID -eq 0 ]] || die "Run as root."
[[ -f /etc/tproxy-server/config.json ]] || die "Main Web Proxy is not installed."
[[ -f "$CF" ]] || die "Caddyfile is missing: $CF"
[[ -f "$CD" ]] || die "Caddy systemd drop-in is missing: $CD"
[[ -f /etc/tproxy-server/profiles.json ]] || die "tproxy-server profiles file is missing."

GO="/opt/go1.26.5/bin/go"
[[ -x "$GO" ]] || GO="$(command -v go || true)"
[[ -n "$GO" ]] || die "Go not found. Run install-webproxy.sh first."

echo "[1/5] Downloading panel files..."
curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 \
  "$RAW/main.go" -o "$TMP/main.go" || die "Failed to download main.go"
curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 \
  "$RAW/panel.service" -o "$TMP/panel.service" || die "Failed to download panel.service"

echo "[2/5] Building panel..."
install -d -m 0750 "$ETC" "$SRC"
install -m 0644 "$TMP/main.go" "$SRC/main.go"
"$GO" build -trimpath -ldflags='-s -w' \
  -o "$BIN" "$SRC/main.go"
chmod 0755 "$BIN"

echo "[3/5] Creating storage and admin..."
if [[ ! -s "$ETC/users.json" ]]; then
    PASS="$(openssl rand -hex 18)"
    HASH="$(printf '%s' "$PASS" | sha256sum | awk '{print $1}')"
    printf '[{"username":"admin","password_hash":"%s","admin":true}]\n' "$HASH" > "$ETC/users.json"
    chmod 0600 "$ETC/users.json"
    NEW_ADMIN=1
else
    PASS=""
    NEW_ADMIN=0
fi

if [[ ! -s "$ETC/profiles.json" ]]; then
    printf '[]\n' > "$ETC/profiles.json"
fi
chmod 0400 "$ETC/profiles.json"

printf 'TPROXY_PANEL_PASSWORD=unused\n' > "$ETC/panel.env"
chmod 0600 "$ETC/panel.env"

install -m 0644 "$TMP/panel.service" "$UNIT"
systemctl daemon-reload
systemctl enable --now tproxy-panel.service

echo "[4/5] Configuring Caddy safely..."
cp -a "$CF" "${CF}.before-panel.$(date +%Y%m%d%H%M%S)"

if ! grep -Fq 'import /etc/caddy/panel-route.caddy' "$CF"; then
  awk '
    BEGIN { added=0 }
    {
      print
      if (!added && $0 ~ /^\{\$TPROXY_HOSTNAME\}[[:space:]]*\{$/) {
        print "    import /etc/caddy/panel-route.caddy"
        added=1
      }
    }
    END {
      if (!added) exit 2
    }
  ' "$CF" > "$TMP/Caddyfile.new" ||
    die "Could not find the existing TPROXY site block in Caddyfile."
  cat "$TMP/Caddyfile.new" > "$CF"
fi

cat > /etc/caddy/panel-route.caddy <<'EOF'
handle_path /panel/* {
    reverse_proxy 127.0.0.1:8090
}
EOF
chmod 0644 /etc/caddy/panel-route.caddy

DOMAIN="$(sed -n 's/^Environment=TPROXY_HOSTNAME=//p' "$CD" | head -n1)"
EMAIL="$(sed -n 's/^Environment=ACME_EMAIL=//p' "$CD" | head -n1)"
[[ -n "$DOMAIN" && -n "$EMAIL" ]] || die "Could not read Caddy environment."

echo "[5/5] Checking panel..."
systemctl is-active --quiet tproxy-panel.service || die "Panel service is not active."

HTTP_STATUS="$(curl -sS -o /dev/null -w '%{http_code}' \
    -u "admin:${PASS:-unused}" http://127.0.0.1:8090/ || true)"

if [[ "$NEW_ADMIN" == "1" && "$HTTP_STATUS" != "200" ]]; then
    die "Panel local check failed (HTTP ${HTTP_STATUS})."
fi

echo
echo "============================================================"
echo "          TELEGRAM WEB PROXY PANEL IS INSTALLED"
echo "============================================================"
echo
echo "Local panel:"
echo "  http://127.0.0.1:8090/"
echo
echo "Admin user:"
echo "  admin"
if [[ "$NEW_ADMIN" == "1" ]]; then
    echo "Admin password:"
    echo "  $PASS"
else
    echo "Admin password:"
    echo "  existing password"
fi
echo
echo "Caddy integration:"
echo "  1. Open /etc/caddy/Caddyfile"
echo "  2. The installer can add the /panel route automatically."
echo
echo "     import /etc/caddy/panel-route.caddy"
echo
echo "  3. Validate with the SAME variables used by Caddy:"
echo '     source /dev/stdin <<EOF'
echo '     export TPROXY_HOSTNAME="YOUR-DOMAIN"'
echo '     export ACME_EMAIL="YOUR-EMAIL"'
echo '     EOF'
echo '     caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile'
echo
echo "  4. Restart Caddy:"
echo "     systemctl restart caddy"
echo
echo "Panel URL:"
echo "  https://${DOMAIN}/panel/"
echo "============================================================"
