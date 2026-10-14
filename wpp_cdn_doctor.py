"""Strict validation and routing helpers for per-profile VLESS XHTTP CDN."""
import re

PROVIDERS = {"yandex", "vk", "beget", "selectel", "custom"}
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
        "uplink_method": "post",
        "fingerprint": "chrome",
    }


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
    if variant not in {"direct", "cdn", "beget", "selectel"}:
        raise CDNError("Неизвестный вариант пути XHTTP.")
    if LEGACY_PATH_RE.fullmatch(root):
        suffixes = {
            "direct": "",
            "cdn": "-cdn",
            "beget": "-beget/segment0.ts",
            "selectel": "-selectel/upload.m3u8",
        }
    else:
        suffixes = {
            "direct": "/index.html",
            "cdn": "/config.json",
            "beget": "/segment0.ts",
            "selectel": "/upload.m3u8",
        }
    return root + suffixes[variant]


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
    result["sni"] = _host(source.get("sni"), "TLS SNI") or result["public_host"]
    result["host_header"] = _host(source.get("host_header"), "HTTP Host") or result["public_host"]
    result["mode"] = str(source.get("mode", "packet-up")).strip().lower()
    if result["mode"] not in MODES:
        raise CDNError("Неизвестный режим XHTTP.")
    result["uplink_method"] = str(source.get("uplink_method", "post")).strip().lower()
    if result["uplink_method"] not in UPLINK_METHODS:
        raise CDNError("Неизвестный HTTP-метод XHTTP.")
    if result["uplink_method"] in {"get", "delete"}:
        result["mode"] = "packet-up"
    result["fingerprint"] = str(source.get("fingerprint", "chrome")).strip().lower()
    if result["fingerprint"] not in FINGERPRINTS:
        raise CDNError("Неизвестный TLS fingerprint.")
    return result


def _get_only_extra():
    return {
        "mode": "packet-up",
        "uplinkHTTPMethod": "GET",
        "uplinkDataPlacement": "header",
        "uplinkDataKey": "X-Data",
        "uplinkChunkSize": 2048,
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
        "sessionIDPlacement": "path",
        "sessionIDKey": "sid",
        "sessionIDTable": "kdleowms............",
        "sessionIDLength": "16-24",
        "seqPlacement": "query",
        "seqKey": "offset",
        "uplinkDataPlacement": "body",
        "uplinkDataKey": "",
        "scMaxEachPostBytes": "500000-1000000",
        "noSSEHeader": False,
        "scMaxBufferedPosts": 50,
        "scStreamUpServerSecs": "20-80",
        "serverMaxHeaderBytes": 0,
        "uplinkHTTPMethod": "GET",
        "headers": {},
        "scMinPostsIntervalMs": "50-150",
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
    """XHTTP profile for Selectel CDN with DELETE forwarding enabled."""
    return {
        "mode": "packet-up",
        "xPaddingBytes": "80-240",
        "xPaddingObfsMode": True,
        "xPaddingKey": "q",
        "xPaddingHeader": "X-Client-Version",
        "xPaddingPlacement": "query",
        "xPaddingMethod": "tokenish",
        "sessionPlacement": "header",
        "sessionKey": "X-Upload-Token",
        "seqPlacement": "query",
        "seqKey": "page",
        "noSSEHeader": False,
        "scMaxBufferedPosts": 30,
        "scStreamUpServerSecs": "20-80",
        "serverMaxHeaderBytes": 0,
        "uplinkHTTPMethod": "DELETE",
        "uplinkChunkSize": 0,
        "noGRPCHeader": False,
        "enableXmux": True,
        "uplinkDataPlacement": "body",
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


def server_xhttp(path, preset="direct"):
    result = {"path": path, "mode": "auto"}
    if preset is True or preset == "get":
        result.update(_get_only_extra())
    elif preset == "beget":
        result.update(_beget_extra())
    elif preset == "selectel":
        result.update(_selectel_extra())
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
    default_method = "delete" if provider == "selectel" else "get"
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
    return {
        "enabled": True,
        "address": value["public_host"],
        "sni": value["sni"],
        "host": value["host_header"],
        "mode": value["mode"],
        "extra": _beget_extra() if beget else (_selectel_extra() if selectel else (_get_only_extra() if value["uplink_method"] == "get" else None)),
        "fingerprint": value["fingerprint"],
        "path_suffix": "-beget/segment0.ts" if beget else ("-selectel/upload.m3u8" if selectel else "-cdn"),
        "path_variant": "beget" if beget else ("selectel" if selectel else "cdn"),
    }
