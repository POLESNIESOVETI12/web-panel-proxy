"""Client-side split routing profiles attached to reusable VLESS connections."""
import json
import os
import re
import tempfile
import time
import urllib.request
from urllib.parse import parse_qs, unquote, urlparse

SCHEMA = 1
MAX_CUSTOM_DOMAINS = 300
MAX_AWG_NETWORKS = 400
OPENCCK_AMNEZIA_URL = "https://russia.iplist.opencck.org/?format=amnezia&data=cidr4"
OPENCCK_MAX_BYTES = 2 * 1024 * 1024
OPENCCK_CACHE_SECONDS = 12 * 60 * 60

# Kept deliberately explicit.  The geosite rule covers the broad RU set while
# these entries keep popular services direct even on clients with older geo data.
RU_DOMAINS = (
    "yandex.ru", "ya.ru", "yandex.net", "yastatic.net", "yastat.net", "yndx.net",
    "mail.ru", "vk.com", "vk.ru", "vk.me", "vk.cc", "vk.link", "vkvideo.ru",
    "vkuser.net", "vkuservideo.net", "vkuservideo.com", "vkuseraudio.net",
    "vkuseraudio.com", "vkuserlive.net", "vkuserlive.com", "vk-cdn.net",
    "vk-cdn.me", "cdn-vk.net", "vkcache.com", "vkcdnservice.com", "ok.ru",
    "dzen.ru", "rutube.ru", "rambler.ru", "sberbank.ru", "sber.ru", "tbank.ru",
    "alfabank.ru", "vtb.ru", "gazprombank.ru", "gosuslugi.ru", "nalog.gov.ru",
    "mos.ru", "government.ru", "kremlin.ru", "ozon.ru", "wildberries.ru",
    "market.yandex.ru", "avito.ru", "2gis.ru", "hh.ru", "kinopoisk.ru",
    "ivi.ru", "okko.tv", "kion.ru", "mts.ru", "megafon.ru", "beeline.ru",
    "tele2.ru", "t2.ru", "rt.ru", "rostelecom.ru", "domclick.ru", "cian.ru",
    "utair.ru", "aeroflot.ru", "rzd.ru", "invitro.ru", "gemotest.ru",
    "prodoctorov.ru", "kaspersky.ru", "xn--d1aqf.xn--p1ai", "vkplay.live",
    "mradx.net", "mvk.com", "vkontakte.com", "vkpay.com", "vkmessenger.com",
    "naydex.net", "turbopages.org", "yadi.sk", "ya.cc", "webvisor.com",
    "webvisor.org", "static-storage.net", "yads.tech", "ydb.tech",
    "shedevrum.com", "sourcecraft.dev", "meteum.ai", "yango.com",
)
CAPTIVE_DOMAINS = (
    "connectivitycheck.gstatic.com", "connectivitycheck.android.com",
    "captive.apple.com", "www.msftconnecttest.com", "detectportal.firefox.com",
)
PUSH_DOMAINS = (
    "push.apple.com", "courier.push.apple.com", "gateway.push.apple.com",
    "mtalk.google.com", "fcm.googleapis.com",
)
YOUTUBE_DOMAINS = (
    "geosite:youtube", "domain:youtube.com", "domain:youtu.be",
    "domain:googlevideo.com", "domain:ytimg.com",
)


class RoutingError(ValueError):
    pass


def _atomic(path, data):
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o750, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".routing-", dir=folder, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, 0o640)
        os.replace(temp, path)
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass


