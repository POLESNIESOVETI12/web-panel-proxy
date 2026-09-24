#!/usr/bin/env python3
"""Small, root-only OpenFlux exit-node controller used by WPP."""

import json
import os
import pwd
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

CONFIG_DIR = Path("/etc/web-proxy-panel/openflux")
STATE_FILE = CONFIG_DIR / "config.json"
URL_FILE = CONFIG_DIR / "document-url"
KEY_FILE = CONFIG_DIR / "encryption-key"
ENABLED_FILE = CONFIG_DIR / "enabled"
BIN = Path("/opt/web-panel-proxy/openflux/openflux")
UNIT = Path("/etc/systemd/system/web-panel-proxy-openflux.service")
SERVICE = "web-panel-proxy-openflux.service"
SERVICE_USER = "wpp-openflux"
LEGACY_IOS_DROPIN = Path("/etc/systemd/system/web-panel-proxy-openflux.service.d/ios.conf")
PROFILES_DIR = CONFIG_DIR / "profiles"
VERSION = "1.0.0"
MAX_OPENFLUX_PROFILES = 32
TRANSPORT = "yandex"
TRANSPORTS = {"yandex", "mailru"}
CODEC = "batched"
MODE = "l4"


class OpenFluxError(RuntimeError):
    pass


def _codec_for(config):
    """Use the codec built into the current iOS and Android clients."""
    return CODEC


def _clean_transport(value):
    value = str(value or TRANSPORT).strip().lower()
    if value not in TRANSPORTS:
        raise OpenFluxError("Выберите Яндекс Документы или Mail.ru Документы.")
    return value


def _transport_for(config):
    value = str((config or {}).get("transport", TRANSPORT)).lower()
    return value if value in TRANSPORTS else TRANSPORT


def _run(args, *, check=True, timeout=20):
    try:
        return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=check)
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", "") or getattr(exc, "stdout", "") or str(exc)
        raise OpenFluxError(detail.strip()[-1200:] or "OpenFlux command failed") from exc


def _atomic(path, value, mode, uid=0, gid=0):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.chown(temporary, uid, gid)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def validate_document_url(value, transport=TRANSPORT):
    transport = _clean_transport(transport)
    value = str(value or "").strip()
    if not value or len(value) > 2048 or any(ord(char) < 32 for char in value):
        raise OpenFluxError("Укажите корректную ссылку на документ Яндекса.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise OpenFluxError("Ссылка на документ имеет неверный формат.") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if transport == "mailru":
        allowed = host == "cloud.mail.ru" and bool(re.fullmatch(r"/public/[^/]+/[^/]+/?", parsed.path))
        provider = "Mail.ru"
    else:
        allowed = host in {"yandex.ru", "yandex.com"} or host.endswith(".yandex.ru") or host.endswith(".yandex.com")
        provider = "Яндекса"
    if parsed.scheme != "https" or not allowed or port not in (None, 443):
        raise OpenFluxError(f"Укажите публичную HTTPS-ссылку на документ {provider}.")
    if parsed.username or parsed.password or parsed.fragment or not parsed.path or parsed.path == "/":
        raise OpenFluxError("Используйте публичную ссылку на конкретный документ без логина и фрагмента.")
    return value


def _load():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise OpenFluxError("Конфигурация OpenFlux повреждена.") from exc
    return data if isinstance(data, dict) else {}


def _service_active():
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", SERVICE],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
        check=False,
    )
    return result.returncode == 0


def _identity():
    try:
        account = pwd.getpwnam(SERVICE_USER)
    except KeyError as exc:
        raise OpenFluxError("Системный пользователь OpenFlux не создан. Повторите обновление WPP.") from exc
    return account.pw_uid, account.pw_gid


def _write_private_files(config):
    uid, gid = _identity()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chown(CONFIG_DIR, 0, gid)
    os.chmod(CONFIG_DIR, 0o750)
    _atomic(URL_FILE, config["url"] + "\n", 0o640, 0, gid)
    _atomic(KEY_FILE, config["key"] + "\n", 0o640, 0, gid)
    _atomic(STATE_FILE, json.dumps(config, ensure_ascii=True, indent=2) + "\n", 0o600)
    if config.get("enabled", True):
        _atomic(ENABLED_FILE, "enabled\n", 0o600)
    else:
        try:
            ENABLED_FILE.unlink()
        except FileNotFoundError:
            pass


