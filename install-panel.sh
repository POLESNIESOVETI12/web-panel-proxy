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
DOMAIN="$(sed -n 's/^Environment=TPROXY_HOSTNAME=//p' /etc/systemd/system/caddy.service.d/tproxy.conf 2>/dev/null | head -n1 || true)"
ACME_EMAIL="$(sed -n 's/^Environment=ACME_EMAIL=//p' /etc/systemd/system/caddy.service.d/tproxy.conf 2>/dev/null | head -n1 || true)"
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
[[ -n "$DOMAIN" ]] || die "TPROXY_HOSTNAME is missing."
[[ -s "$PRIMARY_SECRET" ]] || die "Primary install-time secret not found."
[[ -s "$LOGO_SOURCE" ]] || die "Panel logo file is missing: panel-logo.png"

# An update keeps the existing private panel address.  A new address would
# make an otherwise successful update look like a broken panel to its owner.
if [[ "$UPDATING" == "1" ]]; then
    [[ -s "$DATA_FILE" ]] || die "Existing panel data was not found. Run the full installer instead."
    EXISTING_PATH="$(sed -n 's/^Environment=WEBPROXY_PANEL_PATH=//p' "$SERVICE_FILE" 2>/dev/null | head -n1 || true)"
    [[ "$EXISTING_PATH" =~ ^/panel-[a-f0-9]+$ ]] || die "Existing panel address was not found. Run the full installer instead."
    PANEL_PATH="$EXISTING_PATH"
fi

