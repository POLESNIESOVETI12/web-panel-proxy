"""Strict validation and routing helpers for per-profile VLESS XHTTP CDN."""
import re

PROVIDERS = {"yandex", "vk", "beget", "selectel", "timeweb", "custom"}
MODES = {"packet-up", "stream-up", "auto"}
UPLINK_METHODS = {"post", "get", "delete"}
FINGERPRINTS = {"chrome", "firefox", "safari", "edge", "random", "randomized"}
HOST_RE = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
LEGACY_PATH_RE = re.compile(r"/vless-[a-f0-9]{24}\Z")
STATIC_PATH_RE = re.compile(r"/assets/[a-f0-9]{12}/[a-f0-9]{12}\Z")


class CDNError(ValueError):
    pass


def defaults():
    return {
        "enabled": False,
        "provider": "yandex",
        "public_host": "",
        "sni": "",
        "host_header": "",
        "mode": "packet-up",
        "uplink_method": "get",
        "fingerprint": "chrome",
    }


def transport_defaults(provider):
    """Return the tested transport for a managed CDN provider.

    Selectel forwards a normal POST body and can therefore use XHTTP's
    streaming uplink.  The other managed profiles are retrieval-only at the
    edge and must keep packet-up over GET.  Keeping this decision in one place
    prevents saved connections, generated links and server inbounds from
    silently drifting apart.
    """
    provider = str(provider or "custom").strip().lower()
    if provider == "selectel":
        return {"mode": "stream-up", "uplink_method": "post"}
    return {"mode": "packet-up", "uplink_method": "get"}


def valid_path_root(value):
    """Accept both existing technical roots and new static-looking roots."""
    value = str(value or "")
    return bool(LEGACY_PATH_RE.fullmatch(value) or STATIC_PATH_RE.fullmatch(value))


def endpoint_path(root, variant="direct"):
    """Build an XHTTP endpoint while preserving paths issued by older WPP builds."""
    root = str(root or "")
    if not valid_path_root(root):
        raise CDNError("Некорректный локальный путь XHTTP.")
    variant = str(variant or "direct")
    if variant not in {"direct", "cdn", "vk", "beget", "selectel", "timeweb"}:
        raise CDNError("Неизвестный вариант пути XHTTP.")
    if LEGACY_PATH_RE.fullmatch(root):
        suffixes = {
            "direct": "",
            "cdn": "-cdn",
            "vk": "-vk/media/hls/playlist.m3u8",
            "beget": "-beget/segment0.ts",
            "selectel": "-selectel/upload.m3u8",
            "timeweb": "-timeweb/api/v2/sync/stream",
        }
    else:
        suffixes = {
            "direct": "/index.html",
            "cdn": "/config.json",
            "vk": "/media/hls/playlist.m3u8",
            "beget": "/segment0.ts",
            "selectel": "/upload.m3u8",
            "timeweb": "/api/v2/sync/stream",
        }
    return root + suffixes[variant]


def rewrite_caddy_xhttp_routes(source, root):
    """Align the managed Caddy matchers with the restored XHTTP root."""
    if not isinstance(source, str):
        raise CDNError("Конфигурация Caddy повреждена.")
    routes = {
        "web_panel_vless_direct": "direct",
        "web_panel_vless_cdn": "cdn",
        "web_panel_vless_beget": "beget",
        "web_panel_vless_vk": "vk",
        "web_panel_vless_selectel": "selectel",
        "web_panel_vless_timeweb": "timeweb",
    }
    updated = source
    for matcher, variant in routes.items():
        route_path = endpoint_path(root, variant)
        pattern = r"(?m)^([ \t]*@" + re.escape(matcher) + r"[ \t]+path)[^\r\n]*$"
        updated, count = re.subn(
            pattern,
            lambda match, value=route_path: match.group(1) + " " + value + " " + value + "/*",
            updated,
        )
        if count != 1:
            raise CDNError("Не удалось синхронизировать маршрут Caddy: " + matcher + ".")
    return updated


def _host(value, label, required=False):
    value = str(value or "").strip().lower().rstrip(".")
    if value.startswith("http://") or value.startswith("https://") or "/" in value or ":" in value:
        raise CDNError(f"{label}: укажите только домен без https://, пути и порта.")
    if value and not HOST_RE.fullmatch(value):
        raise CDNError(f"{label}: некорректное доменное имя.")
    if required and not value:
        raise CDNError(f"{label}: поле обязательно.")
    return value