def _unit_text(config=None):
    config = config if isinstance(config, dict) else _load()
    encryption_argument = "" if config.get("ios_compatible", False) else f" --encryption-key-file={KEY_FILE}"
    codec = _codec_for(config)
    transport = _transport_for(config)
    return f"""[Unit]
Description=WEB PANEL PROXY OpenFlux exit node
After=network-online.target
Wants=network-online.target
ConditionPathExists={ENABLED_FILE}

[Service]
Type=simple
User={SERVICE_USER}
Group={SERVICE_USER}
UMask=0077
ExecStart=/bin/sh -c 'exec {BIN} --role=exit --mode={MODE} --codec={codec} --transport={transport} --url "$$(cat {URL_FILE})"{encryption_argument}'
Restart=on-failure
RestartSec=4
TimeoutStopSec=15
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6
RestrictNamespaces=true
LockPersonality=true
CapabilityBoundingSet=
AmbientCapabilities=

[Install]
WantedBy=multi-user.target
"""


def install_service():
    if not BIN.is_file() or not os.access(BIN, os.X_OK):
        raise OpenFluxError("Бинарник OpenFlux не установлен. Повторите обновление WPP.")
    config = _load()
    try:
        legacy_dropin_text = LEGACY_IOS_DROPIN.read_text(encoding="utf-8")
    except FileNotFoundError:
        legacy_dropin_text = ""
    if legacy_dropin_text and "--encryption-key-file" not in legacy_dropin_text:
        config["ios_compatible"] = True
        if config.get("url") and config.get("key"):
            _write_private_files(config)
    expected = _unit_text(config)
    try:
        current = UNIT.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    legacy_dropin_removed = False
    try:
        LEGACY_IOS_DROPIN.unlink()
        legacy_dropin_removed = True
    except FileNotFoundError:
        pass
    if current != expected:
        _atomic(UNIT, expected, 0o644)
        legacy_dropin_removed = True
    if legacy_dropin_removed:
        _run(["systemctl", "daemon-reload"])


def _start():
    install_service()
    _run(["systemctl", "restart", SERVICE])
    stable = 0
    for _ in range(20):
        if _service_active():
            stable += 1
            if stable >= 4:
                return
        else:
            stable = 0
        time.sleep(0.25)
    journal = _run(["journalctl", "-u", SERVICE, "-n", "30", "--no-pager"], check=False).stdout
    raise OpenFluxError("OpenFlux не запустился. " + journal.strip()[-900:])


def configure(document_url, ios_compatible=False, transport=TRANSPORT):
    transport = _clean_transport(transport)
    url = validate_document_url(document_url, transport)
    previous = _load()
    config = {
        "enabled": True,
        "url": url,
        "key": previous.get("key") if isinstance(previous.get("key"), str) and len(previous["key"]) >= 24 else secrets.token_urlsafe(32),
        "transport": transport,
        "codec": CODEC,
        "mode": MODE,
        "ios_compatible": bool(ios_compatible),
        "version": VERSION,
        "updated_at": int(time.time()),
    }
    _write_private_files(config)
    try:
        _start()
    except Exception:
        if previous.get("url") and previous.get("key"):
            _write_private_files(previous)
            try:
                _start()
            except Exception:
                pass
        else:
            subprocess.run(["systemctl", "stop", SERVICE], check=False, timeout=15)
        raise
    return state()


def set_enabled(enabled):
    config = _load()
    if not config.get("url") or not config.get("key"):
        raise OpenFluxError("Сначала сохраните ссылку на документ.")
    config["enabled"] = bool(enabled)
    config["updated_at"] = int(time.time())
    _write_private_files(config)
    if enabled:
        try:
            _start()
        except Exception:
            config["enabled"] = False
            _write_private_files(config)
            subprocess.run(["systemctl", "stop", SERVICE], check=False, timeout=15)
            raise
    else:
        _run(["systemctl", "stop", SERVICE], check=False)
    return state()


def rotate_key():
    config = _load()
    if not config.get("url"):
        raise OpenFluxError("Сначала сохраните ссылку на документ.")
    previous = dict(config)
    config["key"] = secrets.token_urlsafe(32)
    config["enabled"] = True
    config["updated_at"] = int(time.time())
    _write_private_files(config)
    try:
        _start()
    except Exception:
        _write_private_files(previous)
        if previous.get("enabled", True):
            try:
                _start()
            except Exception:
                pass
        raise
    return state()


def restore_if_configured():
    config = _load()
    if not config.get("url") or not config.get("key"):
        restored = False
    else:
        _write_private_files(config)
        install_service()
        if config.get("enabled", True):
            _start()
        else:
            _run(["systemctl", "stop", SERVICE], check=False)
        restored = True
    for profile in _extra_configs():
        _write_extra_files(profile)
        _install_extra_service(profile)
        if profile.get("enabled", True):
            _start_extra(profile)
        else:
            _run(["systemctl", "stop", _extra_service(profile["id"])], check=False)
    return restored or bool(_extra_configs())


