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
CF="/etc/caddy/Caddyfile"
CD="/etc/systemd/system/caddy.service.d/tproxy.conf"

die(){ echo "ERROR: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root."
[[ -f /etc/tproxy-server/config.json && -f /etc/tproxy-server/profiles.json ]] || die "Run install-webproxy.sh first."
[[ -f "$CF" && -f "$CD" ]] || die "Caddy configuration from install-webproxy.sh was not found."

GO="/opt/go1.26.5/bin/go"
[[ -x "$GO" ]] || GO="$(command -v go || true)"
[[ -n "$GO" ]] || die "Go not found."

echo "[1/6] Downloading panel..."
for f in main.go panel.service; do
  curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 "$RAW/$f" -o "$TMP/$f" ||
    die "Failed to download $f"
done

echo "[2/6] Building panel..."
install -d -m 0750 "$ETC" "$SRC"
install -m 0644 "$TMP/main.go" "$SRC/main.go"
"$GO" build -trimpath -ldflags='-s -w' -o "$BIN" "$SRC/main.go"
chmod 0755 "$BIN"

echo "[3/6] Creating panel account..."
if [[ ! -s "$ETC/users.json" ]]; then
  PASS="$(openssl rand -hex 18)"
  HASH="$(printf '%s' "$PASS" | sha256sum | awk '{print $1}')"
  printf '[{"username":"admin","password_hash":"%s","admin":true}]\n' "$HASH" > "$ETC/users.json"
  chmod 0600 "$ETC/users.json"
  NEW=1
else
  PASS=""
  NEW=0
fi
[[ -s "$ETC/profiles.json" ]] || { printf '[]\n' > "$ETC/profiles.json"; chmod 0400 "$ETC/profiles.json"; }
printf 'TPROXY_PANEL_PASSWORD=unused\n' > "$ETC/panel.env"
chmod 0600 "$ETC/panel.env"

install -m 0644 "$TMP/panel.service" "$UNIT"
systemctl daemon-reload
systemctl enable --now tproxy-panel.service

echo "[4/6] Updating Caddy..."
grep -q 'handle_path /panel/\*' "$CF" || {
  cp -a "$CF" "$CF.before-panel.$(date +%Y%m%d%H%M%S)"
  awk '
    !done && $0 ~ /^[[:space:]]*reverse_proxy[[:space:]]+127\.0\.0\.1:8080[[:space:]]*\{/ {
      print "    handle_path /panel/* {"
      print "        reverse_proxy 127.0.0.1:8090"
      print "    }"
      done=1
    }
    {print}
    END { if (!done) exit 2 }
  ' "$CF" > "$TMP/Caddyfile" || die "Could not add /panel route to Caddyfile."
  cat "$TMP/Caddyfile" > "$CF"
}

DOMAIN="$(sed -n 's/^Environment=TPROXY_HOSTNAME=//p' "$CD" | head -n1)"
EMAIL="$(sed -n 's/^Environment=ACME_EMAIL=//p' "$CD" | head -n1)"
[[ -n "$DOMAIN" && -n "$EMAIL" ]] || die "Could not read Caddy environment."

echo "[5/6] Validating and reloading Caddy..."
if ! TPROXY_HOSTNAME="$DOMAIN" ACME_EMAIL="$EMAIL" \
  caddy validate --config "$CF" --adapter caddyfile >/dev/null; then
  echo "Caddy validation failed; restoring previous configuration." >&2
  latest="$(ls -1t "$CF".before-panel.* 2>/dev/null | head -n1 || true)"
  [[ -n "$latest" ]] && cp -a "$latest" "$CF"
  die "Caddy configuration validation failed."
fi
systemctl reload caddy

echo "[6/6] Final checks..."
systemctl is-active --quiet tproxy-panel.service || die "Panel service is not active."
curl -fsS --max-time 5 -u "admin:${PASS:-invalid}" http://127.0.0.1:8090/ >/dev/null 2>&1 || {
  [[ "$NEW" == 0 ]] || die "Panel local health check failed."
}
curl -fsS --max-time 5 http://127.0.0.1:8081/readyz >/dev/null || die "tproxy-server is not ready."

echo
echo "============================================================"
echo "           TELEGRAM WEB PROXY PANEL READY"
echo "============================================================"
echo "Panel:   https://${DOMAIN}/panel/"
echo "User:    admin"
if [[ "$NEW" == 1 ]]; then
  echo "Password: $PASS"
else
  echo "Password: existing admin password"
fi
echo
echo "Create users and Telegram Web Proxy profiles from the panel."
echo "============================================================"