def normalize(value):
    source = value if isinstance(value, dict) else {}
    result = defaults()
    result["enabled"] = bool(source.get("enabled", False))
    result["provider"] = str(source.get("provider", "yandex")).strip().lower()
    if result["provider"] not in PROVIDERS:
        raise CDNError("Неизвестный CDN-провайдер.")
    result["public_host"] = _host(source.get("public_host"), "Публичный домен CDN", result["enabled"])
    if result["provider"] == "custom" and result["public_host"].endswith(".cdn.twcstorage.ru"):
        result["provider"] = "timeweb"
    result["sni"] = _host(source.get("sni"), "TLS SNI") or result["public_host"]
    result["host_header"] = _host(source.get("host_header"), "HTTP Host") or result["public_host"]
    preferred = transport_defaults(result["provider"])
    result["mode"] = str(source.get("mode", preferred["mode"])).strip().lower()
    if result["mode"] not in MODES:
        raise CDNError("Неизвестный режим XHTTP.")
    result["uplink_method"] = str(source.get("uplink_method", preferred["uplink_method"])).strip().lower()
    if result["uplink_method"] not in UPLINK_METHODS:
        raise CDNError("Неизвестный HTTP-метод XHTTP.")
    # Managed presets are intentionally automatic.  This also migrates old
    # Selectel DELETE/packet-up profiles to the tested POST/stream-up pair.
    result.update(preferred)
    result["fingerprint"] = str(source.get("fingerprint", "chrome")).strip().lower()
    if result["fingerprint"] not in FINGERPRINTS:
        raise CDNError("Неизвестный TLS fingerprint.")
    return result


def _get_only_extra():
    """Yandex CDN profile with the XHTTP uplink carried in HTTP headers.

    Keep this object shared by the server inbound and generated client links:
    XHTTP requires the placement/key/session fields to match at both ends.
    """
    return {
        "mode": "packet-up",
        "seqKey": "page",
        "xPaddingKey": "_rupl",
        "seqPlacement": "query",
        "sessionIDKey": "X-Auth-Token",
        "sessionIDTable": "base36",
        "xPaddingHeader": "X-Cache",
        "xPaddingMethod": "tokenish",
        "sessionIDLength": "8-12",
        # Header placement base64-encodes the entire packet.  Keep the whole
        # request comfortably below common 16 KiB CDN header limits; the
        # chunk range only controls how that payload is split across fields.
        "uplinkChunkSize": "3000-4000",
        "uplinkHTTPMethod": "GET",
        "xPaddingObfsMode": True,
        "xPaddingPlacement": "queryInHeader",
        "scMaxEachPostBytes": 8192,
        "scMaxBufferedPosts": 64,
        "sessionIDPlacement": "header",
        "uplinkDataPlacement": "header",
        "uplinkDataKey": "X-Data",
        "serverMaxHeaderBytes": 32768,
        "xPaddingBytes": "100-1000",
    }


def _vk_extra():
    """XHTTP profile that keeps VK Cloud CDN uplink data out of headers."""
    return {
        "mode": "packet-up",
        "uplinkHTTPMethod": "GET",
        "uplinkDataPlacement": "body",
        "sessionIDPlacement": "query",
        "sessionIDKey": "session",
        "seqPlacement": "query",
        "seqKey": "part",
        # Conservative values tested with GET-only CDN edges: large enough
        # for useful upload, but below the buffering/rate-limit threshold
        # seen with half-megabyte GET bodies.
        "scMaxEachPostBytes": "100000-300000",
        "scMaxBufferedPosts": 64,
        "scMinPostsIntervalMs": "20-45",
        "xPaddingBytes": "550-1450",
        "xPaddingObfsMode": True,
        "xPaddingKey": "_t",
        "xPaddingPlacement": "query",
        "xPaddingMethod": "tokenish",
    }


def _beget_extra():
    """XHTTP profile compatible with Beget CDN/CDNvideo GET forwarding."""
    return {
        "mode": "packet-up",
        "xPaddingBytes": "4-10",
        "xPaddingObfsMode": True,
        "xPaddingKey": "_token",
        "xPaddingHeader": "X-Client-Version",
        "xPaddingPlacement": "query",
        "xPaddingMethod": "tokenish",
        # A session in the path turns segment0.ts into a directory-like URL.
        # Several Beget edges reject that form before it reaches Caddy.
        "sessionIDPlacement": "query",
        "sessionIDKey": "sid",
        "sessionIDTable": "kdleowms............",
        "sessionIDLength": "16-24",
        "seqPlacement": "query",
        "seqKey": "offset",
        "uplinkDataPlacement": "body",
        "uplinkDataKey": "",
        "scMaxEachPostBytes": "131072-262144",
        "noSSEHeader": False,
        "scMaxBufferedPosts": 64,
        "scStreamUpServerSecs": "20-80",
        "serverMaxHeaderBytes": 0,
        "uplinkHTTPMethod": "GET",
        "headers": {},
        "scMinPostsIntervalMs": "20-40",
        "uplinkChunkSize": 0,
        "noGRPCHeader": False,
        "xmux": {
            # Xray rejects positive maxConcurrency together with positive
            # maxConnections. Keep the connection pool from the guide and
            # disable the mutually exclusive concurrency limit.
            "maxConcurrency": "0",
            "maxConnections": "4-8",
            "cMaxReuseTimes": "0",
            "hMaxRequestTimes": "300-600",
            "hMaxReusableSecs": "900-1800",
            "hKeepAlivePeriod": 0,
        },
        "enableXmux": True,
    }