def state():
    config = _load()
    configured = bool(config.get("url") and config.get("key"))
    return {
        "configured": configured,
        "enabled": bool(config.get("enabled", True)) if configured else False,
        "active": _service_active() if configured else False,
        "url": config.get("url", "") if configured else "",
        "key": config.get("key", "") if configured else "",
        "transport": _transport_for(config),
        "codec": _codec_for(config),
        "mode": MODE,
        "ios_compatible": bool(config.get("ios_compatible", False)),
        "encrypted": not bool(config.get("ios_compatible", False)),
        "version": VERSION,
    }


def _clean_profile_name(value):
    value = str(value or "").strip()
    if not value or len(value) > 80 or any(ord(char) < 32 for char in value):
        raise OpenFluxError("Укажите имя профиля длиной от 1 до 80 символов.")
    return value


def _clean_platform(value):
    value = str(value or "").lower()
    if value not in ("ios", "android"):
        raise OpenFluxError("Выберите iOS или Android.")
    return value


def _extra_id(value):
    value = str(value or "")
    if not re.fullmatch(r"[a-f0-9]{16}", value):
        raise OpenFluxError("Профиль OpenFlux не найден.")
    return value


def _extra_paths(profile_id):
    directory = PROFILES_DIR / _extra_id(profile_id)
    return {
        "dir": directory,
        "state": directory / "config.json",
        "url": directory / "document-url",
        "key": directory / "encryption-key",
        "enabled": directory / "enabled",
        "unit": Path("/etc/systemd/system") / ("web-panel-proxy-openflux-" + profile_id + ".service"),
    }


def _extra_service(profile_id):
    return "web-panel-proxy-openflux-" + _extra_id(profile_id) + ".service"


