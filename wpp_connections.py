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
import wpp_cdn

SCHEMA = 1
PROTOCOLS = {"vless", "hysteria", "mtproto", "web", "awg20", "awg31"}
PROVIDERS = {"yandex", "vk", "beget", "selectel", "timeweb", "custom"}
FINGERPRINTS = {"chrome", "firefox", "safari", "edge", "random", "randomized"}
MODES = {"packet-up", "stream-up", "auto"}
METHODS = {"GET", "POST", "DELETE"}
ADVANCED_TOP = {"server", "client"}
SERVER_SECTIONS = {"settings", "streamSettings", "sniffing", "allocate"}
CLIENT_SECTIONS = {"mode", "extra", "alpn", "fingerprint"}
HOST_RE = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.I)


class ConnectionError(ValueError):
    pass


def _base(cid, name, protocol, **config):
    return {"id": cid, "name": name, "protocol": protocol, "enabled": True,
            "created_at": int(time.time()), "config": config, "advanced": {}}


def defaults():
    return [
        _base("vless-direct", "VLESS XHTTP · напрямую", "vless", route="direct", fingerprint="chrome", mode="auto", uplink_method="POST"),
        _base("hysteria-primary", "Hysteria2 · основной", "hysteria", port=8443),
        _base("mtproto-primary", "MTProto · отдельный порт", "mtproto", port=0),
        _base("web-primary", "Web Proxy · HTTPS", "web", port=0),
        _base("awg20-primary", "AmneziaWG 2.0 · отдельный профиль", "awg20", port=0),
        _base("awg31-primary", "AmneziaWG 3.1 · отдельный профиль", "awg31", port=0),
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
    deleted=[]
    for value in data.get("deleted", []):
        value=str(value)
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,63}",value) and value not in deleted:
            deleted.append(value)
    clean = {"schema": SCHEMA, "connections": [validate(item, existing=item) for item in data.get("connections", [])],
             "deleted": deleted}
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


def _json_tree(value, label, depth=0):
    """Reject oversized/deep values before they reach Xray's JSON config."""
    if depth > 10: raise ConnectionError(label+": слишком большая глубина JSON.")
    if value is None or isinstance(value, (bool, int, float, str)): return
    if isinstance(value, list):
        if len(value) > 256: raise ConnectionError(label+": слишком много элементов.")
        for item in value: _json_tree(item, label, depth+1)
        return
    if isinstance(value, dict):
        if len(value) > 256: raise ConnectionError(label+": слишком много параметров.")
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 128 or any(ord(ch) < 32 for ch in key):
                raise ConnectionError(label+": некорректное имя параметра.")
            _json_tree(item, label, depth+1)
        return
    raise ConnectionError(label+": поддерживаются только значения JSON.")


def normalize_advanced(value, protocol="vless"):
    """Validate expert Xray overrides while protecting WPP-owned routing."""
    if isinstance(value, str):
        try: value = json.loads(value or "{}")
        except json.JSONDecodeError as exc:
            raise ConnectionError("Расширенные настройки: ошибка JSON в строке "+str(exc.lineno)+".")
    if value in (None, ""): value = {}
    if not isinstance(value, dict): raise ConnectionError("Расширенные настройки должны быть JSON-объектом.")
    unknown=set(value)-ADVANCED_TOP
    if unknown: raise ConnectionError("Неизвестный раздел Xray: "+", ".join(sorted(unknown))+".")
    server=value.get("server",{})
    client=value.get("client",{})
    if not isinstance(server,dict) or not isinstance(client,dict):
        raise ConnectionError("Разделы server и client должны быть JSON-объектами.")
    unknown=set(server)-SERVER_SECTIONS
    if unknown: raise ConnectionError("Недоступный раздел inbound: "+", ".join(sorted(unknown))+".")
    unknown=set(client)-CLIENT_SECTIONS
    if unknown: raise ConnectionError("Недоступный параметр клиента: "+", ".join(sorted(unknown))+".")
    for section in SERVER_SECTIONS:
        if section in server and not isinstance(server[section],dict):
            raise ConnectionError("server."+section+" должен быть JSON-объектом.")
    settings=server.get("settings",{})
    if "clients" in settings:
        raise ConnectionError("server.settings.clients управляется панелью и не может быть переопределён.")
    stream=server.get("streamSettings",{})
    protected=set(stream)&{"network","security"}
    if protected:
        raise ConnectionError("network и security управляются панелью.")
    xhttp=stream.get("xhttpSettings")
    if xhttp is not None:
        if protocol!="vless" or not isinstance(xhttp,dict):
            raise ConnectionError("server.streamSettings.xhttpSettings доступен только для VLESS.")
        if "path" in xhttp:
            raise ConnectionError("Путь XHTTP управляется панелью и не может быть переопределён.")
    if client and protocol!="vless":
        raise ConnectionError("Раздел client пока доступен только для VLESS XHTTP.")
    if "mode" in client and str(client["mode"]) not in MODES:
        raise ConnectionError("client.mode: допустимы auto, packet-up или stream-up.")
    if "extra" in client and not isinstance(client["extra"],dict):
        raise ConnectionError("client.extra должен быть JSON-объектом.")
    if "fingerprint" in client and str(client["fingerprint"]) not in FINGERPRINTS:
        raise ConnectionError("client.fingerprint: неизвестный TLS fingerprint.")
    if "alpn" in client:
        alpn=client["alpn"]
        if not isinstance(alpn,list) or not alpn or len(alpn)>3 or any(item not in ("h2","h3","http/1.1") for item in alpn):
            raise ConnectionError("client.alpn: используйте список из h2, h3 или http/1.1.")
    _json_tree(value,"Расширенные настройки")
    encoded=json.dumps(value,ensure_ascii=False,separators=(",",":"))
    if len(encoded)>32768: raise ConnectionError("Расширенные настройки слишком большие.")
    return copy.deepcopy(value)


