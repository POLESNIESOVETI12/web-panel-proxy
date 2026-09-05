#!/usr/bin/env bash
# Public one-command bootstrapper for WEB PANEL PROXY V 2.1.0.
set -Eeuo pipefail
umask 077

# Public downloads must never prompt for credentials or inherit URL rewrites
# from a root account's Git configuration.
export GIT_TERMINAL_PROMPT=0
export GIT_CONFIG_NOSYSTEM=1
export GIT_CONFIG_GLOBAL=/dev/null

REPOSITORY="https://github.com/POLESNIESOVETI12/web-panel-proxy.git"
RELEASE_REF="${WEB_PANEL_PROXY_REF:-v2.1.0}"

die() { echo "ERROR: $*" >&2; exit 1; }
[[ ${EUID:-1} -eq 0 ]] || die "Run this command with sudo or as root."

# When launched from a complete ZIP, install that package, not an older tag.
# With `bash -c "$(curl ...)"` there is no script file; never trust unrelated
# files from the caller's current directory in that mode.
SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"
LOCAL_BASE=""
if [[ -n "$SCRIPT_SOURCE" && -f "$SCRIPT_SOURCE" ]]; then
    LOCAL_BASE="$(cd "$(dirname "$SCRIPT_SOURCE")" && pwd)"
fi
if [[ -n "$LOCAL_BASE" && -s "$LOCAL_BASE/install-final.sh" && -s "$LOCAL_BASE/wpp_subscriptions.py" ]]; then
    exec bash "$LOCAL_BASE/install-final.sh"
fi

echo "WEB PANEL PROXY V 2.1.0 — downloading installation files..."

if ! command -v git >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get -o DPkg::Lock::Timeout=600 update
    apt-get -o DPkg::Lock::Timeout=600 install -y --no-install-recommends git ca-certificates
fi

INSTALL_WORK="$(mktemp -d /tmp/web-panel-proxy-install.XXXXXX)"
cleanup() { rm -rf "$INSTALL_WORK"; }
trap cleanup EXIT

git clone --depth 1 --branch "$RELEASE_REF" "$REPOSITORY" "$INSTALL_WORK/source"
[[ -f "$INSTALL_WORK/source/install-final.sh" ]] || die "Downloaded installation package is incomplete."

chmod 0700 "$INSTALL_WORK/source"/*.sh
bash "$INSTALL_WORK/source/install-final.sh"