def load(path):
    try:
        with open(path, encoding="utf-8") as stream:
            raw = json.load(stream)
    except FileNotFoundError:
        return {"schema": SCHEMA, "profiles": {}, "awg_profiles": {}}
    except (OSError, json.JSONDecodeError) as exc:
        raise RoutingError("Не удалось прочитать настройки маршрутизации.") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("profiles"), dict):
        raise RoutingError("Файл маршрутизации повреждён.")
    clean = {"schema": SCHEMA, "profiles": {}, "awg_profiles": {}}
    for cid, value in raw["profiles"].items():
        if isinstance(cid, str) and isinstance(value, dict):
            try:
                clean["profiles"][cid] = validate(value)
            except RoutingError:
                continue
    raw_awg = raw.get("awg_profiles", {})
    if not isinstance(raw_awg, dict):
        raw_awg = {}
    for cid, value in raw_awg.items():
        if isinstance(cid, str) and isinstance(value, dict):
            try:
                clean["awg_profiles"][cid] = validate_awg(value)
            except RoutingError:
                continue
    return clean


def _domains(value):
    if isinstance(value, str):
        values = value.replace(",", "\n").splitlines()
    elif isinstance(value, list):
        values = value
    else:
        values = []
    result = []
    for raw in values:
        item = str(raw).strip().lower().rstrip(".")
        if not item:
            continue
        if item.startswith(("domain:", "full:", "regexp:", "geosite:")):
            candidate = item
        else:
            if len(item) > 253 or not re.fullmatch(r"(?:\*\.)?(?:xn--)?[a-z0-9][a-z0-9.-]*[a-z0-9]", item):
                raise RoutingError("Некорректный домен: " + item[:80])
            candidate = "domain:" + item.removeprefix("*.")
        if candidate not in result:
            result.append(candidate)
    if len(result) > MAX_CUSTOM_DOMAINS:
        raise RoutingError("Можно добавить не более 300 своих доменов.")
    return result


def _networks(value):
    import ipaddress
    values = value.replace(",", "\n").splitlines() if isinstance(value, str) else value if isinstance(value, list) else []
    result = []
    for raw in values:
        item = str(raw).strip()
        if not item:
            continue
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError as exc:
            raise RoutingError("Некорректный IP или CIDR: " + item[:80]) from exc
        candidate = str(network)
        if candidate not in result:
            result.append(candidate)
    if len(result) > MAX_AWG_NETWORKS:
        raise RoutingError("Можно добавить не более 400 IP-адресов и подсетей AWG.")
    return result


def _validate_opencck_amnezia(value):
    """Validate the public list before serving it to an administrator."""
    if not isinstance(value, list) or not 100 <= len(value) <= 10000:
        raise RoutingError("OpenCCK вернул некорректный список маршрутизации.")
    clean = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise RoutingError("OpenCCK вернул некорректный список маршрутизации.")
        hostname = str(item.get("hostname", "")).strip()
        ip_value = item.get("ip", "")
        if len(hostname) > 300 or not hostname:
            raise RoutingError("OpenCCK вернул некорректный элемент маршрутизации.")
        # The Amnezia export currently uses CIDR in hostname and an empty ip.
        # Keep the upstream representation intact, but reject arbitrary data.
        try:
            import ipaddress
            hostname = str(ipaddress.ip_network(hostname, strict=False))
        except ValueError as exc:
            raise RoutingError("OpenCCK вернул некорректную CIDR-подсеть.") from exc
        if ip_value not in ("", None):
            raise RoutingError("OpenCCK вернул неподдерживаемый формат списка.")
        clean.append({"hostname": hostname, "ip": ""})
    return clean


def opencck_amnezia_list(cache_path):
    """Return a validated Amnezia split-tunnel list with a 12-hour cache."""
    cached = None
    try:
        with open(cache_path, encoding="utf-8") as stream:
            cached = _validate_opencck_amnezia(json.load(stream))
        if time.time() - os.path.getmtime(cache_path) < OPENCCK_CACHE_SECONDS:
            return json.dumps(cached, ensure_ascii=False, indent=2) + "\n"
    except (OSError, json.JSONDecodeError, RoutingError):
        cached = None
    try:
        request = urllib.request.Request(OPENCCK_AMNEZIA_URL, headers={
            "Accept": "application/json", "User-Agent": "WebPanelProxy/2.6",
        })
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read(OPENCCK_MAX_BYTES + 1)
        if len(raw) > OPENCCK_MAX_BYTES:
            raise RoutingError("Список OpenCCK превышает допустимый размер.")
        fresh = _validate_opencck_amnezia(json.loads(raw.decode("utf-8")))
        _atomic(cache_path, fresh)
        return json.dumps(fresh, ensure_ascii=False, indent=2) + "\n"
    except (OSError, UnicodeError, json.JSONDecodeError, RoutingError) as exc:
        if cached is not None:
            return json.dumps(cached, ensure_ascii=False, indent=2) + "\n"
        if isinstance(exc, RoutingError):
            raise
        raise RoutingError("Не удалось загрузить список российских сайтов OpenCCK.") from exc


