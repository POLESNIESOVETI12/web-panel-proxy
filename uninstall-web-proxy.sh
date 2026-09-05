#!/usr/bin/env bash
# WEB PANEL PROXY V 2.1.0 complete removal utility.
set -Eeuo pipefail

[[ ${EUID:-1} -eq 0 ]] || { echo "Run this script as root." >&2; exit 1; }
command -v flock >/dev/null 2>&1 || { echo "flock is required (package: util-linux)." >&2; exit 1; }
exec 9>/run/lock/web-panel-proxy.lock
flock -n 9 || { echo "Another WEB PANEL PROXY install, update or removal is already running." >&2; exit 1; }

echo "WEB PANEL PROXY V 2.1.0 — complete removal"
echo "Removing all WEB PANEL PROXY V 2.1.0 components..."

DOMAIN="$(sed -n 's/^Environment=TPROXY_HOSTNAME=//p' /etc/systemd/system/caddy.service.d/tproxy.conf 2>/dev/null | head -n1 || true)"
CADDY_MARKER="$(cat /etc/web-proxy-panel/caddy-owned 2>/dev/null || true)"
CADDY_OWNED=0
CADDY_SHARED=0
XRAY_USER_OWNED=0
XRAY_GROUP_OWNED=0
HYSTERIA_UFW_OWNED=0
HYSTERIA_UFW_PORTS=""
MTPROTO_UFW_PORTS=""
MIERU_UFW_OWNED=0
MIERU_PACKAGE_OWNED=0
NAIVE_CADDY_OWNED=0
[[ "$CADDY_MARKER" == "WEB_PANEL_PROXY_V2_CADDY_OWNER" ]] && CADDY_OWNED=1
[[ "$CADDY_MARKER" == "WEB_PANEL_PROXY_V2_CADDY_SHARED" ]] && CADDY_SHARED=1
[[ -e /etc/web-proxy-panel/xray-user-owned ]] && XRAY_USER_OWNED=1
[[ -e /etc/web-proxy-panel/xray-group-owned ]] && XRAY_GROUP_OWNED=1
[[ -e /etc/web-proxy-panel/mieru-ufw-owned ]] && MIERU_UFW_OWNED=1
[[ -e /etc/web-proxy-panel/mita-package-owned ]] && MIERU_PACKAGE_OWNED=1
[[ -e /etc/web-proxy-panel/naive-caddy-owned ]] && NAIVE_CADDY_OWNED=1
if [[ -e /etc/web-proxy-panel/hysteria-ufw-owned ]]; then
  HYSTERIA_UFW_OWNED=1
  HYSTERIA_UFW_PORTS="$(grep -Eo '[0-9]{1,5}' /etc/web-proxy-panel/hysteria-ufw-owned 2>/dev/null | sort -nu | tr '\n' ' ' || true)"
  [[ -n "$HYSTERIA_UFW_PORTS" ]] || HYSTERIA_UFW_PORTS="8443"
fi
if [[ -e /etc/web-proxy-panel/mtproto-ufw-owned ]]; then
  MTPROTO_UFW_PORTS="$(grep -Eo '[0-9]{1,5}' /etc/web-proxy-panel/mtproto-ufw-owned 2>/dev/null | sort -nu | tr '\n' ' ' || true)"
fi

echo "Stopping services..."
for unit in \
  web-panel-proxy-web-update.service \
  web-panel-proxy-metrics.timer web-panel-proxy-metrics.service \
  tproxy-panel.service web-proxy-panel.service web-proxy-panel-mtproxy.service \
  web-proxy-panel-firewall.service web-proxy-panel-traffic.timer web-proxy-panel-traffic.service web-panel-proxy-xray.service \
  web-panel-proxy-sync-tls.timer web-panel-proxy-sync-tls.service \
  tproxy-firewall.service refresh-mtproxy-config.timer refresh-mtproxy-config.service \
  tproxy-server.service mtproxy.service
do
  systemctl disable --now "$unit" 2>/dev/null || true
done
mita stop >/dev/null 2>&1 || true

shopt -s nullglob
USER_UNITS=(/etc/systemd/system/web-proxy-user-*.service)
for unit_path in "${USER_UNITS[@]}"; do
  unit="$(basename "$unit_path")"
  systemctl disable --now "$unit" 2>/dev/null || true
  rm -f -- "$unit_path"
done

# Remove the firewall tables created by the panel and the proxy firewall.
nft delete table inet web_proxy_panel 2>/dev/null || true
nft delete table inet tproxy_backend 2>/dev/null || true
if [[ "$HYSTERIA_UFW_OWNED" == 1 ]] && command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
  for port in $HYSTERIA_UFW_PORTS; do
    [[ "$port" =~ ^[0-9]+$ ]] || continue
    (( port >= 1 && port <= 65535 )) || continue
    ufw --force delete allow "$port/udp" >/dev/null 2>&1 || true
    ufw --force delete allow "$port/udp" >/dev/null 2>&1 || true
  done
