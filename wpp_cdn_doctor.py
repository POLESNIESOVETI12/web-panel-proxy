"""Bounded diagnostics for stored WPP VLESS CDN profiles."""
import http.client
import os
import re
import socket
import ssl
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import quote
import wpp_cdn

HOST_RE = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
PROVIDER_PORTS = {"beget": 10002, "selectel": 10003, "vk": 10004, "yandex": 10001, "timeweb": 10005, "custom": 10001}


def _managed_routes(xray_path):
    if not wpp_cdn.valid_path_root(xray_path):
        raise ValueError("Некорректный локальный путь XHTTP.")
    direct_path = wpp_cdn.endpoint_path(xray_path, "direct")
    cdn_path = wpp_cdn.endpoint_path(xray_path, "cdn")
    beget_path = wpp_cdn.endpoint_path(xray_path, "beget")
    selectel_path = wpp_cdn.endpoint_path(xray_path, "selectel")
    vk_path = wpp_cdn.endpoint_path(xray_path, "vk")
    timeweb_path = wpp_cdn.endpoint_path(xray_path, "timeweb")
    return (
        '    @web_panel_cdn_probe path /__wpp_cdn_probe\n'
        '    handle @web_panel_cdn_probe {\n'
        '        header X-WPP-CDN-Probe "ok"\n'
        '        header Cache-Control "no-store, no-cache, must-revalidate"\n'
        '        header CDN-Cache-Control "no-store"\n'
        '        header Pragma "no-cache"\n'
        '        respond `{"ok":true,"service":"wpp-cdn-doctor"}` 200\n'
        '    }\n\n'
        '    # WPP XRAY ROUTES BEGIN\n'
        f'    @web_panel_vless_direct path {direct_path} {direct_path}/*\n'
        '    handle @web_panel_vless_direct {\n'
        '        reverse_proxy 127.0.0.1:10000 {\n'
        '            transport http {\n'
        '                versions h2c\n'
        '            }\n'
        '            flush_interval -1\n'
        '        }\n'
        '    }\n\n'
        f'    @web_panel_vless_cdn path {cdn_path} {cdn_path}/*\n'
        '    handle @web_panel_vless_cdn {\n'
        '        header Cache-Control "no-store, no-cache"\n'
        '        header CDN-Cache-Control "no-store"\n'
        '        header X-Accel-Buffering "no"\n'
        '        reverse_proxy 127.0.0.1:10001 {\n'
        '            transport http {\n'
        '                versions h2c\n'
        '            }\n'
        '            flush_interval -1\n'
        '        }\n'
        '    }\n\n'
        f'    @web_panel_vless_beget path {beget_path} {beget_path}/*\n'
        '    handle @web_panel_vless_beget {\n'
        '        header Cache-Control "no-store, no-cache"\n'
        '        header CDN-Cache-Control "no-store"\n'
        '        header X-Accel-Buffering "no"\n'
        '        reverse_proxy 127.0.0.1:10002 {\n'
        '            transport http {\n'
        '                versions h2c\n'
        '            }\n'
        '            flush_interval -1\n'
        '        }\n'
        '    }\n\n'
        f'    @web_panel_vless_selectel path {selectel_path} {selectel_path}/*\n'
        '    handle @web_panel_vless_selectel {\n'
        '        header Cache-Control "no-store, no-cache"\n'
        '        header CDN-Cache-Control "no-store"\n'
        '        header X-Accel-Buffering "no"\n'
        '        header Accept-Ranges "none"\n'
        '        reverse_proxy 127.0.0.1:10003 {\n'
        '            transport http {\n'
        '                versions h2c\n'
        '            }\n'
        '            flush_interval -1\n'
        '        }\n'
        '    }\n'
        '\n'
        f'    @web_panel_vless_vk path {vk_path} {vk_path}/*\n'
        '    handle @web_panel_vless_vk {\n'
        '        header Cache-Control "no-store, no-cache"\n'
        '        header CDN-Cache-Control "no-store"\n'
        '        header X-Accel-Buffering "no"\n'
        '        reverse_proxy 127.0.0.1:10004 {\n'
        '            transport http {\n'
        '                versions h2c\n'
        '            }\n'
        '            flush_interval -1\n'
        '        }\n'
        '    }\n'
        '\n'
        f'    @web_panel_vless_timeweb path {timeweb_path} {timeweb_path}/*\n'
        '    handle @web_panel_vless_timeweb {\n'
        '        header Cache-Control "no-store, no-cache"\n'
        '        header CDN-Cache-Control "no-store"\n'
        '        header X-Accel-Buffering "no"\n'
        '        reverse_proxy 127.0.0.1:10005 {\n'
        '            transport http {\n'
        '                versions h2c\n'
        '            }\n'
        '            flush_interval -1\n'
        '        }\n'
        '    }\n'
        '    # WPP XRAY ROUTES END\n\n'
    )