def _deep_merge(base, patch):
    result=copy.deepcopy(base) if isinstance(base,dict) else {}
    for key,value in (patch.items() if isinstance(patch,dict) else []):
        result[key]=_deep_merge(result.get(key),value) if isinstance(value,dict) else copy.deepcopy(value)
    return result


def apply_server_advanced(inbound, advanced, protocol="vless"):
    """Merge validated overrides into an inbound without replacing WPP keys."""
    result=copy.deepcopy(inbound)
    server=normalize_advanced(advanced,protocol).get("server",{})
    for section in SERVER_SECTIONS:
        if section in server: result[section]=_deep_merge(result.get(section),server[section])
    return result


def client_advanced(advanced, protocol="vless"):
    return normalize_advanced(advanced,protocol).get("client",{})


def apply_client_advanced(route, advanced):
    """Apply the client half of an expert VLESS profile to a generated link."""
    result=copy.deepcopy(route)
    client=client_advanced(advanced,"vless")
    if "mode" in client: result["mode"]=str(client["mode"])
    if "fingerprint" in client: result["fingerprint"]=str(client["fingerprint"])
    if "extra" in client: result["extra"]=_deep_merge(result.get("extra") or {},client["extra"])
    if "alpn" in client: result["alpn"]=list(client["alpn"])
    return result


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
        if provider == "custom" and public_host.endswith(".cdn.twcstorage.ru"):
            provider = "timeweb"
        fingerprint = str(config.get("fingerprint", "chrome"))
        if fingerprint not in FINGERPRINTS: raise ConnectionError("Неизвестный TLS fingerprint.")
        transport = wpp_cdn.transport_defaults(provider) if route == "cdn" else {"mode": "auto", "uplink_method": "post"}
        mode = str(config.get("mode", transport["mode"]))
        if mode not in MODES: raise ConnectionError("Неизвестный режим XHTTP.")
        method = str(config.get("uplink_method", transport["uplink_method"])).upper()
        if method not in METHODS: raise ConnectionError("Метод XHTTP должен быть GET, POST или DELETE.")
        if route == "cdn":
            # Provider presets are applied as one atomic pair.  Mixing an old
            # method with a new server mode produces links that download but
            # cannot upload.
            mode = transport["mode"]
            method = transport["uplink_method"].upper()
        else:
            mode = "auto"
            method = "POST"
        result["config"] = {"route": route, "provider": provider, "public_host": public_host,
            "sni": _host(config.get("sni"), "TLS SNI"), "host_header": _host(config.get("host_header"), "HTTP Host"),
            "fingerprint": fingerprint, "mode": mode, "uplink_method": method,
            "padding": str(config.get("padding", "100-1000"))[:40],
            "xmux": bool(config.get("xmux", provider in ("beget", "selectel", "timeweb"))),
            "no_sse_header": bool(config.get("no_sse_header", False))}
    else:
        result["config"] = {"port": _integer(config.get("port"), "Порт", 0, 65535)}
    result["advanced"] = normalize_advanced(raw.get("advanced", old.get("advanced", {})), protocol)
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
            padding=cdn.get("padding", "100-1000"), xmux=cdn.get("xmux", provider in ("beget", "selectel", "timeweb")),
            no_sse_header=cdn.get("no_sse_header", False))
    mapping = {"vless": "vless-direct", "hysteria": "hysteria-primary", "mtproto": "mtproto-primary", "web": "web-primary",
               "awg20": "awg20-primary", "awg31": "awg31-primary"}
    return next((item for item in defaults() if item["id"] == mapping.get(protocol)), None)