def _extra_configs():
    result = []
    try:
        entries = list(PROFILES_DIR.iterdir())
    except FileNotFoundError:
        return result
    for entry in entries:
        if not entry.is_dir() or not re.fullmatch(r"[a-f0-9]{16}", entry.name):
            continue
        try:
            value = json.loads((entry / "config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and value.get("id") == entry.name:
            result.append(value)
    return sorted(result, key=lambda item: (int(item.get("created_at", 0)), item["id"]))


def _write_extra_files(config):
    paths = _extra_paths(config["id"])
    _, gid = _identity()
    paths["dir"].mkdir(parents=True, exist_ok=True)
    os.chown(paths["dir"], 0, gid)
    os.chmod(paths["dir"], 0o750)
    _atomic(paths["url"], config["url"] + "\n", 0o640, 0, gid)
    _atomic(paths["key"], config["key"] + "\n", 0o640, 0, gid)
    _atomic(paths["state"], json.dumps(config, ensure_ascii=True, indent=2) + "\n", 0o600)
    if config.get("enabled", True):
        _atomic(paths["enabled"], "enabled\n", 0o600)
    else:
        try:
            paths["enabled"].unlink()
        except FileNotFoundError:
            pass


def _extra_unit_text(config):
    paths = _extra_paths(config["id"])
    encryption = "" if config.get("platform") == "ios" else " --encryption-key-file=" + str(paths["key"])
    codec = _codec_for(config)
    transport = _transport_for(config)
    return f"""[Unit]
Description=WEB PANEL PROXY OpenFlux profile {config['id']}
After=network-online.target
Wants=network-online.target
ConditionPathExists={paths['enabled']}

[Service]
Type=simple
User={SERVICE_USER}
Group={SERVICE_USER}
UMask=0077
ExecStart=/bin/sh -c 'exec {BIN} --role=exit --mode={MODE} --codec={codec} --transport={transport} --url "$$(cat {paths['url']})"{encryption}'
Restart=on-failure
RestartSec=4
TimeoutStopSec=15
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6
RestrictNamespaces=true
LockPersonality=true
CapabilityBoundingSet=
AmbientCapabilities=

[Install]
WantedBy=multi-user.target
"""


def _install_extra_service(config):
    if not BIN.is_file() or not os.access(BIN, os.X_OK):
        raise OpenFluxError("Бинарник OpenFlux не установлен. Повторите обновление WPP.")
    paths = _extra_paths(config["id"])
    expected = _extra_unit_text(config)
    try:
        current = paths["unit"].read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    if current != expected:
        _atomic(paths["unit"], expected, 0o644)
        _run(["systemctl", "daemon-reload"])


def _extra_active(profile_id):
    return subprocess.run(["systemctl", "is-active", "--quiet", _extra_service(profile_id)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          timeout=5, check=False).returncode == 0


def _start_extra(config):
    _install_extra_service(config)
    service = _extra_service(config["id"])
    _run(["systemctl", "enable", service])
    _run(["systemctl", "restart", service])
    stable = 0
    for _ in range(20):
        if _extra_active(config["id"]):
            stable += 1
            if stable >= 4:
                return
        else:
            stable = 0
        time.sleep(0.25)
    journal = _run(["journalctl", "-u", service, "-n", "30", "--no-pager"], check=False).stdout
    raise OpenFluxError("Профиль OpenFlux не запустился. " + journal.strip()[-900:])


def profile_states():
    result = []
    main = state()
    if main["configured"]:
        main.update({"id": "main", "name": _load().get("name", "OpenFlux"),
                     "platform": "ios" if main["ios_compatible"] else "android"})
        result.append(main)
    for config in _extra_configs():
        result.append({
            "id": config["id"], "name": config.get("name", "OpenFlux"),
            "platform": config.get("platform", "android"), "url": config.get("url", ""),
            "key": config.get("key", ""), "configured": True,
            "enabled": bool(config.get("enabled", True)), "active": _extra_active(config["id"]),
            "encrypted": config.get("platform") != "ios", "transport": _transport_for(config),
            "codec": _codec_for(config), "version": VERSION,
            "created_at": int(config.get("created_at", 0) or 0),
        })
    return result


def create_profile(name, document_url, platform, transport=TRANSPORT):
    if len(_extra_configs()) >= MAX_OPENFLUX_PROFILES:
        raise OpenFluxError("Достигнут лимит профилей OpenFlux.")
    profile_id = secrets.token_hex(8)
    transport = _clean_transport(transport)
    document_url = validate_document_url(document_url, transport)
    platform = _clean_platform(platform)
    existing_urls = {item.get("url") for item in _extra_configs()}
    existing_urls.add(_load().get("url"))
    if document_url in existing_urls:
        raise OpenFluxError("Этот документ уже используется другим профилем OpenFlux.")
    config = {"id": profile_id, "name": _clean_profile_name(name),
              "url": document_url, "platform": platform,
              "key": secrets.token_urlsafe(32), "enabled": True, "transport": transport,
              "codec": CODEC, "mode": MODE, "version": VERSION,
              "created_at": int(time.time()), "updated_at": int(time.time())}
    _write_extra_files(config)
    try:
        _start_extra(config)
    except Exception:
        _run(["systemctl", "disable", "--now", _extra_service(profile_id)], check=False)
        shutil.rmtree(_extra_paths(profile_id)["dir"], ignore_errors=True)
        try:
            _extra_paths(profile_id)["unit"].unlink()
        except FileNotFoundError:
            pass
        _run(["systemctl", "daemon-reload"], check=False)
        raise
    return config


def profile_set_enabled(profile_id, enabled):
    if profile_id == "main":
        return set_enabled(enabled)
    config = next((item for item in _extra_configs() if item["id"] == _extra_id(profile_id)), None)
    if config is None:
        raise OpenFluxError("Профиль OpenFlux не найден.")
    config["enabled"] = bool(enabled)
    config["updated_at"] = int(time.time())
    _write_extra_files(config)
    if enabled:
        try:
            _start_extra(config)
        except Exception:
            config["enabled"] = False
            _write_extra_files(config)
            raise
    else:
        _run(["systemctl", "stop", _extra_service(config["id"])], check=False)


def profile_rotate(profile_id):
    if profile_id == "main":
        return rotate_key()
    config = next((item for item in _extra_configs() if item["id"] == _extra_id(profile_id)), None)
    if config is None:
        raise OpenFluxError("Профиль OpenFlux не найден.")
    config["key"] = secrets.token_urlsafe(32)
    config["enabled"] = True
    config["updated_at"] = int(time.time())
    _write_extra_files(config)
    _start_extra(config)


def delete_profile(profile_id):
    if profile_id == "main":
        config = _load()
        if not config.get("url"):
            raise OpenFluxError("Профиль OpenFlux не найден.")
        _run(["systemctl", "disable", "--now", SERVICE], check=False)
        for path in (STATE_FILE, URL_FILE, KEY_FILE, ENABLED_FILE):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        # Keep the unit template installed so a new main profile can be
        # created later without reinstalling the panel.
        install_service()
        return
    profile_id = _extra_id(profile_id)
    paths = _extra_paths(profile_id)
    if not paths["state"].exists():
        raise OpenFluxError("Профиль OpenFlux не найден.")
    _run(["systemctl", "disable", "--now", _extra_service(profile_id)], check=False)
    try:
        paths["unit"].unlink()
    except FileNotFoundError:
        pass
    shutil.rmtree(paths["dir"], ignore_errors=True)
    _run(["systemctl", "daemon-reload"], check=False)
