#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

BASE="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="/opt/tproxy-panel"
DATA_DIR="/var/lib/tproxy-panel"
DATA_FILE="${DATA_DIR}/data.json"
SERVICE_FILE="/etc/systemd/system/tproxy-panel.service"
FIREWALL_SERVICE_FILE="/etc/systemd/system/web-proxy-panel-firewall.service"
APP_FILE="${APP_DIR}/panel.py"
LOGO_SOURCE="${BASE}/panel-logo.png"
LOGO_FILE="${APP_DIR}/panel-logo.png"
PORT=8090
DOMAIN="${WEB_PANEL_PROXY_DOMAIN:-$(sed -n 's/^Environment=TPROXY_HOSTNAME=//p' /etc/systemd/system/caddy.service.d/tproxy.conf 2>/dev/null | head -n1 || true)}"
ACME_EMAIL="${WEB_PANEL_PROXY_ACME_EMAIL:-$(sed -n 's/^Environment=ACME_EMAIL=//p' /etc/systemd/system/caddy.service.d/tproxy.conf 2>/dev/null | head -n1 || true)}"
MTPROTO_HOST="$(cat /etc/web-proxy-panel/mtproto-host 2>/dev/null || true)"
MTPROTO_HOST="${MTPROTO_HOST:-$DOMAIN}"
PANEL_PATH="/panel-$(openssl rand -hex 16)"
UPDATING="${WEB_PANEL_PROXY_UPDATE:-0}"
MANIFEST="/etc/web-proxy-panel/manifest"
PRIMARY_SECRET="/etc/web-proxy-panel/primary-secret"
USERS_FILE="/etc/web-proxy-panel/users.json"
SECRETS_FILE="/etc/web-proxy-panel/mtproxy-secrets"
MANAGER="/usr/local/sbin/web-proxy-panelctl"
QR_BIN="/usr/bin/qrencode"
XRAY_ROOT="/opt/web-panel-proxy/xray"
XRAY_BIN="${XRAY_ROOT}/xray"
XRAY_CONFIG_DIR="/etc/web-panel-proxy-xray"
XRAY_CONFIG="${XRAY_CONFIG_DIR}/config.json"
XRAY_PATH_FILE="/etc/web-proxy-panel/xray-path"
XRAY_VERSION="26.7.28"
XRAY_SHA256="8195d909f1109b8f3d99eefe401a3c451d7bf4af71f24d3815420f77e5dd2a40"
HYSTERIA_PORT=8443

die(){ echo "ERROR: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "Run as root."
. /etc/os-release
case "${ID:-}" in
    ubuntu)
        dpkg --compare-versions "${VERSION_ID:-0}" ge "22.04" ||
            die "Ubuntu 22.04 or newer is required."
        ;;
    debian)
        dpkg --compare-versions "${VERSION_ID:-0}" ge "12" ||
            die "Debian 12 or newer is required."
        ;;
    *)
        die "Supported systems: Ubuntu 22.04+ or Debian 12+."
        ;;
esac
echo "Platform: ${PRETTY_NAME:-${ID} ${VERSION_ID}}"
command -v python3 >/dev/null || die "python3 required."
command -v openssl >/dev/null || die "openssl required."
[[ "$(uname -m)" == "x86_64" ]] || die "x86_64 is required."

# Older releases did not always retain the Caddy systemd drop-in. Recover the
# hostname from other authoritative project files before asking the operator.
DOMAIN="${DOMAIN#http://}"; DOMAIN="${DOMAIN#https://}"; DOMAIN="${DOMAIN%%/*}"; DOMAIN="${DOMAIN,,}"
if ! [[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ && "$DOMAIN" == *.* ]]; then
    DOMAIN="$(sed -n 's/^Environment=WEBPROXY_DOMAIN=//p' "$SERVICE_FILE" 2>/dev/null | head -n1 || true)"
fi
if ! [[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ && "$DOMAIN" == *.* ]] && [[ -s /etc/tproxy-server/config.json ]]; then
    DOMAIN="$(python3 - /etc/tproxy-server/config.json <<'PY' 2>/dev/null || true
import json,sys
try:
    value=json.load(open(sys.argv[1],encoding="utf-8")).get("public_hostname","")
    print(value if isinstance(value,str) else "")
except Exception:
    pass
PY
)"
fi
if ! [[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ && "$DOMAIN" == *.* ]] && [[ -s /etc/caddy/Caddyfile ]]; then
    DOMAIN="$(sed -n 's/^[[:space:]]*\([a-z0-9][a-z0-9.-]*\)[[:space:]]*{[[:space:]]*$/\1/p' /etc/caddy/Caddyfile |
        grep -vE '^(http|https|localhost)$' | head -n1 || true)"
fi
if ! [[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ && "$DOMAIN" == *.* ]]; then
    [[ -t 0 ]] || die "The domain could not be recovered. Re-run with WEB_PANEL_PROXY_DOMAIN=proxy.example.com."
    echo "Домен старой установки не найден автоматически."
    while true; do
        read -r -p "Введите действующий домен WEB PANEL PROXY: " DOMAIN
        DOMAIN="${DOMAIN#http://}"; DOMAIN="${DOMAIN#https://}"; DOMAIN="${DOMAIN%%/*}"; DOMAIN="${DOMAIN,,}"
        [[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ && "$DOMAIN" == *.* ]] && break
        echo "Некорректный домен. Пример: proxy.example.com"
    done
fi
MTPROTO_HOST="${MTPROTO_HOST:-$DOMAIN}"
[[ -s "$PRIMARY_SECRET" ]] || die "Primary install-time secret not found."
[[ -s "$LOGO_SOURCE" ]] || die "Panel logo file is missing: panel-logo.png"
for module in wpp_subscriptions.py wpp_panel_extras.py wpp_ui.py wpp_metrics.py wpp_update.py; do
    [[ -s "$BASE/$module" ]] || die "Missing panel module: $module; extract the complete archive."
done

# An update keeps the existing private panel address.  A new address would
# make an otherwise successful update look like a broken panel to its owner.
if [[ "$UPDATING" == "1" ]]; then
    [[ -s "$DATA_FILE" ]] || die "Existing panel data was not found. Run the full installer instead."
    EXISTING_PATH="$(sed -n 's/^Environment=WEBPROXY_PANEL_PATH=//p' "$SERVICE_FILE" 2>/dev/null | head -n1 || true)"
    [[ "$EXISTING_PATH" =~ ^/panel-[a-z0-9-]{3,64}$ ]] || die "Existing panel address was not found. Run the full installer instead."
    PANEL_PATH="$EXISTING_PATH"
fi