def ensure(path, users=None, persist=True):
    data = load(path)
    current = {}
    changed = not os.path.exists(path)
    for item in data.get("connections", []):
        if not isinstance(item, dict):
            changed = True
            continue
        normalized = validate(item, existing=item)
        current[normalized.get("id")] = normalized
        changed = changed or normalized != item
    deleted = {str(item) for item in data.get("deleted", [])}
    for base in defaults():
        if base["id"] not in current and base["id"] not in deleted: current[base["id"]] = base; changed = True
    for user in users or []:
        if user.get("subscription_id"): continue
        connection = _from_user(user)
        if not connection: continue
        if connection["id"] in deleted: continue
        if connection["id"] not in current: current[connection["id"]] = connection; changed = True
        if user.get("connection_id") != connection["id"]: user["connection_id"] = connection["id"]
    data = {"schema": SCHEMA, "connections": list(current.values()), "deleted": sorted(deleted)}
    if persist and changed: data = save(path, data)
    return data


def get(data, cid):
    return next((item for item in data.get("connections", []) if item.get("id") == cid), None)


def selectable(item):
    """Return only real connection templates, never subscription runtime keys."""
    if not isinstance(item,dict) or item.get("subscription_id"):
        return False
    if str(item.get("origin","")).lower() in ("subscription","runtime"):
        return False
    name=str(item.get("name","")).casefold()
    return "/ общий профиль" not in name and not re.search(r"/\s*устройство\s+[0-9a-f]{4,}",name)


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
    data["deleted"]=[value for value in data.get("deleted",[]) if value!=item["id"]]
    save(path, data); return item


def remove(path, cid, users, subscriptions=None):
    data = load(path)
    if not get(data, cid): raise ConnectionError("Подключение не найдено.")
    subscriptions = subscriptions if isinstance(subscriptions,list) else []
    removed_users={str(user.get("id","")) for user in users if user.get("connection_id")==cid}
    users[:] = [user for user in users if user.get("connection_id") != cid]
    removed_subscriptions=[]
    for sub in list(subscriptions):
        specs=sub.get("connections") if isinstance(sub.get("connections"),list) else []
        retained=[item for item in specs if not (isinstance(item,dict) and item.get("id")==cid)]
        if len(retained)==len(specs): continue
        if not retained:
            sid=str(sub.get("id","")); removed_subscriptions.append(sid)
            subscriptions.remove(sub)
            users[:] = [user for user in users if user.get("subscription_id")!=sid]
            continue
        sub["connections"]=retained
        sub["protocols"]=list(dict.fromkeys(item.get("protocol") for item in retained if isinstance(item,dict)))
        live_ids={str(user.get("id","")) for user in users if user.get("subscription_id")==sub.get("id")}
        sub["profile_ids"]=[uid for uid in sub.get("profile_ids",[]) if str(uid) in live_ids]
    data["connections"] = [item for item in data["connections"] if item.get("id") != cid]
    deleted=[str(value) for value in data.get("deleted",[])]
    if cid not in deleted: deleted.append(cid)
    data["deleted"]=deleted
    save(path, data)
    return {"ok":True,"removed_users":len(removed_users),"removed_subscriptions":len(removed_subscriptions)}


def clone(path, cid):
    data = load(path); source = get(data, cid)
    if not source: raise ConnectionError("Подключение не найдено.")
    item = copy.deepcopy(source); item["id"] = secrets.token_hex(8); item["name"] += " · копия"; item["created_at"] = int(time.time())
    data["connections"].append(item); save(path, data); return item
