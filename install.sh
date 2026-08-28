#!/usr/bin/env bash
# Public one-command bootstrapper for WEB PANEL PROXY V 2.0.
set -Eeuo pipefail
umask 077

REPOSITORY="https://github.com/POLESNIESOVETI12/web-panel-proxy.git"
RELEASE_REF="${WEB_PANEL_PROXY_REF:-v2.0.2}"

die() { echo "ERROR: $*" >&2; exit 1; }
[[ ${EUID:-1} -eq 0 ]] || die "Run this command with sudo or as root."

echo "WEB PANEL PROXY V 2.0 — downloading installation files..."

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
