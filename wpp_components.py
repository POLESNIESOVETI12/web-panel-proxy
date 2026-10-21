#!/usr/bin/env python3
"""Transactional Xray/OpenFlux version manager for the local WPP server."""

from contextlib import contextmanager
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from wpp_metrics import atomic_json, read_state

ROOT = Path("/var/lib/web-panel-proxy-components")
STATUS = ROOT / "status.json"
ACTION_LOCK = ROOT / "action.lock"
AUTO_CONFIG = ROOT / "auto-update.json"
UNIT = "web-panel-proxy-component-update.service"
OPENFLUX_PROFILES = Path("/etc/web-proxy-panel/openflux/profiles")
SPECS = {
    "xray": {
        "repo": "https://github.com/XTLS/Xray-core.git",
        "releases": "https://github.com/XTLS/Xray-core/releases",
        "release_path": "XTLS/Xray-core",
        "asset": "https://github.com/XTLS/Xray-core/releases/download/{tag}/Xray-linux-64.zip",
        "binary": Path("/opt/web-panel-proxy/xray/xray"),
        "service": "web-panel-proxy-xray.service",
        "version_file": ROOT / "xray.version",
    },
    "openflux": {
        "repo": "https://github.com/p1neappleXpress/OpenFlux.git",
        "releases": "https://github.com/p1neappleXpress/OpenFlux/releases",
        "release_path": "p1neappleXpress/OpenFlux",
        "asset": "https://github.com/p1neappleXpress/OpenFlux/releases/download/{tag}/openflux-linux-amd64",
        "binary": Path("/opt/web-panel-proxy/openflux/openflux"),
        "service": "web-panel-proxy-openflux.service",
        "version_file": ROOT / "openflux.version",
    },
}
COMPONENT_LABELS = {"xray": "Xray", "openflux": "OpenFlux"}
SEMVER_TAG = re.compile(r"v\d+(?:\.\d+){1,3}")
NIGHTLY_TAG = re.compile(r"nightly-(\d{8})-([0-9a-f]{7,40})")


def _run(args, **kwargs):
    return subprocess.run(args, text=True, encoding=kwargs.pop("encoding", "utf-8"),
                          errors=kwargs.pop("errors", "replace"), capture_output=True,
                          timeout=kwargs.pop("timeout", 30), check=False, **kwargs)


def _progress(state, percent, step, message="", phase=None):
    """Persist a user-facing installation stage for the polling UI."""
    state.update(progress=max(0, min(100, int(percent))), step=str(step))
    if message:
        state["message"] = str(message)
    if phase:
        state["phase"] = phase
    atomic_json(STATUS, state)


def _friendly_failure(component, error):
    """Translate installer failures into a concise cause and useful next step."""
    label = COMPONENT_LABELS.get(component, component or "Компонент")
    raw = str(error or "Неизвестная ошибка").strip()
    lowered = raw.lower()
    if "github" in lowered or "скач" in lowered or "curl" in lowered:
        return {
            "error_title": "Не удалось скачать версию",
            "message": f"{label} не обновлён: сервер не получил файл релиза.",
            "hint": "Проверьте доступ VPS к GitHub и повторите установку. Текущая версия сохранена.",
            "error_detail": raw,
        }
    if "распаков" in lowered or "unzip" in lowered:
        return {
            "error_title": "Архив не удалось распаковать",
            "message": f"{label} не обновлён: файл релиза повреждён или имеет неверный формат.",
            "hint": "Обновите список версий и повторите. Текущая версия сохранена.",
            "error_detail": raw,
        }
    if "конфигурац" in lowered or "не принимает" in lowered:
        return {
            "error_title": "Версия несовместима с настройками",
            "message": f"{label} не обновлён: новая версия не приняла действующую конфигурацию.",
            "hint": "Выберите другую версию. Работающее подключение не было заменено.",
            "error_detail": raw,
        }
    if "max" in lowered:
        return {
            "error_title": "Версия не поддерживает MAX",
            "message": "OpenFlux не обновлён: выбранный релиз несовместим с существующими MAX-профилями.",
            "hint": "Выберите более новую версию OpenFlux. Текущая версия сохранена.",
            "error_detail": raw,
        }
    if "служб" in lowered or "systemctl" in lowered or "запуска" in lowered:
        return {
            "error_title": "Компонент не запустился",
            "message": f"{label} не прошёл проверку запуска, поэтому изменение отменено.",
            "hint": "Панель восстановила предыдущий файл. Повторите или выберите другую версию.",
            "error_detail": raw,
        }
    if "исполняем" in lowered or "elf" in lowered:
        return {
            "error_title": "Неверный файл релиза",
            "message": f"{label} не обновлён: загруженный файл не подходит этому серверу.",
            "hint": "Выберите другую опубликованную версию. Текущая версия сохранена.",
            "error_detail": raw,
        }
    return {
        "error_title": "Установка не завершена",
        "message": f"{label} не обновлён. Текущая версия продолжает работать.",
        "hint": "Повторите установку. Если ошибка повторится, передайте администратору техническую причину ниже.",
        "error_detail": raw[:600],
    }


