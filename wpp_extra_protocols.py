#!/usr/bin/env python3
"""Isolated NaiveProxy and Mieru server profiles.

Naive uses a dedicated Caddy build and never replaces the panel Caddy. Mieru
uses one official mita process with one independently assigned port binding
for every enabled server connection.
"""

import base64
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, urlencode

PROTOCOLS = ("naive", "mieru")
NAIVE_BIN = "/usr/local/bin/caddy-naive"
MIERU_BIN = "/usr/local/bin/mita-wpp"
DATA_DIR = Path("/etc/web-proxy-panel/extra-protocols")
NAIVE_CONFIG = DATA_DIR / "Caddyfile.naive"
NAIVE_SERVICE = "web-panel-proxy-naive.service"
MIERU_SERVICE = "web-panel-proxy-mieru.service"
MIERU_CONFIG = DATA_DIR / "mieru-server.json"
NAIVE_CERT = DATA_DIR / "naive-domain.crt"
NAIVE_KEY = DATA_DIR / "naive-domain.key"


class ServiceError(RuntimeError):
    """A credential-free service failure that can be displayed in the panel."""


def _run(*args, check=True, timeout=40):
    proc = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout)
    if check and proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout or "service command failed").strip())
    return proc


def _prepare_data_dir():
    # Naive runs as caddy, Mieru as mita. Both must traverse this shared
    # directory; credentials and private keys remain group-only files (0640).
    # Repair the root:caddy/0750 directory left by old TLS maintenance runs.
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    shutil.chown(DATA_DIR, user="root", group="root")
    os.chmod(DATA_DIR, 0o755)


