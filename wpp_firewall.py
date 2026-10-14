#!/usr/bin/env python3
"""Server connection templates for WEB PANEL PROXY.

Connections describe how traffic reaches a service.  Client credentials stay
in users.json and only reference a connection by id.  This separation lets an
operator repair a CDN hostname or TLS setting once without rotating UUIDs.
"""
import copy
import hashlib
import json
import os
import re
import secrets
import tempfile
import time

SCHEMA = 1
PROTOCOLS = {"vless", "hysteria", "mtproto", "web"}
PROVIDERS = {"yandex", "vk", "beget", "selectel", "custom"}
FINGERPRINTS = {"chrome", "firefox", "safari", "edge", "random", "randomized"}
MODES = {"packet-up", "stream-up", "auto"}
METHODS = {"GET", "POST", "DELETE"}
HOST_RE = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.I)


class ConnectionError(ValueError):
    pass


def _base(cid, name, protocol, **config):
    return {"id": cid, "name": name, "protocol": protocol, "enabled": True,
            "created_at": int(time.time()), "config": config, "advanced": {}}


def defaults():
    return [
        _base("vless-direct", "VLESS XHTTP · напрямую", "vless", route="direct", fingerprint="chrome", mode="packet-up", uplink_method="POST"),
        _base("hysteria-primary", "Hysteria2 · основной", "hysteria", port=8443),
        _base("mtproto-primary", "MTProto · отдельный порт", "mtproto", port=0),
        _base("web-primary", "Web Proxy · HTTPS", "web", port=0),
    ]


def _atomic(path, payload):
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".connections-", dir=folder, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush(); os.fsync(stream.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass


def load(path):
    try:
        with open(path, encoding="utf-8") as stream: data = json.load(stream)
    except FileNotFoundError:
        return {"schema": SCHEMA, "connections": defaults()}
    except (OSError, json.JSONDecodeError) as exc:
        raise ConnectionError("Файл подключений повреждён: "+str(exc))
    if not isinstance(data, dict) or not isinstance(data.get("connections"), list):
        raise ConnectionError("Некорректный формат файла подключений.")
    return data


def save(path, data):
    clean = {"schema": SCHEMA, "connections": [validate(item, existing=item) for item in data.get("connections", [])]}
    _atomic(path, clean)
    return clean


def _host(value, label, required=False):
    value = str(value or "").strip().lower().rstrip(".")
    if required and not value: raise ConnectionError("Укажите "+label+".")
    if value and not HOST_RE.fullmatch(value): raise ConnectionError("Некорректное значение поля «"+label+"».")
    return value


def _integer(value, label, low, high, default=0):
    try: number = int(str(value if value not in (None, "") else default))
    except (TypeError, ValueError): raise ConnectionError(label+" должен быть числом.")
    if number < low or number > high: raise ConnectionError(f"{label}: допустимо от {low} до {high}.")
    return number


def validate(raw, existing=None):
    if not isinstance(raw, dict): raise ConnectionError("Некорректные параметры подключения.")
    old = existing if isinstance(existing, dict) else {}
    cid = str(raw.get("id") or old.get("id") or secrets.token_hex(8))
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,63}", cid): raise ConnectionError("Некорректный идентификатор подключения.")
    name = str(raw.get("name", "")).strip()
    if not name or len(name) > 80 or any(ord(c) < 32 for c in name): raise ConnectionError("Укажите название длиной до 80 символов.")
    protocol = str(raw.get("protocol", "")).lower()
    if protocol not in PROTOCOLS: raise ConnectionError("Неизвестный протокол подключения.")
    if old and old.get("protocol") and protocol != old.get("protocol"):
        raise ConnectionError("Протокол используемого подключения менять нельзя. Создайте новое подключение.")
    old_config = old.get("config") if isinstance(old.get("config"), dict) else {}
    supplied_config = raw.get("config") if isinstance(raw.get("config"), dict) else {}
    config = {**old_config, **supplied_config}
    result = {"id": cid, "name": name, "protocol": protocol,
              "enabled": bool(raw.get("enabled", True)),
              "created_at": int(old.get("created_at") or raw.get("created_at") or time.time()),
              "config": {}, "advanced": {}}
    if protocol == "vless":
        route = str(config.get("route", "direct"))
        if route not in ("direct", "cdn"): raise ConnectionError("Неизвестный маршрут VLESS.")
        provider = str(config.get("provider", "custom"))
        if provider not in PROVIDERS: raise ConnectionError("Неизвестный CDN-провайдер.")
        public_host = _host(config.get("public_host"), "публичный домен CDN", route == "cdn")
        fingerprint = str(config.get("fingerprint", "chrome"))
        if fingerprint not in FINGERPRINTS: raise ConnectionError("Неизвестный TLS fingerprint.")
        mode = str(config.get("mode", "packet-up"))
        if mode not in MODES: raise ConnectionError("Неизвестный режим XHTTP.")
        method = str(config.get("uplink_method", "GET" if route == "cdn" else "POST")).upper()
        if method not in METHODS: raise ConnectionError("Метод XHTTP должен быть GET, POST или DELETE.")
        result["config"] = {"route": route, "provider": provider, "public_host": public_host,
            "sni": _host(config.get("sni"), "TLS SNI"), "host_header": _host(config.get("host_header"), "HTTP Host"),
            "fingerprint": fingerprint, "mode": mode, "uplink_method": method,
            "padding": str(config.get("padding", "100-1000"))[:40],
            "xmux": bool(config.get("xmux", provider in ("beget", "selectel"))),
            "no_sse_header": bool(config.get("no_sse_header", False))}
    else:
        result["config"] = {"port": _integer(config.get("port"), "Порт", 0, 65535)}
    advanced = raw.get("advanced", old.get("advanced", {}))
    if isinstance(advanced, str):
        try: advanced = json.loads(advanced or "{}")
        except json.JSONDecodeError: raise ConnectionError("Расширенные настройки должны быть корректным JSON-объектом.")
    if not isinstance(advanced, dict): raise ConnectionError("Расширенные настройки должны быть JSON-объектом.")
    encoded = json.dumps(advanced, ensure_ascii=False)
    if len(encoded) > 16384: raise ConnectionError("Расширенные настройки слишком большие.")
    result["advanced"] = advanced
    return result


