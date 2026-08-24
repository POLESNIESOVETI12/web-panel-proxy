#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
BASE=/root/tproxy-panel
SRC=/root/tproxy-server/panel
ETC=/etc/tproxy-panel
BIN=/usr/local/bin/tproxy-panel

[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
[[ -f /etc/tproxy-server/config.json && -f /etc/tproxy-server/profiles.json ]] || { echo "Run the main install-webproxy.sh first."; exit 1; }

GO=/opt/go1.26.5/bin/go
[[ -x "$GO" ]] || GO="$(command -v go || true)"
[[ -n "$GO" ]] || { echo "Go not found."; exit 1; }

install -d -m 0750 "$ETC" "$SRC"
cp "$BASE/main.go" "$SRC/main.go"

# Create the first admin only once.
if [[ ! -s "$ETC/users.json" ]]; then
  PASS="$(openssl rand -hex 18)"
  printf '[{"username":"admin","password_hash":"%s","admin":true}]\n' "$(printf '%s' "$PASS" | sha256sum | awk '{print $1}')" > "$ETC/users.json"
  chmod 0600 "$ETC/users.json"
else
  PASS="(existing admin password)"
fi

"$GO" build -trimpath -ldflags='-s -w' -o "$BIN" "$SRC/main.go"
chmod 0755 "$BIN"

# Initialize panel profiles from the current default profile.
if [[ ! -s "$ETC/profiles.json" ]]; then
  printf '[]\n' > "$ETC/profiles.json"
  chmod 0400 "$ETC/profiles.json"
fi

printf 'TPROXY_PANEL_PASSWORD=%s\n' "$PASS" > "$ETC/panel.env"
chmod 0600 "$ETC/panel.env"

install -m 0644 "$BASE/panel.service" /etc/systemd/system/tproxy-panel.service
systemctl daemon-reload
systemctl enable --now tproxy-panel.service

echo
echo "Panel: http://127.0.0.1:8090/"
echo "User: admin"
echo "Password: $PASS"
echo
echo "Expose through Caddy with:"
cat "$BASE/CADDY-SNIPPET.txt"