def _version_tuple(value):
    numbers = re.findall(r"\d+", str(value))
    return tuple(int(x) for x in numbers[:4])


def _valid_tag(component, value):
    value = str(value or "")
    return bool(SEMVER_TAG.fullmatch(value) or
                (component == "openflux" and NIGHTLY_TAG.fullmatch(value)))


def _tag_rank(component, value):
    """Prefer numbered releases; nightlies remain available as test builds."""
    value = str(value or "")
    nightly = NIGHTLY_TAG.fullmatch(value) if component == "openflux" else None
    if nightly:
        return (1, int(nightly.group(1)), 0, 0, 0, nightly.group(2))
    numbers = list(_version_tuple(value))
    if not numbers:
        return (0, 0, 0, 0, 0, "")
    numbers.extend([0] * (4 - len(numbers)))
    return (2, *numbers[:4], "")


def _display_version(component, value):
    raw = str(value or "").removeprefix("v")
    if component != "openflux":
        return raw
    if raw == "0.13.0":
        return "1.6.0"
    nightly = NIGHTLY_TAG.fullmatch(str(value or ""))
    if nightly:
        date = nightly.group(1)
        return f"тестовая сборка {date[6:8]}.{date[4:6]}.{date[:4]}"
    return raw


def _current(component):
    binary = SPECS[component]["binary"]
    if not binary.is_file():
        return "не установлен"
    try:
        saved = SPECS[component]["version_file"].read_text(encoding="ascii").strip()
    except OSError:
        saved = ""
    if re.fullmatch(r"v?\d+(?:\.\d+){1,3}", saved) or NIGHTLY_TAG.fullmatch(saved):
        return saved.removeprefix("v")
    commands = [[str(binary), "version"], [str(binary), "--version"], [str(binary), "-version"]]
    for command in commands:
        try:
            result = _run(command, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        value = (result.stdout + " " + result.stderr).strip().splitlines()
        if value:
            match = re.search(r"v?(\d+(?:\.\d+){1,3})", " ".join(value[:2]))
            if match:
                return match.group(1)
    return "установлен"


def _record_version(component, tag):
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = SPECS[component]["version_file"]
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(str(tag).removeprefix("v") + "\n", encoding="ascii")
    os.chmod(temporary, 0o600)
    temporary.replace(destination)


@contextmanager
def _action_lock():
    import fcntl
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = ACTION_LOCK.open("a")
    os.chmod(lock.name, 0o600)
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        lock.close()


def _unit_running():
    try:
        result = _run(["systemctl", "show", UNIT, "-p", "ActiveState", "--value"], timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.stdout.strip() in ("active", "activating", "reloading")


def _tags(component):
    spec = SPECS[component]
    result = _run(["curl", "-fsSL", "--retry", "2", "--retry-all-errors",
                   "--connect-timeout", "15", "--max-time", "45", spec["releases"]],
                  timeout=55)
    if result.returncode:
        raise ValueError("Страница релизов GitHub недоступна. Повторите позже.")
    release_link = re.compile(
        r'href="/' + re.escape(spec["release_path"]) + r'/releases/tag/([^"?#<]+)"', re.I
    )
    tags = {match.group(1) for match in release_link.finditer(result.stdout)
            if _valid_tag(component, match.group(1))}
    if not tags:
        raise ValueError("На странице GitHub Releases не найдены подходящие версии с Linux-сборкой.")
    return sorted(tags, key=lambda value: _tag_rank(component, value), reverse=True)[:30]


def _catalog(force=False):
    state = read_state(STATUS)
    cached = state.get("catalog", {})
    if state.get("phase") in ("queued", "running"):
        state.update(current={name: _current(name) for name in SPECS}, catalog=cached,
                     auto_update=auto_status())
        return state
    if force or time.time() - int(state.get("checked", 0)) > 300 or not cached:
        cached = {name: _tags(name) for name in SPECS}
        state.update(catalog=cached, checked=int(time.time()), phase="checked",
                     message="Версии компонентов загружены.")
        atomic_json(STATUS, state)
    state.update(current={name: _current(name) for name in SPECS}, catalog=cached,
                 phase=state.get("phase", "idle"), message=state.get("message", ""),
                 auto_update=auto_status())
    return state


def catalog(force=False):
    with _action_lock():
        return _catalog(force)


def status():
    state = read_state(STATUS)
    try:
        started = int(state.get("started", 0) or 0)
    except (TypeError, ValueError):
        started = 0
    if (state.get("phase") in ("queued", "running") and started and
            time.time() - started > 60 and not _unit_running()):
        state.update(phase="failed", finished=int(time.time()), progress=0,
                     error_title="Установка была прервана",
                     message="Компонент не обновлён: служба установки неожиданно остановилась.",
                     hint="Повторите установку. Текущая версия компонента сохранена.",
                     error_detail="Служба web-panel-proxy-component-update завершилась до окончания операции.")
        atomic_json(STATUS, state)
    state["current"] = {name: _current(name) for name in SPECS}
    state["auto_update"] = auto_status()
    return state


def auto_status():
    config = read_state(AUTO_CONFIG)
    enabled = config.get("enabled", True) is not False
    return {"enabled": enabled, "last_check": int(config.get("last_check", 0) or 0),
            "message": str(config.get("message") or
                           ("Автообновление компонентов включено." if enabled else
                            "Автообновление компонентов выключено."))}


def set_auto_update(enabled):
    enabled = bool(enabled)
    current = read_state(AUTO_CONFIG)
    current.update(enabled=enabled, changed=int(time.time()),
                   message=("Xray и OpenFlux будут проверяться автоматически." if enabled else
                            "Автообновление Xray и OpenFlux выключено."))
    atomic_json(AUTO_CONFIG, current)
    return auto_status()


def start(component, tag):
    if component not in SPECS or not _valid_tag(component, tag):
        raise ValueError("Некорректный компонент или версия.")
    with _action_lock():
        state = status()
        if state.get("phase") in ("queued", "running") or _unit_running():
            raise ValueError("Другая операция с компонентами уже выполняется.")
        info = _catalog()
        if tag not in info["catalog"].get(component, []):
            raise ValueError("Эта версия отсутствует среди опубликованных релизов GitHub.")
        state = read_state(STATUS)
        label = COMPONENT_LABELS[component]
        state.update(phase="queued", component=component, target=tag, started=int(time.time()),
                     finished=0, progress=2, step="Ожидание запуска",
                     message=f"{label} {_display_version(component, tag)} поставлен в очередь.",
                     error_title="", error_detail="", hint="")
        atomic_json(STATUS, state)
        result = _run(["systemctl", "start", "--no-block", UNIT], timeout=10)
        if result.returncode:
            state.update(phase="failed", progress=0, error_title="Не удалось запустить установку",
                         message=f"{label} не обновлён: системная служба установки не запустилась.",
                         hint="Повторите попытку. Если ошибка сохранится, перезапустите панель.",
                         error_detail=(result.stderr or result.stdout or "systemctl вернул ошибку")[-600:])
            atomic_json(STATUS, state)
            raise ValueError(state["message"])
        return status()


def _download(url, destination):
    result = _run(["curl", "-fL", "--retry", "3", "--retry-all-errors", "--connect-timeout", "20",
                   "--max-time", "300", "-o", str(destination), url], timeout=360)
    if result.returncode or not destination.is_file() or destination.stat().st_size < 100000:
        raise RuntimeError("Не удалось скачать выбранный релиз с GitHub.")


def _active_openflux_units():
    result = _run(["systemctl", "list-units", "--type=service", "--state=active", "--no-legend",
                   "web-panel-proxy-openflux*.service"])
    return [line.split()[0] for line in result.stdout.splitlines()
            if line.split() and re.fullmatch(r"web-panel-proxy-openflux(?:-[a-f0-9]{16})?\.service", line.split()[0])]


def _max_profiles_configured():
    """Return True when replacing OpenFlux must preserve the MAX transport."""
    if not OPENFLUX_PROFILES.is_dir():
        return False
    for config_path in OPENFLUX_PROFILES.glob("*/config.json"):
        try:
            import json
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(config, dict) and config.get("transport") == "oneme":
            return True
    return False


def _install(component, tag, directory, state):
    spec = SPECS[component]
    label = COMPONENT_LABELS[component]
    binary = spec["binary"]
    candidate = directory / "candidate"
    download = directory / "download"
    _progress(state, 12, "Загрузка файла", f"Скачиваем {label} {_display_version(component, tag)} с GitHub…", "running")
    _download(spec["asset"].format(tag=tag), download)
    _progress(state, 45, "Файл загружен", "Проверяем содержимое релиза…")
    if component == "xray":
        result = subprocess.run(["unzip", "-q", str(download), "xray", "-d", str(directory)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False)
        if result.returncode:
            raise RuntimeError("Не удалось распаковать Xray.")
        (directory / "xray").replace(candidate)
    else:
        download.replace(candidate)
    _progress(state, 58, "Проверка файла", "Проверяем формат и права запуска…")
    os.chmod(candidate, 0o755)
    if _run(["readelf", "-h", str(candidate)], timeout=10).returncode:
        raise RuntimeError("Загруженный файл не является исполняемым Linux-бинарником.")
    if component == "xray":
        _progress(state, 66, "Проверка совместимости", "Проверяем текущую конфигурацию Xray…")
        test = _run([str(candidate), "run", "-test", "-config", "/etc/web-panel-proxy-xray/config.json"], timeout=20)
        if test.returncode:
            raise RuntimeError("Выбранная версия Xray не принимает текущую конфигурацию.")
        active = [spec["service"]] if _run(["systemctl", "is-active", "--quiet", spec["service"]]).returncode == 0 else []
    else:
        _progress(state, 66, "Проверка совместимости", "Проверяем возможности новой версии OpenFlux…")
        test = _run([str(candidate), "--help"], timeout=10)
        if test.returncode not in (0, 1, 2):
            raise RuntimeError("Выбранный бинарник OpenFlux не запускается.")
        help_text = (test.stdout + "\n" + test.stderr).lower()
        if _max_profiles_configured() and not all(marker in help_text for marker in ("maxtoken", "maxuid", "oneme")):
            raise RuntimeError(
                "Выбранная версия OpenFlux не поддерживает MAX. "
                "Удалите MAX-профили или выберите более новый релиз."
            )
        active = _active_openflux_units()
    backup = directory / "previous"
    _progress(state, 74, "Резервная копия", "Сохраняем текущую рабочую версию для быстрого отката…")
    shutil.copy2(binary, backup)
    try:
        _progress(state, 82, "Подготовка служб", "Кратковременно останавливаем активные службы…")
        for service in active:
            _run(["systemctl", "stop", service], timeout=30)
        _progress(state, 88, "Установка файла", f"Устанавливаем {label} {_display_version(component, tag)}…")
        shutil.copy2(candidate, binary)
        os.chmod(binary, 0o755)
        _progress(state, 94, "Проверка запуска", "Запускаем службы и проверяем их состояние…")
        for service in active:
            result = _run(["systemctl", "restart", service], timeout=40)
            if result.returncode or _run(["systemctl", "is-active", "--quiet", service], timeout=10).returncode:
                raise RuntimeError("Служба не запустилась с выбранной версией.")
        _progress(state, 98, "Сохранение версии", "Записываем установленную версию…")
        _record_version(component, tag)
    except Exception:
        _progress(state, 96, "Автоматический откат", "Возвращаем предыдущую рабочую версию…")
        shutil.copy2(backup, binary)
        os.chmod(binary, 0o755)
        for service in active:
            _run(["systemctl", "restart", service], timeout=40)
        raise


def run():
    import fcntl
    lock_path = Path("/run/lock/web-panel-proxy.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        state = read_state(STATUS)
        state.update(phase="failed", progress=0, error_title="Сервер занят обновлением",
                     message="Новая установка не запущена: уже выполняется другая операция.",
                     hint="Дождитесь завершения текущей операции и повторите.",
                     error_detail="Глобальная блокировка обновления занята.")
        atomic_json(STATUS, state)
        return
    state = read_state(STATUS)
    component, tag = state.get("component"), state.get("target")
    if state.get("phase") != "queued" or component not in SPECS:
        raise SystemExit("No queued component update")
    label = COMPONENT_LABELS[component]
    _progress(state, 6, "Подготовка", f"Подготавливаем установку {label} {_display_version(component, tag)}…", "running")
    try:
        with tempfile.TemporaryDirectory(prefix="wpp-component-") as directory:
            _install(component, tag, Path(directory), state)
        state.update(phase="done", progress=100, step="Готово", error_title="", error_detail="", hint="",
                     message=f"{label} {_display_version(component, tag)} установлен. Службы работают нормально.")
    except Exception as exc:
        state.update(phase="failed", progress=0, step="Установка отменена",
                     **_friendly_failure(component, exc))
    state["finished"] = int(time.time())
    atomic_json(STATUS, state)


def run_auto_update():
    import fcntl
    config = read_state(AUTO_CONFIG)
    if config.get("enabled", True) is False:
        return
    lock_path = Path("/run/lock/web-panel-proxy.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    global_lock = lock_path.open("a")
    try:
        fcntl.flock(global_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        config.update(last_check=int(time.time()), message="Проверка отложена: выполняется другое обновление.")
        atomic_json(AUTO_CONFIG, config)
        return
    messages=[]
    with _action_lock():
        state=read_state(STATUS)
        if state.get("phase") in ("queued","running") or _unit_running():
            config.update(last_check=int(time.time()), message="Проверка отложена: компоненты уже обновляются.")
            atomic_json(AUTO_CONFIG, config)
            return
        for component in SPECS:
            try:
                tags=_tags(component)
                if not tags:
                    messages.append(component+": нет опубликованных версий")
                    continue
                latest=tags[0]; current=_current(component)
                if _tag_rank(component,current) >= _tag_rank(component,latest):
                    messages.append(COMPONENT_LABELS[component]+": актуальная версия "+_display_version(component,current))
                    continue
                label=COMPONENT_LABELS[component]
                state.update(phase="running",component=component,target=latest,started=int(time.time()),
                             finished=0,progress=6,step="Автоматическое обновление",
                             message=f"Подготавливаем автоматическое обновление {label} {_display_version(component, latest)}…",
                             error_title="",error_detail="",hint="")
                atomic_json(STATUS,state)
                with tempfile.TemporaryDirectory(prefix="wpp-component-auto-") as directory:
                    _install(component,latest,Path(directory),state)
                state.update(phase="done",finished=int(time.time()),progress=100,step="Готово",
                             message=f"{label} {_display_version(component, latest)} установлен автоматически.")
                atomic_json(STATUS,state)
                messages.append(label+": установлен "+_display_version(component,latest))
            except Exception as exc:
                state.update(phase="failed",finished=int(time.time()),progress=0,
                             step="Автоматическое обновление отменено",
                             **_friendly_failure(component,exc))
                atomic_json(STATUS,state)
                messages.append(component+": прежняя версия сохранена")
    config.update(enabled=True,last_check=int(time.time()),message="; ".join(messages))
    atomic_json(AUTO_CONFIG,config)


if __name__ == "__main__":
    command=os.sys.argv[1:]
    if command == ["run"]: run()
    elif command == ["auto"]: run_auto_update()
    else: raise SystemExit("Usage: wpp_components.py run|auto")