def _signature(user):
    protocol = str(user.get("protocol", ""))
    cdn = user.get("cdn") if isinstance(user.get("cdn"), dict) else {}
    if protocol == "vless" and cdn.get("enabled"):
        return (protocol, "cdn", cdn.get("provider", "custom"), cdn.get("public_host", ""), cdn.get("sni", ""), cdn.get("host_header", ""))
    return (protocol, "direct")


def _from_user(user):
    sig = _signature(user)
    protocol = sig[0]
    if protocol == "vless" and sig[1] == "cdn":
        cdn = user.get("cdn", {})
        seed = "|".join(str(x) for x in sig)
        cid = "vless-" + hashlib.sha256(seed.encode()).hexdigest()[:12]
        provider = str(cdn.get("provider", "custom"))
        title = provider.title() if provider != "custom" else "CDN"
        return _base(cid, f"VLESS XHTTP · {title} · {cdn.get('public_host','')}", "vless",
            route="cdn", provider=provider, public_host=cdn.get("public_host", ""), sni=cdn.get("sni", ""),
            host_header=cdn.get("host_header", ""), fingerprint=cdn.get("fingerprint", "chrome"),
            mode=cdn.get("mode", "packet-up"), uplink_method=cdn.get("uplink_method", "GET"),
            padding=cdn.get("padding", "100-1000"), xmux=cdn.get("xmux", provider in ("beget", "selectel")),
            no_sse_header=cdn.get("no_sse_header", False))
    mapping = {"vless": "vless-direct", "hysteria": "hysteria-primary", "mtproto": "mtproto-primary", "web": "web-primary"}
    return next((item for item in defaults() if item["id"] == mapping.get(protocol)), None)


def ensure(path, users=None, persist=True):
    data = load(path)
    current = {item.get("id"): item for item in data.get("connections", []) if isinstance(item, dict)}
    changed = not os.path.exists(path)
    for base in defaults():
        if base["id"] not in current: current[base["id"]] = base; changed = True
    for user in users or []:
        if user.get("subscription_id"): continue
        connection = _from_user(user)
        if not connection: continue
        if connection["id"] not in current: current[connection["id"]] = connection; changed = True
        if user.get("connection_id") != connection["id"]: user["connection_id"] = connection["id"]
    data = {"schema": SCHEMA, "connections": list(current.values())}
    if persist and changed: data = save(path, data)
    return data


def get(data, cid):
    return next((item for item in data.get("connections", []) if item.get("id") == cid), None)


def request_for_connection(connection):
    connection = validate(connection, existing=connection)
    if not connection.get("enabled"): raise ConnectionError("Это подключение отключено.")
    cfg = connection["config"]
    request = {"protocol": connection["protocol"], "connection_id": connection["id"]}
    if connection["protocol"] == "vless":
        request.update({"vless_route": cfg["route"], "vless_cdn_provider": cfg.get("provider", "custom"),
            "vless_cdn_public_host": cfg.get("public_host", ""), "vless_cdn_sni": cfg.get("sni", ""),
            "vless_cdn_host_header": cfg.get("host_header", ""), "vless_cdn_fingerprint": cfg.get("fingerprint", "chrome"),
            "vless_cdn_mode": cfg.get("mode", "packet-up"), "vless_cdn_uplink_method": cfg.get("uplink_method", "GET")})
    elif connection["protocol"] == "mtproto" and cfg.get("port"):
        request["port"] = cfg["port"]
    return request