def repair_caddy_text(text, xray_path):
    """Return an idempotently repaired Caddyfile without changing external CDN settings."""
    value = str(text or "")
    value = re.sub(
        r'(?ms)^[ \t]*@web_panel_cdn_probe[ \t]+path[ \t]+/__wpp_cdn_probe[ \t]*\n'
        r'[ \t]*handle[ \t]+@web_panel_cdn_probe[ \t]*\{[ \t]*\n'
        r'.*?^[ \t]*\}[ \t]*\n?',
        '', value,
    )
    value = re.sub(
        r'\n?\s*# WPP XRAY ROUTES BEGIN\n.*?\n\s*# WPP XRAY ROUTES END\n?',
        '\n', value, flags=re.S,
    )
    relay = re.search(r'(?m)^[ \t]*reverse_proxy 127\.0\.0\.1:8080[ \t]*\{', value)
    if not relay:
        raise ValueError("В Caddyfile не найден локальный маршрут WPP.")
    prefix = value[:relay.start()].rstrip() + "\n\n"
    return prefix + _managed_routes(xray_path) + value[relay.start():]


def _atomic_write(path, content, source_stat=None):
    directory = os.path.dirname(path) or "."
    fd, temporary = tempfile.mkstemp(prefix=".wpp-caddy-", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if source_stat is not None:
            os.chmod(temporary, source_stat.st_mode & 0o7777)
            try:
                os.chown(temporary, source_stat.st_uid, source_stat.st_gid)
            except (AttributeError, PermissionError):
                pass
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def repair_caddyfile(path, xray_path):
    """Repair only WPP-owned probe/XHTTP routes and return the previous contents."""
    source_stat = os.stat(path)
    with open(path, encoding="utf-8") as stream:
        original = stream.read()
    repaired = repair_caddy_text(original, xray_path)
    if repaired != original:
        _atomic_write(path, repaired, source_stat)
    return original, repaired != original


def restore_caddyfile(path, original):
    source_stat = os.stat(path)
    _atomic_write(path, original, source_stat)


def _result(key, label, level, detail, fix=""):
    return {"key": key, "label": label, "level": level, "detail": detail, "fix": fix}


def _host(value):
    value = str(value or "").strip().lower().rstrip(".")
    if not HOST_RE.fullmatch(value):
        raise ValueError("Некорректный домен CDN в сохранённом профиле.")
    return value


def _resolve(host):
    values = []
    for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
        address = item[4][0]
        if address not in values:
            values.append(address)
    return values[:6]


def _tls(host, timeout):
    context = ssl.create_default_context()
    with socket.create_connection((host, 443), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as secure:
            cert = secure.getpeercert()
            protocol = secure.version() or "TLS"
    expires = cert.get("notAfter", "")
    days = None
    if expires:
        expiry = datetime.strptime(expires, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        days = int((expiry - datetime.now(timezone.utc)).total_seconds() // 86400)
    return protocol, days


def _request(host, method, path, timeout):
    started = time.monotonic()
    connection = http.client.HTTPSConnection(host, 443, timeout=timeout, context=ssl.create_default_context())
    headers = {
        "User-Agent": "WPP-CDN-Doctor/1.0",
        "Accept": "application/json",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    body = b"" if method in {"POST", "DELETE"} else None
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read(4096)
        result_headers = {key.lower(): value for key, value in response.getheaders()}
        return response.status, result_headers, payload, int((time.monotonic() - started) * 1000)
    finally:
        connection.close()


def _local_port(port, timeout):
    with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
        return True


def run(profile, origin_domain, xray_path, timeout=7):
    """Return a user-facing report without accepting arbitrary probe targets."""
    provider = str(profile.get("provider", "custom")).strip().lower()
    cdn_host = _host(profile.get("public_host"))
    origin_host = _host(origin_domain)
    variant = "beget" if provider == "beget" else ("selectel" if provider == "selectel" else ("vk" if provider == "vk" else ("timeweb" if provider == "timeweb" else "cdn")))
    transport = wpp_cdn.transport_defaults(provider)
    method = transport["uplink_method"].upper()
    expected_path = wpp_cdn.endpoint_path(xray_path, variant)
    results = []

    try:
        addresses = _resolve(cdn_host)
        results.append(_result("dns", "DNS домена CDN", "pass", ", ".join(addresses) if addresses else "Адрес найден"))
    except (OSError, socket.gaierror) as exc:
        results.append(_result("dns", "DNS домена CDN", "fail", "Домен не разрешается: " + str(exc), "Проверьте домен CDN и дождитесь завершения его активации."))

    try:
        protocol, days = _tls(cdn_host, timeout)
        if days is not None and days < 1:
            results.append(_result("tls", "HTTPS-сертификат CDN", "fail", "Сертификат истёк.", "Выпустите или привяжите сертификат в CDN."))
        elif days is not None and days < 14:
            results.append(_result("tls", "HTTPS-сертификат CDN", "warn", f"{protocol}, осталось {days} дн.", "Проверьте автоматическое продление сертификата."))
        else:
            detail = protocol + (f", действует ещё {days} дн." if days is not None else "")
            results.append(_result("tls", "HTTPS-сертификат CDN", "pass", detail))
    except (OSError, ssl.SSLError, ValueError) as exc:
        results.append(_result("tls", "HTTPS-сертификат CDN", "fail", str(exc), "Проверьте сертификат и HTTPS в настройках CDN."))

    probe_path = "/__wpp_cdn_probe?doctor=" + quote(str(time.time_ns()))
    origin_ok = False
    try:
        status, headers, payload, latency = _request(origin_host, "GET", probe_path, timeout)
        origin_ok = status == 200 and headers.get("x-wpp-cdn-probe") == "ok"
        if origin_ok:
            results.append(_result("origin", "Источник WPP", "pass", f"HTTP {status}, {latency} мс"))
        else:
            results.append(_result("origin", "Источник WPP", "fail", f"HTTP {status}, контрольный заголовок не получен.", "Обновите WPP и проверьте основной домен на порту 443."))
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        results.append(_result("origin", "Источник WPP", "fail", str(exc), "Проверьте DNS основного домена, порт 443 и службу Caddy."))

    cdn_headers = {}
    try:
        status, cdn_headers, payload, latency = _request(cdn_host, "GET", probe_path, timeout)
        signature = cdn_headers.get("x-wpp-cdn-probe") == "ok"
        if status == 200 and signature:
            results.append(_result("edge", "Маршрут через CDN", "pass", f"HTTP {status}, {latency} мс"))
        elif status == 403 and provider == "selectel":
            reason = cdn_headers.get("x-reason-code", "не указан")
            results.append(_result(
                "edge", "Маршрут через CDN", "warn",
                f"Служебный путь вернул HTTP 403, код CDN: {reason}.",
                "Для Selectel это допустимо при ограничении путей ресурса и не означает, что XHTTP не работает.",
            ))
        elif status == 403:
            reason = cdn_headers.get("x-reason-code", "не указан")
            results.append(_result("edge", "Маршрут через CDN", "fail", f"HTTP 403, код CDN: {reason}.", "Дождитесь ACTIVE; проверьте баланс, ограничения и Hostname источника."))
        elif status in (502, 503, 504):
            results.append(_result("edge", "Маршрут через CDN", "fail", f"HTTP {status}: CDN не получает корректный ответ источника.", "Укажите источник HTTPS, порт 443 и Hostname основного домена WPP."))
        else:
            results.append(_result("edge", "Маршрут через CDN", "fail", f"HTTP {status}, подпись WPP не получена.", "Проверьте источник и передачу заголовка Host в CDN."))
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        results.append(_result("edge", "Маршрут через CDN", "fail", str(exc), "Проверьте статус CDN и доступность публичного домена."))

    try:
        status, headers, payload, latency = _request(cdn_host, method, probe_path, timeout)
        if status == 200 and headers.get("x-wpp-cdn-probe") == "ok":
            results.append(_result("method", f"Метод {method}", "pass", f"Разрешён, {latency} мс"))
        elif status in (403, 405, 413, 501):
            results.append(_result("method", f"Метод {method}", "fail", f"CDN вернул HTTP {status}.", f"Разрешите метод {method} в настройках CDN."))
        else:
            results.append(_result("method", f"Метод {method}", "warn", f"Получен HTTP {status}.", "Проверьте разрешённые HTTP-методы CDN."))
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        results.append(_result("method", f"Метод {method}", "fail", str(exc), f"Проверьте поддержку метода {method} у CDN."))

    if transport["mode"] == "stream-up":
        results.append(_result("uplink", "Режим исходящего трафика", "pass", "stream-up · потоковый POST без дробления upload"))
    else:
        results.append(_result(
            "uplink", "Режим исходящего трафика", "warn",
            "packet-up · CDN принимает только GET, поэтому скорость upload зависит от лимитов edge-сервера.",
        ))

    cache = cdn_headers.get("x-cdn-edge-cache", cdn_headers.get("x-cache", ""))
    if "HIT" in cache.upper():
        results.append(_result("cache", "Кеширование CDN", "warn", "Уникальный диагностический запрос получен из кеша: " + cache, "Отключите кеширование и очистите кеш CDN-ресурса."))
    else:
        results.append(_result("cache", "Кеширование CDN", "pass", cache or "Признаков кеширования диагностического запроса нет."))

    port = PROVIDER_PORTS.get(provider, 10001)
    try:
        _local_port(port, min(timeout, 3))
        results.append(_result("xray", "Локальный Xray inbound", "pass", f"127.0.0.1:{port} принимает соединения"))
    except OSError as exc:
        results.append(_result("xray", "Локальный Xray inbound", "fail", str(exc), "Перезапустите Xray через обновление WPP и проверьте журнал службы."))

    results.append(_result("path", "Путь XHTTP", "pass", expected_path))
    failed = sum(item["level"] == "fail" for item in results)
    warned = sum(item["level"] == "warn" for item in results)
    return {
        "ok": failed == 0,
        "provider": provider,
        "cdn_host": cdn_host,
        "origin_host": origin_host,
        "method": method,
        "path": expected_path,
        "failed": failed,
        "warned": warned,
        "results": results,
    }
