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
AUTO_CONFIG = ROOT / 'auto-update.json'
VERSION = Path('/etc/web-proxy-panel/version')
UNIT = 'web-panel-proxy-web-update.service'
REPO = 'https://github.com/POLESNIESOVETI12/web-panel-proxy.git'
UPDATER = '/usr/local/sbin/web-panel-proxy-update'


def failure_message(log_path):
    """Return a safe, useful diagnosis without exposing URLs or credentials."""
    try:
        tail = log_path.read_text(encoding='utf-8', errors='replace')[-24000:].lower()
    except OSError:
        return 'Обновление завершилось ошибкой. Журнал обновления недоступен.'
    stages={'read':'чтение файлов настроек','atomic':'сохранение настроек','version':'проверка номера версии',
            'capabilities':'чтение формата выбранного релиза','legacy_capabilities':'чтение встроенного менеджера старой версии','collection':'проверка структуры записей',
            'merge':'объединение сохранённых настроек','merge_document':'восстановление записей',
            'supported':'проверка параметров подключения','policy_supported':'проверка ограничений доступа',
            'subscription_specs':'перенос подключений подписки','prepare':'подготовка данных','finalize':'сохранение данных восстановления',
            'prepare_caddy':'переключение HTTPS со шлюза SmartDNS на выбранную версию'}
    for line in reversed(tail.splitlines()):
        if not line.startswith('wpp_data_error ') or len(line)>1024:continue
        try:data=json.loads(line.split(' ',1)[1])
        except (ValueError,TypeError):continue
        if isinstance(data,dict) and isinstance(data.get('stage'),str) and data['stage'] in stages and data.get('error') in ('oserror','valueerror','keyerror','typeerror','syntaxerror','indexerror'):
            return 'Не удалось подготовить настройки: '+stages[data['stage']]+'. Предыдущее состояние восстановлено. Проверьте журнал через WPP или SSH.'
    cases = (
        (('could not read the selected release tag from github',),
         'Не удалось получить выбранный тег из GitHub. Проверьте наличие версии и доступ к репозиторию.'),
        (('downloaded release does not match the github tag', 'github release commit is missing or invalid', 'reference is not a tree'),
         'Не удалось подтвердить коммит выбранного GitHub-релиза. Файлы релиза не установлены; повторите загрузку.'),
        (('testloadacceptssystemdcredentialreadpermissions', 'group/other-readable profiles file outside a credential directory was accepted'),
         'Сбой теста relay из-за режима создания временных файлов. Обновите установщик с исправлением umask.'),
        (('ssl connection timeout', 'connection timed out', 'could not resolve host'),
         'Не удалось скачать файлы релиза: GitHub недоступен или соединение прервано.'),
        (('openflux-linux-amd64 is missing', 'missing assets/openflux-linux-amd64', 'openflux checksum verification failed'),
         'Пакет OpenFlux отсутствует или повреждён.'),
        (('xray checksum verification failed',), 'Архив Xray не прошёл проверку целостности.'),
        (('port 80 is occupied', 'port 443 is occupied', 'port 8080 is occupied'),
         'Один из обязательных портов занят другой программой.'),
        (('caddy reload failed', 'could not configure caddy', 'caddy diagnostic'),
         'Не удалось применить конфигурацию HTTPS/Caddy.'),
        (('no supported web proxy installation was found',),
         'Установленная версия панели не распознана безопасным обновлением.'),
    )
    for needles, message in cases:
        if any(needle in tail for needle in needles): return message
    return 'Обновление завершилось ошибкой. Откройте журнал через WPP или SSH.'


def current_version():
    try: return VERSION.read_text(encoding='ascii').strip()
    except OSError: return 'unknown'


def version_tuple(value):
    m = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:(?:-connections)?[~.-](alpha|beta|rc)(\d+))?', value)
    if not m: return None
    return (*map(int, m.group(1, 2, 3)), {'alpha':0,'beta':1,'rc':2}.get(m[4],3), int(m[5] or 0))