def _atomic(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush(); os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        try: os.unlink(name)
        except FileNotFoundError: pass


def _wait_listeners(bindings, label):
    """Confirm that systemd being active also means the proxy is listening."""
    expected = {(str(protocol).lower(), int(port)) for protocol, port in bindings}
    # A small single-core VPS can need several seconds after systemd reports the
    # Go service as active.  Do not roll back an otherwise healthy profile too
    # early.
    for _ in range(60):
        sockets = _run("ss", "-lntuH", check=False).stdout or ""
        missing = []
        for protocol, port in expected:
            pattern = rf"(?m)^{re.escape(protocol)}\s+.*(?:\]|:|\.){port}(?:\s|$)"
            if not re.search(pattern, sockets): missing.append((protocol, port))
        if not missing: return
        time.sleep(0.5)
    values = ", ".join(f"{protocol.upper()} {port}" for protocol, port in sorted(missing))
    raise RuntimeError(f"{label} service is active but does not listen on {values}")


def new_user(protocol, uid):
    if protocol not in PROTOCOLS or not re.fullmatch(r"[a-f0-9]{16}", str(uid)):
        raise RuntimeError("Invalid external proxy profile")
    return {
        "username": "wpp" + str(uid)[:10],
        "secret": secrets.token_urlsafe(24),
    }


def upgrade_users(users):
    """Normalize records left by withdrawn experimental protocol builds."""
    changed = False
    occupied = set()
    for user in users:
        if user.get("protocol") not in PROTOCOLS: continue
        username = str(user.get("username", ""))
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", username) or username in occupied:
            seed = re.sub(r"[^a-f0-9]", "", str(user.get("id", "")).lower())[:10]
            username = "wpp" + (seed or secrets.token_hex(5))
            while username in occupied: username = "wpp" + secrets.token_hex(5)
            user["username"] = username
            changed = True
        occupied.add(username)
        password = str(user.get("secret", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", password):
            user["secret"] = secrets.token_urlsafe(24)
            changed = True
        if user.get("max_devices") != 1:
            user["max_devices"] = 1
            changed = True
        try: traffic_limit = int(user.get("traffic_limit_bytes", 0) or 0)
        except (TypeError, ValueError): traffic_limit = -1
        if "traffic_limit_bytes" not in user or traffic_limit != 0:
            user["traffic_limit_bytes"] = 0
            changed = True
    return changed


def _connection_map(connections):
    return {str(item.get("id", "")): item for item in connections
            if item.get("protocol") in PROTOCOLS and item.get("enabled", True)}


def _port(connection):
    try: value = int((connection.get("config") or {}).get("port", 0))
    except (TypeError, ValueError): value = 0
    if not 1025 <= value <= 65535:
        raise RuntimeError("Naive/Mieru connection has no valid port")
    return value


def _eligible(users, connections, protocol):
    registry = _connection_map(connections)
    result = []
    for user in users:
        if user.get("protocol") != protocol or not user.get("enabled", True):
            continue
        connection = registry.get(str(user.get("connection_id", "")))
        if connection and connection.get("protocol") == protocol:
            result.append((user, connection))
    return result


def _sync_naive(users, connections, domain, cert, key):
    _prepare_data_dir()
    selected = _eligible(users, connections, "naive")
    grouped = {}
    for user, connection in selected:
        grouped.setdefault(str(connection["id"]), {"connection": connection, "users": []})["users"].append(user)
    if grouped:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cert, NAIVE_CERT)
        shutil.copyfile(key, NAIVE_KEY)
        shutil.chown(NAIVE_CERT, user="root", group="caddy"); os.chmod(NAIVE_CERT, 0o640)
        shutil.chown(NAIVE_KEY, user="root", group="caddy"); os.chmod(NAIVE_KEY, 0o640)
    lines = ["{", "\tadmin off", "\tauto_https off",
             "\torder forward_proxy before respond", "}", ""]
    for value in grouped.values():
        connection = value["connection"]
        # A forward proxy must match CONNECT requests for arbitrary target
        # hosts. A domain-only Caddy site label would reject those requests;
        # the explicit TLS certificate still authenticates our public domain.
        lines.extend([f":{_port(connection)} {{", f"\ttls {NAIVE_CERT} {NAIVE_KEY}",
                      "\tforward_proxy {"])
        for user in value["users"]:
            username = str(user.get("username", ""))
            password = str(user.get("secret", ""))
            if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", username) or not password or any(ch.isspace() for ch in password):
                raise RuntimeError("Invalid NaiveProxy credentials")
            lines.append(f"\t\tbasic_auth {username} {password}")
        lines.extend(["\t\thide_ip", "\t\thide_via", "\t\tprobe_resistance", "\t}",
                      "\trespond \"Not Found\" 404", "}", ""])
    _atomic(NAIVE_CONFIG, "\n".join(lines), 0o640)
    shutil.chown(NAIVE_CONFIG, user="root", group="caddy")
    if grouped:
        _run(NAIVE_BIN, "validate", "--config", str(NAIVE_CONFIG), "--adapter", "caddyfile")
        _run("systemctl", "enable", NAIVE_SERVICE)
        _run("systemctl", "restart", NAIVE_SERVICE)
        if _run("systemctl", "is-active", "--quiet", NAIVE_SERVICE, check=False).returncode:
            raise RuntimeError("NaiveProxy service did not start")
        _wait_listeners((("tcp", _port(value["connection"])) for value in grouped.values()), "NaiveProxy")
    else:
        _run("systemctl", "disable", "--now", NAIVE_SERVICE, check=False)


def _sync_mieru(users, connections):
    _prepare_data_dir()
    selected = _eligible(users, connections, "mieru")
    grouped = {}
    for user, connection in selected:
        grouped.setdefault(str(connection["id"]), {"connection": connection, "users": []})["users"].append(user)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for path in DATA_DIR.glob("mieru-*.json"):
        if path != MIERU_CONFIG:
            try: path.unlink()
            except FileNotFoundError: pass
    bindings = []
    credentials = []
    for cid, value in grouped.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,63}", cid):
            raise RuntimeError("Invalid Mieru connection id")
        connection = value["connection"]
        transport = str((connection.get("config") or {}).get("transport", "TCP")).upper()
        if transport not in ("TCP", "UDP"): transport = "TCP"
        bindings.append({"port": _port(connection), "protocol": transport})
        credentials.extend({"name": str(user["username"]), "password": str(user["secret"])}
                           for user in value["users"])
    if grouped:
        payload = {"portBindings": bindings, "users": credentials,
                   "loggingLevel": "INFO", "mtu": 1400,
                   "dns": {"dualStack": "PREFER_IPv4"}}
        _atomic(MIERU_CONFIG, json.dumps(payload, ensure_ascii=True, indent=2) + "\n", 0o640)
        shutil.chown(MIERU_CONFIG, user="root", group="mita")
        _run("systemctl", "enable", MIERU_SERVICE)
        _run("systemctl", "restart", MIERU_SERVICE)
        if _run("systemctl", "is-active", "--quiet", MIERU_SERVICE, check=False).returncode:
            raise RuntimeError("Mieru service did not start")
        # `mita run` starts the management daemon first.  With some existing
        # installations it can remain IDLE instead of starting the proxy even
        # though systemd reports the unit as active.  Ask the daemon to start
        # explicitly through the same private UDS used by this service.
        start = None
        for _ in range(40):
            start = _run(
                "env",
                f"MITA_CONFIG_JSON_FILE={MIERU_CONFIG}",
                "MITA_UDS_PATH=/run/mita-wpp/mita.sock",
                "MITA_LOG_NO_TIMESTAMP=true",
                MIERU_BIN, "start", check=False,
            )
            if start.returncode == 0:
                break
            time.sleep(0.25)
        if start is None or start.returncode:
            raise RuntimeError((start.stderr or start.stdout or
                                "Mieru daemon did not start the proxy").strip())
        _wait_listeners(((str(binding["protocol"]).lower(), binding["port"]) for binding in bindings), "Mieru")
        status = _run(
            "env",
            f"MITA_CONFIG_JSON_FILE={MIERU_CONFIG}",
            "MITA_UDS_PATH=/run/mita-wpp/mita.sock",
            "MITA_LOG_NO_TIMESTAMP=true",
            MIERU_BIN, "status", check=False,
        )
        status_text = (status.stdout or "") + "\n" + (status.stderr or "")
        if status.returncode or "RUNNING" not in status_text.upper():
            raise RuntimeError((status_text.strip() or "Mieru proxy is not in RUNNING state"))
    else:
        _run("systemctl", "disable", "--now", MIERU_SERVICE, check=False)
        try: MIERU_CONFIG.unlink()
        except FileNotFoundError: pass


def sync(users, connections, domain, cert, key):
    for label, unit, apply_config, args in (
        ("NaiveProxy", NAIVE_SERVICE, _sync_naive, (users, connections, domain, cert, key)),
        ("Mieru", MIERU_SERVICE, _sync_mieru, (users, connections)),
    ):
        try:
            apply_config(*args)
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            detail = str(exc).lower()
            if "permission denied" in detail or isinstance(exc, PermissionError):
                reason = "Проверьте права доступа службы к конфигурации."
            elif "does not listen" in detail:
                reason = "Служба не открыла порт подключения."
            elif isinstance(exc, subprocess.TimeoutExpired):
                reason = "Превышено время ожидания запуска службы."
            else:
                reason = "Служба не смогла применить конфигурацию."
            # Never return command stderr or config contents: they may contain
            # proxy credentials. Preserve the cause in the manager traceback.
            raise ServiceError(
                f"Не удалось настроить {label}. {reason} "
                f"Диагностика: journalctl -u {unit} -n 30 --no-pager"
            ) from exc


def service_for(user):
    protocol = user.get("protocol")
    if protocol == "naive": return NAIVE_SERVICE
    if protocol == "mieru": return MIERU_SERVICE
    return ""


def _pb_varint(value):
    """Encode the small non-negative protobuf integers used by Mieru config."""
    value = int(value)
    if value < 0:
        raise RuntimeError("Invalid Mieru protobuf value")
    result = bytearray()
    while value > 0x7f:
        result.append((value & 0x7f) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def _pb_int(field, value):
    return _pb_varint((int(field) << 3) | 0) + _pb_varint(value)


def _pb_bytes(field, value):
    raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    return _pb_varint((int(field) << 3) | 2) + _pb_varint(len(raw)) + raw


def _mieru_client_link(username, password, domain, port, transport):
    """Build the official full mieru:// ClientConfig sharing link.

    The human-readable mierus:// form only contains a ClientProfile. Official
    Mieru rejects it on a fresh installation because local RPC and SOCKS ports
    are absent. The standard form serializes the complete protobuf config.
    """
    protocol = 1 if str(transport).upper() == "UDP" else 2  # appctlpb TransportProtocol
    user = _pb_bytes(1, username) + _pb_bytes(2, password)
    binding = _pb_int(1, port) + _pb_int(2, protocol)
    try:
        ipaddress.ip_address(domain)
        server = _pb_bytes(1, domain)
    except ValueError:
        server = _pb_bytes(2, domain)
    server += _pb_bytes(3, binding)
    multiplexing = _pb_int(1, 4)  # MULTIPLEXING_HIGH
    profile = b"".join((
        _pb_bytes(1, "default"),
        _pb_bytes(2, user),
        _pb_bytes(3, server),
        _pb_int(4, 1400),
        _pb_bytes(5, multiplexing),
        _pb_int(6, 1),  # HANDSHAKE_STANDARD is more stable than 0-RTT.
    ))
    config = b"".join((
        _pb_bytes(1, profile),
        _pb_bytes(2, "default"),
        _pb_int(3, 8964),
        _pb_int(4, 1080),
        _pb_int(6, 4),  # INFO
    ))
    return "mieru://" + base64.b64encode(config).decode("ascii")


def native_client_link(user, connection, domain):
    """Full config for the official Mieru CLI, including its local ports."""
    if user.get("protocol") != "mieru":
        raise RuntimeError("Native client config is only available for Mieru")
    transport = str((connection.get("config") or {}).get("transport", "TCP")).upper()
    return _mieru_client_link(str(user.get("username", "")),
                              str(user.get("secret", "")), domain, _port(connection), transport)


def client_link(user, connection, domain, name="Proxy"):
    protocol = user.get("protocol")
    port = _port(connection)
    username = quote(str(user.get("username", "")), safe="")
    password = quote(str(user.get("secret", "")), safe="")
    if protocol == "naive":
        return f"naive+https://{username}:{password}@{domain}:{port}#{quote(str(name), safe='')}"
    if protocol == "mieru":
        transport = str((connection.get("config") or {}).get("transport", "TCP")).upper()
        # Shadowrocket imports the simple server profile, not the official
        # CLI's full protobuf config. Put the port in both authority and query
        # for clients that read only one of these locations.
        host = str(domain).strip("[]")
        if ":" in host:
            host = "[" + host + "]"
        query = urlencode({"profile": str(name), "port": str(port), "protocol": transport,
                           "mtu": "1400", "multiplexing": "MULTIPLEXING_HIGH",
                           "handshake-mode": "HANDSHAKE_STANDARD"}, quote_via=quote)
        return f"mierus://{username}:{password}@{host}:{port}?{query}#{quote(str(name), safe='')}"
    raise RuntimeError("Unsupported external protocol")