fi
if [[ -n "$MTPROTO_UFW_PORTS" ]] && command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
  for port in $MTPROTO_UFW_PORTS; do
    [[ "$port" =~ ^[0-9]+$ ]] || continue
    (( port >= 1 && port <= 65535 )) || continue
    ufw --force delete allow "$port/tcp" >/dev/null 2>&1 || true
  done
fi
if [[ "$MIERU_UFW_OWNED" == 1 ]] && command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
  ufw --force delete allow 8965/tcp >/dev/null 2>&1 || true
fi

# Remove Caddy only when this release installed it and its configuration still
# contains only the project site. If another site was added later, remove just
# the project's top-level site block and preserve the shared Caddy deployment.
REMOVE_CADDY=0
PRESERVE_CADDY=0
if [[ "$((CADDY_OWNED + CADDY_SHARED))" -gt 0 && -n "$DOMAIN" && -f /etc/caddy/Caddyfile ]]; then
  CADDY_TMP="$(mktemp /tmp/web-panel-proxy-Caddyfile.XXXXXX)"
  set +e
  python3 - /etc/caddy/Caddyfile "$CADDY_TMP" "$DOMAIN" <<'PY'
import re,sys
source,target,domain=sys.argv[1:]
text=open(source,encoding="utf-8").read()
text=re.sub(r"\n?[ \t]*# WPP NAIVE GLOBAL BEGIN\n.*?\n[ \t]*# WPP NAIVE GLOBAL END\n?","\n",text,flags=re.S)
text=re.sub(r"\n?[ \t]*# WPP NAIVE BEGIN\n.*?\n[ \t]*# WPP NAIVE END\n?","\n",text,flags=re.S)
text=re.sub(r"(?m)^\s*:443,\s*"+re.escape(domain)+r"\s*\{\s*$",domain+" {",text,count=1)
lines=text.splitlines(keepends=True)
start=None
pattern=re.compile(r"^\s*"+re.escape(domain)+r"\s*\{\s*$")
for i,line in enumerate(lines):
    if pattern.match(line):
        start=i
        break
if start is None:
    raise SystemExit(2)
depth=0
end=None
for i in range(start,len(lines)):
    depth+=lines[i].count("{")-lines[i].count("}")
    if depth==0:
        end=i
        break
if end is None:
    raise SystemExit(3)
remaining=lines[:start]+lines[end+1:]
while remaining and not remaining[0].strip(): remaining.pop(0)
while remaining and not remaining[-1].strip(): remaining.pop()
meaningful=[x for x in remaining if x.strip() and not x.lstrip().startswith("#")]
if not meaningful:
    open(target,"w",encoding="utf-8").write("")
    raise SystemExit(10)
open(target,"w",encoding="utf-8").writelines(remaining)

# A project-owned canonical Caddyfile also contains one global options block
# (`{ ... }`). It is not another website. Report status 10 when that block and
# comments/blank lines are all that remain. Shared Caddy is still preserved by
# the shell branch below, together with this global block.
depth=0
outside=[]
for line in remaining:
    stripped=line.strip()
    if not stripped or stripped.startswith("#"):
        continue
    if depth == 0 and stripped == "{":
        depth=1
        continue
    if depth > 0:
        depth+=line.count("{")-line.count("}")
        continue
    outside.append(stripped)
if depth != 0:
    raise SystemExit(3)
if not outside:
    raise SystemExit(10)
PY
  PARSE_STATUS=$?
  set -e
  if [[ "$PARSE_STATUS" == 10 ]]; then
    if [[ "$CADDY_OWNED" == 1 ]]; then
      REMOVE_CADDY=1
      systemctl disable --now caddy.service 2>/dev/null || true
    else
      install -o root -g caddy -m 0640 "$CADDY_TMP" /etc/caddy/Caddyfile
      PRESERVE_CADDY=1
    fi
  elif [[ "$PARSE_STATUS" == 0 ]]; then
    if caddy validate --config "$CADDY_TMP" --adapter caddyfile >/dev/null 2>&1; then
      install -o root -g caddy -m 0640 "$CADDY_TMP" /etc/caddy/Caddyfile
      PRESERVE_CADDY=1
    else
      PRESERVE_CADDY=1
      echo "WARNING: the remaining Caddy configuration did not validate; the original file was preserved."
    fi
  else
    PRESERVE_CADDY=1
    echo "WARNING: the project Caddy block could not be removed automatically; Caddy was preserved."
  fi
  rm -f -- "$CADDY_TMP"
  [[ "$PRESERVE_CADDY" == 1 ]] && rm -f -- /etc/caddy/Caddyfile.before-web-panel-proxy
fi