def _timestamp(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def newer(tag, current):
    a, b = version_tuple(tag), version_tuple(current)
    return bool(a and b and a > b and re.fullmatch(r'v\d+\.\d+\.\d+', tag))


def record_cli_success(release_source=None):
    """Called only after the CLI updater has committed and checked the install."""
    with _lock(blocking=True):
        previous=read_state(STATUS)
        if previous.get('phase') in ('queued','running'):
            return False  # Never overwrite a concurrent web job.
        installed=current_version()
        if not version_tuple(installed):raise ValueError('Installed version is invalid.')
        state={key:previous[key] for key in ('latest','releases','checked') if key in previous}
        if previous.get('phase') in ('failed','interrupted','done','blocked'):
            state['previous_attempt']={key:previous[key] for key in ('target','phase','progress','step','message','started','finished','current','source_version') if key in previous}
        state.update(phase='done',target='v'+installed.lstrip('v'),current=installed,progress=100,
                     step='Установка завершена',finished=int(time.time()),automatic=False,source='cli',
                     message='Установка завершена. Текущая версия: '+installed+'.')
        if isinstance(release_source,dict):
            release_source_event(state,'WPP_RELEASE_SOURCE GitHub '+str(release_source.get('tag',''))+' '+str(release_source.get('commit','')))
        atomic_json(STATUS,state)
        return True


def auto_status():
    value = read_state(AUTO_CONFIG)
    return {
        'enabled': value.get('enabled') is True,
        'last_checked': _timestamp(value.get('last_checked')),
        'last_started': _timestamp(value.get('last_started')),
        'blocked_target': str(value.get('blocked_target', '')),
        'message': str(value.get('message', 'Автообновление выключено.')),
    }


def set_auto_update(enabled):
    value = auto_status()
    value.update(enabled=bool(enabled), changed=int(time.time()), blocked_target='',
                 message='Автообновление включено. Проверка стабильных релизов выполняется каждые 6 часов.'
                         if enabled else 'Автообновление выключено.')
    atomic_json(AUTO_CONFIG, value)
    return value


def unit_running():
    r = subprocess.run(['systemctl', 'show', UNIT, '-p', 'ActiveState', '--value'], capture_output=True, text=True, timeout=5)
    return r.stdout.strip() in ('active', 'activating', 'reloading')


def release_source_event(state,line):
    if len(line)>512:return False
    match=re.fullmatch(r'WPP_RELEASE_SOURCE GitHub (v\d+\.\d+\.\d+(?:[.-][A-Za-z0-9.-]+)?) ([a-f0-9]{40})\s*',line)
    if not match or state.get('target') not in (None,'',match[1]):return False
    state['release_source']={'provider':'github','repository':REPO,'tag':match[1],'commit':match[2]}
    return True


def progress_event(state, line):
    """Accept only updater markers; never expose arbitrary installation log text."""
    match = re.fullmatch(r'WPP_PANEL_PROGRESS ([0-9]{1,3}) [^\r\n]*\r?\n?', line)
    if not match: return False
    stages = {0:'Проверка установки',15:'Подготовка временного отката',25:'Получение файлов',
              40:'Проверка файлов',50:'Проверка выбранной версии',60:'Установка панели',
              90:'Проверка служб',96:'Проверка компонентов',100:'Завершение установки'}
    value = int(match[1])
    if value not in stages or value < int(state.get('progress',0)): return False
    state.update(progress=min(99,value),step=stages[value],message=stages[value])
    return True


def version_data_event(state,line):
    if not line.startswith('WPP_VERSION_DATA ') or len(line)>4096:return False
    try:data=json.loads(line.split(' ',1)[1])
    except (ValueError,TypeError):return False
    if not isinstance(data,dict):return False
    if any(type(data.get(key)) is not int or not 0<=data[key]<=1000000 for key in ('suspended_records','conflicts')) or type(data.get('recovered')) is not bool:return False
    state['version_data']={key:data[key] for key in ('suspended_records','recovered','conflicts')}
    state['message']='Данные подготовлены для выбранной версии. Неподдерживаемые подключения сохранены для восстановления.' if data['suspended_records'] else 'Данные подготовлены для выбранной версии.'
    return True


def compatibility_event(state,line):
    if not line.startswith('WPP_UPDATE_INCOMPATIBLE ') or len(line)>4096:return False
    try:data=json.loads(line.split(' ',1)[1])
    except (ValueError,TypeError):return False
    reasons={'unsupported-connections':'Выбранная версия не поддерживает сохранённые подключения.',
             'unsupported-schema':'Выбранная версия не поддерживает формат текущего реестра подключений.',
             'unknown-capabilities':'Невозможно подтвердить поддержку текущих подключений выбранной версией.',
             'invalid-connections':'Не удалось проверить реестр подключений.',
             'invalid-users':'Не удалось проверить данные пользователей.'}
    if not isinstance(data,dict) or data.get('reason') not in reasons:return False
    for field in ('protocols','providers','features'):
        values=data.get(field,[])
        if not isinstance(values,list) or len(values)>32 or any(not isinstance(v,str) or not re.fullmatch(r'[a-z0-9_-]{1,32}',v) for v in values):return False
    labels={'awg20':'AWG 2.0','awg31':'AWG 3.1','naive':'NaiveProxy','mieru':'Mieru','vless':'VLESS','hysteria':'Hysteria2','web':'Web Proxy','mtproto':'MTProto','cascade':'мост','ssh':'RU-вход по SSH'}
    details=[]
    for field,title in (('protocols','Протоколы'),('providers','CDN'),('features','Функции')):
        if data.get(field):details.append(title+': '+', '.join(labels.get(v,v) for v in data[field])+'.')
    state['compatibility']={key:data.get(key,[]) for key in ('protocols','providers','features')}
    state['compatibility']['reason']=data['reason']
    state['compatibility_message']=' '.join([reasons[data['reason']],*details,'Установка отменена до изменения подключений. Выберите совместимую версию.'])
    return True


def get_status():
    data = read_state(STATUS)
    # A killed/rebooted updater cannot remain "running" forever in the UI.
    if data.get('phase') in ('running', 'queued') and time.time() - _timestamp(data.get('started')) > 60:
        try:
            if not unit_running(): data.update(phase='interrupted', message='Обновление прервано. Проверьте журнал через WPP/SSH.')
        except (OSError, subprocess.TimeoutExpired): pass
    data['current'] = current_version()
    data['available'] = newer(data.get('latest', ''), data['current'])
    data['can_install'] = bool(data.get('releases'))
    data['auto_update'] = auto_status()
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
        if time.time() - _timestamp(state.get('checked')) < 60: return state
        env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
        try:
            r = subprocess.run(['git', 'ls-remote', '--tags', '--refs', REPO, 'v[0-9]*'], capture_output=True, text=True, timeout=20, env=env)
            if r.returncode: raise ValueError('GitHub недоступен. Повторите позже.')
            tags = re.findall(r'refs/tags/(v\d+\.\d+\.\d+)\s*$', r.stdout, re.M)
            if not tags: raise ValueError('Опубликованные стабильные теги не найдены.')
            tags = sorted(set(tags), key=version_tuple, reverse=True)[:30]
            latest = tags[0]
            state.update(latest=latest, releases=tags, checked=int(time.time()))
            if state.get('phase') not in ('done','failed','interrupted','blocked'):
                state.update(phase='checked',message='Версии загружены.')
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('Не удалось проверить GitHub. Повторите позже.')
        atomic_json(STATUS, state)
        return get_status()


def start_update(target='', automatic=False):
    with _lock():
        state = get_status()
        if state.get('phase') in ('running', 'queued') or unit_running():
            raise ValueError('Обновление уже выполняется.')
        releases=state.get('releases',[])
        target=str(target or state.get('latest',''))
        if time.time() - _timestamp(state.get('checked')) > 600 or target not in releases:
            raise ValueError('Сначала обновите список и выберите опубликованный стабильный релиз.')
        if target.lstrip('v') == current_version().lstrip('v'):
            raise ValueError('Эта версия уже установлена.')
        target_version, installed_version = version_tuple(target), version_tuple(current_version())
        action = 'Откат' if target_version and installed_version and target_version < installed_version else 'Обновление'
        state.pop('compatibility',None);state.pop('compatibility_message',None)
        state.pop('version_data',None)
        state.pop('release_source',None)
        state.update(phase='queued', target=target,source_version=current_version(), automatic=bool(automatic), started=int(time.time()),progress=0,step='Ожидание запуска',
                     message=action+' запускается. Панель временно отключится.')
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
        if (state.get('phase') != 'queued' or time.time() - _timestamp(state.get('started')) > 120 or
                tag not in state.get('releases',[]) or not version_tuple(tag) or tag.lstrip('v')==current_version().lstrip('v')):
            raise ValueError('Нет подтверждённого релиза для установки.')
        state.pop('compatibility',None);state.pop('compatibility_message',None)
        state.pop('version_data',None)
        state.pop('release_source',None)
        state.update(phase='running',progress=0,step='Подготовка установки', message='Подготовка временного отката и обновление. Подождите несколько минут.')
        atomic_json(STATUS, state)
    # Preserve status outside panel backup paths; run in a separate systemd unit.
    env = {**os.environ, 'WEB_PANEL_PROXY_REF': tag, 'GIT_TERMINAL_PROMPT': '0', 'DEBIAN_FRONTEND': 'noninteractive','WPP_WEB_UPDATE':'1'}
    try:
        with (ROOT / 'update.log').open('w', encoding='utf-8') as log:
            os.chmod(log.name, 0o600)
            with subprocess.Popen(['/usr/bin/bash', UPDATER],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT,env=env,text=True,encoding='utf-8',errors='replace',bufsize=1) as process:
                for line in process.stdout:
                    log.write(line);log.flush()
                    if progress_event(state,line) or release_source_event(state,line) or version_data_event(state,line) or compatibility_event(state,line): atomic_json(STATUS,state)
                code=process.wait()
        blocked=code==78 and bool(state.get('compatibility'))
        state.update(phase='done' if code == 0 else 'blocked' if blocked else 'failed', code=code,
                     message='Обновление завершено. Войдите в панель заново.' if code == 0 else state['compatibility_message'] if blocked else failure_message(ROOT / 'update.log'))
        if blocked:state['step']='Несовместимая версия · установка отменена'
        if code == 0:state.update(progress=100,step='Установка завершена')
        if code == 0 and state.get('version_data',{}).get('suspended_records'):
            state['message']='Установка завершена. Подключения, которых нет в выбранной версии, сохранены вместе с ключами. Они восстановятся при переходе на версию, которая поддерживает их настройки.'
        if code == 0 and state.get('version_data',{}).get('conflicts'):
            state['message']+=' Совпавшие идентификаторы не перезаписаны; исходные записи сохранены отдельно.'
        if code == 0 and state.get('release_source'):
            state['message']+=' Источник: GitHub · '+state['release_source']['tag']+'.'
    except OSError:
        state.update(phase='failed', message='Не удалось выполнить установщик. Проверьте журнал через SSH.')
    state['finished'] = int(time.time())
    atomic_json(STATUS, state)
    if state.get('automatic'):
        config = auto_status()
        if state.get('phase') == 'done':
            config.update(blocked_target='', message='Автоматическое обновление успешно установлено.')
        else:
            config.update(blocked_target=tag,
                          message='Автообновление не установлено и не будет повторяться для '+tag+'.')
        atomic_json(AUTO_CONFIG, config)


def run_auto_update():
    """Install only a newer stable tag when the administrator opted in."""
    config = auto_status()
    if not config['enabled']:
        return
    now = int(time.time())
    try:
        state = check_release()
        config.update(last_checked=now, message='Новых стабильных версий пока нет.')
        target = state.get('latest', '')
        if newer(target, current_version()):
            if config.get('blocked_target') == target:
                config.update(message='Версия '+target+' пропущена после неудачной установки. Дождитесь следующего релиза или установите её вручную.')
            else:
                start_update(target, automatic=True)
                config.update(last_started=now, message='Новая стабильная версия '+target+' поставлена в очередь.')
    except ValueError as exc:
        config.update(last_checked=now, message='Автопроверка: '+str(exc))
    except (OSError, subprocess.TimeoutExpired):
        config.update(last_checked=now, message='Автопроверка временно недоступна.')
    atomic_json(AUTO_CONFIG, config)


if __name__ == '__main__':
    if sys.argv[1:] == ['run']:
        run_update()
    elif sys.argv[1:] == ['auto']:
        run_auto_update()
    else:
        raise SystemExit('Usage: wpp_update.py run|auto')
