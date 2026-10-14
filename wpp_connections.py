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
UNIT = "web-panel-proxy-component-update.service"
OPENFLUX_PROFILES = Path("/etc/web-proxy-panel/openflux/profiles")
SPECS = {
    "xray": {
        "repo": "https://github.com/XTLS/Xray-core.git",
        "asset": "https://github.com/XTLS/Xray-core/releases/download/{tag}/Xray-linux-64.zip",
        "binary": Path("/opt/web-panel-proxy/xray/xray"),
        "service": "web-panel-proxy-xray.service",
    },
    "openflux": {
        "repo": "https://github.com/damnurmum/OpenFlux-Android.git",
        "asset": "https://github.com/damnurmum/OpenFlux-Android/releases/download/{tag}/openflux-linux-amd64",
        "binary": Path("/opt/web-panel-proxy/openflux/openflux"),
        "service": "web-panel-proxy-openflux.service",
    },
}


def _run(args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, timeout=kwargs.pop("timeout", 30), check=False, **kwargs)


def _version_tuple(value):
    numbers = re.findall(r"\d+", str(value))
    return tuple(int(x) for x in numbers[:4])


def _current(component):
    binary = SPECS[component]["binary"]
    if not binary.is_file():
        return "не установлен"
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
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    result = _run(["git", "ls-remote", "--tags", "--refs", SPECS[component]["repo"], "v[0-9]*"],
                  timeout=25, env=env)
    if result.returncode:
        raise ValueError("GitHub недоступен. Повторите позже.")
    tags = set(re.findall(r"refs/tags/(v\d+(?:\.\d+){1,3})\s*$", result.stdout, re.M))
    return sorted(tags, key=_version_tuple, reverse=True)[:30]


def _catalog(force=False):
    state = read_state(STATUS)
    cached = state.get("catalog", {})
    if state.get("phase") in ("queued", "running"):
        return {"current": {name: _current(name) for name in SPECS}, "catalog": cached,
                "phase": state.get("phase"), "message": state.get("message", "")}
    if force or time.time() - int(state.get("checked", 0)) > 300 or not cached:
        cached = {name: _tags(name) for name in SPECS}
        state.update(catalog=cached, checked=int(time.time()), phase="checked",
                     message="Версии компонентов загружены.")
        atomic_json(STATUS, state)
    return {"current": {name: _current(name) for name in SPECS}, "catalog": cached,
            "phase": state.get("phase", "idle"), "message": state.get("message", "")}


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
        state.update(phase="failed", finished=int(time.time()),
                     message="Операция была прервана. Повторите её или проверьте журнал службы.")
        atomic_json(STATUS, state)
    state["current"] = {name: _current(name) for name in SPECS}
    return state


def start(component, tag):
    if component not in SPECS or not re.fullmatch(r"v\d+(?:\.\d+){1,3}", str(tag)):
        raise ValueError("Некорректный компонент или версия.")
    with _action_lock():
        state = status()
        if state.get("phase") in ("queued", "running") or _unit_running():
            raise ValueError("Другая операция с компонентами уже выполняется.")
        info = _catalog()
        if tag not in info["catalog"].get(component, []):
            raise ValueError("Эта версия отсутствует среди опубликованных релизов GitHub.")
        state = read_state(STATUS)
        state.update(phase="queued", component=component, target=tag, started=int(time.time()),
                     message=f"Подготовка {component} {tag}…")
        atomic_json(STATUS, state)
        result = _run(["systemctl", "start", "--no-block", UNIT], timeout=10)
        if result.returncode:
            state.update(phase="failed", message="Не удалось запустить обновление компонента.")
            atomic_json(STATUS, state)
            raise ValueError(state["message"])
        return state


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


def _install(component, tag, directory):
    spec = SPECS[component]
    binary = spec["binary"]
    candidate = directory / "candidate"
    download = directory / "download"
    _download(spec["asset"].format(tag=tag), download)
    if component == "xray":
        result = subprocess.run(["unzip", "-q", str(download), "xray", "-d", str(directory)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False)
        if result.returncode:
            raise RuntimeError("Не удалось распаковать Xray.")
        (directory / "xray").replace(candidate)
    else:
        download.replace(candidate)
    os.chmod(candidate, 0o755)
    if _run(["readelf", "-h", str(candidate)], timeout=10).returncode:
        raise RuntimeError("Загруженный файл не является исполняемым Linux-бинарником.")
    if component == "xray":
        test = _run([str(candidate), "run", "-test", "-config", "/etc/web-panel-proxy-xray/config.json"], timeout=20)
        if test.returncode:
            raise RuntimeError("Выбранная версия Xray не принимает текущую конфигурацию.")
        active = [spec["service"]] if _run(["systemctl", "is-active", "--quiet", spec["service"]]).returncode == 0 else []
    else:
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
    shutil.copy2(binary, backup)
    try:
        for service in active:
            _run(["systemctl", "stop", service], timeout=30)
        shutil.copy2(candidate, binary)
        os.chmod(binary, 0o755)
        for service in active:
            result = _run(["systemctl", "restart", service], timeout=40)
            if result.returncode or _run(["systemctl", "is-active", "--quiet", service], timeout=10).returncode:
                raise RuntimeError("Служба не запустилась с выбранной версией.")
    except Exception:
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
        state.update(phase="failed", message="Установка или другое обновление уже выполняется.")
        atomic_json(STATUS, state)
        return
    state = read_state(STATUS)
    component, tag = state.get("component"), state.get("target")
    if state.get("phase") != "queued" or component not in SPECS:
        raise SystemExit("No queued component update")
    state.update(phase="running", message=f"Устанавливается {component} {tag}. Создана резервная копия.")
    atomic_json(STATUS, state)
    try:
        with tempfile.TemporaryDirectory(prefix="wpp-component-") as directory:
            _install(component, tag, Path(directory))
        state.update(phase="done", message=f"{component} {tag} установлен. Проверка службы пройдена.")
    except Exception as exc:
        state.update(phase="failed", message="Изменение отменено: " + str(exc)[:600])
    state["finished"] = int(time.time())
    atomic_json(STATUS, state)


if __name__ == "__main__":
    if os.sys.argv[1:] != ["run"]:
        raise SystemExit("Usage: wpp_components.py run")
    run()