echo "Removing WEB PANEL PROXY V 2.1.0 files..."
rm -f -- \
  /etc/systemd/system/web-panel-proxy-web-update.service \
  /etc/systemd/system/web-panel-proxy-metrics.service \
  /etc/systemd/system/web-panel-proxy-metrics.timer \
  /etc/systemd/system/tproxy-panel.service \
  /etc/systemd/system/web-proxy-panel.service \
  /etc/systemd/system/web-proxy-panel-mtproxy.service \
  /etc/systemd/system/web-proxy-panel-firewall.service \
  /etc/systemd/system/web-proxy-panel-traffic.service \
  /etc/systemd/system/web-proxy-panel-traffic.timer \
  /etc/systemd/system/web-panel-proxy-xray.service \
  /etc/systemd/system/web-panel-proxy-sync-tls.service \
  /etc/systemd/system/web-panel-proxy-sync-tls.timer \
  /etc/systemd/system/tproxy-firewall.service \
  /etc/systemd/system/tproxy-server.service \
  /etc/systemd/system/mtproxy.service \
  /etc/systemd/system/refresh-mtproxy-config.timer \
  /etc/systemd/system/refresh-mtproxy-config.service \
  /usr/local/bin/tproxy-server \
  /usr/local/bin/tproxy-server.previous \
  /usr/local/bin/tproxy-server.next \
  /usr/local/sbin/web-proxy-panelctl \
  /usr/local/sbin/web-proxy-panel-user-firewall \
  /usr/local/sbin/web-panel-proxy-sync-tls \
  /usr/local/sbin/web-panel-proxy-update \
  /usr/local/sbin/WPP \
  /usr/local/sbin/wpp \
  /usr/local/sbin/web-proxy-public-mtproxy \
  /usr/local/sbin/web-proxy-panel-mtproxy \
  /usr/local/sbin/web-panel-proxy-uninstall \
  /usr/local/sbin/refresh-mtproxy-config \
  /usr/local/libexec/web-proxy-user-backend.py

rm -rf -- \
  /var/lib/web-panel-proxy-update \
  /etc/systemd/system/mtproxy.service.d \
  /etc/tproxy-server \
  /etc/mtproxy \
  /etc/web-proxy-panel \
  /etc/web-panel-proxy-xray \
  /etc/tproxy-panel \
  /opt/MTProxy \
  /opt/go1.26.5 \
  /opt/tproxy-panel \
  /opt/web-panel-proxy \
  /opt/tproxy-site \
  /srv/tproxy-site \
  /var/lib/tproxy-panel \
  /var/lib/web-panel-proxy-xray \
  /root/tproxy-server

# qrencode is the only Debian package installed exclusively for the panel.
if command -v apt-get >/dev/null 2>&1; then
  DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 purge -y qrencode 2>/dev/null || true
  if [[ "$MIERU_PACKAGE_OWNED" == 1 ]]; then
    DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 purge -y mita 2>/dev/null || true
  fi
fi

if [[ "$REMOVE_CADDY" == 1 ]]; then
  rm -f -- /usr/local/bin/caddy /etc/caddy/Caddyfile /etc/systemd/system/caddy.service.d/tproxy.conf
  rmdir /etc/systemd/system/caddy.service.d 2>/dev/null || true
  rm -f -- /etc/systemd/system/caddy.service
  rm -rf -- /etc/caddy /var/lib/caddy
  id caddy >/dev/null 2>&1 && userdel caddy 2>/dev/null || true
  echo "WEB PROXY Caddy configuration removed."
elif [[ "$PRESERVE_CADDY" == 1 ]]; then
  rm -f -- /etc/systemd/system/caddy.service.d/tproxy.conf
  rmdir /etc/systemd/system/caddy.service.d 2>/dev/null || true
  systemctl daemon-reload
  if command -v caddy >/dev/null 2>&1 && caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
    systemctl restart caddy.service 2>/dev/null || true
    echo "Other Caddy sites were preserved; only the WEB PANEL PROXY site block was removed."
  else
    echo "WARNING: preserved Caddy configuration requires manual validation."
  fi
else
  rm -f -- /etc/systemd/system/caddy.service.d/tproxy.conf
  rmdir /etc/systemd/system/caddy.service.d 2>/dev/null || true
  echo "Caddy was preserved because it was not marked as installed by WEB PANEL PROXY V 2.1.0."
fi

id mtproxy >/dev/null 2>&1 && userdel mtproxy 2>/dev/null || true
id tproxy >/dev/null 2>&1 && userdel tproxy 2>/dev/null || true
[[ "$XRAY_USER_OWNED" == 1 ]] && id xray >/dev/null 2>&1 && userdel xray 2>/dev/null || true
[[ "$XRAY_GROUP_OWNED" == 1 ]] && getent group xray >/dev/null 2>&1 && groupdel xray 2>/dev/null || true

systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true
echo "WEB PANEL PROXY V 2.1.0 has been removed."
