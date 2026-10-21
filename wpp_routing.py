"""Client-side split routing profiles attached to reusable VLESS connections."""
import json
import os
import re
import tempfile
import time
from urllib.parse import parse_qs, unquote, urlparse

SCHEMA = 1
MAX_CUSTOM_DOMAINS = 300

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
        return {"schema": SCHEMA, "profiles": {}}
    except (OSError, json.JSONDecodeError) as exc:
        raise RoutingError("Не удалось прочитать настройки маршрутизации.") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("profiles"), dict):
        raise RoutingError("Файл маршрутизации повреждён.")
    clean = {"schema": SCHEMA, "profiles": {}}
    for cid, value in raw["profiles"].items():
        if isinstance(cid, str) and isinstance(value, dict):
            try:
                clean["profiles"][cid] = validate(value)
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


def delete_profile(path, connection_id):
    state = load(path)
    state["profiles"].pop(str(connection_id), None)
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