def validate_awg(value):
    if not isinstance(value, dict):
        raise RoutingError("Некорректный профиль маршрутизации AWG.")
    mode = str(value.get("mode", "custom"))
    if mode == "local_direct":
        return {"mode": "custom", "allowed_ips": [],
                "updated_at": int(value.get("updated_at", time.time()))}
    if mode != "custom":
        mode = "custom"
    return {"mode": mode, "allowed_ips": _networks(value.get("allowed_ips", [])),
            "updated_at": int(value.get("updated_at", time.time()))}


def awg_public_routes():
    """IPv4 internet routes with local, carrier, multicast and reserved nets outside AWG."""
    import ipaddress
    excluded = [ipaddress.ip_network(value) for value in (
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
        "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16",
        "224.0.0.0/4", "240.0.0.0/4",
    )]
    routes = [ipaddress.ip_network("0.0.0.0/0")]
    for blocked in excluded:
        next_routes = []
        for route in routes:
            if blocked.subnet_of(route):
                next_routes.extend(route.address_exclude(blocked))
            else:
                next_routes.append(route)
        routes = next_routes
    return [str(route) for route in sorted(routes, key=lambda item: (int(item.network_address), item.prefixlen))] + ["::/0"]


def awg_allowed_ips(state, connection_id):
    profile = state.get("awg_profiles", {}).get(str(connection_id)) if isinstance(state, dict) else None
    return validate_awg(profile)["allowed_ips"] if isinstance(profile, dict) else ["0.0.0.0/0", "::/0"]


def update_awg_selected(path, connection_id, form, remove=False):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,63}", str(connection_id)):
        raise RoutingError("Подключение AWG не найдено.")
    changes = _networks(form.get("allowed_ips_delta", ""))
    if not changes:
        raise RoutingError("Укажите хотя бы один IP-адрес или CIDR.")
    state = load(path)
    stored = validate_awg(state["awg_profiles"].get(str(connection_id), {}))
    current = stored["allowed_ips"]
    mode = stored["mode"]
    if remove:
        current = [item for item in current if item not in changes]
    else:
        mode = "custom"
        for item in changes:
            if item not in current:
                current.append(item)
    if current:
        state["awg_profiles"][str(connection_id)] = validate_awg({"mode": mode, "allowed_ips": current, "updated_at": int(time.time())})
    else:
        state["awg_profiles"].pop(str(connection_id), None)
    _atomic(path, state)
    return state["awg_profiles"].get(str(connection_id))


def validate(value):
    if not isinstance(value, dict):
        raise RoutingError("Некорректный профиль маршрутизации.")
    return {
        "enabled": bool(value.get("enabled", True)),
        "ru_direct": bool(value.get("ru_direct", True)),
        "private_direct": bool(value.get("private_direct", True)),
        "captive_direct": bool(value.get("captive_direct", True)),
        "push_proxy": bool(value.get("push_proxy", True)),
        "youtube_proxy": bool(value.get("youtube_proxy", True)),
        "block_bittorrent": bool(value.get("block_bittorrent", True)),
        "split_dns": bool(value.get("split_dns", True)),
        "direct_domains": _domains(value.get("direct_domains", [])),
        "proxy_domains": _domains(value.get("proxy_domains", [])),
        "updated_at": int(value.get("updated_at", time.time())),
    }