if ! [[ "$ACME_EMAIL" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; then
    echo "Caddy ACME email is missing or invalid."
    read -r -p "ACME email: " ACME_EMAIL
    [[ "$ACME_EMAIL" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] ||
        die "Invalid ACME email."
fi

export DEBIAN_FRONTEND=noninteractive
if ! command -v qrencode >/dev/null 2>&1; then
    apt-get -o DPkg::Lock::Timeout=600 update
    apt-get -o DPkg::Lock::Timeout=600 install -y --no-install-recommends qrencode
fi

install -d -m 0755 "$APP_DIR" /etc/web-proxy-panel
install -d -m 0700 "$DATA_DIR"
install -o root -g root -m 0644 "$LOGO_SOURCE" "$LOGO_FILE"
chmod 0600 "$PRIMARY_SECRET"

if [[ "$UPDATING" == "1" ]]; then
    echo "Updating WEB PANEL PROXY V 2.0..."
else
    echo "Configuring WEB PANEL PROXY V 2.0..."
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
        [[ ${#PASS} -ge 3 ]] || { echo "Пароль должен содержать минимум 3 символа."; continue; }
        break
    done
fi

echo "[1/6] Writing manager..."

cat > "$MANAGER" <<'PY'
#!/usr/bin/env python3
import copy, json, os, re, secrets, shutil, subprocess, sys

USERS="/etc/web-proxy-panel/users.json"
PROFILES="/etc/tproxy-server/profiles.json"
UNIT_DIR="/etc/systemd/system"
FIREWALL_SCRIPT="/usr/local/sbin/web-proxy-panel-user-firewall"
MT_BIN="/opt/MTProxy/objs/bin/mtproto-proxy"
MT_AES="/etc/mtproxy/proxy-secret"
MT_CONF="/etc/mtproxy/proxy-multi.conf"
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
            for u in d["users"]: u.setdefault("protocol","web")
            return d
    except Exception:
        return {"users":[],"traffic":{}}

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
    web_ports=[int(u["backend_port"]) for u in d["users"] if u.get("enabled",True) and u.get("protocol","web")=="web"]
    mtproto_ports=[int(u["backend_port"]) for u in d["users"] if u.get("enabled",True) and u.get("protocol","web")=="mtproto"]
    stats=[int(u["stats_port"]) for u in d["users"] if u.get("enabled",True)]
    lines=[
        "#!/usr/bin/env bash",
        "set -e",
        "nft list table inet web_proxy_panel >/dev/null 2>&1 && nft delete table inet web_proxy_panel || true",
        "nft add table inet web_proxy_panel",
        "nft 'add chain inet web_proxy_panel input { type filter hook input priority -20; policy accept; }'"
    ]
    if mtproto_ports:
        lines.append("nft 'add rule inet web_proxy_panel input iifname != \"lo\" tcp dport { %s } counter accept'" % ",".join(map(str,sorted(mtproto_ports))))
    if web_ports:
        lines.append("nft 'add rule inet web_proxy_panel input iifname != \"lo\" tcp dport { %s } counter drop'" % ",".join(map(str,sorted(web_ports))))
    if stats:
        lines.append("nft 'add rule inet web_proxy_panel input iifname != \"lo\" tcp dport { %s } counter drop'" % ",".join(map(str,sorted(stats))))
    tmp=FIREWALL_SCRIPT+".tmp"
    with open(tmp,"w",encoding="utf-8") as f: f.write("\n".join(lines)+"\n")
    os.chmod(tmp,0o750)
    os.replace(tmp,FIREWALL_SCRIPT)
    run(FIREWALL_SCRIPT,check=True)
    # If UFW is enabled, expose only direct MTProto ports. WEB Proxy users
    # remain behind HTTPS and their backend ports stay blocked externally.
    if mtproto_ports and shutil.which("ufw"):
        status=run("ufw","status").stdout or ""
        if "Status: active" in status:
            for port in sorted(mtproto_ports):
                run("ufw","--force","allow",str(port)+"/tcp","comment","WEB PANEL PROXY V 2.0 MTProto")

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

def remove_old_units(d):
    keep={"web-proxy-user-"+u["id"]+".service" for u in d["users"] if u.get("enabled",True)}
    for name in os.listdir(UNIT_DIR):
        if name.startswith("web-proxy-user-") and name.endswith(".service") and name not in keep:
            run("systemctl","disable","--now",name,check=False)
            try: os.remove(os.path.join(UNIT_DIR,name))
            except FileNotFoundError: pass

def apply(d,restart=True):
    old_profiles=open(PROFILES,encoding="utf-8").read()
    old_users=load()
    try:
        for u in d["users"]:
            if u.get("enabled",True): write_unit(u)
        remove_old_units(d)
        sync_profiles(d)
        sync_firewall(d)
        run("systemctl","daemon-reload",check=True)
        if restart:
            for u in d["users"]:
                if u.get("enabled",True):
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
        raise

def add(protocol,name):
    d=load()
    before=copy.deepcopy(d)
    # A failed request from an older manager can leave a systemd unit in an
    # auto-restart loop even though it is absent from users.json. Remove such
    # orphan units before choosing ports for the next user.
    remove_old_units(d)
    run("systemctl","daemon-reload",check=True)
    if protocol not in ("web","mtproto"):
        raise RuntimeError("Unknown proxy protocol")
    if len(d["users"])>=MAX_USERS:
        raise RuntimeError("Maximum panel users reached")
    port,stats=alloc_ports(d)
    u={"id":secrets.token_hex(8),"name":name.strip(),"secret":secrets.token_hex(16),
       "protocol":protocol,"enabled":True,"backend_port":port,"stats_port":stats}
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

def init():
    d=load()
    if not os.path.exists(USERS): save(d)
    for u in d["users"]:
        if u.get("enabled",True): write_unit(u)
    remove_old_units(d)
    sync_profiles(d)
    sync_firewall(d)
    run("systemctl","daemon-reload",check=True)

cmd=sys.argv[1] if len(sys.argv)>1 else "init"
if cmd=="init": init()
elif cmd=="add": add(sys.argv[2]," ".join(sys.argv[3:]))
elif cmd=="delete": print(json.dumps({"deleted":delete(sys.argv[2])}))
elif cmd=="sync": apply(load(),True)
elif cmd=="firewall": sync_firewall(load())
elif cmd=="users": print(json.dumps(load(),ensure_ascii=True))
else: raise SystemExit("usage: init|add|delete|sync|firewall|users")

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
import grp
import threading
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from collections import defaultdict, deque

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
def web_link(secret):
    return "https://t.me/webproxy?server="+DOMAIN+"&secret="+secret
def mtproto_link(secret,port):
    return "tg://proxy?server="+MTPROTO_HOST+"&port="+str(int(port))+"&secret="+secret
def proxy_link(protocol,secret,port=443):
    return mtproto_link(secret,port) if protocol=="mtproto" else web_link(secret)
def qr_png_bytes(link):
    return subprocess.run([QR,"-o","-","-t","PNG","-s","6","-m","2",link],
                          stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True).stdout
def layout(title,body,active=""):
    nav=[
        (PANEL_PATH+"/users","◉","Пользователи","users"),
        (PANEL_PATH+"/settings","⚙","Настройки","settings"),
        (PANEL_PATH+"/logout","↪","Выйти","logout"),
    ]
    links="".join(
        '<a class="n %s" href="%s"><span>%s</span><b>%s</b></a>'%
        ("a" if k==active else "",h,i,l) for h,i,l,k in nav
    )
    return """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s — WEB PANEL PROXY V 2.0</title>
<style>
*{box-sizing:border-box}body{margin:0;min-width:320px;background:#080b16;color:#f5f7ff;font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}.s{display:grid;grid-template-columns:260px minmax(0,1fr);height:100vh;overflow:hidden;background:radial-gradient(circle at 82%% 8%%,#35246b55,transparent 28%%),radial-gradient(circle at 40%% 90%%,#075a7355,transparent 35%%),#080b16}
aside{position:relative;height:100vh;padding:28px 16px;overflow:hidden;border-right:1px solid #ffffff10;background:linear-gradient(180deg,#10152aee,#0b0e1ddd)}aside:before{content:"";position:absolute;width:210px;height:210px;top:-115px;left:-80px;border-radius:50%%;background:#7557ff30;filter:blur(14px)}.brand{position:relative;display:flex;gap:12px;align-items:center;margin:3px 8px 42px}.logo{width:48px;height:48px;flex:0 0 48px;overflow:hidden;border:1px solid #65dfff35;border-radius:15px;background:#07183c;box-shadow:0 12px 30px #0bd9fa2b}.logo img{display:block;width:100%%;height:100%%;object-fit:cover}.brand b{display:block;font-size:13px;letter-spacing:.08em}.brand small{display:block;margin-top:2px;color:#9ca9c9;font-size:11px}.nav{position:relative;display:grid;gap:8px}.n{display:flex;gap:12px;align-items:center;padding:13px 14px;border:1px solid transparent;border-radius:14px;color:#aeb9d4;text-decoration:none;transition:.18s ease}.n span{display:grid;place-items:center;width:24px;height:24px;border-radius:8px;background:#ffffff09;color:#a793ff;font-size:14px}.n b{font-size:13px}.n.a,.n:hover{color:#fff;border-color:#a894ff35;background:linear-gradient(100deg,#7b5cff29,#2ccce51a);box-shadow:inset 0 1px #ffffff12}.n.a span{background:#8b70ff;color:#fff;box-shadow:0 6px 16px #775cff77}.n[href$="/logout"]{margin-top:8px;color:#d8a9b9}.n[href$="/logout"] span{color:#ff91b2;background:#ff6f9d12}
main{min-width:0;width:100%%;height:100vh;overflow-y:auto;padding:42px clamp(22px,4vw,64px);scrollbar-width:thin;scrollbar-color:#58627a #0b0f1d}main::-webkit-scrollbar{width:10px}main::-webkit-scrollbar-track{background:#0b0f1d}main::-webkit-scrollbar-thumb{min-height:48px;border:2px solid #0b0f1d;border-radius:999px;background:#58627a}main::-webkit-scrollbar-thumb:hover{background:#737e99}.topbar{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;margin-bottom:26px}.topbar h1{margin:0;font-size:clamp(27px,3vw,38px);letter-spacing:-.045em}.topbar p{margin:7px 0 0;color:#94a3c4}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}.card{position:relative;min-width:0;margin:0 0 16px;padding:22px;overflow:hidden;border:1px solid #ffffff12;border-radius:20px;background:linear-gradient(135deg,#161b32e8,#0e1223e8);box-shadow:0 18px 50px #00000022}.card h3{font-size:15px}.card>form{display:block;min-width:0}.v{font-size:28px;font-weight:750;margin-top:4px}.m,.muted{color:#9aa7c4}.notice{padding:15px 17px;border:1px solid #4cd1e12e;border-radius:16px;background:#1732423d;color:#b8dce2}.code{word-break:break-all;color:#bbaeff}
input,textarea{width:100%%;padding:12px 13px;border:1px solid #ffffff17;border-radius:12px;outline:0;background:#080c1a;color:#f7f8ff;box-shadow:inset 0 1px #ffffff08;transition:border .18s,box-shadow .18s}input:focus,textarea:focus{border-color:#967eff;box-shadow:0 0 0 3px #8367ff24}textarea{min-height:220px;resize:vertical;font:12px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace}label{display:block;margin:3px 0 5px;color:#cbd4ed;font-size:12px;font-weight:650}button,.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;padding:10px 15px;border:1px solid #ffffff1c;border-radius:11px;background:#202741;color:#f7f8ff;font:600 13px inherit;text-decoration:none;cursor:pointer;transition:transform .16s,filter .16s,background .16s}button:hover,.btn:hover{filter:brightness(1.12);transform:translateY(-1px)}.primary{border:0;background:linear-gradient(135deg,#7658ff,#2bcce2);box-shadow:0 9px 22px #6548bd3b}.danger{background:#321b2d;border-color:#ff7aa433;color:#ffb2c7}
table{width:100%%;border-collapse:separate;border-spacing:0;min-width:760px;overflow:hidden;border:1px solid #ffffff0e;border-radius:14px}th{padding:12px 14px;background:#ffffff06;color:#8f9ab8;font-size:11px;letter-spacing:.06em;text-transform:uppercase;text-align:left}td{padding:14px;border-top:1px solid #ffffff0c;color:#dce3f7}tr:hover td{background:#ffffff045}.detail{display:grid;gap:9px;text-align:left;margin:16px 0}.detail-row{padding:12px 13px;border:1px solid #ffffff10;border-radius:12px;background:#090d1b}.detail-row span{display:block;color:#8995b3;font-size:10px;letter-spacing:.08em;text-transform:uppercase;margin-bottom:4px}.detail-row code{word-break:break-all;color:#bbaeff}.preset-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}.preset-card{display:flex;min-width:0;min-height:160px;flex-direction:column;align-items:flex-start;padding:17px;border:1px solid #ffffff12;border-radius:16px;background:linear-gradient(145deg,#0a1023,#15152b);box-shadow:inset 0 1px #ffffff0a}.preset-card b{font-size:14px}.preset-card p{min-height:42px;margin:8px 0 16px}.preset-card button{margin-top:auto;width:100%%}.modal{display:none;position:fixed;inset:0;z-index:9999;background:#050710c9;backdrop-filter:blur(11px);align-items:center;justify-content:center;padding:20px}.modal.open{display:flex}.modal-box{width:min(430px,100%%);padding:28px;border:1px solid #ffffff18;border-radius:24px;background:#12172add;box-shadow:0 30px 100px #000b;text-align:center}.qr{width:240px;height:240px;padding:10px;border-radius:18px;background:#fff;display:block;margin:0 auto 16px}.modal-actions{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin-top:16px}.side-note{position:absolute;bottom:25px;left:24px;right:24px;color:#687493;font-size:11px}
.side-links{position:absolute;bottom:24px;left:24px;right:24px;display:flex;gap:8px;opacity:.58}.side-links a{display:inline-flex;align-items:center;gap:5px;padding:5px 7px;color:#8490ad;font-size:10px;text-decoration:none;transition:color .18s ease,opacity .18s ease}.side-links a span{font-size:11px;line-height:1;color:#98a5c5}.side-links a:hover{color:#dce3f7;opacity:1}
@media(max-width:820px){.s{grid-template-columns:1fr;height:auto;min-height:100vh;overflow:visible}aside{height:auto;padding:14px 18px;border-right:0;border-bottom:1px solid #ffffff10}.brand{margin:0 0 14px}.nav{grid-template-columns:repeat(3,1fr)}.n[href$="/logout"]{margin-top:0}.side-note,.side-links{display:none}main{height:auto;overflow:visible;padding:28px 18px}}@media(max-width:520px){.topbar{flex-direction:column}.topbar h1{font-size:28px}.card{padding:16px}.nav{gap:7px}.n{padding:10px}.n b{font-size:12px}.preset-grid{grid-template-columns:1fr}table{min-width:620px}}
select{width:100%%;margin:0 0 14px;padding:12px 13px;border:1px solid #ffffff17;border-radius:12px;outline:0;background:#080c1a;color:#f7f8ff;font:inherit}select:focus{border-color:#967eff;box-shadow:0 0 0 3px #8367ff24}
</style>
<div class="s"><aside><div class="brand"><div class="logo"><img src="%s/__logo" alt="WPP"></div><div><b>WEB PANEL PROXY</b><small>V 2.0 · Панель управления</small></div></div><nav class="nav">%s</nav><div class="side-links"><a href="https://www.youtube.com/@POLESNIESOVETI12" target="_blank" rel="noopener noreferrer" aria-label="YouTube"><span>▶</span>YouTube</a><a href="https://github.com/POLESNIESOVETI12/webtelegram" target="_blank" rel="noopener noreferrer" aria-label="GitHub"><span>◉</span>GitHub</a></div></aside><main>%s</main></div>
"""%(esc(title),esc(PANEL_PATH),links,body)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def send_html(self,s,code=200):
        b=s.encode(); self.send_response(code); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def send_png(self,b):
        self.send_response(200); self.send_header("Content-Type","image/png"); self.send_header("Cache-Control","no-store"); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def send_logo(self,b):
        self.send_response(200); self.send_header("Content-Type","image/png"); self.send_header("Cache-Control","public, max-age=86400"); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def redirect(self,p):
        self.send_response(303); self.send_header("Location",PANEL_PATH+p if p.startswith("/") else p); self.end_headers()
    def form(self):
        try: n=int(self.headers.get("Content-Length","0"))
        except ValueError: n=0
        if n < 0 or n > MAX_HTML_BYTES: raise ValueError("Invalid form size")
        return {k:v[-1] for k,v in parse_qs(self.rfile.read(n).decode("utf-8")).items()}
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
            login_page="""<!doctype html><html lang=ru><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Вход — WEB PROXY</title><style>*{box-sizing:border-box}body{min-width:320px;margin:0;min-height:100vh;display:grid;place-items:center;overflow:hidden;background:radial-gradient(circle at 15% 20%,#7658ff55,transparent 30%),radial-gradient(circle at 85% 75%,#22cce755,transparent 35%),#080b16;color:#f7f8ff;font:14px Inter,ui-sans-serif,system-ui,sans-serif}.orb{position:fixed;width:340px;height:340px;border:1px solid #ffffff14;border-radius:50%;filter:blur(1px)}.orb.one{top:-180px;left:-100px}.orb.two{right:-180px;bottom:-100px}.login{position:relative;width:min(430px,calc(100vw - 36px));padding:34px;border:1px solid #ffffff18;border-radius:28px;background:linear-gradient(145deg,#171c34eF,#0d1020ee);box-shadow:0 30px 90px #0008}.mark{display:grid;place-items:center;width:82px;height:82px;margin-bottom:24px;overflow:hidden;border:1px solid #62e5ff38;border-radius:23px;background:#07183c;box-shadow:0 14px 34px #06d6f33a}.mark img{display:block;width:100%;height:100%;object-fit:cover}h1{margin:0;font-size:27px;letter-spacing:-.04em}.sub{margin:8px 0 26px;color:#9eabca;line-height:1.55}label{display:block;margin:0 0 7px;color:#c6d0ea;font-size:12px;font-weight:700}input{width:100%;margin:0 0 17px;padding:13px;border:1px solid #ffffff19;border-radius:12px;outline:0;background:#080c1a;color:#fff;font:14px inherit}input:focus{border-color:#967eff;box-shadow:0 0 0 3px #8367ff2b}button{width:100%;padding:13px;border:0;border-radius:12px;background:linear-gradient(135deg,#7658ff,#2bcce2);box-shadow:0 11px 24px #6548bd44;color:white;font:700 14px inherit;cursor:pointer;transition:transform .16s,filter .16s}button:hover{filter:brightness(1.08);transform:translateY(-1px)}.hint{margin:20px 0 0;color:#697595;font-size:11px;text-align:center}</style><div class="orb one"></div><div class="orb two"></div><main class=login><div class=mark><img src="__LOGIN_PATH__/__logo" alt="WPP"></div><h1>Добро пожаловать</h1><p class=sub>Войдите в панель управления WEB PROXY.</p><form method="post" action="__LOGIN_PATH__/login"><label>Логин</label><input name=user required autocomplete=username><label>Пароль</label><input type=password name=password required autocomplete=current-password><button type=submit>Войти в панель</button></form><p class=hint>Защищённое соединение · HTTPS</p></main></html>"""
            login_page=login_page.replace("<title>Вход — WEB PROXY</title>","<title>Вход — WEB PANEL PROXY V 2.0</title>").replace("WEB PROXY.","WEB PANEL PROXY V 2.0.").replace("</style>","<style>.login{text-align:center}.mark{margin:0 auto 24px}.login form{text-align:left}</style>")
            self.send_html(login_page.replace("__LOGIN_PATH__",esc(PANEL_PATH))); return
        if path==PANEL_PATH+"/logout":
            self.send_response(303); self.send_header("Set-Cookie",self.session_cookie("",0)); self.send_header("Location",PANEL_PATH+"/login"); self.end_headers(); return
        if not self.auth():
            self.redirect("/login"); return

        if path==PANEL_PATH or path==PANEL_PATH+"/":
            self.redirect("/users"); return

        if path==PANEL_PATH+"/users":
            rows=[]
            current_users=users()
            primary_a={"id":"primary","name":"Основной","secret":primary(),"protocol":"web","enabled":True,"backend_port":443}
            for u in [primary_a]+current_users:
                sec=u["secret"]; protocol=u.get("protocol","web"); port=int(u.get("backend_port",443)); link=proxy_link(protocol,sec,port)
                rows.append(
                    '<tr><td><b>'+esc(u["name"])+'</b></td>'
                    '<td><code>'+esc(u["id"])+'</code></td>'
                    '<td><span class="muted">'+('MTProto' if protocol=='mtproto' else 'WEB Proxy')+'</span></td>'
                    '<td><code class="code">'+esc(sec)+'</code></td>'
                    '<td>'
                    '<button class="btn primary" type="button" data-secret="'+esc(sec)+'" data-protocol="'+esc(protocol)+'" data-port="'+str(port)+'" data-link="'+esc(link)+'" onclick="openQr(this.dataset.secret,this.dataset.protocol,this.dataset.port,this.dataset.link)">QR-код</button> '
                    '<button class="btn" type="button" data-link="'+esc(link)+'" onclick="copyLink(this.dataset.link,this)">Скопировать ссылку</button>'
                    '</td><td>'+('<span class="muted">Основной</span>' if u["id"]=="primary" else '<form method=post action="'+PANEL_PATH+'/delete-user"><input type=hidden name=csrf value="'+esc(self.csrf())+'"><input type=hidden name=id value="'+esc(u["id"])+'"><button class="btn danger" type="submit">Удалить</button></form>')+'</td></tr>'
                )
            primary_s=primary()
            primary_link=web_link(primary_s)
            body=f"""<div class=topbar><div><h1>Пользователи</h1><p>Рабочие Telegram WEB Proxy подключения</p></div></div>
<div class=card><h3 style="margin-top:0">Основной Secret установки</h3><div class=detail-row><span>Домен</span><code>{esc(DOMAIN)}</code></div><div class=detail-row style="margin-top:8px"><span>Secret</span><code class=code>{esc(primary_s)}</code></div><div style="margin-top:12px"><button class="btn primary" data-link="{esc(primary_link)}" onclick="copyLink(this.dataset.link,this)">Скопировать основную ссылку</button> <button class="btn" data-secret="{esc(primary_s)}" data-protocol="web" data-port="443" data-link="{esc(primary_link)}" onclick="openQr(this.dataset.secret,this.dataset.protocol,this.dataset.port,this.dataset.link)">QR основной ссылки</button></div></div>
<div class=card><form method=post action="{PANEL_PATH}/add-user"><input type=hidden name=csrf value="{esc(self.csrf())}"><label>Имя пользователя</label><input name=name placeholder="Ivan" maxlength=80 required><button class="btn primary">＋ Создать WEB Proxy</button></form></div>
<div class=card style="overflow:auto"><table><tr><th>Имя</th><th>ID</th><th>Протокол</th><th>Secret</th><th>Подключение</th><th></th></tr>{''.join(rows) if rows else '<tr><td colspan=6 class=muted>Пользователей пока нет.</td></tr>'}</table></div>
<div id=qrModal class=modal onclick="closeQr(event)"><div class=modal-box onclick="event.stopPropagation()"><h3>Подключение Telegram</h3><div class=sub>QR-код и данные подключения</div><img id=qrImage class=qr src="" alt="QR"><div class=detail><div class=detail-row><span>Домен</span><code id=qrDomain>{esc(DOMAIN)}</code></div><div id=qrPortRow class="detail-row"><span>Порт</span><code id=qrPort></code></div><div class=detail-row><span>Secret</span><code id=qrSecret></code></div></div><div class=modal-actions><button class="btn primary" onclick="copyLink(window.currentLink,this)">Скопировать ссылку</button><button class="btn" onclick="closeQr()">Закрыть</button></div></div></div>
<script>
window.currentLink="";
function openQr(secret,protocol,port,link){{window.currentLink=link;document.getElementById("qrSecret").textContent=secret;document.getElementById("qrDomain").textContent=protocol==="mtproto"?"{esc(MTPROTO_HOST)}":"{esc(DOMAIN)}";const row=document.getElementById("qrPortRow");row.style.display=protocol==="mtproto"?"block":"none";document.getElementById("qrPort").textContent=port;document.getElementById("qrImage").src="{PANEL_PATH}/__qr?secret="+encodeURIComponent(secret)+"&protocol="+encodeURIComponent(protocol)+"&port="+encodeURIComponent(port);document.getElementById("qrModal").classList.add("open");document.body.style.overflow="hidden"}}
function closeQr(e){{if(e&&e.target!==e.currentTarget)return;document.getElementById("qrModal").classList.remove("open");document.body.style.overflow=""}}
async function copyLink(t,b){{try{{await navigator.clipboard.writeText(t)}}catch(e){{const x=document.createElement("textarea");x.value=t;document.body.appendChild(x);x.select();document.execCommand("copy");x.remove()}}const o=b.textContent;b.textContent="Скопировано ✓";setTimeout(()=>b.textContent=o,1400)}}
</script>"""
            self.send_html(layout("Пользователи",body,"users")); return

        if path==PANEL_PATH+"/__qr":
            query=parse_qs(urlparse(self.path).query)
            q=query.get("secret",[""])[0]; protocol=query.get("protocol",["web"])[0]
            port=query.get("port",["443"])[0]
            current_users=users()
            matching=next((x for x in current_users if x.get("secret")==q),None)
            if q==primary(): matching={"protocol":"web","backend_port":443}
            if not matching or protocol!=matching.get("protocol","web"):
                self.send_html("Not found",404); return
            try:
                expected=str(int(matching.get("backend_port",443)))
                if protocol=="mtproto" and port!=expected: self.send_html("Not found",404); return
                self.send_png(qr_png_bytes(proxy_link(protocol,q,expected)))
            except Exception as e:self.send_html("QR error: "+esc(e),500)
            return


        if path==PANEL_PATH+"/settings":
            token=esc(self.csrf())
            try: site_html=esc(read_site_html())
            except Exception as e: site_html="<!-- "+esc(e)+" -->"
            preset_cards="".join(
                '<form method=post action="'+PANEL_PATH+'/apply-preset" class="preset-card"><input type=hidden name=csrf value="'+token+'"><input type=hidden name=preset value="'+esc(preset["id"])+'"><b>'+esc(preset["name"])+'</b><p class=muted>'+esc(preset["description"])+'</p><button class="btn">Применить</button></form>'
                for preset in PRESETS
            )
            body=f'''<div class=topbar><div><h1>Настройки</h1><p>Учётная запись панели</p></div></div>
<div class=card><h3 style="margin-top:0">Пресеты заглушек</h3><p class=muted>Пресет заменит текущую главную страницу. Предыдущая версия и CSS сохраняются в резервной копии.</p><div class=preset-grid>{preset_cards}</div></div>
<div class=card><h3 style="margin-top:0">HTML главной страницы</h3><p class=muted>Вставьте полный HTML одним файлом. Панель хранит исходник отдельно и при каждом сохранении заново создаёт локальные CSS/JS-файлы для WEB Proxy. После публикации проверяется доступность стилей и скриптов; при ошибке автоматически восстанавливается предыдущая версия.</p><form method=post action="{PANEL_PATH}/site-html"><input type=hidden name=csrf value="{token}"><textarea name=html rows=24 spellcheck=false maxlength="{MAX_HTML_BYTES}" required>{site_html}</textarea><button class="btn primary">Сохранить HTML</button></form></div>
<div class=card><h3 style="margin-top:0">Сменить пароль администратора</h3><form method=post action="{PANEL_PATH}/password"><input type=hidden name=csrf value="{token}"><label>Новый пароль</label><input type=password name=a minlength=3 required autocomplete=new-password><button class="btn primary">Сохранить пароль</button></form></div>'''
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
            try: form=self.form()
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
                self.send_header("Location",PANEL_PATH+"/users")
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
        except ValueError as e:
            self.send_html(esc(e),400); return
        d=load()

        if not self.valid_csrf(form):
            self.send_html("Недействительный запрос. Обновите страницу и попробуйте снова.",403)
            return

        if path==PANEL_PATH+"/add-user":
            name=form.get("name","").strip()
            protocol="web"
            if not name or len(name)>80:
                self.send_html("Имя пользователя обязательно.",400); return
            try:
                result=ctl("add",protocol,name)
                self.redirect("/users")
            except Exception as e:
                self.send_html("Ошибка создания пользователя: "+esc(e),500)
            return

        if path==PANEL_PATH+"/delete-user":
            uid=form.get("id","")
            if not uid or uid=="primary":
                self.send_html("Нельзя удалить основной профиль.",400); return
            try:
                ctl("delete",uid)
                self.redirect("/users")
            except Exception as e:
                self.send_html("Ошибка удаления пользователя: "+esc(e),500)
            return

        if path==PANEL_PATH+"/site-html":
            try:
                write_site_html(form.get("html",""))
                self.redirect("/settings")
            except (ValueError,RuntimeError,OSError) as e:
                self.send_html("Ошибка сохранения HTML: "+esc(e),400)
            return

        if path==PANEL_PATH+"/apply-preset":
            try:
                preset=get_preset(form.get("preset",""))
                write_site_html(preset["html"])
                self.redirect("/settings")
            except (ValueError,RuntimeError,OSError) as e:
                self.send_html("Ошибка применения пресета: "+esc(e),400)
            return

        if path==PANEL_PATH+"/password":
            a=form.get("a","")
            if len(a)<3:
                self.send_html("Пароль должен содержать минимум 3 символа.",400)
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
Description=WEB PANEL PROXY V 2.0
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

echo "[4.5/6] Configuring Caddy panel route..."
CADDYFILE="/etc/caddy/Caddyfile"
test -s "$CADDYFILE" || die "Caddyfile is missing."

python3 - "$CADDYFILE" "$PANEL_PATH" "$DOMAIN" "$ACME_EMAIL" <<'PY'
import re, sys
p, path, domain, email = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
s = open(p, encoding="utf-8").read()

# Materialize the core Caddyfile placeholders before validation.
s = s.replace("{$TPROXY_HOSTNAME}", domain)
s = s.replace("{$ACME_EMAIL}", email)
s = re.sub(
    r'\n\s*handle(?:_path)? /panel-[a-f0-9]+/\*\s*\{\s*reverse_proxy 127\.0\.0\.1:8090\s*\}\s*',
    '\n',
    s,
    flags=re.S,
)
m = re.search(r'(?m)^\s*reverse_proxy 127\.0\.0\.1:8080\s*\{', s)
if not m:
    raise SystemExit("Could not locate tproxy relay reverse_proxy in Caddyfile")
route = (
    "    handle " + path + "/* {\n"
    "        reverse_proxy 127.0.0.1:8090\n"
    "    }\n\n"
)
s = s[:m.start()] + route + s[m.start():]
open(p, "w", encoding="utf-8").write(s)
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
systemctl enable web-proxy-panel-firewall.service
systemctl restart web-proxy-panel-firewall.service
systemctl enable tproxy-panel.service
systemctl restart tproxy-panel.service

echo "      Initializing user/secret manager..."
"$MANAGER" init

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

echo
echo "============================================================"
if [[ "$UPDATING" == "1" ]]; then
echo "            WEB PANEL PROXY V 2.0 UPDATED"
else
echo "            WEB PANEL PROXY V 2.0 IS READY"
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