def _selectel_extra():
    """Fast XHTTP profile for Selectel CDN using a streamed POST body."""
    return {
        "mode": "stream-up",
        "xPaddingBytes": "80-240",
        "xPaddingObfsMode": True,
        "xPaddingKey": "q",
        "xPaddingHeader": "X-Client-Version",
        "xPaddingPlacement": "query",
        "xPaddingMethod": "tokenish",
        "seqPlacement": "query",
        "seqKey": "page",
        "noSSEHeader": False,
        "scStreamUpServerSecs": "20-80",
        "serverMaxHeaderBytes": 0,
        "uplinkHTTPMethod": "POST",
        "noGRPCHeader": False,
        "enableXmux": True,
        "xmux": {
            "maxConcurrency": "16-32",
            "maxConnections": 0,
            "cMaxReuseTimes": 1000,
            "hMaxRequestTimes": "600-900",
            "hMaxReusableSecs": "100",
            "hKeepAlivePeriod": 20000,
        },
        "sessionIDPlacement": "query",
        "sessionIDKey": "sid",
    }


def _timeweb_extra():
    """Packet-up profile tuned for Timeweb's GET-capable CDN edge."""
    return {
        "mode": "packet-up",
        "uplinkHTTPMethod": "GET",
        "uplinkDataPlacement": "body",
        "sessionIDPlacement": "query",
        "sessionIDKey": "session",
        "seqPlacement": "query",
        "seqKey": "part",
        "scMaxEachPostBytes": "400000-800000",
        "scMaxBufferedPosts": 48,
        "scMinPostsIntervalMs": "50-100",
        "xPaddingBytes": "550-1450",
        "xPaddingObfsMode": True,
        "xPaddingHeader": "X-Request-ID",
        "xPaddingPlacement": "header",
        "xPaddingMethod": "tokenish",
        "xmux": {
            "maxConcurrency": "4-8",
            "maxConnections": 0,
            "cMaxReuseTimes": 0,
            "hMaxRequestTimes": "600-900",
            "hMaxReusableSecs": "1800-3000",
            "hKeepAlivePeriod": 0,
        },
        "enableXmux": True,
    }


def _selectel_server_extra():
    """Accept new stream-up links and cached legacy packet-up links.

    The client still receives the fast POST/stream-up preset.  Keeping the
    inbound in auto mode avoids an outage between the panel update and the
    next subscription refresh on end-user devices.
    """
    result = _selectel_extra()
    result["mode"] = "auto"
    result.pop("uplinkHTTPMethod", None)
    return result


def server_xhttp(path, preset="direct"):
    result = {"path": path, "mode": "auto"}
    if preset is True or preset == "get":
        result.update(_get_only_extra())
    elif preset == "vk":
        result.update(_vk_extra())
    elif preset == "beget":
        result.update(_beget_extra())
    elif preset == "selectel":
        result.update(_selectel_server_extra())
    elif preset == "timeweb":
        result.update(_timeweb_extra())
    return result


def profile_from_request(request):
    """Return a validated per-VLESS CDN profile or None for a direct route."""
    source = request if isinstance(request, dict) else {}
    route = str(source.get("vless_route", "direct")).strip().lower()
    if route == "direct":
        return None
    if route != "cdn":
        raise CDNError("Неизвестный маршрут VLESS.")
    provider = str(source.get("vless_cdn_provider", "yandex")).strip().lower()
    default_method = transport_defaults(provider)["uplink_method"]
    return normalize({
        "enabled": True,
        "provider": provider,
        "public_host": source.get("vless_cdn_public_host", ""),
        "sni": source.get("vless_cdn_sni", ""),
        "host_header": source.get("vless_cdn_host_header", ""),
        "mode": source.get("vless_cdn_mode", "packet-up"),
        "uplink_method": source.get("vless_cdn_uplink_method", default_method),
        "fingerprint": source.get("vless_cdn_fingerprint", "chrome"),
    })


def vless_route(origin_domain, profile=None):
    try:
        value = normalize(profile) if isinstance(profile, dict) else defaults()
    except CDNError:
        value = defaults()
    if not value["enabled"]:
        return {
            "enabled": False,
            "address": origin_domain,
            "sni": origin_domain,
            "host": origin_domain,
            "mode": "auto",
            "extra": None,
            "fingerprint": value.get("fingerprint", "chrome"),
            "path_suffix": "",
            "path_variant": "direct",
        }
    beget = value["provider"] == "beget"
    selectel = value["provider"] == "selectel"
    vk = value["provider"] == "vk"
    timeweb = value["provider"] == "timeweb"
    return {
        "enabled": True,
        "address": value["public_host"],
        "sni": value["sni"],
        "host": value["host_header"],
        "mode": value["mode"],
        "extra": _beget_extra() if beget else (_selectel_extra() if selectel else (_vk_extra() if vk else (_timeweb_extra() if timeweb else (_get_only_extra() if value["uplink_method"] == "get" else None)))),
        "fingerprint": value["fingerprint"],
        "path_suffix": "-beget/segment0.ts" if beget else ("-selectel/upload.m3u8" if selectel else ("-vk/media/hls/playlist.m3u8" if vk else ("-timeweb/api/v2/sync/stream" if timeweb else "-cdn"))),
        "path_variant": "beget" if beget else ("selectel" if selectel else ("vk" if vk else ("timeweb" if timeweb else "cdn"))),
    }