def save_profile(path, connection_id, form):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,63}", str(connection_id)):
        raise RoutingError("Подключение не найдено.")
    state = load(path)
    state["profiles"][connection_id] = validate({
        "enabled": form.get("enabled") == "1",
        "ru_direct": form.get("ru_direct") == "1",
        "private_direct": form.get("private_direct") == "1",
        "captive_direct": form.get("captive_direct") == "1",
        "push_proxy": form.get("push_proxy") == "1",
        "youtube_proxy": form.get("youtube_proxy") == "1",
        "block_bittorrent": form.get("block_bittorrent") == "1",
        "split_dns": form.get("split_dns") == "1",
        "direct_domains": form.get("direct_domains", ""),
        "proxy_domains": form.get("proxy_domains", ""),
        "updated_at": int(time.time()),
    })
    _atomic(path, state)
    return state["profiles"][connection_id]


ROUTING_FLAGS = (
    "ru_direct", "private_direct", "captive_direct", "push_proxy",
    "youtube_proxy", "block_bittorrent", "split_dns",
)


def update_selected(path, connection_id, form, remove=False):
    """Add or remove only the routing items explicitly selected in the UI."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,63}", str(connection_id)):
        raise RoutingError("Подключение не найдено.")
    state = load(path)
    stored = state["profiles"].get(str(connection_id))
    if isinstance(stored, dict) and stored.get("enabled", True):
        profile = validate(stored)
    else:
        profile = validate({
            "enabled": False,
            **{key: False for key in ROUTING_FLAGS},
            "direct_domains": [], "proxy_domains": [],
        })

    selected = [key for key in ROUTING_FLAGS if form.get("select_" + key) == "1"]
    domain_changes = {
        "direct_domains": _domains(form.get("direct_domains_delta", "")),
        "proxy_domains": _domains(form.get("proxy_domains_delta", "")),
    }
    if not selected and not any(domain_changes.values()):
        raise RoutingError("Сначала выберите правила или укажите домены.")

    for key in selected:
        profile[key] = not remove
    for key, values in domain_changes.items():
        if remove:
            profile[key] = [value for value in profile[key] if value not in values]
        else:
            for value in values:
                if value not in profile[key]:
                    profile[key].append(value)

    active = any(profile[key] for key in ROUTING_FLAGS) or any(
        profile[key] for key in ("direct_domains", "proxy_domains")
    )
    if not active:
        state["profiles"].pop(str(connection_id), None)
        _atomic(path, state)
        return None
    profile["enabled"] = True
    profile["updated_at"] = int(time.time())
    state["profiles"][str(connection_id)] = validate(profile)
    _atomic(path, state)
    return state["profiles"][str(connection_id)]


def delete_profile(path, connection_id):
    state = load(path)
    state["profiles"].pop(str(connection_id), None)
    state["awg_profiles"].pop(str(connection_id), None)
    _atomic(path, state)


def _domain_rule(values, outbound):
    return {"type": "field", "domain": list(values), "outboundTag": outbound}


def routing_config(profile):
    profile = validate(profile)
    rules = []
    if profile["block_bittorrent"]:
        rules.append({"type": "field", "protocol": ["bittorrent"], "outboundTag": "block"})
    if profile["captive_direct"]:
        rules.append(_domain_rule(["domain:" + x for x in CAPTIVE_DOMAINS], "direct"))
    if profile["push_proxy"]:
        rules.append(_domain_rule(["domain:" + x for x in PUSH_DOMAINS], "proxy"))
    if profile["youtube_proxy"]:
        rules.append(_domain_rule(YOUTUBE_DOMAINS, "proxy"))
    if profile["proxy_domains"]:
        rules.append(_domain_rule(profile["proxy_domains"], "proxy"))
    if profile["split_dns"]:
        rules.append(_domain_rule(["domain:dns.google", "domain:cloudflare-dns.com", "domain:one.one.one.one"], "proxy"))
        rules.append(_domain_rule(["domain:common.dot.dns.yandex.net"], "direct"))
        rules.append({"type": "field", "ip": ["77.88.8.8", "77.88.8.1"], "outboundTag": "direct"})
    ip = []
    if profile["private_direct"]:
        ip.append("geoip:private")
    if profile["ru_direct"]:
        ip.append("geoip:ru")
    if ip:
        rules.append({"type": "field", "ip": ip, "outboundTag": "direct"})
    direct = list(profile["direct_domains"])
    if profile["ru_direct"]:
        direct = ["geosite:category-ru", "regexp:.*\\.(ru|su|xn--p1ai)$"] + ["domain:" + x for x in RU_DOMAINS] + direct
    if direct:
        rules.append(_domain_rule(direct, "direct"))
    rules.append({"type": "field", "network": "tcp,udp", "outboundTag": "proxy"})
    return {"domainStrategy": "IPIfNonMatch", "rules": rules}


def dns_config(profile):
    profile = validate(profile)
    if not profile["split_dns"]:
        return {"queryStrategy": "UseIP", "servers": ["https://1.1.1.1/dns-query", "https://8.8.8.8/dns-query"]}
    ru = ["geosite:category-ru", "regexp:.*\\.(ru|su|xn--p1ai)$"] + ["domain:" + x for x in RU_DOMAINS]
    return {"queryStrategy": "UseIP", "hosts": {
        "common.dot.dns.yandex.net": ["77.88.8.8", "77.88.8.1"],
    }, "servers": [
        {"address": "https://common.dot.dns.yandex.net/dns-query", "domains": ru, "skipFallback": True},
        "https://1.1.1.1/dns-query", "https://8.8.8.8/dns-query",
    ]}


def _vless_outbound(link):
    parsed = urlparse(link)
    if parsed.scheme != "vless" or not parsed.hostname or not parsed.username:
        raise RoutingError("Маршрутизация поддерживается только для VLESS.")
    query = parse_qs(parsed.query)
    one = lambda key, default="": query.get(key, [default])[0]
    extra = {}
    if one("extra"):
        try:
            candidate = json.loads(one("extra"))
            if isinstance(candidate, dict):
                extra = candidate
        except json.JSONDecodeError:
            pass
    stream = {
        "network": "xhttp", "security": one("security", "tls"),
        "tlsSettings": {"serverName": one("sni", parsed.hostname), "fingerprint": one("fp", "chrome"),
                        "alpn": [x for x in one("alpn", "h2").split(",") if x]},
        "xhttpSettings": {"host": one("host", parsed.hostname), "path": one("path", "/"),
                          "mode": one("mode", "auto")},
    }
    if extra:
        stream["xhttpSettings"]["extra"] = extra
    return {"tag": "proxy", "protocol": "vless", "settings": {"vnext": [{
        "address": parsed.hostname, "port": parsed.port or 443,
        "users": [{"id": unquote(parsed.username), "encryption": one("encryption", "none")}],
    }]}, "streamSettings": stream}


def _hysteria_outbound(link):
    parsed = urlparse(link)
    if parsed.scheme not in ("hysteria2", "hy2") or not parsed.hostname or not parsed.username:
        raise RoutingError("Некорректное подключение Hysteria2.")
    query = parse_qs(parsed.query)
    one = lambda key, default="": query.get(key, [default])[0]
    return {
        "tag": "proxy", "protocol": "hysteria",
        "settings": {"version": 2, "address": parsed.hostname, "port": parsed.port or 443},
        "streamSettings": {
            "network": "hysteria", "security": "tls",
            "tlsSettings": {"serverName": one("sni", parsed.hostname),
                            "alpn": [x for x in one("alpn", "h3").split(",") if x]},
            "hysteriaSettings": {"version": 2, "auth": unquote(parsed.username), "udpIdleTimeout": 60},
        },
    }


def _proxy_outbound(link):
    scheme = urlparse(link).scheme.lower()
    if scheme == "vless":
        return _vless_outbound(link)
    if scheme in ("hysteria2", "hy2"):
        return _hysteria_outbound(link)
    raise RoutingError("Xray-маршрутизация поддерживает VLESS и Hysteria2.")


def hiddify_compatible_links(links):
    """Remove XHTTP links that can terminate Hiddify's sing-box core.

    Hiddify can safely keep Hysteria2 and non-XHTTP VLESS links from mixed
    subscriptions.  Happ and native Xray clients continue to receive the
    original list through their normal render path.
    """
    result = []
    for link in links if isinstance(links, (list, tuple)) else []:
        if not isinstance(link, str):
            continue
        parsed = urlparse(link)
        if parsed.scheme.lower() == "vless":
            transport = parse_qs(parsed.query).get("type", [""])[0].strip().lower()
            if transport in ("xhttp", "splithttp"):
                continue
        result.append(link)
    return result


def failover_xray_config(links, profile=None, name="WPP · Автопереключение"):
    """Build one Xray profile whose ordered proxy pool is monitored by leastLoad.

    The subscription URL and individual UUIDs remain unchanged.  Xray probes
    every outbound, moves new connections away from an unavailable route and
    automatically considers it again after recovery.
    """
    outbounds = []
    for index, link in enumerate(links, 1):
        if not isinstance(link, str) or urlparse(link).scheme.lower() not in ("vless", "hysteria2", "hy2"):
            continue
        try:
            outbound = _proxy_outbound(link)
        except RoutingError:
            continue
        outbound["tag"] = "proxy-%d" % index
        outbounds.append(outbound)
    if not outbounds:
        raise RoutingError("Для автоматического переключения нет поддерживаемых подключений.")

    if isinstance(profile, dict) and profile.get("enabled", True):
        selected = validate(profile)
        routing = routing_config(selected)
        dns = dns_config(selected)
    else:
        routing = {"domainStrategy": "IPIfNonMatch", "rules": [
            {"type": "field", "network": "tcp,udp", "balancerTag": "auto"},
        ]}
        dns = {"queryStrategy": "UseIP", "servers": [
            "https://1.1.1.1/dns-query", "https://8.8.8.8/dns-query",
        ]}
    for rule in routing.get("rules", []):
        if rule.get("outboundTag") == "proxy":
            rule.pop("outboundTag", None)
            rule["balancerTag"] = "auto"
    costs = [{"regexp": False, "match": "proxy-%d" % index, "value": float((index - 1) * 10)}
             for index in range(1, len(outbounds) + 1)]
    routing["balancers"] = [{
        "tag": "auto", "selector": ["proxy-"], "fallbackTag": "proxy-1",
        "strategy": {"type": "leastLoad", "settings": {
            "expected": 1, "maxRTT": "3s", "tolerance": 0.5, "costs": costs,
        }},
    }]
    return {
        "remarks": str(name),
        "log": {"loglevel": "warning"},
        "dns": dns,
        "inbounds": [
            {"tag": "socks-in", "listen": "127.0.0.1", "port": 10808,
             "protocol": "socks", "settings": {"udp": True}},
            {"tag": "http-in", "listen": "127.0.0.1", "port": 10809,
             "protocol": "http", "settings": {}},
        ],
        "outbounds": outbounds + [
            {"tag": "direct", "protocol": "freedom", "settings": {}},
            {"tag": "block", "protocol": "blackhole", "settings": {}},
        ],
        "routing": routing,
        "observatory": {
            "subjectSelector": ["proxy-"],
            "probeUrl": "https://www.gstatic.com/generate_204",
            "probeInterval": "15s",
            "enableConcurrency": True,
        },
    }


def xray_config(link, profile, name="WPP · Маршрутизация"):
    return {
        "remarks": str(name),
        "log": {"loglevel": "warning"},
        "dns": dns_config(profile),
        "inbounds": [
            {"tag": "socks-in", "listen": "127.0.0.1", "port": 10808, "protocol": "socks", "settings": {"udp": True}},
            {"tag": "http-in", "listen": "127.0.0.1", "port": 10809, "protocol": "http", "settings": {}},
        ],
        "outbounds": [
            _proxy_outbound(link),
            {"tag": "direct", "protocol": "freedom", "settings": {}},
            {"tag": "block", "protocol": "blackhole", "settings": {}},
        ],
        "routing": routing_config(profile),
    }


def plain_xray_config(link, name="WPP"):
    """Build an Xray profile without split-routing rules for one server."""
    return {
        "remarks": str(name),
        "log": {"loglevel": "warning"},
        "inbounds": [
            {"tag": "socks-in", "listen": "127.0.0.1", "port": 10808, "protocol": "socks", "settings": {"udp": True}},
            {"tag": "http-in", "listen": "127.0.0.1", "port": 10809, "protocol": "http", "settings": {}},
        ],
        "outbounds": [_proxy_outbound(link)],
    }


def routed_configs(users, link_builder, profiles, name_prefix="WPP"):
    result = []
    for user in users:
        profile = profiles.get(str(user.get("connection_id", "")))
        if user.get("protocol") not in ("vless", "hysteria") or not profile or not profile.get("enabled", True):
            continue
        link = link_builder(user)
        result.append(xray_config(link, profile, name_prefix + " · " + str(user.get("connection_name") or user.get("name") or "VLESS")))
    return result


def merge_profiles(values):
    """Merge connection profiles into the single routing profile Happ permits per subscription."""
    active = [validate(value) for value in values if isinstance(value, dict) and value.get("enabled", True)]
    if not active:
        return None
    merged = {
        "enabled": True,
        "ru_direct": any(item["ru_direct"] for item in active),
        "private_direct": any(item["private_direct"] for item in active),
        "captive_direct": any(item["captive_direct"] for item in active),
        "push_proxy": any(item["push_proxy"] for item in active),
        "youtube_proxy": any(item["youtube_proxy"] for item in active),
        "block_bittorrent": any(item["block_bittorrent"] for item in active),
        "split_dns": any(item["split_dns"] for item in active),
        "direct_domains": [], "proxy_domains": [], "updated_at": 0,
    }
    for key in ("direct_domains", "proxy_domains"):
        for item in active:
            for domain in item[key]:
                if domain not in merged[key]:
                    merged[key].append(domain)
    merged["updated_at"] = max(item["updated_at"] for item in active)
    return merged


def subscription_xray_configs(links, values, name_prefix="WPP"):
    """Build importable Xray JSON profiles for all supported subscription links."""
    profile = merge_profiles(values)
    if profile is None:
        return []
    result = []
    for index, link in enumerate(links, 1):
        if not isinstance(link, str) or urlparse(link).scheme.lower() not in ("vless", "hysteria2", "hy2"):
            continue
        parsed = urlparse(link)
        label = unquote(parsed.fragment).strip() or f"Сервер {index}"
        try:
            result.append(xray_config(link, profile, str(name_prefix) + " · " + label))
        except RoutingError:
            continue
    return result


def subscription_xray_configs_by_link(entries, name_prefix="WPP"):
    """Build Xray profiles while keeping routing attached to its connection.

    ``entries`` contains ``(link, profile-or-None)`` pairs. A missing profile
    deliberately produces a plain Xray configuration, so routing removed from
    one connection cannot leak back from another connection in the subscription.
    """
    result = []
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        link, profile = entry
        if not isinstance(link, str) or urlparse(link).scheme.lower() not in ("vless", "hysteria2", "hy2"):
            continue
        parsed = urlparse(link)
        label = unquote(parsed.fragment).strip() or f"Сервер {index}"
        name = str(name_prefix) + " · " + label
        try:
            if isinstance(profile, dict) and profile.get("enabled", True):
                result.append(xray_config(link, profile, name))
            else:
                result.append(plain_xray_config(link, name))
        except RoutingError:
            continue
    return result