if ! [[ "$ACME_EMAIL" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] && [[ -s /etc/caddy/Caddyfile ]]; then
    ACME_EMAIL="$(sed -n 's/^[[:space:]]*email[[:space:]][[:space:]]*\([^[:space:]]*\)[[:space:]]*$/\1/p' /etc/caddy/Caddyfile | head -n1 || true)"
fi
if ! [[ "$ACME_EMAIL" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; then
    echo "Caddy ACME email is missing or invalid."
    read -r -p "ACME email: " ACME_EMAIL
    [[ "$ACME_EMAIL" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] ||
        die "Invalid ACME email."
fi

# Keep one canonical source for the menu, future updates and Caddy itself.
install -d -m 0755 /etc/systemd/system/caddy.service.d
cat > /etc/systemd/system/caddy.service.d/tproxy.conf <<EOF
[Service]
Environment=TPROXY_HOSTNAME=$DOMAIN
Environment=TPROXY_SITE_ROOT=/srv/tproxy-site
Environment=ACME_EMAIL=$ACME_EMAIL
ReadWritePaths=/etc/caddy
EOF
chmod 0644 /etc/systemd/system/caddy.service.d/tproxy.conf

export DEBIAN_FRONTEND=noninteractive
if ! command -v qrencode >/dev/null 2>&1 || ! command -v unzip >/dev/null 2>&1 || ! command -v xz >/dev/null 2>&1; then
    apt-get -o DPkg::Lock::Timeout=600 update
    apt-get -o DPkg::Lock::Timeout=600 install -y --no-install-recommends qrencode unzip xz-utils
fi

install -d -m 0755 "$APP_DIR" /etc/web-proxy-panel
install -d -m 0700 "$DATA_DIR"
install -o root -g root -m 0644 "$LOGO_SOURCE" "$LOGO_FILE"
chmod 0600 "$PRIMARY_SECRET"

echo "      Preparing Xray ${XRAY_VERSION}..."
if ! getent group xray >/dev/null 2>&1; then
    groupadd --system xray
    : > /etc/web-proxy-panel/xray-group-owned
    chmod 0600 /etc/web-proxy-panel/xray-group-owned
fi
if ! id xray >/dev/null 2>&1; then
    useradd --system --gid xray --home /var/lib/web-panel-proxy-xray --create-home --shell /usr/sbin/nologin xray
    : > /etc/web-proxy-panel/xray-user-owned
    chmod 0600 /etc/web-proxy-panel/xray-user-owned
else
    usermod -a -G xray xray
fi
install -d -o root -g root -m 0755 "$XRAY_ROOT"
install -d -o root -g xray -m 0750 "$XRAY_CONFIG_DIR"
install -d -o root -g xray -m 0750 "${XRAY_CONFIG_DIR}/tls"
install -d -o xray -g xray -m 0750 /var/lib/web-panel-proxy-xray
if [[ ! -x "$XRAY_BIN" ]] || ! "$XRAY_BIN" version 2>/dev/null | grep -q "${XRAY_VERSION}"; then
    XRAY_ARCHIVE="$(mktemp /tmp/web-panel-proxy-xray.XXXXXX.zip)"
    XRAY_UNPACK="$(mktemp -d /tmp/web-panel-proxy-xray.XXXXXX)"
    curl --fail --silent --show-error --location \
        --proto '=https' --proto-redir '=https' --tlsv1.2 \
        --retry 3 --retry-all-errors --connect-timeout 20 \
        --output "$XRAY_ARCHIVE" \
        "https://github.com/XTLS/Xray-core/releases/download/v${XRAY_VERSION}/Xray-linux-64.zip"
    echo "${XRAY_SHA256}  ${XRAY_ARCHIVE}" | sha256sum -c - >/dev/null || die "Xray checksum verification failed."
    unzip -q "$XRAY_ARCHIVE" xray -d "$XRAY_UNPACK"
    install -o root -g root -m 0755 "$XRAY_UNPACK/xray" "$XRAY_BIN"
    rm -f "$XRAY_ARCHIVE"
    rm -rf "$XRAY_UNPACK"
fi

# Remove only blocks managed by the former experimental NaiveProxy integration.
# The distribution Caddy binary is retained and used again after this migration.
if [[ -s /etc/caddy/Caddyfile ]]; then
    python3 - /etc/caddy/Caddyfile <<'PY'
from pathlib import Path
import re,sys
p=Path(sys.argv[1]); source=p.read_text(encoding="utf-8")
source=re.sub(r"\n?[ \t]*# WPP NAIVE GLOBAL BEGIN\n.*?\n[ \t]*# WPP NAIVE GLOBAL END\n?","\n",source,flags=re.S)
source=re.sub(r"\n?[ \t]*# WPP NAIVE BEGIN\n.*?\n[ \t]*# WPP NAIVE END\n?","\n",source,flags=re.S)
source=re.sub(r"(?m)^\s*:443,\s*([a-z0-9][a-z0-9.-]*)\s*\{\s*$",r"\1 {",source,count=1)
tmp=p.with_suffix(".wpp-stable.tmp"); tmp.write_text(source,encoding="utf-8")
tmp.chmod(0o640); tmp.replace(p)
PY
    chown root:caddy /etc/caddy/Caddyfile
fi
command -v caddy >/dev/null 2>&1 || die "Caddy is missing. Run the full installer to restore it."
cat > /etc/systemd/system/caddy.service.d/tproxy.conf <<EOF
[Service]
Environment=TPROXY_HOSTNAME=$DOMAIN
Environment=TPROXY_SITE_ROOT=/srv/tproxy-site
Environment=ACME_EMAIL=$ACME_EMAIL
ReadWritePaths=/etc/caddy
EOF
chmod 0644 /etc/systemd/system/caddy.service.d/tproxy.conf
if [[ ! -s "$XRAY_PATH_FILE" ]]; then
    printf '/vless-%s\n' "$(openssl rand -hex 12)" > "$XRAY_PATH_FILE"
fi
chmod 0600 "$XRAY_PATH_FILE"
XRAY_PATH="$(cat "$XRAY_PATH_FILE")"
[[ "$XRAY_PATH" =~ ^/vless-[a-f0-9]{24}$ ]] || die "Stored VLESS path is invalid."

if [[ "$UPDATING" == "1" ]]; then
    echo "Updating WEB PANEL PROXY V 2.1.0..."
else
    echo "Configuring WEB PANEL PROXY V 2.1.0..."
fi
INSTALL_CREDENTIALS="/etc/web-proxy-panel/install-credentials"
if [[ "$UPDATING" == "1" ]]; then
    ADMIN="$(python3 - "$DATA_FILE" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    d=json.load(f)
admin=d.get("admin",{})
if not isinstance(admin.get("user"),str) or not admin.get("user") or not isinstance(admin.get("hash"),str) or not admin.get("hash"):
    raise SystemExit(1)
print(admin["user"])
PY
)" || die "Existing administrator data is invalid. Run the full installer instead."
    PASS=""
elif [[ -s "$INSTALL_CREDENTIALS" ]]; then
    ADMIN="$(sed -n '1p' "$INSTALL_CREDENTIALS")"
    PASS="$(sed -n '2p' "$INSTALL_CREDENTIALS")"
    rm -f "$INSTALL_CREDENTIALS"
    [[ -n "$ADMIN" && -n "$PASS" ]] || die "Panel credentials are invalid."
else
    read -r -p "Логин администратора [admin]: " ADMIN
    ADMIN="${ADMIN:-admin}"
    while true; do
        read -r -s -p "Пароль администратора: " PASS
        echo
        [[ ${#PASS} -ge 8 ]] || { echo "Пароль должен содержать минимум 8 символов."; continue; }
        break
    done
fi

echo "[1/6] Writing manager..."

for module in wpp_subscriptions.py wpp_panel_extras.py wpp_ui.py wpp_metrics.py wpp_update.py; do
    [[ -s "$BASE/$module" ]] || die "Package is incomplete: $module is missing."
    install -o root -g root -m 0644 "$BASE/$module" "$APP_DIR/$module"
done

cat > "$MANAGER" <<'PY'
#!/usr/bin/env python3
import copy, fcntl, grp, json, os, re, secrets, shutil, subprocess, sys, time, uuid
sys.path.insert(0,"/opt/tproxy-panel")
from wpp_subscriptions import mutate as mutate_subscription, issue as issue_subscription, SubscriptionError

USERS="/etc/web-proxy-panel/users.json"
PROFILES="/etc/tproxy-server/profiles.json"
UNIT_DIR="/etc/systemd/system"
FIREWALL_SCRIPT="/usr/local/sbin/web-proxy-panel-user-firewall"
MT_BIN="/opt/MTProxy/objs/bin/mtproto-proxy"
MT_AES="/etc/mtproxy/proxy-secret"
MT_CONF="/etc/mtproxy/proxy-multi.conf"
XRAY_BIN="/opt/web-panel-proxy/xray/xray"
XRAY_CONFIG="/etc/web-panel-proxy-xray/config.json"
XRAY_PATH_FILE="/etc/web-proxy-panel/xray-path"
XRAY_CERT="/etc/web-panel-proxy-xray/tls/domain.crt"
XRAY_KEY="/etc/web-panel-proxy-xray/tls/domain.key"
XRAY_SERVICE="web-panel-proxy-xray.service"
XRAY_TLS_SYNC="/usr/local/sbin/web-panel-proxy-sync-tls"
XRAY_VLESS_PORT=10000
XRAY_API="127.0.0.1:10085"
HYSTERIA_PORT=8443
CADDYFILE="/etc/caddy/Caddyfile"
TRAFFIC_FILE="/var/lib/tproxy-panel/traffic.json"
TRAFFIC_LOCK="/var/lib/tproxy-panel/traffic.lock"
UFW_HYSTERIA_MARKER="/etc/web-proxy-panel/hysteria-ufw-owned"
UFW_MTPROTO_MARKER="/etc/web-proxy-panel/mtproto-ufw-owned"
BASE_PORT=2399
BASE_STATS=8889
MAX_USERS=32

def run(*args, check=False, timeout=60):
    p=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=timeout)
    if check and p.returncode:
        raise RuntimeError(p.stderr.strip() or "command failed")
    return p

def load():
    try:
        with open(USERS,encoding="utf-8") as f:
            d=json.load(f)
            d.setdefault("users",[])
            d.setdefault("traffic",{})
            d.setdefault("subscriptions",[])
            # Remove the two withdrawn experimental protocols. Subscription
            # records keep working with their stable VLESS/Hysteria2 profiles.
            d["users"]=[u for u in d["users"] if u.get("protocol","web") not in ("mieru","naive")]
            for sub in d["subscriptions"]:
                supported=[p for p in sub.get("protocols",[]) if p in ("vless","hysteria")]
                sub["protocols"]=supported or ["vless","hysteria"]
            for u in d["users"]:
                protocol=u.setdefault("protocol","web")
                # V2.2 briefly exposed experimental per-profile transports and
                # ports.  Stable V2.1 deliberately has one tested VLESS XHTTP
                # listener behind Caddy/443 and one Hysteria2 UDP/8443 listener.
                # Normalize those records while preserving IDs and secrets.
                if protocol=="vless":
                    u["backend_port"]=443
                    for key in ("transport","path","xray_port","xhttp_mode","fingerprint","legacy_shared"):
                        u.pop(key,None)
                elif protocol=="hysteria":
                    u["backend_port"]=HYSTERIA_PORT
                    for key in ("udp_idle_timeout","masquerade"):
                        u.pop(key,None)
            return d
    except FileNotFoundError:
        return {"users":[],"traffic":{},"subscriptions":[]}

def save(d):
    tmp=USERS+".tmp"
    with open(tmp,"w",encoding="utf-8") as f:
        json.dump(d,f,ensure_ascii=True,indent=2)
    os.chmod(tmp,0o600)
    os.replace(tmp,USERS)

def port_in_use(port):
    # Do not rely solely on users.json: a stopped/old installation can still
    # have an MTProxy process listening on a port that is absent from the file.
    sockets=run("ss","-lnt").stdout or ""
    return re.search(r"[:.]%d\b" % int(port),sockets) is not None

def alloc_ports(d):
    used={int(u.get("backend_port",0)) for u in d["users"]}
    used_stats={int(u.get("stats_port",0)) for u in d["users"]}
    p,s=BASE_PORT,BASE_STATS
    while p in used or s in used_stats or port_in_use(p) or port_in_use(s):
        p+=1; s+=1
    if p>=BASE_PORT+MAX_USERS:
        raise RuntimeError("Maximum panel users reached")
    return p,s

def write_unit(u):
    if u.get("protocol","web") not in ("web","mtproto"):
        return
    path=os.path.join(UNIT_DIR,f"web-proxy-user-{u['id']}.service")
    content=f"""[Unit]
Description=WEB Proxy User {u['id']}
After=network-online.target web-proxy-panel-firewall.service
Wants=network-online.target
Requires=web-proxy-panel-firewall.service

[Service]
Type=simple
User=root
Group=root
ExecStart={MT_BIN} -u nobody -p {int(u['stats_port'])} -H {int(u['backend_port'])} -S {u['secret']} --aes-pwd {MT_AES} {MT_CONF} -M 1
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
"""
    tmp=path+".tmp"
    with open(tmp,"w",encoding="utf-8") as f: f.write(content)
    os.chmod(tmp,0o644)
    os.replace(tmp,path)

def sync_firewall(d):
    # Preserve the last counters before recreating the nftables table.
    try: collect_traffic(d)
    except Exception: pass
    web_ports=[int(u["backend_port"]) for u in d["users"] if u.get("enabled",True) and u.get("protocol","web")=="web"]
    mtproto_ports=[int(u["backend_port"]) for u in d["users"] if u.get("enabled",True) and u.get("protocol","web")=="mtproto"]
    stats=[int(u["stats_port"]) for u in d["users"] if u.get("enabled",True) and u.get("protocol","web") in ("web","mtproto") and u.get("stats_port")]
    hysteria_enabled=any(u.get("enabled",True) and u.get("protocol")=="hysteria" for u in d["users"])
    lines=[
        "#!/usr/bin/env bash",
        "set -e",
        "nft list table inet web_proxy_panel >/dev/null 2>&1 && nft delete table inet web_proxy_panel || true",
        "nft add table inet web_proxy_panel",
        "nft 'add chain inet web_proxy_panel input { type filter hook input priority -20; policy accept; }'",
        "nft 'add chain inet web_proxy_panel output { type filter hook output priority -20; policy accept; }'",
        "nft 'add rule inet web_proxy_panel output oifname \"lo\" tcp dport 2398 counter comment \"wpp:primary:up\"'",
        "nft 'add rule inet web_proxy_panel input iifname \"lo\" tcp sport 2398 counter comment \"wpp:primary:down\"'"
    ]
    for u in d["users"]:
        if u.get("enabled",True) and u.get("protocol","web")=="web":
            uid=u["id"]
            port=int(u["backend_port"])
            lines.append("nft 'add rule inet web_proxy_panel output oifname \"lo\" tcp dport %d counter comment \"wpp:%s:up\"'"%(port,uid))
            lines.append("nft 'add rule inet web_proxy_panel input iifname \"lo\" tcp sport %d counter comment \"wpp:%s:down\"'"%(port,uid))
        elif u.get("enabled",True) and u.get("protocol")=="mtproto":
            uid=u["id"]
            port=int(u["backend_port"])
            # nft requires the terminal verdict before the optional rule comment.
            lines.append("nft 'add rule inet web_proxy_panel input iifname != \"lo\" tcp dport %d counter accept comment \"wpp:%s:up\"'"%(port,uid))
            lines.append("nft 'add rule inet web_proxy_panel output oifname != \"lo\" tcp sport %d counter accept comment \"wpp:%s:down\"'"%(port,uid))
    if web_ports:
        lines.append("nft 'add rule inet web_proxy_panel input iifname != \"lo\" tcp dport { %s } counter drop'" % ",".join(map(str,sorted(web_ports))))
    if stats:
        lines.append("nft 'add rule inet web_proxy_panel input iifname != \"lo\" tcp dport { %s } counter drop'" % ",".join(map(str,sorted(stats))))
    if hysteria_enabled:
        lines.append("nft 'add rule inet web_proxy_panel input udp dport %d counter accept'" % HYSTERIA_PORT)
    tmp=FIREWALL_SCRIPT+".tmp"
    with open(tmp,"w",encoding="utf-8") as f: f.write("\n".join(lines)+"\n")
    os.chmod(tmp,0o750)
    os.replace(tmp,FIREWALL_SCRIPT)
    run(FIREWALL_SCRIPT,check=True)
    if shutil.which("ufw"):
        status=run("ufw","status").stdout or ""
        if "Status: active" in status:
            # Track only rules actually created by WPP, so deleting a client
            # never removes a pre-existing administrator firewall rule.
            previous_mt=set()
            if os.path.exists(UFW_MTPROTO_MARKER):
                try:
                    previous_mt={int(x) for x in re.findall(r"\b\d{1,5}\b",open(UFW_MTPROTO_MARKER,encoding="ascii").read()) if 1 <= int(x) <= 65535}
                except Exception:
                    previous_mt=set()
            desired_mt=set(mtproto_ports)
            for port in sorted(previous_mt-desired_mt):
                run("ufw","--force","delete","allow",str(port)+"/tcp")
            owned_mt=previous_mt & desired_mt
            status=run("ufw","status").stdout or ""
            for port in sorted(desired_mt):
                present=re.search(r"(?m)^%d/tcp\s+ALLOW\b" % port,status) is not None
                if not present:
                    added=run("ufw","--force","allow",str(port)+"/tcp","comment","WEB PANEL PROXY MTProto")
                    if added.returncode:
                        raise RuntimeError("Could not open MTProto in UFW: "+(added.stderr or added.stdout)[-1000:])
                    owned_mt.add(port)
            if owned_mt:
                with open(UFW_MTPROTO_MARKER,"w",encoding="ascii") as f: f.write("".join(str(p)+"\n" for p in sorted(owned_mt)))
                os.chmod(UFW_MTPROTO_MARKER,0o600)
            elif os.path.exists(UFW_MTPROTO_MARKER):
                os.unlink(UFW_MTPROTO_MARKER)
            previous=[]
            if os.path.exists(UFW_HYSTERIA_MARKER):
                try:
                    marker=open(UFW_HYSTERIA_MARKER,encoding="ascii").read()
                    previous=[int(x) for x in re.findall(r"\b\d{1,5}\b",marker) if 1 <= int(x) <= 65535]
                    if not previous and marker.strip()=="owned": previous=[HYSTERIA_PORT]
                except Exception:
                    previous=[]
            # Close only ports recorded as owned by WEB PANEL PROXY.  This also
            # removes experimental V2.2 Hysteria ports during a stable rollback.
            for port in sorted(set(previous)-({HYSTERIA_PORT} if hysteria_enabled else set())):
                run("ufw","--force","delete","allow",str(port)+"/udp")
            status=run("ufw","status").stdout or ""
            present=re.search(r"(?m)^%d/udp\s+ALLOW\b" % HYSTERIA_PORT,status) is not None
            if hysteria_enabled and not present:
                added=run("ufw","--force","allow",str(HYSTERIA_PORT)+"/udp","comment","WEB PANEL PROXY Hysteria 2")
                if added.returncode:
                    raise RuntimeError("Could not open Hysteria 2 in UFW: "+(added.stderr or added.stdout)[-1000:])
            if hysteria_enabled:
                with open(UFW_HYSTERIA_MARKER,"w",encoding="ascii") as f: f.write(str(HYSTERIA_PORT)+"\n")
                os.chmod(UFW_HYSTERIA_MARKER,0o600)
            elif os.path.exists(UFW_HYSTERIA_MARKER):
                os.unlink(UFW_HYSTERIA_MARKER)

def sync_profiles(d):
    with open(PROFILES,encoding="utf-8") as f:
        old=json.load(f)
    keep=[p for p in old.get("profiles",[]) if not str(p.get("name","")).startswith("panel:")]
    for u in d["users"]:
        if u.get("enabled",True) and u.get("protocol","web")=="web":
            keep.append({
                "name":"panel:"+u["id"],
                "secret":u["secret"],
                "backend":"127.0.0.1:%d"%int(u["backend_port"]),
                "carrier_mode":"https"
            })
    tmp=PROFILES+".tmp"
    with open(tmp,"w",encoding="utf-8") as f:
        json.dump({"profiles":keep},f,ensure_ascii=True,indent=2)
    os.chmod(tmp,0o400)
    c=run("/usr/local/bin/tproxy-server","-config","/etc/tproxy-server/config.json","-profiles-file",tmp,"-check")
    if c.returncode:
        try: os.unlink(tmp)
        except FileNotFoundError: pass
        raise RuntimeError("tproxy-server config check failed: "+(c.stderr or c.stdout)[-2000:])
    os.replace(tmp,PROFILES)

def sync_xray(d):
    with open(XRAY_PATH_FILE,encoding="utf-8") as f:
        xray_path=f.read().strip()
    if not re.fullmatch(r"/vless-[a-f0-9]{24}",xray_path):
        raise RuntimeError("Invalid stored VLESS path")
    vless=[]
    hysteria=[]
    for u in d["users"]:
        if not u.get("enabled",True):
            continue
        protocol=u.get("protocol","web")
        if protocol=="vless":
            vless.append({"id":u["secret"],"email":"panel:"+u["id"],"level":0})
        elif protocol=="hysteria":
            hysteria.append({"auth":u["secret"],"email":"panel:"+u["id"],"level":0})
    inbounds=[]
    if vless:
        inbounds.append({
            "tag":"vless-xhttp",
            "listen":"127.0.0.1",
            "port":XRAY_VLESS_PORT,
            "protocol":"vless",
            "settings":{"clients":vless,"decryption":"none"},
            "streamSettings":{
                # Xray's JSON stream selector is named "network".
                "network":"xhttp",
                "security":"none",
                "xhttpSettings":{"path":xray_path,"mode":"auto"}
            },
            "sniffing":{"enabled":True,"destOverride":["http","tls","quic"],"routeOnly":True}
        })
    if hysteria:
        if not (os.path.isfile(XRAY_CERT) and os.path.isfile(XRAY_KEY)):
            raise RuntimeError("TLS certificate for Hysteria 2 is not ready")
        inbounds.append({
            "tag":"hysteria2",
            "listen":"0.0.0.0",
            "port":HYSTERIA_PORT,
            "protocol":"hysteria",
            "settings":{"version":2,"clients":hysteria},
            "streamSettings":{
                "network":"hysteria",
                "security":"tls",
                "tlsSettings":{
                    "alpn":["h3"],
                    "minVersion":"1.3",
                    "certificates":[{"certificateFile":XRAY_CERT,"keyFile":XRAY_KEY}]
                },
                "hysteriaSettings":{"version":2,"udpIdleTimeout":60}
            },
            "sniffing":{"enabled":True,"destOverride":["http","tls","quic"],"routeOnly":True}
        })
    config={
        "log":{"loglevel":"warning"},
        "api":{"tag":"api","listen":XRAY_API,"services":["StatsService"]},
        "stats":{},
        "policy":{
            "levels":{"0":{"statsUserUplink":True,"statsUserDownlink":True}},
            "system":{"statsInboundUplink":True,"statsInboundDownlink":True}
        },
        "inbounds":inbounds,
        "outbounds":[{"tag":"direct","protocol":"freedom"}]
    }
    # Xray selects the configuration parser from the final extension.  A name
    # such as config.json.tmp is rejected before JSON parsing, so keep .json
    # as the temporary file's last suffix.
    tmp=os.path.splitext(XRAY_CONFIG)[0]+".tmp.json"
    with open(tmp,"w",encoding="utf-8") as f:
        json.dump(config,f,ensure_ascii=True,indent=2)
    os.chown(tmp,0,grp.getgrnam("xray").gr_gid)
    os.chmod(tmp,0o640)
    check=run(XRAY_BIN,"run","-test","-config",tmp,timeout=60)
    if check.returncode:
        try: os.unlink(tmp)
        except FileNotFoundError: pass
        raise RuntimeError("Xray config check failed: "+(check.stderr or check.stdout)[-3000:])
    os.replace(tmp,XRAY_CONFIG)
    if inbounds:
        run("systemctl","enable",XRAY_SERVICE,check=True)
        run("systemctl","restart",XRAY_SERVICE,check=True)
        if run("systemctl","is-active","--quiet",XRAY_SERVICE).returncode:
            st=run("systemctl","status",XRAY_SERVICE,"--no-pager","--full")
            log=run("journalctl","-u",XRAY_SERVICE,"-n","50","--no-pager")
            raise RuntimeError("Xray failed: "+((st.stdout or st.stderr)+"\n"+(log.stdout or log.stderr))[-4000:])
    else:
        run("systemctl","disable","--now",XRAY_SERVICE,check=False)

def _load_traffic():
    try:
        with open(TRAFFIC_FILE,encoding="utf-8") as f:
            value=json.load(f)
            return value if isinstance(value,dict) else {}
    except Exception:
        return {}

def _save_traffic(value):
    os.makedirs(os.path.dirname(TRAFFIC_FILE),mode=0o700,exist_ok=True)
    tmp=TRAFFIC_FILE+".tmp"
    with open(tmp,"w",encoding="utf-8") as f:
        json.dump(value,f,ensure_ascii=True,indent=2)
    os.chmod(tmp,0o600)
    os.replace(tmp,TRAFFIC_FILE)

def _nft_traffic():
    result={}
    p=run("nft","-j","list","table","inet","web_proxy_panel",timeout=10)
    if p.returncode: return result
    try: doc=json.loads(p.stdout)
    except Exception: return result
    for item in doc.get("nftables",[]):
        rule=item.get("rule",{})
        comment=str(rule.get("comment", ""))
        m=re.fullmatch(r"wpp:([A-Za-z0-9_-]+):(up|down)",comment)
        if not m: continue
        count=0
        for expr in rule.get("expr",[]):
            if "counter" in expr:
                count=int(expr["counter"].get("bytes",0)); break
        result.setdefault(m.group(1),{"up":0,"down":0})[m.group(2)]=count
    return result

def _xray_traffic():
    result={}
    if run("systemctl","is-active","--quiet",XRAY_SERVICE).returncode:
        return result
    p=run(XRAY_BIN,"api","statsquery","--server="+XRAY_API,timeout=15)
    if p.returncode: return result
    try:
        start=p.stdout.find("{")
        doc=json.loads(p.stdout[start:])
    except Exception:
        return result
    for stat in doc.get("stat",[]):
        m=re.fullmatch(r"user>>>panel:([a-f0-9]+)>>>traffic>>>(uplink|downlink)",str(stat.get("name","")))
        if not m: continue
        key="up" if m.group(2)=="uplink" else "down"
        result.setdefault(m.group(1),{"up":0,"down":0})[key]=int(stat.get("value",0))
    return result

def _collect_traffic_unlocked(d=None):
    d=d or load()
    now=int(time.time())
    current=_nft_traffic()
    current.update(_xray_traffic())
    state=_load_traffic()
    targets={"primary":{"protocol":"web","enabled":True}}
    targets.update({u["id"]:u for u in d.get("users",[])})
    service_states={}
    for uid,u in targets.items():
        raw=current.get(uid)
        entry=state.setdefault(uid,{"up":0,"down":0,"raw_up":0,"raw_down":0,"last_change":0})
        changed=False
        for direction in ("up","down"):
            # A missing API/counter sample is not a reset to zero. Keeping the
            # baseline prevents counting all historical bytes again next time.
            if raw is None: continue
            value=max(0,int(raw.get(direction,0)))
            previous=max(0,int(entry.get("raw_"+direction,0)))
            delta=value-previous if value>=previous else value
            if delta>0:
                entry[direction]=max(0,int(entry.get(direction,0)))+delta
                changed=True
            entry["raw_"+direction]=value
        if changed: entry["last_change"]=now
        protocol=u.get("protocol","web")
        if uid=="primary":
            unit="mtproxy.service"
        elif protocol=="web":
            unit="web-proxy-user-"+uid+".service"
        elif protocol in ("vless","hysteria"):
            unit=XRAY_SERVICE
        elif protocol=="mtproto":
            unit="web-proxy-user-"+uid+".service"
        else:
            unit=""
        if unit not in service_states:
            service_states[unit]=(run("systemctl","is-active","--quiet",unit).returncode==0)
        entry["service_active"]=service_states[unit] and u.get("enabled",True)
        if raw is not None: entry["updated_at"]=now
        entry["protocol"]=protocol
    _save_traffic(state)
    return state

def collect_traffic(d=None):
    os.makedirs(os.path.dirname(TRAFFIC_LOCK),mode=0o700,exist_ok=True)
    with open(TRAFFIC_LOCK,"a+",encoding="ascii") as lock:
        os.chmod(TRAFFIC_LOCK,0o600)
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
        return _collect_traffic_unlocked(d)

def remove_old_units(d):
    keep={"web-proxy-user-"+u["id"]+".service" for u in d["users"] if u.get("enabled",True) and u.get("protocol","web") in ("web","mtproto")}
    for name in os.listdir(UNIT_DIR):
        if name.startswith("web-proxy-user-") and name.endswith(".service") and name not in keep:
            run("systemctl","disable","--now",name,check=False)
            try: os.remove(os.path.join(UNIT_DIR,name))
            except FileNotFoundError: pass

def apply(d,restart=True):
    with open(PROFILES,encoding="utf-8") as f:
        old_profiles=f.read()
    if os.path.exists(XRAY_CONFIG):
        with open(XRAY_CONFIG,encoding="utf-8") as f:
            old_xray=f.read()
    else:
        old_xray=None
    with open(CADDYFILE,encoding="utf-8") as f:
        old_caddy=f.read()
    old_users=load()
    try:
        for u in d["users"]:
            if u.get("enabled",True): write_unit(u)
        remove_old_units(d)
        sync_profiles(d)
        sync_firewall(d)
        run("systemctl","daemon-reload",check=True)
        sync_xray(d)
        if restart:
            for u in d["users"]:
                if u.get("enabled",True) and u.get("protocol","web") in ("web","mtproto"):
                    unit="web-proxy-user-"+u["id"]+".service"
                    run("systemctl","enable",unit,check=True)
                    run("systemctl","restart",unit,check=True)
                    if run("systemctl","is-active","--quiet",unit).returncode:
                        st=run("systemctl","status",unit,"--no-pager","--full")
                        log=run("journalctl","-u",unit,"-n","30","--no-pager")
                        detail=((st.stdout or st.stderr)+"\n"+(log.stdout or log.stderr))[-3500:]
                        raise RuntimeError("User MTProxy failed: "+detail)
                    # Verify the actual WEB backend listener on its loopback port.
                    chk=run("bash","-lc",f"ss -lnt | grep -Eq ':{int(u['backend_port'])}\\b'")
                    if chk.returncode:
                        st=run("systemctl","status",unit,"--no-pager","--full")
                        raise RuntimeError("User MTProxy is active but backend port is not listening: "+(st.stdout or st.stderr)[-2000:])
            run("systemctl","restart","tproxy-server.service",check=True)
    except Exception:
        with open(PROFILES,"w",encoding="utf-8") as f: f.write(old_profiles)
        os.chmod(PROFILES,0o400)
        save(old_users)
        if old_xray is None:
            try: os.unlink(XRAY_CONFIG)
            except FileNotFoundError: pass
        else:
            with open(XRAY_CONFIG,"w",encoding="utf-8") as f: f.write(old_xray)
            os.chmod(XRAY_CONFIG,0o640)
        if old_xray is None:
            run("systemctl","disable","--now",XRAY_SERVICE,check=False)
        else:
            run("systemctl","restart",XRAY_SERVICE,check=False)
        with open(CADDYFILE,"w",encoding="utf-8") as f: f.write(old_caddy)
        try: shutil.chown(CADDYFILE,user="root",group="caddy")
        except Exception: pass
        os.chmod(CADDYFILE,0o640)
        run("systemctl","reload","caddy.service",check=False)
        raise

def add(protocol,name):
    d=load()
    before=copy.deepcopy(d)
    # A failed request from an older manager can leave a systemd unit in an
    # auto-restart loop even though it is absent from users.json. Remove such
    # orphan units before choosing ports for the next user.
    remove_old_units(d)
    run("systemctl","daemon-reload",check=True)
    if protocol not in ("web","mtproto","vless","hysteria"):
        raise RuntimeError("Unknown proxy protocol")
    if sum(not u.get("subscription_id") for u in d["users"])>=MAX_USERS:
        raise RuntimeError("Maximum panel users reached")
    u={"id":secrets.token_hex(8),"name":name.strip(),"protocol":protocol,"enabled":True,"created_at":int(time.time())}
    if protocol in ("web","mtproto"):
        port,stats=alloc_ports(d)
        u.update({"secret":secrets.token_hex(16),"backend_port":port,"stats_port":stats})
    elif protocol=="vless":
        u.update({"secret":str(uuid.uuid4()),"backend_port":443})
    elif protocol=="hysteria":
        u.update({"secret":str(uuid.uuid4()),"backend_port":HYSTERIA_PORT})
        tls=run(XRAY_TLS_SYNC)
        if tls.returncode:
            raise RuntimeError("Hysteria 2 TLS certificate is not ready: "+(tls.stderr or tls.stdout)[-1500:])
    d["users"].append(u)
    save(d)
    try:
        apply(d,True)
    except Exception:
        # Re-apply the saved state so that a failed new unit is stopped and
        # deleted. Without this rollback a restart loop holds the same port
        # and every following attempt to create a user fails as well.
        save(before)
        try: apply(before,True)
        except Exception: pass
        raise
    print(json.dumps(u,ensure_ascii=True))

def delete(uid):
    before=load()
    if any(u.get("id")==uid and u.get("subscription_id") for u in before["users"]):
        raise RuntimeError("Управляйте этим профилем через вкладку Подписки.")
    try: collect_traffic(before)
    except Exception: pass
    d=copy.deepcopy(before)
    if not any(u.get("id")==uid for u in d["users"]):
        # Deletion may be repeated after a browser refresh or after a prior
        # successful request.  Treat that situation as an already-completed
        # deletion instead of returning a traceback to the panel.
        return False
    d["users"]=[u for u in d["users"] if u.get("id")!=uid]
    save(d)
    try:
        apply(d,True)
    except Exception:
        save(before)
        try: apply(before,True)
        except Exception: pass
        raise
    return True

def edit_user(uid,enabled=None,name=None):
    before=load()
    target=next((u for u in before['users'] if u['id']==uid),None)
    if uid=='primary' or target is None or target.get('subscription_id'):
        raise ValueError('Отдельный пользователь не найден или недоступен для изменения.')
    if enabled is not None and not isinstance(enabled,bool): raise ValueError('Некорректное состояние доступа.')
    if name is not None and (not name.strip() or len(name.strip())>80 or any(ord(c)<32 for c in name)):
        raise ValueError('Имя должно содержать от 1 до 80 символов.')
    after=copy.deepcopy(before)
    user=next(u for u in after['users'] if u['id']==uid)
    if enabled is not None: user['enabled']=enabled
    if name is not None: user['name']=name.strip()
    if after==before: return
    runtime_changed=user.get('enabled',True)!=target.get('enabled',True)
    if runtime_changed: collect_traffic(before)
    save(after)
    if runtime_changed:
        try: apply(after,True)
        except Exception:
            save(before)
            try: apply(before,True)
            except Exception: raise RuntimeError('Не удалось восстановить службы. Проверьте VPS через SSH.')
            raise

def init():
    d=load()
    # Persist stable normalization when upgrading or rolling back from an
    # experimental build; existing user IDs and secrets remain unchanged.
    save(d)
    for u in d["users"]:
        if u.get("enabled",True): write_unit(u)
    remove_old_units(d)
    sync_profiles(d)
    sync_firewall(d)
    run("systemctl","daemon-reload",check=True)
    sync_xray(d)
    collect_traffic(d)

def subscription_command():
    try:
        raw=sys.stdin.read(8193)
        if len(raw)>8192: raise SubscriptionError("Request too large")
        request=json.loads(raw)
        if not isinstance(request,dict): raise SubscriptionError("Invalid request")
        before=load()
        after,result=(issue_subscription if request.get("operation")=="fetch" else mutate_subscription)(before,request)
        changed=before["users"]!=after["users"]
        if changed:
            if any(u.get("enabled",True) and u.get("protocol")=="hysteria" for u in after["users"]):
                run(XRAY_TLS_SYNC,check=True)
            collect_traffic(before)
            try:
                sync_firewall(after)
                sync_xray(after)
                save(after)
            except Exception:
                # Database remains unchanged until both runtime components pass.
                # Restore only managed Xray/firewall; never restart WEB backends.
                try:
                    sync_firewall(before)
                    sync_xray(before)
                except Exception:
                    raise RuntimeError("Не удалось восстановить службы после ошибки; требуется диагностика VPS.")
                raise
        elif after!=before:
            save(after)
        print(json.dumps(result,ensure_ascii=True))
    except SubscriptionError as exc:
        print(json.dumps({"ok":False,"status":exc.status,"code":exc.code,"message":str(exc)},ensure_ascii=True))
    except Exception:
        print(json.dumps({"ok":False,"status":503,"code":"backend","message":"Не удалось применить подписку. Проверьте Xray и TLS на сервере."}))

cmd=sys.argv[1] if len(sys.argv)>1 else "init"
# Serialize state changes, including slot allocation, with CLI and the panel.
# Traffic has its own lock and only reads users.json via atomic replacement.
if cmd not in ("users","traffic"):
    manager_lock=open("/etc/web-proxy-panel/manager.lock","a+")
    os.chmod(manager_lock.name,0o600)
    deadline=time.monotonic()+15
    while True:
        try:
            fcntl.flock(manager_lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic()>deadline: raise SystemExit("Менеджер занят. Повторите запрос позже.")
            time.sleep(0.1)
if cmd=="init": init()
elif cmd=="subscription": subscription_command()
elif cmd=="add": add(sys.argv[2]," ".join(sys.argv[3:]))
elif cmd=="delete": print(json.dumps({"deleted":delete(sys.argv[2])}))
elif cmd=="set-user":
    if sys.argv[3] not in ('0','1'): raise SystemExit('Invalid enabled state')
    edit_user(sys.argv[2],enabled=sys.argv[3]=='1')
    print(json.dumps({'ok':True}))
elif cmd=="rename-user":
    edit_user(sys.argv[2],name=sys.argv[3])
    print(json.dumps({'ok':True}))
elif cmd=="sync": apply(load(),True)
elif cmd=="firewall": sync_firewall(load())
elif cmd=="traffic":
    traffic_state=collect_traffic()
    print(json.dumps(traffic_state,ensure_ascii=True))
elif cmd=="users": print(json.dumps(load(),ensure_ascii=True))
else: raise SystemExit("usage: init|add|delete|sync|firewall|traffic|users")

PY

chmod 0755 "$MANAGER"

cat > "$FIREWALL_SERVICE_FILE" <<'EOF'
[Unit]
Description=WEB PANEL PROXY persistent user-port firewall
After=nftables.service
PartOf=nftables.service
Before=network-online.target tproxy-panel.service

[Service]
Type=oneshot
RemainAfterExit=true
ExecStart=/usr/local/sbin/web-proxy-panelctl firewall
ExecReload=/usr/local/sbin/web-proxy-panelctl firewall
ExecStop=-/usr/sbin/nft delete table inet web_proxy_panel

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "$FIREWALL_SERVICE_FILE"

cat > /etc/systemd/system/web-proxy-panel-traffic.service <<'EOF'
[Unit]
Description=WEB PANEL PROXY traffic collector
After=network-online.target web-proxy-panel-firewall.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/web-proxy-panelctl traffic
TimeoutStartSec=60
Nice=10
EOF

cat > /etc/systemd/system/web-proxy-panel-traffic.timer <<'EOF'
[Unit]
Description=Collect WEB PANEL PROXY traffic every 10 seconds

[Timer]
OnActiveSec=5s
OnUnitInactiveSec=10s
AccuracySec=1s
Unit=web-proxy-panel-traffic.service

[Install]
WantedBy=timers.target
EOF
chmod 0644 /etc/systemd/system/web-proxy-panel-traffic.service /etc/systemd/system/web-proxy-panel-traffic.timer

# Resource metrics must not depend on the success of the proxy Stats API.
cat > /etc/systemd/system/web-panel-proxy-metrics.service <<'EOF'
[Unit]
Description=WEB PANEL PROXY VPS metrics

[Service]
Type=oneshot
User=root
Group=root
UMask=0077
ExecStart=/usr/bin/python3 /opt/tproxy-panel/wpp_metrics.py collect
TimeoutStartSec=20
Nice=10
EOF
cat > /etc/systemd/system/web-panel-proxy-metrics.timer <<'EOF'
[Unit]
Description=Collect WEB PANEL PROXY VPS metrics every 10 seconds

[Timer]
OnActiveSec=5s
OnUnitInactiveSec=10s
AccuracySec=1s
Unit=web-panel-proxy-metrics.service

[Install]
WantedBy=timers.target
EOF
chmod 0644 /etc/systemd/system/web-panel-proxy-metrics.service /etc/systemd/system/web-panel-proxy-metrics.timer

cat > /usr/local/sbin/web-panel-proxy-sync-tls <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
MODE="${1:-sync}"
[[ "$MODE" == "sync" || "$MODE" == "--maintain" || "$MODE" == "--force" ]] || {
    echo "Usage: web-panel-proxy-sync-tls [--maintain|--force]" >&2
    exit 2
}
exec 8>/run/lock/web-panel-proxy-tls.lock
flock -w 15 8 || { echo "Another TLS check is still running" >&2; exit 1; }
DOMAIN="$(sed -n 's/^Environment=TPROXY_HOSTNAME=//p' /etc/systemd/system/caddy.service.d/tproxy.conf 2>/dev/null | head -n1 || true)"
[[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] || { echo "Invalid WEB Proxy domain" >&2; exit 1; }
DEST_DIR="/etc/web-panel-proxy-xray/tls"
DEST_CERT="$DEST_DIR/domain.crt"
DEST_KEY="$DEST_DIR/domain.key"
LIVE_CERT="$(mktemp /tmp/wpp-live-certificate.XXXXXX)"
trap 'rm -f "$LIVE_CERT"' EXIT

read_live_certificate() {
    : > "$LIVE_CERT"
    timeout 12 openssl s_client -connect 127.0.0.1:443 -servername "$DOMAIN" </dev/null 2>/dev/null |
        openssl x509 -outform PEM -out "$LIVE_CERT" 2>/dev/null
}

if [[ "$MODE" != "sync" ]]; then
    needs_attention=0
    if ! read_live_certificate || ! openssl x509 -in "$LIVE_CERT" -noout -checkend 1209600 >/dev/null 2>&1; then
        needs_attention=1
    fi
    if [[ "$MODE" == "--force" || "$needs_attention" == 1 ]]; then
        caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
        # The canonical Caddyfile intentionally has `admin off`; SIGUSR1 is
        # Caddy's supported config reload signal for `caddy run` in this mode.
        if ! systemctl kill --signal=SIGUSR1 --kill-who=main caddy.service >/dev/null 2>&1; then
            systemctl restart caddy.service
        fi
        renewed=0
        for _ in $(seq 1 24); do
            if read_live_certificate && openssl x509 -in "$LIVE_CERT" -noout -checkend 0 >/dev/null 2>&1; then
                renewed=1
                break
            fi
            sleep 5
        done
        [[ "$renewed" == 1 ]] || { echo "Caddy did not provide a valid TLS certificate for $DOMAIN" >&2; exit 1; }
    fi
    read_live_certificate || { echo "Could not read the active TLS certificate for $DOMAIN" >&2; exit 1; }
    echo "TLS certificate: $(openssl x509 -in "$LIVE_CERT" -noout -enddate | cut -d= -f2-)"
fi

CADDY_USER="$(systemctl show -p User --value caddy.service 2>/dev/null || true)"
CADDY_USER="${CADDY_USER:-caddy}"
CADDY_HOME="$(getent passwd "$CADDY_USER" | cut -d: -f6 || true)"
SEARCH_ROOTS=(
    "${CADDY_HOME:-/var/lib/caddy}/.local/share/caddy/certificates"
    "/etc/caddy/caddy/.local/share/caddy/certificates"
    "/var/lib/caddy/.local/share/caddy/certificates"
    "/root/.local/share/caddy/certificates"
)
SOURCE_CERT=""
SOURCE_KEY=""
[[ "$MODE" == "sync" ]] || echo "Searching the Caddy certificate store..."
for root in "${SEARCH_ROOTS[@]}"; do
    [[ -d "$root" ]] || continue
    while IFS= read -r candidate; do
        key="${candidate%.crt}.key"
        [[ -s "$key" ]] || continue
        if openssl x509 -in "$candidate" -noout -checkend 86400 >/dev/null 2>&1; then
            SOURCE_CERT="$candidate"
            SOURCE_KEY="$key"
            break 2
        fi
    done < <(timeout 12 find "$root" -xdev -type f -path "*/${DOMAIN}/${DOMAIN}.crt" -print 2>/dev/null || true)
done
# Caddy can be started with a custom HOME/XDG_DATA_HOME by a pre-existing
# service.  Search only the known Caddy state directories as a bounded
# fallback; never scan the whole VPS.
if [[ -z "$SOURCE_CERT" ]]; then
    for root in /etc/caddy /var/lib/caddy /root/.local/share/caddy; do
        [[ -d "$root" ]] || continue
        while IFS= read -r candidate; do
            key="${candidate%.crt}.key"
            [[ -s "$key" ]] || continue
            if openssl x509 -in "$candidate" -noout -checkend 86400 >/dev/null 2>&1; then
                SOURCE_CERT="$candidate"
                SOURCE_KEY="$key"
                break 2
            fi
        done < <(timeout 12 find "$root" -xdev -type f -path "*/${DOMAIN}/${DOMAIN}.crt" -print 2>/dev/null || true)
    done
fi
[[ -n "$SOURCE_CERT" && -n "$SOURCE_KEY" ]] || { echo "Caddy TLS certificate for $DOMAIN was not found" >&2; exit 1; }
CERT_PUB="$(timeout 10 openssl x509 -in "$SOURCE_CERT" -pubkey -noout | openssl sha256)"
KEY_PUB="$(timeout 10 openssl pkey -in "$SOURCE_KEY" -pubout 2>/dev/null | openssl sha256)"
[[ -n "$CERT_PUB" && "$CERT_PUB" == "$KEY_PUB" ]] || { echo "Caddy certificate/private key mismatch" >&2; exit 1; }
install -d -o root -g xray -m 0750 "$DEST_DIR"
changed=0
if ! cmp -s "$SOURCE_CERT" "$DEST_CERT" 2>/dev/null; then
    install -o root -g xray -m 0640 "$SOURCE_CERT" "$DEST_CERT"
    changed=1
fi
if ! cmp -s "$SOURCE_KEY" "$DEST_KEY" 2>/dev/null; then
    install -o root -g xray -m 0640 "$SOURCE_KEY" "$DEST_KEY"
    changed=1
fi
if [[ "$changed" == 1 ]] && systemctl is-active --quiet web-panel-proxy-xray.service; then
    [[ "$MODE" == "sync" ]] || echo "Applying the renewed certificate to Hysteria2..."
    timeout 45 systemctl try-restart web-panel-proxy-xray.service || {
        echo "Xray did not restart within 45 seconds" >&2
        exit 1
    }
fi
[[ "$MODE" == "sync" ]] || echo "TLS maintenance completed."
SH
chmod 0750 /usr/local/sbin/web-panel-proxy-sync-tls

cat > /etc/systemd/system/web-panel-proxy-sync-tls.service <<'EOF'
[Unit]
Description=Synchronize Caddy certificate for WEB PANEL PROXY Xray
After=caddy.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/web-panel-proxy-sync-tls --maintain
TimeoutStartSec=180
EOF

cat > /etc/systemd/system/web-panel-proxy-sync-tls.timer <<'EOF'
[Unit]
Description=Refresh WEB PANEL PROXY Xray TLS certificate

[Timer]
OnBootSec=5min
OnUnitActiveSec=6h
RandomizedDelaySec=15min
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > /etc/systemd/system/web-panel-proxy-xray.service <<EOF
[Unit]
Description=WEB PANEL PROXY Xray (VLESS and Hysteria 2)
After=network-online.target caddy.service
Wants=network-online.target

[Service]
Type=simple
User=xray
Group=xray
ExecStart=$XRAY_BIN run -config $XRAY_CONFIG
Restart=on-failure
RestartSec=2
LimitNOFILE=1048576
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/var/lib/web-panel-proxy-xray

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 /etc/systemd/system/web-panel-proxy-sync-tls.service /etc/systemd/system/web-panel-proxy-sync-tls.timer /etc/systemd/system/web-panel-proxy-xray.service

systemctl daemon-reload
systemctl enable --now web-panel-proxy-sync-tls.timer
if ! /usr/local/sbin/web-panel-proxy-sync-tls; then
    echo "      NOTE: Caddy certificate is not available to Xray yet; VLESS remains available, while Hysteria 2 can be created after certificate issuance."
fi

echo "[2/6] Writing panel..."

cat > "$APP_FILE" <<'PY'
#!/usr/bin/env python3
import base64
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import grp
import threading
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlparse
from collections import defaultdict, deque
from wpp_subscriptions import PREFIX as SUB_PREFIX
from wpp_panel_extras import preview_document
from wpp_ui import page_layout, login_ui, dashboard_body, dashboard_page, users_ui, editor_ui, client_records
import wpp_metrics as server_metrics
import wpp_update as web_updates

HOST="127.0.0.1"
PORT=8090
DATA="/var/lib/tproxy-panel/data.json"
KEY="/var/lib/tproxy-panel/session.key"
DOMAIN=os.environ.get("WEBPROXY_DOMAIN","")
MTPROTO_HOST=os.environ.get("WEBPROXY_MTPROTO_HOST",DOMAIN)
PANEL_PATH=os.environ.get("WEBPROXY_PANEL_PATH","")
PRIMARY="/etc/web-proxy-panel/primary-secret"
PROFILES="/etc/tproxy-server/profiles.json"
USERS="/etc/web-proxy-panel/users.json"
TRAFFIC="/var/lib/tproxy-panel/traffic.json"
XRAY_PATH_FILE="/etc/web-proxy-panel/xray-path"
HYSTERIA_PORT=8443
MANAGER="/usr/local/sbin/web-proxy-panelctl"
QR="/usr/bin/qrencode"
LOGO="/opt/tproxy-panel/panel-logo.png"
SITE_INDEX="/srv/tproxy-site/index.html"
SITE_BACKUP="/var/lib/tproxy-panel/index.html.bak"
SITE_SOURCE="/var/lib/tproxy-panel/site-source.html"
SITE_SOURCE_BACKUP="/var/lib/tproxy-panel/site-source.html.bak"
SITE_CSS="/srv/tproxy-site/panel-site.css"
SITE_CSS_BACKUP="/var/lib/tproxy-panel/panel-site.css.bak"
SITE_JS="/srv/tproxy-site/panel-site.js"
SITE_JS_BACKUP="/var/lib/tproxy-panel/panel-site.js.bak"
MAX_HTML_BYTES=1024*1024
SITE_DRAFT="/var/lib/tproxy-panel/site-draft.html"
SUB_FETCH_SLOTS=threading.BoundedSemaphore(4)
SUB_RATE_LOCK=threading.Lock()
SUB_REQUESTS={}

# Presets are deliberately standalone: no CDN, no separate CSS/JS files and
# no icon font. This is essential because WEB Proxy exposes the cover page as
# one document, not as a conventional static-file web server.
PRESETS=[
 {"id":"countdown","name":"Обратный отсчёт","description":"Светлая страница с живым таймером и адаптацией для телефона.","html":'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#101b46"><title>Скоро открытие</title>
<style>*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;color:#111936;font-family:Inter,ui-sans-serif,system-ui,sans-serif;background:radial-gradient(circle at 12% 12%,#bce7ff,transparent 34%),radial-gradient(circle at 88% 92%,#d9c8ff,transparent 36%),#f5f7ff}.card{width:min(780px,100%);padding:clamp(32px,7vw,70px);text-align:center;border:1px solid #fff;border-radius:34px;background:#fffffff0;box-shadow:0 25px 70px #344f8b27}.mark{width:68px;height:68px;margin:0 auto 22px;display:grid;place-items:center;border-radius:22px;background:linear-gradient(135deg,#2868ff,#7b4dff);color:#fff;font-size:32px;box-shadow:0 14px 28px #4168ce55}h1{margin:0;font-size:clamp(34px,7vw,62px);letter-spacing:-.06em}h1 span{color:#2868ff}p{max-width:530px;margin:18px auto 0;color:#56637e;font-size:clamp(16px,2.5vw,20px);line-height:1.6}.timer{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;max-width:510px;margin:38px auto}.unit{padding:17px 8px;border-radius:20px;background:#f6f8ff;border:1px solid #e7ebfa}.n{display:block;font-size:clamp(28px,5vw,45px);font-weight:800;line-height:1}.l{display:block;margin-top:8px;color:#77829b;font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase}.note{display:inline-flex;align-items:center;gap:9px;padding:12px 16px;border-radius:99px;background:#eef3ff;color:#3c568d;font-size:14px}.dot{width:8px;height:8px;border-radius:50%;background:#2ccf91;box-shadow:0 0 0 5px #2ccf9128}@media(max-width:440px){.card{padding:35px 18px;border-radius:26px}.timer{gap:7px}.unit{padding:14px 4px;border-radius:15px}.l{font-size:9px}}</style></head>
<body><main class="card"><div class="mark">✦</div><h1><span>Скоро</span> открытие</h1><p>Мы готовим что-то особенное. Оставьте эту страницу открытой — запуск уже близко.</p><section class="timer" aria-label="Обратный отсчёт"><div class="unit"><b class="n" id="d">00</b><i class="l">дней</i></div><div class="unit"><b class="n" id="h">00</b><i class="l">часов</i></div><div class="unit"><b class="n" id="m">00</b><i class="l">минут</i></div><div class="unit"><b class="n" id="s">00</b><i class="l">секунд</i></div></section><div class="note"><span class="dot"></span> Следите за обновлениями</div></main><script>const end=Date.now()+14*864e5;function tick(){let x=Math.max(0,end-Date.now());const v=[Math.floor(x/864e5),Math.floor(x/36e5)%24,Math.floor(x/6e4)%60,Math.floor(x/1e3)%60];['d','h','m','s'].forEach((id,i)=>document.getElementById(id).textContent=String(v[i]).padStart(2,'0'))}tick();setInterval(tick,1000)</script></body></html>'''},
 {"id":"cars","name":"Продажа авто","description":"Тёмная автомобильная витрина с акцентом на заявки.","html":'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#0b0d14"><title>Автомобили скоро</title><style>*{box-sizing:border-box}body{margin:0;min-height:100vh;overflow:hidden;font-family:Inter,ui-sans-serif,system-ui,sans-serif;color:#f8f9fc;background:#0b0d14}.glow{position:fixed;inset:0;background:radial-gradient(ellipse at 18% 15%,#ef4f5d36,transparent 32%),radial-gradient(ellipse at 82% 84%,#ffbb5a22,transparent 35%)}main{position:relative;min-height:100vh;display:grid;align-content:center;max-width:1100px;margin:auto;padding:36px}.tag{display:inline-flex;width:max-content;padding:8px 12px;border:1px solid #ffffff22;border-radius:99px;background:#ffffff0b;color:#ffb861;font-size:12px;font-weight:800;letter-spacing:.1em;text-transform:uppercase}.hero{display:grid;grid-template-columns:1.1fr .9fr;gap:34px;align-items:center;margin-top:22px}.kicker{color:#ffb861;font-weight:700;letter-spacing:.08em;text-transform:uppercase}h1{margin:12px 0 16px;font-size:clamp(44px,8vw,86px);line-height:.95;letter-spacing:-.07em}p{margin:0;max-width:560px;color:#afb6c8;font-size:18px;line-height:1.7}.car{min-height:285px;display:grid;place-items:center;border:1px solid #ffffff14;border-radius:30px;background:linear-gradient(145deg,#1c2132,#10131d);box-shadow:0 26px 70px #0007;font-size:clamp(130px,22vw,230px);transform:rotate(-4deg)}.action{display:inline-block;margin-top:30px;padding:15px 21px;border-radius:14px;background:#f4f6ff;color:#121621;text-decoration:none;font-weight:800;box-shadow:0 12px 28px #0005}.foot{margin-top:42px;padding-top:20px;border-top:1px solid #ffffff14;color:#71798e;font-size:13px}@media(max-width:700px){main{padding:24px}.hero{grid-template-columns:1fr}.car{min-height:190px;order:-1}p{font-size:16px}}</style></head><body><div class="glow"></div><main><span class="tag">Новая коллекция</span><section class="hero"><div><div class="kicker">Премиальный выбор</div><h1>Авто,<br>которые<br>ждут вас.</h1><p>Готовим каталог автомобилей с прозрачной историей, честными ценами и персональным подбором.</p><a class="action" href="mailto:info@example.com">Получить уведомление →</a></div><div class="car" aria-label="Автомобиль">🏎️</div></section><div class="foot">СКОРО ОТКРЫТИЕ · ПОДБОР · ПРОВЕРКА · ДОСТАВКА</div></main></body></html>'''},
 {"id":"cats-repair","name":"КОТИКИ ЧИНЯТ САЙТ","description":"Весёлая автономная заглушка с анимированными котиками, прогрессом и интерактивной кнопкой.","html":'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#1a1a2e"><title>Котики чинят сайт</title><style>
*{box-sizing:border-box}html,body{margin:0;min-height:100%}body{min-height:100vh;display:flex;align-items:center;justify-content:center;overflow:hidden;padding:24px;color:#f0ece6;font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;background:radial-gradient(ellipse at 20% 50%,#ffc86414,transparent 60%),radial-gradient(ellipse at 80% 50%,#ff649614,transparent 60%),#1a1a2e}.stars{position:fixed;inset:0;pointer-events:none;overflow:hidden}.star{position:absolute;opacity:.3;font-size:1.5rem;animation:floatStar 8s ease-in-out infinite}.star:nth-child(1){top:10%;left:5%}.star:nth-child(2){top:20%;right:8%;animation-delay:1.5s}.star:nth-child(3){bottom:25%;left:10%;animation-delay:3s;font-size:2rem}.star:nth-child(4){right:5%;bottom:15%;animation-delay:4.5s}.star:nth-child(5){top:50%;left:2%;animation-delay:2s}.star:nth-child(6){top:40%;right:3%;animation-delay:3.5s}.box{position:relative;z-index:1;width:min(700px,100%);padding:48px 40px;text-align:center;border:1px solid #ffffff10;border-radius:48px;background:#ffffff0a;box-shadow:0 40px 80px #0006;backdrop-filter:blur(12px)}.cats{display:flex;justify-content:center;gap:24px;margin-bottom:28px;flex-wrap:wrap}.cat{display:inline-block;font-size:4.5rem;filter:drop-shadow(0 8px 24px #ffc86426);cursor:pointer;user-select:none;animation:catDance 1.8s ease-in-out infinite}.cat:nth-child(2){font-size:5rem;animation-delay:.3s}.cat:nth-child(3){animation-delay:.6s}.cat:nth-child(4){font-size:4.8rem;animation-delay:.9s}.cat:hover{animation-play-state:paused}.title{margin:0 0 8px;font-size:clamp(32px,7vw,44px);font-weight:900;letter-spacing:-.04em}.title span{color:#fbbf24}.subtitle{margin:0 0 28px;color:#a09088;font-size:17px;line-height:1.6}.track{height:8px;overflow:hidden;border-radius:8px;background:#ffffff0f}.bar{width:0;height:100%;border-radius:inherit;background:linear-gradient(90deg,#fbbf24,#f59e0b,#fbbf24);transition:width .08s linear}.progress-text{display:flex;justify-content:space-between;margin-top:10px;color:#756861;font-size:13px}.paws{color:#fbbf24;letter-spacing:2px}.status{min-height:72px;margin:24px 0 28px;padding:18px;display:flex;align-items:center;justify-content:center;gap:12px;flex-wrap:wrap;border:1px solid #ffffff0a;border-radius:20px;background:#ffffff08}.status-emoji{font-size:2rem;animation:pop 1s ease-in-out infinite}.message{font-size:17px}.message span{color:#fbbf24}.fun{display:inline-flex;align-items:center;justify-content:center;gap:10px;padding:15px 38px;border:0;border-radius:60px;color:#1a1a2e;background:linear-gradient(135deg,#fbbf24,#f59e0b);box-shadow:0 8px 24px #fbbf2433;font:700 17px inherit;cursor:pointer;transition:transform .2s,box-shadow .2s}.fun:hover{transform:scale(1.04);box-shadow:0 12px 32px #fbbf244d}.counter{margin-top:22px;color:#756861;font-size:14px}.counter b{color:#fbbf24;font-size:18px}@keyframes floatStar{50%{transform:translateY(-30px) rotate(180deg);opacity:.8}}@keyframes catDance{0%,100%{transform:rotate(-8deg)}25%{transform:rotate(8deg) translateY(-8px)}50%{transform:rotate(-5deg)}75%{transform:rotate(10deg) translateY(-5px)}}@keyframes pop{50%{transform:scale(1.2)}}@media(max-width:600px){.box{padding:32px 24px;border-radius:32px}.cats{gap:12px}.cat,.cat:nth-child(2),.cat:nth-child(4){font-size:3.2rem}.subtitle{font-size:15px}.message{font-size:14px}.fun{width:100%;padding:14px}.stars{display:none}}@media(max-width:400px){.cat,.cat:nth-child(2),.cat:nth-child(4){font-size:2.6rem}.box{padding:28px 16px}.status{padding:14px}}
</style></head><body><div class="stars"><span class="star">✨</span><span class="star">⭐</span><span class="star">🌟</span><span class="star">✨</span><span class="star">⭐</span><span class="star">🌟</span></div><main class="box"><div class="cats"><span class="cat">🐱</span><span class="cat">😺</span><span class="cat">😸</span><span class="cat">🐈</span></div><h1 class="title"><span>Котики</span> чинят сайт</h1><p class="subtitle">🐾 Мяу-инженеры уже в пути! Подождите немного… 🐾</p><section><div class="track"><div class="bar" id="progressBar"></div></div><div class="progress-text"><span class="paws">🐾🐾🐾</span><span id="progressPercent">0%</span><span class="paws">🐾🐾🐾</span></div></section><div class="status"><span class="status-emoji" id="statusEmoji">🔧</span><span class="message" id="statusMessage"><span>Котики</span> настраивают сервер…</span></div><button class="fun" id="funButton">🐾 Погладить котика 🐾</button><div class="counter">Котиков погладили: <b id="clickCount">0</b> раз</div></main><script>
(function(){const statuses=[['🔧','<span>Котики</span> настраивают сервер…'],['🐱','Один котик <span>залип</span> в клавиатуре…'],['💻','<span>Кот-программист</span> пишет мяу-код…'],['☕','Котики <span>пьют</span> кофе…'],['🐾','Котики <span>топчут</span> сервер лапками…'],['😹','Котики <span>смеются</span> над багами…'],['🍕','Котики <span>едят</span> пиццу…'],['✨','Котики <span>колдуют</span> над сайтом…'],['🛠️','<span>Главный кот</span> чинит провода…']];const emoji=document.getElementById('statusEmoji'),message=document.getElementById('statusMessage'),bar=document.getElementById('progressBar'),percent=document.getElementById('progressPercent'),button=document.getElementById('funButton'),countNode=document.getElementById('clickCount');let statusIndex=0,progress=0,count=0;function changeStatus(){const item=statuses[statusIndex++%statuses.length];emoji.textContent=item[0];message.innerHTML=item[1]}function pet(){countNode.textContent=++count;emoji.textContent='🐱';message.innerHTML='<span>Котик</span> мурлычет от счастья!';const old=button.textContent;button.textContent='😻 Котик доволен!';setTimeout(function(){button.textContent=old;changeStatus()},900)}button.addEventListener('click',pet);document.querySelectorAll('.cat').forEach(function(cat){cat.addEventListener('click',pet)});changeStatus();setInterval(changeStatus,2500);setInterval(function(){progress=(progress+.8)%100;bar.style.width=progress+'%';percent.textContent=Math.round(progress)+'%'},80)})();
</script></body></html>'''},
 {"id":"loading","name":"Загрузка","description":"Минималистичный экран статуса для технического запуска.","html":'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#070a12"><title>Подготовка сервиса</title><style>*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;color:#edf1ff;font-family:Inter,ui-sans-serif,system-ui,sans-serif;background:#070a12}.grid{position:fixed;inset:0;opacity:.3;background-image:linear-gradient(#ffffff0a 1px,transparent 1px),linear-gradient(90deg,#ffffff0a 1px,transparent 1px);background-size:34px 34px;mask-image:radial-gradient(circle at center,#000,transparent 75%)}main{position:relative;width:min(540px,calc(100% - 40px));padding:44px 38px;border:1px solid #ffffff19;border-radius:28px;background:#111726cc;box-shadow:0 28px 90px #0008}.icon{display:grid;place-items:center;width:58px;height:58px;border-radius:18px;background:#78f0cf;color:#07120f;font-size:25px;box-shadow:0 0 35px #78f0cf55}h1{margin:25px 0 10px;font-size:34px;letter-spacing:-.05em}p{margin:0;color:#aab4c9;line-height:1.6}.bar{height:9px;margin:30px 0 15px;overflow:hidden;border-radius:20px;background:#ffffff12}.bar i{display:block;width:42%;height:100%;border-radius:inherit;background:linear-gradient(90deg,#78f0cf,#78b5ff);animation:load 1.8s ease-in-out infinite}.row{display:flex;justify-content:space-between;color:#8d99b0;font-size:12px}@keyframes load{0%{transform:translateX(-100%)}100%{transform:translateX(340%)}}</style></head><body><div class="grid"></div><main><div class="icon">↻</div><h1>Почти готово</h1><p>Сервис запускается и проверяет безопасное соединение. Это займёт совсем немного времени.</p><div class="bar"><i></i></div><div class="row"><span>Подготовка</span><span id="status">Проверяем систему…</span></div></main><script>const s=['Проверяем систему…','Настраиваем доступ…','Завершаем запуск…'];let i=0;setInterval(()=>document.getElementById('status').textContent=s[i++%s.length],2200)</script></body></html>'''}
]

os.makedirs(os.path.dirname(DATA),exist_ok=True)
if not os.path.exists(KEY):
    with open(KEY,"wb") as f: f.write(secrets.token_bytes(32))
with open(KEY,"rb") as f: SESSION_KEY=f.read()
os.chmod(KEY,0o600)
STATE_LOCK=threading.RLock()
LOGIN_LOCK=threading.RLock()
LOGIN_FAILURES=defaultdict(deque)
LOGIN_FAILURES_GLOBAL=deque()
LOGIN_WINDOW=10*60
LOGIN_LIMIT=8
LOGIN_GLOBAL_LIMIT=200

def esc(x): return html.escape(str(x),quote=True)
def hash_password(p):
    salt=secrets.token_bytes(16)
    d=hashlib.scrypt(p.encode(),salt=salt,n=16384,r=8,p=1,dklen=32)
    return base64.b64encode(salt+d).decode()
def check_password(p,h):
    try:
        raw=base64.b64decode(h); salt,exp=raw[:16],raw[16:]
        got=hashlib.scrypt(p.encode(),salt=salt,n=16384,r=8,p=1,dklen=32)
        return secrets.compare_digest(exp,got)
    except Exception:
        return False
def sign(x): return x+"."+hmac.new(SESSION_KEY,x.encode(),hashlib.sha256).hexdigest()
def rotate_session_key():
    global SESSION_KEY
    fresh=secrets.token_bytes(32)
    tmp=KEY+".tmp"
    with open(tmp,"wb") as f:
        f.write(fresh); f.flush(); os.fsync(f.fileno())
    os.chmod(tmp,0o600)
    os.replace(tmp,KEY)
    SESSION_KEY=fresh
def client_id(handler):
    forwarded=handler.headers.get("X-Forwarded-For","")
    candidate=forwarded.split(",")[-1].strip() if forwarded else handler.client_address[0]
    try: return str(ipaddress.ip_address(candidate))
    except ValueError: return "unknown"
def login_blocked(client):
    now=time.monotonic(); cutoff=now-LOGIN_WINDOW
    with LOGIN_LOCK:
        bucket=LOGIN_FAILURES[client]
        while bucket and bucket[0]<cutoff: bucket.popleft()
        while LOGIN_FAILURES_GLOBAL and LOGIN_FAILURES_GLOBAL[0]<cutoff: LOGIN_FAILURES_GLOBAL.popleft()
        if not LOGIN_FAILURES_GLOBAL:
            LOGIN_FAILURES.clear()
            bucket=LOGIN_FAILURES[client]
        return len(bucket)>=LOGIN_LIMIT or len(LOGIN_FAILURES_GLOBAL)>=LOGIN_GLOBAL_LIMIT
def login_failed(client):
    now=time.monotonic()
    with LOGIN_LOCK:
        LOGIN_FAILURES[client].append(now)
        LOGIN_FAILURES_GLOBAL.append(now)
def login_succeeded(client):
    with LOGIN_LOCK: LOGIN_FAILURES.pop(client,None)
def load():
    try:
        with open(DATA,encoding="utf-8") as f: return json.load(f)
    except Exception:
        return {"admin":{"user":"admin","hash":""}}
def save(d):
    t=DATA+".tmp"
    with open(t,"w",encoding="utf-8") as f: json.dump(d,f,ensure_ascii=True,indent=2)
    os.chmod(t,0o600); os.replace(t,DATA)
def primary():
    try:
        with open(PRIMARY,encoding="utf-8") as f: return f.read().strip()
    except Exception: return ""
def users():
    try:
        with open(USERS,encoding="utf-8") as f: return json.load(f).get("users",[])
    except Exception: return []
def traffic():
    try:
        with open(TRAFFIC,encoding="utf-8") as f:
            value=json.load(f)
            return value if isinstance(value,dict) else {}
    except Exception: return {}
def human_bytes(value):
    value=max(0,int(value or 0))
    units=("Б","КБ","МБ","ГБ","ТБ")
    size=float(value)
    for unit in units:
        if size<1024 or unit==units[-1]:
            return ("%.0f"%size if unit=="Б" else "%.1f"%size)+" "+unit
        size/=1024
def traffic_info(uid,state=None):
    state=state or traffic()
    item=state.get(uid,{})
    up=max(0,int(item.get("up",0)))
    down=max(0,int(item.get("down",0)))
    last=max(0,int(item.get("last_change",0)))
    active=bool(item.get("service_active")) and last>0 and time.time()-last<=90
    return {"up":up,"down":down,"total":up+down,"last":last,"active":active,"service":bool(item.get("service_active"))}
def read_site_html():
    try:
        # Keep the author source separate from the generated public files.
        # Reading index.html here used to make the next edit depend on the
        # previous preset's CSS/JS files.
        if os.path.exists(SITE_SOURCE):
            with open(SITE_SOURCE,encoding="utf-8") as f: return f.read()
        with open(SITE_INDEX,encoding="utf-8") as f: return hydrate_legacy_assets(f.read())
    except Exception as e:
        raise RuntimeError("Не удалось прочитать index.html: "+str(e))
def install_public_file(path,raw):
    tmp=path+".tmp"
    try:
        with open(tmp,"wb") as f:
            f.write(raw); f.flush(); os.fsync(f.fileno())
        os.chown(tmp,0,grp.getgrnam("tproxy").gr_gid)
        os.chmod(tmp,0o640)
        os.replace(tmp,path)
    except Exception:
        try: os.unlink(tmp)
        except FileNotFoundError: pass
        raise
def install_private_file(path,raw):
    tmp=path+".tmp"
    try:
        with open(tmp,"wb") as f:
            f.write(raw); f.flush(); os.fsync(f.fileno())
        os.chown(tmp,0,0)
        os.chmod(tmp,0o600)
        os.replace(tmp,path)
    except Exception:
        try: os.unlink(tmp)
        except FileNotFoundError: pass
        raise
def restart_public_site():
    r=subprocess.run(["systemctl","restart","tproxy-server.service"],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=30)
    if r.returncode or subprocess.run(["systemctl","is-active","--quiet","tproxy-server.service"],timeout=10).returncode:
        raise RuntimeError((r.stderr or r.stdout or "tproxy-server failed to restart").strip())
def verify_public_asset(path, marker):
    r=subprocess.run(["curl","-kfsS","--max-time","12","https://"+DOMAIN+path],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=15)
    if r.returncode or marker not in r.stdout:
        raise RuntimeError("Relay did not publish "+path+" after restart")
def externalize_inline_assets(source):
    # Static public pages intentionally block inline CSS/JS. Keep generated
    # assets at local paths that tproxy-server can serve from public_dir.
    styles=[]
    def replace_style(match):
        css=match.group(1).strip()
        if not css: return ""
        styles.append(css)
        return '<link rel="stylesheet" href="/panel-site.css">' if len(styles)==1 else ""
    rendered=re.sub(r"<style\b[^>]*>(.*?)</style\s*>",replace_style,source,flags=re.I|re.S)
    scripts=[]
    def replace_script(match):
        code=match.group(1).strip()
        if not code: return ""
        scripts.append(code)
        return '<script src="/panel-site.js" defer></script>' if len(scripts)==1 else ""
    rendered=re.sub(r"<script\b(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script\s*>",replace_script,rendered,flags=re.I|re.S)
    # The relay CSP intentionally rejects style="..." attributes. Convert
    # them to same-origin stylesheet rules so standalone HTML pasted into the
    # editor keeps its layout without enabling unsafe-inline globally.
    inline_styles=[]
    def replace_inline_style(match):
        value=match.group(2).strip()
        if not value: return ""
        index=len(inline_styles)
        marker="wpp-%d"%index
        inline_styles.append('[data-wpp-style="%s"]{%s}'%(marker,value))
        return ' data-wpp-style="'+marker+'"'
    rendered=re.sub(r'\sstyle\s*=\s*(["\'])(.*?)\1',replace_inline_style,rendered,flags=re.I|re.S)
    if inline_styles:
        styles.append("\n".join(inline_styles))
    css="/* WEB PANEL PROXY public CSS */\n"+"\n\n".join(styles) if styles else ""
    javascript="/* WEB PANEL PROXY public JS */\n"+"\n\n".join(scripts) if scripts else ""
    css_name="panel-site-"+hashlib.sha256(css.encode()).hexdigest()[:12]+".css" if css else ""
    js_name="panel-site-"+hashlib.sha256(javascript.encode()).hexdigest()[:12]+".js" if javascript else ""
    if css_name:
        rendered=rendered.replace('/panel-site.css','/'+css_name)
        if '/'+css_name not in rendered:
            rendered='<link rel="stylesheet" href="/'+css_name+'">'+rendered
    if js_name:
        rendered=rendered.replace('/panel-site.js','/'+js_name)
    # Never keep a reference to a generated asset unless we generated it in
    # this exact save. It prevents a stale link from a damaged old page.
    if not styles:
        rendered=re.sub(r'<link\b[^>]*\bhref\s*=\s*(["\'])/panel-site(?:-[a-f0-9]{12})?\.css\1[^>]*>\s*',"",rendered,flags=re.I)
    if not scripts:
        rendered=re.sub(r'<script\b[^>]*\bsrc\s*=\s*(["\'])/panel-site(?:-[a-f0-9]{12})?\.js\1[^>]*>\s*</script\s*>\s*',"",rendered,flags=re.I|re.S)
    return rendered,css,javascript,css_name,js_name
def hydrate_legacy_assets(source):
    """Convert pages saved by older panel versions back to one HTML file."""
    css_ref=re.search(r'/((?:panel-site)(?:-[a-f0-9]{12})?\.css)',source,flags=re.I)
    js_ref=re.search(r'/((?:panel-site)(?:-[a-f0-9]{12})?\.js)',source,flags=re.I)
    try:
        css_path=os.path.join(os.path.dirname(SITE_INDEX),css_ref.group(1)) if css_ref else SITE_CSS
        with open(css_path,encoding="utf-8") as f: css=f.read()
    except Exception: css=""
    try:
        js_path=os.path.join(os.path.dirname(SITE_INDEX),js_ref.group(1)) if js_ref else SITE_JS
        with open(js_path,encoding="utf-8") as f: javascript=f.read()
    except Exception: javascript=""
    if css:
        source=re.sub(
            r'<link\b[^>]*\bhref\s*=\s*(["\'])/panel-site(?:-[a-f0-9]{12})?\.css\1[^>]*>',
            '<style>\n'+css+'\n</style>', source, flags=re.I)
    if javascript:
        source=re.sub(
            r'<script\b[^>]*\bsrc\s*=\s*(["\'])/panel-site(?:-[a-f0-9]{12})?\.js\1[^>]*>\s*</script\s*>',
            '<script>\n'+javascript+'\n</script>', source, flags=re.I|re.S)
    return source
def write_site_html(source):
    rendered,css,javascript,css_name,js_name=externalize_inline_assets(source)
    raw=rendered.encode("utf-8")
    if not source.strip(): raise ValueError("HTML не может быть пустым")
    if len(source.encode("utf-8"))>MAX_HTML_BYTES: raise ValueError("HTML превышает лимит 1 МБ")
    with STATE_LOCK:
        had_index_backup=os.path.exists(SITE_INDEX)
        had_source_backup=os.path.exists(SITE_SOURCE)
        had_css_backup=os.path.exists(SITE_CSS)
        had_js_backup=os.path.exists(SITE_JS)
        if had_index_backup:
            shutil.copy2(SITE_INDEX,SITE_BACKUP)
            os.chmod(SITE_BACKUP,0o600)
        if had_source_backup:
            shutil.copy2(SITE_SOURCE,SITE_SOURCE_BACKUP)
            os.chmod(SITE_SOURCE_BACKUP,0o600)
        if had_css_backup:
            shutil.copy2(SITE_CSS,SITE_CSS_BACKUP)
            os.chmod(SITE_CSS_BACKUP,0o600)
        if had_js_backup:
            shutil.copy2(SITE_JS,SITE_JS_BACKUP)
            os.chmod(SITE_JS_BACKUP,0o600)
        try:
            css_path=os.path.join(os.path.dirname(SITE_INDEX),css_name) if css_name else ""
            js_path=os.path.join(os.path.dirname(SITE_INDEX),js_name) if js_name else ""
            if css:
                install_public_file(css_path,css.encode("utf-8"))
            if javascript:
                install_public_file(js_path,javascript.encode("utf-8"))
            install_public_file(SITE_INDEX,raw)
            # tproxy-server serves public_dir from memory; a successful
            # restart makes the edited landing page visible immediately.
            restart_public_site()
            if css: verify_public_asset("/"+css_name,"WEB PANEL PROXY public CSS")
            if javascript: verify_public_asset("/"+js_name,"WEB PANEL PROXY public JS")
            install_private_file(SITE_SOURCE,source.encode("utf-8"))
            # Keep a few prior immutable assets for rollback/open browser tabs.
            generated=[]
            for name in os.listdir(os.path.dirname(SITE_INDEX)):
                if re.fullmatch(r"panel-site-[a-f0-9]{12}\.(?:css|js)",name):
                    path=os.path.join(os.path.dirname(SITE_INDEX),name)
                    generated.append((os.path.getmtime(path),path))
            for _,path in sorted(generated,reverse=True)[12:]:
                try: os.unlink(path)
                except FileNotFoundError: pass
        except Exception:
            if had_index_backup and os.path.exists(SITE_BACKUP):
                try:
                    install_public_file(SITE_INDEX,open(SITE_BACKUP,"rb").read())
                    if had_css_backup and os.path.exists(SITE_CSS_BACKUP):
                        install_public_file(SITE_CSS,open(SITE_CSS_BACKUP,"rb").read())
                    elif os.path.exists(SITE_CSS):
                        os.unlink(SITE_CSS)
                    if had_js_backup and os.path.exists(SITE_JS_BACKUP):
                        install_public_file(SITE_JS,open(SITE_JS_BACKUP,"rb").read())
                    elif os.path.exists(SITE_JS):
                        os.unlink(SITE_JS)
                    if had_source_backup and os.path.exists(SITE_SOURCE_BACKUP):
                        install_private_file(SITE_SOURCE,open(SITE_SOURCE_BACKUP,"rb").read())
                    elif os.path.exists(SITE_SOURCE):
                        os.unlink(SITE_SOURCE)
                    restart_public_site()
                except Exception: pass
            raise
def get_preset(preset_id):
    for preset in PRESETS:
        if preset.get("id")==preset_id: return preset
    raise ValueError("Пресет не найден")

def ctl(*args):
    r=subprocess.run([MANAGER,*args],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=60)
    if r.returncode: raise RuntimeError(r.stderr.strip() or "manager failed")
    return json.loads(r.stdout) if r.stdout.strip() else None
def subscription_registry():
    try:
        with open(USERS,encoding="utf-8") as f:
            return json.load(f).get("subscriptions",[])
    except FileNotFoundError:
        return []
def ctl_subscription(request):
    # Tokens and hardware identifiers never appear in process arguments.
    try:
        r=subprocess.run([MANAGER,"subscription"],input=json.dumps(request),stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE,text=True,timeout=180)
        if not r.returncode:
            value=json.loads(r.stdout)
            if isinstance(value,dict): return value
    except (OSError,subprocess.TimeoutExpired,ValueError):
        pass
    return {"ok":False,"status":503,"message":"Менеджер занят или недоступен. Повторите позже."}
def allow_subscription_request(client):
    now=time.monotonic()
    with SUB_RATE_LOCK:
        for key in list(SUB_REQUESTS):
            if SUB_REQUESTS[key][0]<now-60: del SUB_REQUESTS[key]
        if client not in SUB_REQUESTS:
            if len(SUB_REQUESTS)>=2048: return False
            SUB_REQUESTS[client]=[now,0]
        SUB_REQUESTS[client][1]+=1
        return SUB_REQUESTS[client][1]<=30
def validate_html(source):
    if not source.strip(): raise ValueError("HTML не может быть пустым")
    if len(source.encode("utf-8"))>MAX_HTML_BYTES: raise ValueError("HTML превышает лимит 1 МБ")
    return source
def web_link(secret):
    return "https://t.me/webproxy?server="+DOMAIN+"&secret="+secret
def mtproto_link(secret,port):
    return "https://t.me/proxy?server="+MTPROTO_HOST+"&port="+str(int(port))+"&secret="+secret
def xray_path():
    with open(XRAY_PATH_FILE,encoding="utf-8") as f: value=f.read().strip()
    if not re.fullmatch(r"/vless-[a-f0-9]{24}",value): raise RuntimeError("Некорректный путь VLESS")
    return value
def proxy_link(protocol,secret,port=443,name="Proxy",username=""):
    if protocol=="mtproto": return mtproto_link(secret,port)
    if protocol=="web": return web_link(secret)
    label=quote(name or "Proxy",safe="")
    if protocol=="vless":
        query=urlencode({"encryption":"none","security":"tls","sni":DOMAIN,"fp":"chrome","type":"xhttp","host":DOMAIN,"path":xray_path(),"mode":"auto","alpn":"h2"})
        return "vless://"+quote(secret,safe="-")+"@"+DOMAIN+":443?"+query+"#"+label
    if protocol=="hysteria":
        query=urlencode({"sni":DOMAIN,"alpn":"h3"})
        return "hysteria2://"+quote(secret,safe="-")+"@"+DOMAIN+":"+str(HYSTERIA_PORT)+"/?"+query+"#"+label
    raise RuntimeError("Неизвестный протокол")
def qr_png_bytes(link):
    return subprocess.run([QR,"-o","-","-t","PNG","-s","6","-m","2",link],
                          stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True,timeout=10).stdout
def layout(title,body,active=""):
    return page_layout(title,body,PANEL_PATH,active,DOMAIN)

class Handler(BaseHTTPRequestHandler):
    timeout=20
    def log_message(self,*a): pass
    def send_html(self,s,code=200):
        b=s.encode(); self.send_response(code); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(b))); self.send_header("Cache-Control","no-store"); self.send_header("X-Frame-Options","DENY"); self.send_header("X-Content-Type-Options","nosniff"); self.send_header("Referrer-Policy","no-referrer")
        # srcdoc is inline content. Deny network frame navigations as well as
        # requests from within the sandbox, including location/meta refresh.
        self.send_header("Content-Security-Policy","default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        self.end_headers(); self.wfile.write(b)
    def send_data(self,body,code=200,mime="text/plain; charset=utf-8",headers=None):
        raw=body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type",mime)
        self.send_header("Content-Length",str(len(raw)))
        self.send_header("Cache-Control","no-store, private")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("Referrer-Policy","no-referrer")
        for key,value in (headers or {}).items(): self.send_header(key,str(value))
        self.end_headers()
        self.wfile.write(raw)
    def send_json(self,value,code=200):
        self.send_data(json.dumps(value,ensure_ascii=True),code,"application/json")
    def send_png(self,b):
        self.send_response(200); self.send_header("Content-Type","image/png"); self.send_header("Cache-Control","no-store"); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def send_logo(self,b):
        self.send_response(200); self.send_header("Content-Type","image/png"); self.send_header("Cache-Control","public, max-age=86400"); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def redirect(self,p):
        self.send_response(303); self.send_header("Location",PANEL_PATH+p if p.startswith("/") else p); self.end_headers()
    def form(self,max_bytes=MAX_HTML_BYTES*3+8192):
        try: n=int(self.headers.get("Content-Length","0"))
        except ValueError: n=0
        if n < 0 or n > max_bytes: raise ValueError("Invalid form size")
        return {k:v[-1] for k,v in parse_qs(self.rfile.read(n).decode("utf-8"),max_num_fields=32).items()}
    def auth(self):
        c=cookies.SimpleCookie(self.headers.get("Cookie","")); v=c.get("sid")
        if not v:return False
        try:
            x,_=v.value.rsplit(".",1)
            issued=int(x.split("-",1)[0])
            return secrets.compare_digest(sign(x),v.value) and 0 <= time.time()-issued < 86400
        except Exception:
            return False
    def session_cookie(self,value,max_age):
        # Caddy supplies this header for public requests.  Keeping Secure for
        # HTTPS prevents accidental exposure, while loopback diagnostics still
        # receive a usable cookie.
        secure="; Secure" if self.headers.get("X-Forwarded-Proto","").lower()=="https" else ""
        return f"sid={value}; Path={PANEL_PATH}; Max-Age={max_age}; HttpOnly{secure}; SameSite=Lax"
    def csrf(self):
        c=cookies.SimpleCookie(self.headers.get("Cookie","")); v=c.get("sid")
        if not v: return ""
        return hmac.new(SESSION_KEY,b"csrf:"+v.value.encode(),hashlib.sha256).hexdigest()
    def valid_csrf(self,form):
        return secrets.compare_digest(form.get("csrf",""),self.csrf())
    def do_GET(self):
        path=urlparse(self.path).path
        if path.startswith(SUB_PREFIX):
            self.serve_subscription(path[len(SUB_PREFIX):]); return
        d=load()
        if path==PANEL_PATH+"/__health":
            self.send_response(200)
            self.send_header("Content-Type","text/plain; charset=utf-8")
            self.send_header("Cache-Control","no-store")
            body=b"OK"
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path==PANEL_PATH+"/__logo":
            try:
                with open(LOGO,"rb") as f: logo=f.read()
                self.send_logo(logo)
            except OSError:
                self.send_html("Logo not found",404)
            return

        if path==PANEL_PATH+"/login":
            self.send_html(login_ui(PANEL_PATH)); return
        if path==PANEL_PATH+"/logout":
            self.send_response(303); self.send_header("Set-Cookie",self.session_cookie("",0)); self.send_header("Location",PANEL_PATH+"/login"); self.end_headers(); return
        if not self.auth():
            self.redirect("/login"); return

        if path==PANEL_PATH or path==PANEL_PATH+"/":
            self.redirect("/dashboard"); return

        if path in (PANEL_PATH+"/dashboard",PANEL_PATH+"/dashboard-data"):
            try: hours=int(parse_qs(urlparse(self.path).query).get("hours",["1"])[0])
            except ValueError: hours=1
            if hours not in (1,6,24): hours=1
            profiles=[{"id":"primary","name":"Основной WEB Proxy","secret":primary(),"protocol":"web","enabled":True,"backend_port":443}]+users()
            body=dashboard_body(server_metrics.dashboard_data(hours),subscription_registry(),profiles,traffic(),
                                PANEL_PATH,DOMAIN,self.csrf(),proxy_link,web_updates.current_version(),hours)
            if path.endswith("/dashboard-data"):
                self.send_json({"html":body,"update":web_updates.get_status()})
            else:
                self.send_html(layout("Дашборд",dashboard_page(body,PANEL_PATH,self.csrf()),"dashboard"))
            return

        if path==PANEL_PATH+'/clients-state':
            profiles=[{'id':'primary','name':'Основной WEB Proxy','secret':primary(),'protocol':'web','enabled':True,'backend_port':443}]+users()
            records=client_records(subscription_registry(),profiles,traffic(),DOMAIN,proxy_link)
            self.send_json({'clients':[{'id':r['id'],'name':r['name'],'kind':r['kind'],'enabled':r['enabled'],
                'protocols':r['protocols'],'devices':r['devices'],'limit':r['limit'],**r['totals']} for r in records]})
            return

        if path==PANEL_PATH+"/users":
            profiles=[{"id":"primary","name":"Основной WEB Proxy","secret":primary(),"protocol":"web","enabled":True,"backend_port":443}]+users()
            body=users_ui(subscription_registry(),profiles,traffic(),PANEL_PATH,DOMAIN,self.csrf(),proxy_link)
            self.send_html(layout("Клиенты",body,"users")); return
        if path==PANEL_PATH+"/subscriptions":
            self.redirect("/users"); return
        if path==PANEL_PATH+"/update-status":
            self.send_json(web_updates.get_status()); return
        if path==PANEL_PATH+"/subscription-qr":
            sid=parse_qs(urlparse(self.path).query).get("id",[""])[0]
            sub=next((s for s in subscription_registry() if s["id"]==sid),None)
            if sub is None: self.send_html("Not found",404); return
            try: self.send_png(qr_png_bytes("https://"+DOMAIN+SUB_PREFIX+sub["token"]))
            except (OSError,subprocess.SubprocessError): self.send_json({'message':'Не удалось сформировать QR. Проверьте qrencode на сервере.'},503)
            return

        if path==PANEL_PATH+"/__qr":
            query=parse_qs(urlparse(self.path).query)
            q=query.get("secret",[""])[0]; protocol=query.get("protocol",["web"])[0]
            port=query.get("port",["443"])[0]
            current_users=users()
            matching=next((x for x in current_users if x.get("secret")==q),None)
            if q==primary(): matching={"protocol":"web","backend_port":443,"name":"Основной WEB Proxy"}
            if not matching or protocol!=matching.get("protocol","web"):
                self.send_html("Not found",404); return
            try:
                expected=str(int(matching.get("backend_port",443)))
                if protocol in ("mtproto","hysteria") and port!=expected:
                    self.send_html("Not found",404); return
                if protocol in ("web","vless") and port!="443":
                    self.send_html("Not found",404); return
                self.send_png(qr_png_bytes(proxy_link(protocol,q,expected,matching.get("name","Proxy"),matching.get("username",""))))
            except Exception:self.send_json({'message':'Не удалось сформировать QR. Проверьте qrencode на сервере.'},503)
            return


        if path==PANEL_PATH+"/settings":
            token=esc(self.csrf())
            has_draft=os.path.exists(SITE_DRAFT)
            try:
                if has_draft:
                    with open(SITE_DRAFT,encoding="utf-8") as f: site_html=f.read()
                else: site_html=read_site_html()
            except Exception: site_html="<!-- Не удалось прочитать исходник -->"
            editor=editor_ui(site_html,PANEL_PATH,self.csrf(),PRESETS,has_draft)
            body=f'''<div class="page-head"><div><span class="eyebrow">WPP / STUDIO</span><h1>Настройки</h1><p>Оформление сайта и доступ к панели</p></div></div>
{editor}
<div class=card><h2>Пароль администратора</h2><form method=post action="{PANEL_PATH}/password"><input type=hidden name=csrf value="{token}"><label for="adminNewPassword">Новый пароль</label><input id="adminNewPassword" type=password name=a minlength=8 required autocomplete=new-password><div class="actions" style="margin-top:16px"><button class="btn primary">Сохранить пароль</button><small>Минимум 8 символов · смена пароля завершит все сессии панели</small></div></form></div>'''
            self.send_html(layout("Настройки",body,"settings")); return

        self.redirect("/")

    def do_POST(self):
        path=urlparse(self.path).path

        # Login does not require an authenticated session.
        if path==PANEL_PATH+"/login":
            client=client_id(self)
            if login_blocked(client):
                body="Слишком много попыток входа. Повторите позже.".encode("utf-8")
                self.send_response(429)
                self.send_header("Retry-After",str(LOGIN_WINDOW))
                self.send_header("Content-Type","text/html; charset=utf-8")
                self.send_header("Content-Length",str(len(body)))
                self.end_headers(); self.wfile.write(body)
                return
            try: form=self.form(8192)
            except (ValueError,UnicodeDecodeError):
                self.send_html("Некорректный запрос.",400); return
            d=load()
            username=form.get("user","")
            password=form.get("password","")
            if username==d.get("admin",{}).get("user","admin") and check_password(password,d.get("admin",{}).get("hash","")):
                # A cookie-safe token: the old ':' separator was accepted by
                # most browsers but is rejected/rewritten by some proxies.
                token=str(int(time.time()))+"-"+secrets.token_hex(16)
                sid=sign(token)
                login_succeeded(client)
                self.send_response(303)
                self.send_header("Set-Cookie",self.session_cookie(sid,86400))
                self.send_header("Location",PANEL_PATH+"/dashboard")
                self.end_headers()
            else:
                login_failed(client)
                self.send_html("""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#060910;color:#fff;font:15px system-ui}.b{width:min(420px,90vw);padding:28px;border:1px solid #223148;border-radius:22px;background:#0d1520}a{color:#8edcff}</style>
<div class=b><h2>Неверный логин или пароль</h2><p>Попробуйте войти ещё раз.</p><a href="%s/login">Вернуться</a></div>""" % esc(PANEL_PATH),401)
            return

        # Everything below requires an authenticated session.
        if not self.auth():
            self.redirect("/login")
            return

        try: form=self.form()
        except (ValueError,UnicodeDecodeError) as e:
            self.send_html(esc(e),400); return
        d=load()

        if not self.valid_csrf(form):
            self.send_html("Недействительный запрос. Обновите страницу и попробуйте снова.",403)
            return

        if path in (PANEL_PATH+"/update-check",PANEL_PATH+"/update-start"):
            try:
                result=web_updates.start_update() if path.endswith("/update-start") else web_updates.check_release()
                self.send_json(result)
            except ValueError as exc: self.send_json({"message":str(exc)},400)
            except (OSError,subprocess.TimeoutExpired): self.send_json({"message":"Служба обновления недоступна. Проверьте VPS через SSH."},503)
            return

        if path==PANEL_PATH+"/create-account":
            name=form.get("name","").strip()
            kind=form.get("kind","")
            if not name or len(name)>80 or any(ord(c)<32 for c in name):
                self.send_html("Укажите имя длиной от 1 до 80 символов.",400); return
            if kind=="subscription":
                result=ctl_subscription({"operation":"create","name":name,"max_devices":form.get("max_devices","2"),
                                         "protocols":[p for p in ("vless","hysteria") if form.get(p)=="1"]})
                if not result.get("ok"):
                    self.send_html(esc(result.get("message","Ошибка создания подписки")),int(result.get("status",400))); return
            elif kind in ("web","mtproto","vless","hysteria"):
                try: ctl("add",kind,name)
                except Exception as exc:
                    print("create connection failed:",type(exc).__name__,file=sys.stderr,flush=True)
                    self.send_html("Не удалось создать подключение. Проверьте службы через SSH и повторите попытку.",503); return
            else:
                self.send_html("Неизвестный тип доступа",400); return
            self.redirect("/users"); return

        if path==PANEL_PATH+"/client-action":
            uid=form.get('id',''); kind=form.get('kind',''); operation=form.get('operation','')
            if uid=='primary' or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',uid):
                self.send_json({'message':'Основное подключение нельзя изменить.'},400); return
            if operation=='state' and form.get('enabled') not in ('0','1'):
                self.send_json({'message':'Некорректное состояние доступа.'},400); return
            if kind not in ('subscription','direct') or operation not in ('state','rename'):
                self.send_json({'message':'Недопустимая операция.'},400); return
            if operation=='rename' and (not form.get('name','').strip() or len(form['name'].strip())>80 or any(ord(c)<32 for c in form['name'])):
                self.send_json({'message':'Имя должно содержать от 1 до 80 символов без управляющих знаков.'},400); return
            try:
                if kind=='subscription':
                    request={'id':uid,'operation':'set-enabled' if operation=='state' else 'update'}
                    if operation=='state': request['enabled']=form['enabled']=='1'
                    else: request['name']=form.get('name','')
                    result=ctl_subscription(request)
                    if not result.get('ok'):
                        self.send_json({'message':result.get('message','Изменение не применено.')},int(result.get('status',400))); return
                else:
                    if operation=='state': ctl('set-user',uid,form['enabled'])
                    else: ctl('rename-user',uid,form.get('name',''))
                self.send_json({'ok':True})
            except Exception:
                self.send_json({'message':'Изменение не применено. Проверьте службы через SSH и обновите список.'},503)
            return

        if path==PANEL_PATH+"/subscription-action":
            request={k:form[k] for k in ("operation","id","device_id","name","max_devices") if k in form}
            if request.get("operation") not in ("create","update","toggle","rotate","delete","revoke","allow"):
                self.send_html("Недопустимая операция",400); return
            if request["operation"] in ("create","update"):
                request["protocols"]=[p for p in ("vless","hysteria") if form.get(p)=="1"]
            result=ctl_subscription(request)
            if result.get("ok"): self.redirect("/users")
            else: self.send_html(esc(result.get("message","Ошибка подписки")),int(result.get("status",400)))
            return

        if path in (PANEL_PATH+"/preview-html",PANEL_PATH+"/save-draft",PANEL_PATH+"/draft-preset",PANEL_PATH+"/discard-draft"):
            try:
                if path.endswith("/discard-draft"):
                    with STATE_LOCK:
                        if os.path.exists(SITE_DRAFT): os.unlink(SITE_DRAFT)
                else:
                    source=validate_html(get_preset(form.get("preset",""))["html"] if path.endswith("/draft-preset") else form.get("html",""))
                    if path.endswith("/preview-html"):
                        self.send_json({"document":preview_document(source,externalize_inline_assets)}); return
                    with STATE_LOCK: install_private_file(SITE_DRAFT,source.encode("utf-8"))
                self.redirect("/settings")
            except ValueError as exc:
                self.send_json({"message":str(exc)},400)
            except (RuntimeError,OSError) as exc:
                print("landing draft failed:",type(exc).__name__,file=sys.stderr,flush=True)
                self.send_json({"message":"Не удалось обработать черновик. Проверьте службы через SSH."},503)
            return

        if path==PANEL_PATH+"/add-user":
            name=form.get("name","").strip()
            protocol=form.get("protocol","web").strip().lower()
            if not name or len(name)>80:
                self.send_html("Имя пользователя обязательно.",400); return
            if protocol not in ("web","mtproto","vless","hysteria"):
                self.send_html("Неизвестный протокол подключения.",400); return
            try:
                result=ctl("add",protocol,name)
                self.redirect("/users")
            except Exception as exc:
                print("create user failed:",type(exc).__name__,file=sys.stderr,flush=True)
                self.send_html("Не удалось создать пользователя. Проверьте службы через SSH и повторите попытку.",503)
            return

        if path==PANEL_PATH+"/delete-user":
            uid=form.get("id","")
            if not uid or uid=="primary":
                self.send_html("Нельзя удалить основной профиль.",400); return
            try:
                ctl("delete",uid)
                self.redirect("/users")
            except Exception as exc:
                print("delete user failed:",type(exc).__name__,file=sys.stderr,flush=True)
                self.send_html("Не удалось удалить пользователя. Обновите список и повторите попытку.",503)
            return

        if path==PANEL_PATH+"/site-html":
            try:
                with STATE_LOCK:
                    source=validate_html(form.get("html",""))
                    install_private_file(SITE_DRAFT,source.encode("utf-8"))
                    write_site_html(source)
                    if os.path.exists(SITE_DRAFT): os.unlink(SITE_DRAFT)
                self.redirect("/settings")
            except ValueError as exc:
                self.send_html("Ошибка сохранения HTML: "+esc(exc),400)
            except (RuntimeError,OSError) as exc:
                print("landing publish failed:",type(exc).__name__,file=sys.stderr,flush=True)
                self.send_html("Не удалось опубликовать HTML. Предыдущая страница сохранена; проверьте службы через SSH.",503)
            return

        if path==PANEL_PATH+"/apply-preset":
            try:
                preset=get_preset(form.get("preset",""))
                write_site_html(preset["html"])
                self.redirect("/settings")
            except ValueError as exc:
                self.send_html("Ошибка применения пресета: "+esc(exc),400)
            except (RuntimeError,OSError) as exc:
                print("landing preset failed:",type(exc).__name__,file=sys.stderr,flush=True)
                self.send_html("Не удалось применить пресет. Предыдущая страница сохранена; проверьте службы через SSH.",503)
            return

        if path==PANEL_PATH+"/password":
            a=form.get("a","")
            if len(a)<8:
                self.send_html("Пароль должен содержать минимум 8 символов.",400)
                return
            d["admin"]["hash"]=hash_password(a)
            save(d)
            rotate_session_key()
            self.send_response(303)
            self.send_header("Set-Cookie",self.session_cookie("",0))
            self.send_header("Location",PANEL_PATH+"/login")
            self.end_headers()
            return

        self.send_html("Not found",404)

    def serve_subscription(self,token):
        if not re.fullmatch(r"[a-f0-9]{64}",token):
            self.send_data("Not found",404); return
        if not allow_subscription_request(client_id(self)):
            self.send_data("Слишком много запросов. Повторите через минуту.",429,headers={"Retry-After":"60"}); return
        if not SUB_FETCH_SLOTS.acquire(blocking=False):
            self.send_data("Сервис занят. Повторите позже.",503,headers={"Retry-After":"15"}); return
        try:
            # Reject unknown URLs before spawning any privileged helper.
            if not any(s.get("enabled") and secrets.compare_digest(s["token"],token) for s in subscription_registry()):
                self.send_data("Not found",404); return
            if "text/html" in self.headers.get("Accept",""):
                self.send_data("Добавьте эту ссылку как подписку в клиент. Для ограниченной подписки нужен X-HWID (Happ).",200); return
            result=ctl_subscription({"operation":"fetch","token":token,"hwid":self.headers.get("X-HWID","")})
            if not result.get("ok"):
                headers={"X-Hwid-Active":"true","subscription-always-hwid-enable":"true"}
                if result.get("code")=="hwid_required": headers["X-Hwid-Not-Supported"]="true"
                if result.get("code")=="device_limit": headers.update({"X-Hwid-Limit":"true","X-Hwid-Max-Devices-Reached":"true"})
                self.send_data(result.get("message","Подписка недоступна"),int(result.get("status",503)),headers=headers); return
            labels={"vless":"VLESS","hysteria":"Hysteria2"}
            lines=[proxy_link(u["protocol"],u["secret"],u["backend_port"],result["name"]+" · "+labels[u["protocol"]],u.get("username","")) for u in result["users"]]
            state=traffic()
            up=sum(int(state.get(u["id"],{}).get("up",0)) for u in result["users"])
            down=sum(int(state.get(u["id"],{}).get("down",0)) for u in result["users"])
            headers={"profile-title":"base64:"+base64.b64encode(result["name"].encode()).decode(),"profile-update-interval":"6",
                     "subscription-userinfo":f"upload={up}; download={down}; total=0; expire=0"}
            if result["limited"]: headers.update({"X-Hwid-Active":"true","subscription-always-hwid-enable":"true"})
            self.send_data(base64.b64encode(("\n".join(lines)+"\n").encode()).decode(),headers=headers)
        except Exception:
            self.send_data("Подписка временно недоступна.",503)
        finally:
            SUB_FETCH_SLOTS.release()

def main():
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()

if __name__=="__main__":
    main()



PY

if [[ "$UPDATING" != "1" ]]; then
python3 - "${DATA_FILE}" "${ADMIN}" "${PASS}" <<'PY'
import base64,hashlib,json,os,secrets,sys

data_file,admin,password=sys.argv[1],sys.argv[2],sys.argv[3]
salt=secrets.token_bytes(16)
digest=hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1,dklen=32)
data={
    "admin":{"user":admin,"hash":base64.b64encode(salt+digest).decode()},
    "site":{"html":"<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Система подключения</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#05070b;color:#fff;font:16px system-ui}.card{width:min(700px,88vw);padding:48px;text-align:center;border:1px solid #ffffff14;border-radius:28px;background:#101722e8;box-shadow:0 30px 100px #0009}.ok{color:#65efad}h1{font-size:clamp(34px,6vw,58px)}</style></head><body><div class=\"card\"><div class=\"ok\">● ONLINE</div><h1>Система подключения</h1><p>Безопасное соединение активно.</p></div></body></html>"}
}
tmp=data_file+".tmp"
with open(tmp,"w",encoding="utf-8") as f: json.dump(data,f,ensure_ascii=True,indent=2)
os.chmod(tmp,0o600)
os.replace(tmp,data_file)
PY
fi

python3 -m py_compile "$APP_FILE"
python3 -m py_compile "$APP_DIR/wpp_subscriptions.py" "$APP_DIR/wpp_panel_extras.py" "$APP_DIR/wpp_ui.py" "$APP_DIR/wpp_metrics.py" "$APP_DIR/wpp_update.py"


# ---- Finish installation: service, Caddy route, permissions, start ----
echo "[3/6] Creating data..."
python3 - <<PY
import json
with open("${DATA_FILE}", encoding="utf-8") as f:
    json.load(f)
PY
chown root:root "$DATA_FILE"
chmod 0600 "$DATA_FILE"

echo "[3.5/6] Verifying administrator credentials..."
if [[ "$UPDATING" == "1" ]]; then
python3 - "$DATA_FILE" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    d=json.load(f)
assert d["admin"]["user"] and d["admin"]["hash"]
print("      Existing administrator credentials retained.")
PY
else
python3 - "$DATA_FILE" "$ADMIN" "$PASS" <<'PY'
import base64, hashlib, json, secrets, sys
p, user, password = sys.argv[1], sys.argv[2], sys.argv[3]
with open(p, encoding="utf-8") as f:
    d=json.load(f)
assert d["admin"]["user"] == user
raw=base64.b64decode(d["admin"]["hash"])
salt, expected = raw[:16], raw[16:]
actual=hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1,dklen=32)
assert secrets.compare_digest(expected, actual)
print("      Administrator credentials verified.")
PY
fi

echo "[4/6] Creating systemd service..."
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=WEB PANEL PROXY V 2.1.0
After=network-online.target caddy.service tproxy-server.service mtproxy.service web-proxy-panel-firewall.service
Wants=network-online.target
Requires=web-proxy-panel-firewall.service

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/python3 $APP_FILE
Environment=WEBPROXY_DOMAIN=$DOMAIN
Environment=WEBPROXY_MTPROTO_HOST=$MTPROTO_HOST
Environment=WEBPROXY_PANEL_PATH=$PANEL_PATH
Restart=always
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true
ReadWritePaths=$DATA_DIR /etc/web-proxy-panel /srv/tproxy-site
[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "$SERVICE_FILE"

echo "[4.2/6] Installing WPP console menu..."
install -o root -g root -m 0755 "$BASE/update.sh" /usr/local/sbin/web-panel-proxy-update
cat > /etc/systemd/system/web-panel-proxy-web-update.service <<'UNIT'
[Unit]
Description=WEB PANEL PROXY administrator-requested update
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
Group=root
UMask=0077
ExecStart=/usr/bin/python3 /opt/tproxy-panel/wpp_update.py run
TimeoutStartSec=infinity
# Separate cgroup: stopping tproxy-panel during an update must not kill this job.
# No Install section: this unit runs only after an authenticated admin request.
UNIT
chmod 0644 /etc/systemd/system/web-panel-proxy-web-update.service
cat > /usr/local/sbin/WPP <<'WPP'
#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SERVICE="/etc/systemd/system/tproxy-panel.service"
DATA="/var/lib/tproxy-panel/data.json"
CADDYFILE="/etc/caddy/Caddyfile"
DROPIN="/etc/systemd/system/caddy.service.d/tproxy.conf"
LOCK="/run/lock/web-panel-proxy.lock"

die(){ echo "ОШИБКА: $*" >&2; exit 1; }
[[ ${EUID:-1} -eq 0 ]] || die "Запустите меню от root: sudo WPP"
[[ -s "$SERVICE" && -s "$DATA" ]] || die "WEB PANEL PROXY не установлен полностью."

domain(){ sed -n 's/^Environment=TPROXY_HOSTNAME=//p' "$DROPIN" 2>/dev/null | head -n1; }
panel_path(){ sed -n 's/^Environment=WEBPROXY_PANEL_PATH=//p' "$SERVICE" 2>/dev/null | head -n1; }
admin_name(){ python3 - "$DATA" <<'PY'
import json,sys
print(json.load(open(sys.argv[1],encoding="utf-8")).get("admin",{}).get("user","не задан"))
PY
}
service_state(){ systemctl is-active "$1" 2>/dev/null || echo "не найден"; }
ssl_expiry(){
    local d
    d="$(domain)"
    timeout 10 openssl s_client -connect 127.0.0.1:443 -servername "$d" </dev/null 2>/dev/null |
        openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2- || echo "не удалось прочитать"
}
port_80_state(){
    if ss -lntp 2>/dev/null | grep -Eq '(^|[[:space:]])[^[:space:]]*:80[[:space:]]'; then
        if ss -lntp 2>/dev/null | grep -E ':80[[:space:]]' | grep -q 'caddy'; then
            echo "занят Caddy (проверьте конфигурацию)"
        else
            echo "занят другой программой — это допустимо"
        fi
    else
        echo "свободен"
    fi
}
pause(){ echo; read -r -p "Нажмите Enter, чтобы вернуться в меню..." _; }
lock_changes(){ exec 9>"$LOCK"; flock -n 9 || die "Установка, обновление или удаление уже выполняется."; }
unlock_changes(){ flock -u 9 2>/dev/null || true; exec 9>&-; }

show_info(){
    local d p version
    d="$(domain)"; p="$(panel_path)"; version="$(cat /etc/web-proxy-panel/version 2>/dev/null || echo '2.1.0')"
    echo
    echo "============================================================"
    echo "                 WEB PANEL PROXY"
    echo "============================================================"
    printf 'Версия:          %s\n' "$version"
    printf 'Домен:           %s\n' "$d"
    printf 'URL панели:      https://%s%s/login\n' "$d" "$p"
    printf 'Логин:           %s\n' "$(admin_name)"
    echo  "Пароль:          не хранится в открытом виде; его можно сменить"
    echo
    printf 'Панель:          %s\n' "$(service_state tproxy-panel.service)"
    printf 'Caddy:           %s\n' "$(service_state caddy.service)"
    printf 'WEB Proxy:       %s\n' "$(service_state tproxy-server.service)"
    printf 'MTProxy:         %s\n' "$(service_state mtproxy.service)"
    printf 'Xray:            %s\n' "$(service_state web-panel-proxy-xray.service)"
    printf 'SSL действует до:%s\n' " $(ssl_expiry)"
    printf 'TCP/80:          %s\n' "$(port_80_state)"
    echo "============================================================"
}

change_credentials(){
    local current new_user new_pass
    current="$(admin_name)"
    echo
    read -r -p "Новый логин [$current]: " new_user
    new_user="${new_user:-$current}"
    [[ ${#new_user} -ge 1 && ${#new_user} -le 64 ]] || { echo "Логин должен содержать от 1 до 64 символов."; return 1; }
    read -r -s -p "Новый пароль (минимум 8 символов): " new_pass
    echo
    [[ ${#new_pass} -ge 8 ]] || { echo "Пароль должен содержать минимум 8 символов."; return 1; }
    lock_changes
    if ! WPP_NEW_USER="$new_user" WPP_NEW_PASS="$new_pass" python3 - "$DATA" <<'PY'
import base64,hashlib,json,os,secrets,sys,tempfile
p=sys.argv[1]
user=os.environ.pop("WPP_NEW_USER","")
password=os.environ.pop("WPP_NEW_PASS","")
if not user or len(user)>64 or len(password)<8:
    raise SystemExit("Некорректные учётные данные")
with open(p,encoding="utf-8") as f: d=json.load(f)
salt=secrets.token_bytes(16)
digest=hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1,dklen=32)
d["admin"]={"user":user,"hash":base64.b64encode(salt+digest).decode()}
fd,tmp=tempfile.mkstemp(prefix="data.json.",dir=os.path.dirname(p))
try:
    with os.fdopen(fd,"w",encoding="utf-8") as f:
        json.dump(d,f,ensure_ascii=True,indent=2); f.flush(); os.fsync(f.fileno())
    os.chmod(tmp,0o600); os.replace(tmp,p)
finally:
    if os.path.exists(tmp): os.unlink(tmp)
PY
    then
        unlock_changes
        echo "Не удалось сохранить учётные данные."
        return 1
    fi
    systemctl restart tproxy-panel.service
    unlock_changes
    echo "Логин и пароль изменены. Все старые сессии завершены."
}

change_path(){
    local old suffix new backup service_backup d
    old="$(panel_path)"; d="$(domain)"
    echo
    echo "Текущий путь: $old"
    read -r -p "Новый суффикс после panel- (пусто = создать случайный): " suffix
    if [[ -z "$suffix" ]]; then suffix="$(openssl rand -hex 16)"; fi
    suffix="${suffix,,}"
    [[ "$suffix" =~ ^[a-z0-9][a-z0-9-]{2,58}[a-z0-9]$ ]] || {
        echo "Используйте 4–60 символов: латинские буквы, цифры и дефис; без дефиса по краям."
        return 1
    }
    new="/panel-$suffix"
    [[ "$new" != "$old" ]] || { echo "Этот путь уже используется."; return 1; }
    lock_changes
    backup="$(mktemp /tmp/wpp-Caddyfile.XXXXXX)"
    service_backup="$(mktemp /tmp/wpp-panel-service.XXXXXX)"
    cp -a "$CADDYFILE" "$backup"; cp -a "$SERVICE" "$service_backup"
    rollback_path(){
        cp -a "$backup" "$CADDYFILE"; cp -a "$service_backup" "$SERVICE"
        systemctl daemon-reload
        systemctl restart tproxy-panel.service caddy.service 2>/dev/null || true
        rm -f "$backup" "$service_backup"
        unlock_changes
    }
    if ! python3 - "$CADDYFILE" "$SERVICE" "$old" "$new" <<'PY'
import re,sys
caddy,service,old,new=sys.argv[1:]
s=open(caddy,encoding="utf-8").read()
pattern=r'\n\s*handle(?:_path)?\s+'+re.escape(old)+r'/\*\s*\{\s*reverse_proxy\s+127\.0\.0\.1:8090\s*\}\s*'
s,n=re.subn(pattern,'\n',s,count=1,flags=re.S)
if n!=1: raise SystemExit("Не найден текущий маршрут панели в Caddy")
m=re.search(r'(?m)^\s*reverse_proxy 127\.0\.0\.1:8080\s*\{',s)
if not m: raise SystemExit("Не найден маршрут WEB Proxy в Caddy")
route="    handle "+new+"/* {\n        reverse_proxy 127.0.0.1:8090\n    }\n\n"
s=s[:m.start()]+route+s[m.start():]
open(caddy,"w",encoding="utf-8").write(s)
u=open(service,encoding="utf-8").read()
u,n=re.subn(r'(?m)^Environment=WEBPROXY_PANEL_PATH=.*$',"Environment=WEBPROXY_PANEL_PATH="+new,u,count=1)
if n!=1: raise SystemExit("Не найден путь панели в systemd-службе")
open(service,"w",encoding="utf-8").write(u)
PY
    then
        rollback_path; echo "Смена пути отменена."; return 1
    fi
    caddy fmt --overwrite "$CADDYFILE" >/dev/null 2>&1 || true
    if ! caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1; then
        rollback_path; echo "Новая конфигурация Caddy не прошла проверку. Старый путь восстановлен."; return 1
    fi
    systemctl daemon-reload
    if ! systemctl restart tproxy-panel.service caddy.service ||
       ! curl -fsS --max-time 5 "http://127.0.0.1:8090${new}/__health" >/dev/null ||
       ! curl -kfsS --max-time 10 "https://${d}${new}/__health" >/dev/null; then
        rollback_path; echo "Проверка нового URL не прошла. Старый путь восстановлен."; return 1
    fi
    rm -f "$backup" "$service_backup"
    unlock_changes
    echo "Новый URL панели: https://${d}${new}/login"
}

run_update(){
    echo
    echo "Запускается безопасное обновление до последней опубликованной версии..."
    exec /usr/local/sbin/web-panel-proxy-update
}

maintain_ssl(){
    echo
    echo "Проверяю HTTPS-сертификат и запускаю обслуживание Caddy..."
    lock_changes
    if /usr/local/sbin/web-panel-proxy-sync-tls --force; then
        unlock_changes
        echo "SSL-сертификат действителен; копия для Hysteria2 синхронизирована."
        echo "TCP/80: $(port_80_state)"
    else
        unlock_changes
        echo "Не удалось подтвердить обновление SSL. Проверьте DNS, TCP/443 и журнал Caddy."
        return 1
    fi
}

run_remove(){
    echo
    echo "Запускается полное удаление WEB PANEL PROXY..."
    exec /usr/local/sbin/web-panel-proxy-uninstall
}

while true; do
    clear 2>/dev/null || true
    echo "============================================================"
    echo "              WEB PANEL PROXY — WPP MENU"
    echo "============================================================"
    echo "  1) Информация"
    echo "  2) Обновить"
    echo "  3) Сменить логин и пароль"
    echo "  4) Сменить URL-путь панели"
    echo "  5) Проверить SSL-сертификат"
    echo "  6) Удалить"
    echo "  0) Выход"
    echo "============================================================"
    read -r -p "Выберите пункт: " choice
    case "$choice" in
        1) show_info; pause ;;
        2) run_update ;;
        3) change_credentials || true; pause ;;
        4) change_path || true; pause ;;
        5) maintain_ssl || true; pause ;;
        6) run_remove ;;
        0) exit 0 ;;
        *) echo "Неизвестный пункт."; sleep 1 ;;
    esac
done
WPP
chmod 0755 /usr/local/sbin/WPP
ln -sfn /usr/local/sbin/WPP /usr/local/sbin/wpp

echo "[4.5/6] Configuring Caddy panel route..."
CADDYFILE="/etc/caddy/Caddyfile"
test -s "$CADDYFILE" || die "Caddyfile is missing."

python3 - "$CADDYFILE" "$PANEL_PATH" "$DOMAIN" "$ACME_EMAIL" "$XRAY_PATH" <<'PY'
import re, sys
from pathlib import Path
p, path, domain, email, xray_path = sys.argv[1:]
s = Path(p).read_text(encoding="utf-8")

# Materialize the core Caddyfile placeholders before validation.
s = s.replace("{$TPROXY_HOSTNAME}", domain)
s = s.replace("{$ACME_EMAIL}", email)
s = re.sub(
    r'\n\s*handle /wpp-sub/\*\s*\{\s*reverse_proxy 127\.0\.0\.1:8090\s*\}\s*',
    '\n', s, flags=re.S,
)
s = re.sub(
    r'\n\s*handle(?:_path)? /panel-[a-z0-9-]{3,64}/\*\s*\{\s*reverse_proxy 127\.0\.0\.1:8090\s*\}\s*',
    '\n',
    s,
    flags=re.S,
)
# Remove the complete managed block written by the experimental V2.2 router
# before installing the single stable VLESS XHTTP route.
s = re.sub(
    r'\n?\s*# WPP XRAY ROUTES BEGIN\n.*?\n\s*# WPP XRAY ROUTES END\n?',
    '\n',
    s,
    flags=re.S,
)
s = re.sub(
    r'\n\s*@web_panel_vless\s+path\s+/vless-[a-f0-9]{24}(?:\s+/vless-[a-f0-9]{24}/\*)?\s*\n\s*reverse_proxy\s+@web_panel_vless\s+h2c://127\.0\.0\.1:10000\s*\{\s*flush_interval\s+-1\s*\}\s*',
    '\n',
    s,
    flags=re.S,
)
s = re.sub(
    r'\n\s*@web_panel_vless\s+path\s+/vless-[a-f0-9]{24}(?:\s+/vless-[a-f0-9]{24}/\*)?\s*\n\s*handle\s+@web_panel_vless\s*\{\s*reverse_proxy\s+127\.0\.0\.1:10000\s*\{\s*transport\s+http\s*\{\s*versions\s+h2c\s*\}\s*flush_interval\s+-1\s*\}\s*\}\s*',
    '\n',
    s,
    flags=re.S,
)
m = re.search(r'(?m)^\s*reverse_proxy 127\.0\.0\.1:8080\s*\{', s)
if not m:
    raise SystemExit("Could not locate tproxy relay reverse_proxy in Caddyfile")
route = (
    "    handle /wpp-sub/* {\n"
    "        reverse_proxy 127.0.0.1:8090\n"
    "    }\n\n"
    "    @web_panel_vless path " + xray_path + " " + xray_path + "/*\n"
    "    handle @web_panel_vless {\n"
    "        reverse_proxy 127.0.0.1:10000 {\n"
    "            transport http {\n"
    "                versions h2c\n"
    "            }\n"
    "            flush_interval -1\n"
    "        }\n"
    "    }\n\n"
    "    handle " + path + "/* {\n"
    "        reverse_proxy 127.0.0.1:8090\n"
    "    }\n\n"
)
s = s[:m.start()] + route + s[m.start():]

# Port 80 must remain available to other applications. Disable Caddy's
# permanent HTTP redirect listener and force ACME for this domain to use the
# TLS-ALPN challenge on 443. This is idempotent for installs and updates.
s = re.sub(
    r'\n?\s*# WPP TLS WITHOUT PORT 80 BEGIN\n.*?\n\s*# WPP TLS WITHOUT PORT 80 END\n?',
    '\n', s, flags=re.S,
)

def block_end(lines, start):
    depth = 0
    for index in range(start, len(lines)):
        depth += lines[index].count('{') - lines[index].count('}')
        if depth == 0:
            return index
    raise SystemExit('Unclosed Caddy block')

lines = s.splitlines(keepends=True)
global_start = next((i for i, line in enumerate(lines) if line.strip() == '{'), None)
if global_start is None:
    lines[0:0] = ['{\n', '\tauto_https disable_redirects\n', '}\n', '\n']
else:
    global_end = block_end(lines, global_start)
    body = [
        line for line in lines[global_start + 1:global_end]
        if not re.match(r'^\s*auto_https\b', line)
    ]
    lines[global_start + 1:global_end] = ['\tauto_https disable_redirects\n'] + body

site_pattern = re.compile(r'^\s*' + re.escape(domain) + r'\s*\{\s*$')
site_start = next((i for i, line in enumerate(lines) if site_pattern.match(line)), None)
if site_start is None:
    raise SystemExit('WEB PANEL PROXY Caddy site block was not found')
tls_block = [
    '\t# WPP TLS WITHOUT PORT 80 BEGIN\n',
    '\ttls {\n',
    '\t\tissuer acme {\n',
    '\t\t\tdisable_http_challenge\n',
    '\t\t}\n',
    '\t}\n',
    '\t# WPP TLS WITHOUT PORT 80 END\n',
    '\n',
]
lines[site_start + 1:site_start + 1] = tls_block
s = ''.join(lines)
Path(p).write_text(s, encoding="utf-8")
PY

if grep -Eq '\{\$(TPROXY_HOSTNAME|ACME_EMAIL)\}' "$CADDYFILE"; then
    echo "ERROR: unresolved Caddy environment placeholders remain."
    sed -n '1,100p' "$CADDYFILE" || true
    exit 1
fi

caddy fmt --overwrite "$CADDYFILE" >/dev/null 2>&1 || true

if ! caddy validate --config "$CADDYFILE" --adapter caddyfile; then
    echo
    echo "ERROR: Caddy validation failed."
    sed -n '1,100p' "$CADDYFILE" || true
    exit 1
fi

systemctl daemon-reload
echo "      Activating Caddy..."
if ! systemctl restart caddy.service; then
    systemctl --no-pager --full status caddy.service >&2 || true
    journalctl -u caddy.service -n 60 --no-pager >&2 || true
    die "Caddy failed to start."
fi
systemctl enable web-proxy-panel-firewall.service
systemctl restart web-proxy-panel-firewall.service
systemctl enable web-proxy-panel-traffic.timer web-panel-proxy-metrics.timer
systemctl stop web-proxy-panel-traffic.timer web-panel-proxy-metrics.timer
systemctl reset-failed web-proxy-panel-traffic.service web-panel-proxy-metrics.service 2>/dev/null || true
systemctl enable tproxy-panel.service
systemctl restart tproxy-panel.service

echo "      Initializing user/secret manager..."
"$MANAGER" init

echo "      Checking initial traffic collection..."
systemctl restart web-proxy-panel-traffic.service || {
    journalctl -u web-proxy-panel-traffic.service -n 30 --no-pager >&2 || true
    die "Traffic collection failed; update cannot be considered successful."
}
echo "      Checking initial VPS metrics..."
systemctl restart web-panel-proxy-metrics.service || {
    journalctl -u web-panel-proxy-metrics.service -n 30 --no-pager >&2 || true
    die "VPS metrics failed; update cannot be considered successful."
}
systemctl restart web-proxy-panel-traffic.timer web-panel-proxy-metrics.timer
for timer in web-proxy-panel-traffic.timer web-panel-proxy-metrics.timer; do
    systemctl is-active --quiet "$timer" || die "Collector timer did not start: $timer"
    timer_state="$(systemctl show --property=SubState --value "$timer")"
    case "$timer_state" in
        waiting|running) ;;
        *) die "Collector timer has no future trigger: $timer ($timer_state)" ;;
    esac
done

chown -R root:tproxy /srv/tproxy-site
find /srv/tproxy-site -type d -exec chmod 0750 {} +
find /srv/tproxy-site -type f -exec chmod 0640 {} +

echo "[5/6] Starting service..."
systemctl restart caddy.service
systemctl restart tproxy-server.service
systemctl restart mtproxy.service

# Ensure no stale copy of this exact panel occupies 127.0.0.1:8090.
systemctl stop tproxy-panel.service 2>/dev/null || true
pkill -f '[/]opt/tproxy-panel/panel\.py' 2>/dev/null || true
sleep 0.3

if ss -lntp 2>/dev/null | grep -Eq ':8090\\b'; then
    echo "ERROR: 127.0.0.1:8090 is still occupied before starting the panel."
    ss -lntp 2>/dev/null | grep -E ':8090\\b' || true
    exit 1
fi

systemctl start tproxy-panel.service
sleep 1

for unit in caddy.service tproxy-server.service mtproxy.service tproxy-panel.service; do
    systemctl is-active --quiet "$unit" || {
        echo "ERROR: $unit failed to become active."
        systemctl --no-pager --full status "$unit" || true
        if [[ "$unit" == "tproxy-panel.service" ]]; then
            echo "--- Current panel journal ---"
            journalctl -u tproxy-panel.service --since "2 minutes ago" -n 120 --no-pager || true
        fi
        exit 1
    }
done

echo "[6/6] Checking panel service and route..."
if ! systemctl is-active --quiet tproxy-panel.service; then
    echo "ERROR: tproxy-panel.service is not active."
    systemctl --no-pager --full status tproxy-panel.service || true
    journalctl -u tproxy-panel.service --since "10 minutes ago" -n 80 --no-pager || true
    exit 1
fi

if ! curl -fsS --max-time 5 "http://127.0.0.1:8090${PANEL_PATH}/__health" >/dev/null; then
    echo "ERROR: panel is not answering on 127.0.0.1:8090."
    ss -lntp 2>/dev/null | grep -E ':8090\b' || true
    journalctl -u tproxy-panel.service --since "10 minutes ago" -n 80 --no-pager || true
    exit 1
fi

if ! curl -k -fsS --max-time 10 "https://${DOMAIN}${PANEL_PATH}/__health" >/dev/null; then
    echo "ERROR: Caddy panel route returned an error."
    echo "--- Caddy route ---"
    grep -n -A4 -B2 "${PANEL_PATH}" "$CADDYFILE" || true
    echo "--- panel service ---"
    systemctl --no-pager --full status tproxy-panel.service || true
    journalctl -u tproxy-panel.service --since "10 minutes ago" -n 50 --no-pager || true
    exit 1
fi

# Retire components owned by the withdrawn experimental integrations only
# after the stable panel, Caddy and proxy services have passed health checks.
if [[ -e /etc/web-proxy-panel/mieru-ufw-owned ]]; then
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
        while read -r old_port; do
            [[ "$old_port" =~ ^[0-9]+$ ]] || continue
            (( old_port >= 1 && old_port <= 65535 )) || continue
            ufw --force delete allow "${old_port}/tcp" >/dev/null 2>&1 || true
        done < /etc/web-proxy-panel/mieru-ufw-owned
    fi
    rm -f /etc/web-proxy-panel/mieru-ufw-owned
fi
if [[ -e /etc/web-proxy-panel/mita-package-owned ]]; then
    command -v mita >/dev/null 2>&1 && mita stop >/dev/null 2>&1 || true
    apt-get -o DPkg::Lock::Timeout=600 remove -y mita >/dev/null || die "Could not remove the WPP-owned Mieru package."
    rm -f /etc/web-proxy-panel/mita-package-owned
fi
rm -f /etc/web-proxy-panel/mieru-server.json /etc/web-proxy-panel/mieru-port
if [[ -e /etc/web-proxy-panel/naive-caddy-owned ]]; then
    rm -rf -- /opt/web-panel-proxy/caddy-naive
    rm -f /etc/web-proxy-panel/naive-caddy-owned
fi

echo
echo "============================================================"
if [[ "$UPDATING" == "1" ]]; then
echo "           WEB PANEL PROXY V 2.1.0 UPDATED"
else
echo "          WEB PANEL PROXY V 2.1.0 IS READY"
fi
echo "============================================================"
echo
echo "Panel URL:"
echo "  https://${DOMAIN}${PANEL_PATH}/login"
echo
echo "Administrator login:"
echo "  ${ADMIN}"
echo
if [[ "$UPDATING" == "1" ]]; then
echo "Administrator password: retained (unchanged)"
echo
else
echo "Administrator password:"
echo "  ${PASS}"
echo
fi
echo "YouTube:"
echo "  https://www.youtube.com/@POLESNIESOVETI12"
echo
echo "GitHub:"
echo "  https://github.com/POLESNIESOVETI12/webtelegram"
echo "============================================================"
