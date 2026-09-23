"""Admin-triggered, fixed-repository updates run outside the panel's cgroup."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from wpp_metrics import atomic_json, read_state

ROOT = Path('/var/lib/web-panel-proxy-update')
STATUS = ROOT / 'status.json'
VERSION = Path('/etc/web-proxy-panel/version')
UNIT = 'web-panel-proxy-web-update.service'
REPO = 'https://github.com/POLESNIESOVETI12/web-panel-proxy.git'
UPDATER = '/usr/local/sbin/web-panel-proxy-update'


def current_version():
    try: return VERSION.read_text(encoding='ascii').strip()
    except OSError: return 'unknown'


def version_tuple(value):
    m = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:[~.-](rc\d+))?', value)
    if not m: return None
    return (*map(int, m.group(1, 2, 3)), 0 if m[4] else 1, int(m[4][2:]) if m[4] else 0)


def newer(tag, current):
    a, b = version_tuple(tag), version_tuple(current)
    return bool(a and b and a > b and re.fullmatch(r'v\d+\.\d+\.\d+', tag))


def unit_running():
    r = subprocess.run(['systemctl', 'show', UNIT, '-p', 'ActiveState', '--value'], capture_output=True, text=True, timeout=5)
    return r.stdout.strip() in ('active', 'activating', 'reloading')


def get_status():
    data = read_state(STATUS)
    # A killed/rebooted updater cannot remain "running" forever in the UI.
    if data.get('phase') in ('running', 'queued') and time.time() - data.get('started', 0) > 60:
        try:
            if not unit_running(): data.update(phase='interrupted', message='Обновление прервано. Проверьте журнал через WPP/SSH.')
        except (OSError, subprocess.TimeoutExpired): pass
    data['current'] = current_version()
    data['available'] = newer(data.get('latest', ''), data['current'])
    return data


def _lock(blocking=False):
    import fcntl
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (ROOT / 'action.lock').open('a')
    os.chmod(lock.name, 0o600)
    try: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except BlockingIOError:
        lock.close()
        raise ValueError('Другая операция обновления уже выполняется.')
    return lock


def check_release():
    with _lock():
        state = get_status()
        if state.get('phase') in ('running', 'queued'): return state
        if time.time() - state.get('checked', 0) < 60: return state
        env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
        try:
            r = subprocess.run(['git', 'ls-remote', '--tags', '--refs', REPO, 'v[0-9]*'], capture_output=True, text=True, timeout=20, env=env)
            if r.returncode: raise ValueError('GitHub недоступен. Повторите позже.')
            tags = re.findall(r'refs/tags/(v\d+\.\d+\.\d+)\s*$', r.stdout, re.M)
            if not tags: raise ValueError('Опубликованные стабильные теги не найдены.')
            latest = max(tags, key=version_tuple)
            state.update(latest=latest, checked=int(time.time()), phase='checked', message='Проверка завершена.')
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('Не удалось проверить GitHub. Повторите позже.')
        atomic_json(STATUS, state)
        return get_status()


def start_update():
    with _lock():
        state = get_status()
        if state.get('phase') in ('running', 'queued') or unit_running():
            raise ValueError('Обновление уже выполняется.')
        if time.time() - state.get('checked', 0) > 600 or not state.get('available'):
            raise ValueError('Сначала проверьте обновления. Нужен новый опубликованный стабильный релиз.')
        state.update(phase='queued', target=state['latest'], started=int(time.time()), message='Обновление запускается. Панель временно отключится.')
        atomic_json(STATUS, state)
        r = subprocess.run(['systemctl', 'start', '--no-block', UNIT], capture_output=True, text=True, timeout=10)
        if r.returncode:
            state.update(phase='failed', message='Не удалось запустить службу обновления.')
            atomic_json(STATUS, state)
            raise ValueError(state['message'])
        return state


def run_update():
    # The service may start before the HTTP request releases action.lock.
    with _lock(blocking=True):
        state = read_state(STATUS)
        tag = state.get('target', '')
        if state.get('phase') != 'queued' or time.time() - state.get('started', 0) > 120 or not newer(tag, current_version()):
            raise ValueError('Нет подтверждённого нового релиза для установки.')
        state.update(phase='running', message='Создание резервной копии и обновление. Подождите несколько минут.')
        atomic_json(STATUS, state)
    # Preserve status outside panel backup paths; run in a separate systemd unit.
    env = {**os.environ, 'WEB_PANEL_PROXY_REF': tag, 'GIT_TERMINAL_PROMPT': '0', 'DEBIAN_FRONTEND': 'noninteractive'}
    try:
        with (ROOT / 'update.log').open('w', encoding='utf-8') as log:
            os.chmod(log.name, 0o600)
            result = subprocess.run(['/usr/bin/bash', UPDATER], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env)
        state.update(phase='done' if result.returncode == 0 else 'failed', code=result.returncode,
                     message='Обновление завершено. Войдите в панель заново.' if result.returncode == 0 else 'Обновление завершилось ошибкой. Проверьте службы и /var/lib/web-panel-proxy-update/update.log через SSH.')
    except OSError:
        state.update(phase='failed', message='Не удалось выполнить установщик. Проверьте журнал через SSH.')
    state['finished'] = int(time.time())
    atomic_json(STATUS, state)


if __name__ == '__main__':
    if sys.argv[1:] != ['run']: raise SystemExit('Usage: wpp_update.py run')
    run_update()
