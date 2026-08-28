#!/usr/bin/env bash
# Updates only the relay binary that serves public-site files.
set -Eeuo pipefail
umask 077

die() { echo "ERROR: $*" >&2; exit 1; }
[[ ${EUID} -eq 0 ]] || die "Run as root."
command -v git >/dev/null 2>&1 || die "Git is required. Install it with: apt install -y git"
TPROXY_REF="52a5feb7fac38f68da5afef9cedd9b3bfc8473ca"

WORK="$(mktemp -d /tmp/tproxy-relay-update.XXXXXX)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

echo "Downloading current relay source..."
mkdir -p "$WORK/source"
git -C "$WORK/source" init
git -C "$WORK/source" remote add origin https://github.com/telegramdesktop/tproxy-server.git
git -C "$WORK/source" fetch --depth 1 origin "$TPROXY_REF"
git -C "$WORK/source" checkout --detach FETCH_HEAD
[[ "$(git -C "$WORK/source" rev-parse HEAD)" == "$TPROXY_REF" ]] ||
    die "Pinned relay source verification failed."

[[ -x "$WORK/source/deploy/update-relay.sh" ]] ||
    die "The upstream transactional relay updater is missing."

echo "Testing, building and installing the relay transactionally..."
# The official updater runs Go tests, validates the candidate against the
# installed configuration, keeps the previous binary and rolls back when
# either healthz or readyz regresses.
bash "$WORK/source/deploy/update-relay.sh"

DOMAIN="$(python3 -c 'import json; print(json.load(open("/etc/tproxy-server/config.json"))["public_hostname"])')"
echo "Testing public CSS through the relay..."
CSS_PATH="$(python3 - <<'PY'
import re
s=open('/srv/tproxy-site/index.html',encoding='utf-8').read()
m=re.search(r'href=["\'](/panel-site-[a-f0-9]{12}\.css)["\']',s,re.I)
print(m.group(1) if m else '')
PY
)"
if [[ -n "$CSS_PATH" ]]; then
    curl -kfsSI --max-time 12 "https://${DOMAIN}${CSS_PATH}" | grep -qi '^content-type: text/css' ||
        die "Relay still does not serve the current CSS file."
fi
echo "Relay update completed successfully."
