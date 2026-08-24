#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

REPO="https://raw.githubusercontent.com/POLESNIESOVETI12/webtelegram/main/panel"
TMP="$(mktemp -d /tmp/tproxy-panel.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

SRC="/root/tproxy-server/panel"
ETC="/etc/tproxy-panel"
BIN="/usr/local/bin/tproxy-panel"
UNIT="/etc/systemd/system/tproxy-panel.service"

die(){ echo "ERROR: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root."
[[ -f /etc/tproxy-server/config.json && -f /etc/tproxy-server/profiles.json ]] ||
  die "Run the main install-webproxy.sh first."

GO="/opt/go1.26.5/bin/go"
[[ -x "$GO" ]] || GO="$(command -v go || true)"
[[ -n "$GO" ]] || die "Go not found. Run install-webproxy.sh first."

echo "[1/4] Downloading panel files..."
curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 \
  "$REPO/main.go" -o "$TMP/main.go" ||
  die "Failed to download main.go"

curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 \
  "$REPO/panel.service" -o "$TMP/panel.service" ||
  die "Failed to download panel.service"

curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 \
  "$REPO/CADDY-SNIPPET.txt" -o "$TMP/CADDY-SNIPPET.txt" ||
  die "Failed to download CADDY-SNIPPET.txt"

echo "[2/4] Building panel..."
install -d -m 0750 "$ETC" "$SRC"
install -m 0644 "$TMP/main.go" "$SRC/main.go"

"$GO" build -trimpath -ldflags='-s -w' \
  -o "$BIN" "$SRC/main.go"
chmod 0755 "$BIN"

echo "[3/4] Preparing panel data..."
if [[ ! -s "$ETC/users.json" ]]; then
  PASS="$(openssl rand -hex 18)"
  HASH="$(printf '%s' "$PASS" | sha256sum | awk '{print $1}')"
  printf '[{"username":"admin","password_hash":"%s","admin":true}]\n' "$HASH" > "$ETC/users.json"
  chmod 0600 "$ETC/users.json"
  NEW_ADMIN=1
else
  NEW_ADMIN=0
fi

if [[ ! -s "$ETC/profiles.json" ]]; then
  printf '[]\n' > "$ETC/profiles.json"
  chmod 0400 "$ETC/profiles.json"
fi

# Environment file is kept for compatibility with the systemd unit.
# The application authenticates against users.json.
printf 'TPROXY_PANEL_PASSWORD=%s\n' "${PASS:-existing}" > "$ETC/panel.env"
chmod 0600 "$ETC/panel.env"

install -m 0644 "$TMP/panel.service" "$UNIT"
systemctl daemon-reload
systemctl enable --now tproxy-panel.service

echo "[4/4] Done."
echo
echo "Panel: http://127.0.0.1:8090/"
echo "User: admin"
if [[ "$NEW_ADMIN" == "1" ]]; then
  echo "Password: $PASS"
else
  echo "Password: existing admin password"
fi
echo
echo "Caddy route:"
cat "$TMP/CADDY-SNIPPET.txt"
echo
echo "The panel is local-only until you add the Caddy route."
