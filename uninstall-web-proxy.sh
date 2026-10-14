#!/usr/bin/env bash
# Updates only the relay binary that serves public-site files.
set -Eeuo pipefail
umask 077

die() { echo "ERROR: $*" >&2; exit 1; }
[[ ${EUID} -eq 0 ]] || die "Run as root."
TPROXY_REF="52a5feb7fac38f68da5afef9cedd9b3bfc8473ca"

relay_context() {
    DOMAIN="$(python3 -c 'import json; print(json.load(open("/etc/tproxy-server/config.json"))["public_hostname"])' 2>/dev/null || true)"
    CSS_PATH="$(python3 - <<'PY' 2>/dev/null || true
import re
from pathlib import Path
try:
    source=Path('/srv/tproxy-site/index.html').read_text(encoding='utf-8')
    match=re.search(r'href=["\'](/panel-site(?:-[a-f0-9]{12})?\.css)["\']',source,re.I)
    if match:
        print(match.group(1))
    else:
        candidates=sorted(Path('/srv/tproxy-site').glob('*.css'))
        print('/'+candidates[0].name if candidates else '')
except Exception:
    pass
PY
)"
}

# A previous interrupted update may already have installed and verified this
# pinned relay. Do not download, rebuild or restart it again when all public
# checks pass; the panel/protocol update can then continue offline from GitHub.
relay_context
if systemctl is-active --quiet tproxy-server.service &&
   [[ -n "$DOMAIN" && -n "$CSS_PATH" ]] &&
   [[ "$(curl -fsS --max-time 3 http://127.0.0.1:8081/healthz 2>/dev/null || true)" == "ok" ]] &&
   [[ "$(curl -fsS --max-time 3 http://127.0.0.1:8081/readyz 2>/dev/null || true)" == "ready" ]] &&
   curl -kfsSI --max-time 12 "https://${DOMAIN}${CSS_PATH}" 2>/dev/null | grep -qi '^content-type: text/css'; then
    echo "Relay and public CSS are already healthy; relay rebuild skipped."
    exit 0
fi

command -v git >/dev/null 2>&1 || die "Git is required. Install it with: apt install -y git"
git_clean() {
    GIT_TERMINAL_PROMPT=0 \
    GIT_ASKPASS=/bin/false \
    GIT_CONFIG_NOSYSTEM=1 \
    GIT_CONFIG_GLOBAL=/dev/null \
    GIT_CONFIG_COUNT=0 \
        git -c credential.helper= "$@"
}

WORK="$(mktemp -d /tmp/tproxy-relay-update.XXXXXX)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

echo "Downloading current relay source..."
mkdir -p "$WORK/source"
git_clean -c init.defaultBranch=main -C "$WORK/source" init -q
git_clean -C "$WORK/source" remote add origin https://github.com/telegramdesktop/tproxy-server.git
git_clean -C "$WORK/source" fetch --no-tags --depth 1 origin "$TPROXY_REF"
git_clean -C "$WORK/source" checkout --detach --force FETCH_HEAD
[[ "$(git_clean -C "$WORK/source" rev-parse HEAD)" == "$TPROXY_REF" ]] ||
    die "Pinned relay source verification failed."

[[ -x "$WORK/source/deploy/update-relay.sh" ]] ||
    die "The upstream transactional relay updater is missing."

echo "Testing, building and installing the relay transactionally..."
# The official updater runs Go tests, validates the candidate against the
# installed configuration, keeps the previous binary and rolls back when
# either healthz or readyz regresses.
# The upstream permission test creates a deliberately readable 0444 fixture
# with os.WriteFile. Inheriting our 077 mask silently turns it into 0400 and
# makes the negative permission test fail. Scope the conventional test/build
# mask to this child only. WORK and upstream mktemp directories stay private;
# production files are installed with explicit modes by the upstream updater.
(
    umask 022
    bash "$WORK/source/deploy/update-relay.sh"
)

relay_context
echo "Testing public CSS through the relay..."
if [[ -n "$CSS_PATH" ]]; then
    curl -kfsSI --max-time 12 "https://${DOMAIN}${CSS_PATH}" | grep -qi '^content-type: text/css' ||
        die "Relay still does not serve the current CSS file."
fi
echo "Relay update completed successfully."