def apply_to_user(user, connection):
    """Apply mutable transport settings without touching client credentials."""
    connection = validate(connection, existing=connection)
    if user.get("protocol") != connection.get("protocol"):
        raise ConnectionError("Протокол подключения не совпадает с протоколом пользователя.")
    user["connection_id"] = connection["id"]
    if user.get("protocol") == "vless":
        cfg = connection["config"]
        if cfg.get("route") == "direct":
            user.pop("cdn", None)
        else:
            method = str(cfg.get("uplink_method", "GET")).lower()
            if cfg.get("provider") == "selectel": method = "delete"
            user["cdn"] = {"enabled": True, "provider": cfg.get("provider", "custom"),
                "public_host": cfg.get("public_host", ""), "sni": cfg.get("sni") or cfg.get("public_host", ""),
                "host_header": cfg.get("host_header") or cfg.get("public_host", ""),
                "fingerprint": cfg.get("fingerprint", "chrome"), "mode": cfg.get("mode", "packet-up"),
                "uplink_method": method}
    return user


def sync_users(data, users):
    by_id = {item.get("id"): item for item in data.get("connections", [])}
    changed = False
    for user in users:
        cid = user.get("connection_id")
        if not cid or cid not in by_id: continue
        before = json.dumps(user, sort_keys=True, ensure_ascii=True)
        apply_to_user(user, by_id[cid])
        changed = changed or before != json.dumps(user, sort_keys=True, ensure_ascii=True)
    return changed


def subscription_spec(connection):
    connection=validate(connection,existing=connection)
    spec={"id":connection["id"],"name":connection["name"],"protocol":connection["protocol"]}
    if connection["protocol"]=="vless" and connection["config"].get("route")=="cdn":
        cfg=connection["config"]; method=str(cfg.get("uplink_method","GET")).lower()
        if cfg.get("provider")=="selectel": method="delete"
        spec["cdn"]={"enabled":True,"provider":cfg.get("provider","custom"),
            "public_host":cfg.get("public_host",""),"sni":cfg.get("sni") or cfg.get("public_host",""),
            "host_header":cfg.get("host_header") or cfg.get("public_host",""),
            "fingerprint":cfg.get("fingerprint","chrome"),"mode":cfg.get("mode","packet-up"),
            "uplink_method":method}
    return spec


def sync_subscriptions(data, subscriptions):
    by_id={item.get("id"):item for item in data.get("connections",[])}; changed=False
    for sub in subscriptions:
        specs=sub.get("connections")
        if not isinstance(specs,list): continue
        refreshed=[]
        for spec in specs:
            connection=by_id.get(spec.get("id")) if isinstance(spec,dict) else None
            refreshed.append(subscription_spec(connection) if connection else spec)
        if refreshed!=specs:
            sub["connections"]=refreshed
            sub["protocols"]=list(dict.fromkeys(item.get("protocol") for item in refreshed if isinstance(item,dict)))
            changed=True
    return changed


def upsert(path, raw):
    data = load(path); cid = str(raw.get("id", "")); old = get(data, cid) if cid else None
    item = validate(raw, existing=old)
    if any(row.get("id") == item["id"] and row is not old for row in data["connections"]):
        raise ConnectionError("Подключение с таким идентификатором уже существует.")
    if old: data["connections"] = [item if row.get("id") == item["id"] else row for row in data["connections"]]
    else: data["connections"].append(item)
    save(path, data); return item


def remove(path, cid, users, subscriptions=None):
    if cid in {item["id"] for item in defaults()}:
        raise ConnectionError("Базовое подключение можно отключить, но нельзя удалить.")
    if any(user.get("connection_id") == cid for user in users):
        raise ConnectionError("Сначала перенесите привязанных пользователей на другое подключение.")
    if any(any(isinstance(item,dict) and item.get("id")==cid for item in sub.get("connections",[])) for sub in (subscriptions or [])):
        raise ConnectionError("Подключение используется клиентской подпиской. Сначала измените клиента.")
    data = load(path)
    if not get(data, cid): raise ConnectionError("Подключение не найдено.")
    data["connections"] = [item for item in data["connections"] if item.get("id") != cid]
    save(path, data)


def clone(path, cid):
    data = load(path); source = get(data, cid)
    if not source: raise ConnectionError("Подключение не найдено.")
    item = copy.deepcopy(source); item["id"] = secrets.token_hex(8); item["name"] += " · копия"; item["created_at"] = int(time.time())
    data["connections"].append(item); save(path, data); return item
