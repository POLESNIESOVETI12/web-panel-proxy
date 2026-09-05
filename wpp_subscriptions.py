"""Local subscription/device registry. HWID is client-supplied, not attested hardware."""
import copy
import hashlib
import re
import secrets
import time
import uuid

PREFIX = '/wpp-sub/'
MAX_ACCOUNTS = 64
MAX_PROFILES = 256


class SubscriptionError(ValueError):
    def __init__(self, message, status=400, code='invalid'):
        super().__init__(message)
        self.status, self.code = status, code


def accounts(data):
    value = data.setdefault('subscriptions', [])
    if not isinstance(value, list):
        raise SubscriptionError('Повреждён реестр подписок.', 503, 'storage')
    return value


def find_account(data, sid):
    value = next((s for s in accounts(data) if s['id'] == sid), None)
    if value is None:
        raise SubscriptionError('Подписка не найдена.', 404, 'not_found')
    return value


def clean_name(value, maximum=80):
    value = str(value).strip()
    if not value or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise SubscriptionError('Укажите имя длиной от 1 до %d символов.' % maximum)
    return value


def limits(value):
    try:
        number = int(value)
    except (ValueError, TypeError):
        raise SubscriptionError('Лимит должен быть целым числом от 0 до 20.')
    if not 0 <= number <= 20:
        raise SubscriptionError('Лимит должен быть от 0 до 20; 0 — без HWID-привязки.')
    return number


def protocols(value):
    supported = ('vless', 'hysteria')
    if not isinstance(value, list) or not value or any(p not in supported for p in value):
        raise SubscriptionError('Выберите хотя бы один поддерживаемый протокол.')
    return list(dict.fromkeys(value))


def drop_profiles(data, sid, did=None):
    data['users'] = [u for u in data['users'] if not (
        u.get('subscription_id') == sid and (did is None or u.get('device_id') == did))]


def provision(data, sub, device):
    for protocol in sub['protocols']:
        if any(u.get('subscription_id') == sub['id'] and u.get('device_id') == device['id']
               and u['protocol'] == protocol for u in data['users']):
            continue
        if sum(bool(u.get('subscription_id')) for u in data['users']) >= MAX_PROFILES:
            raise SubscriptionError('Достигнут общий лимит профилей подписок.', 503, 'capacity')
        uid = secrets.token_hex(8)
        sub.setdefault('profile_ids', []).append(uid)
        ports = {'vless': 443, 'hysteria': 8443}
        profile = {
            'id': uid, 'name': sub['name'] + ' / ' + device['name'],
            'protocol': protocol, 'enabled': sub['enabled'], 'secret': str(uuid.uuid4()),
            'backend_port': ports[protocol],
            'subscription_id': sub['id'], 'device_id': device['id'],
        }
        data['users'].append(profile)


def mutate(data, request):
    """Pure state transition. Caller serializes and commits config + data atomically."""
    data = copy.deepcopy(data)
    operation = request.get('operation')
    if operation == 'create':
        if len(accounts(data)) >= MAX_ACCOUNTS:
            raise SubscriptionError('Достигнут лимит подписок.')
        sub = {'id': secrets.token_hex(8), 'token': secrets.token_hex(32),
               'name': clean_name(request.get('name', '')), 'enabled': True,
               'max_devices': limits(request.get('max_devices', 1)),
               'protocols': protocols(request.get('protocols')), 'devices': [],
               'created_at': int(time.time())}
        accounts(data).append(sub)
    else:
        sub = find_account(data, request.get('id'))
        if operation == 'delete':
            drop_profiles(data, sub['id'])
            data['subscriptions'].remove(sub)
            return data, {'ok': True}
        if operation == 'update':
            limit = limits(request.get('max_devices', sub['max_devices']))
            active = [d for d in sub['devices'] if not d.get('revoked')]
            if bool(limit) != bool(sub['max_devices']):
                # Switching to/from a shared subscription must revoke the old keys.
                drop_profiles(data, sub['id'])
                sub['devices'] = []
            elif limit and len(active) > limit:
                raise SubscriptionError('Сначала отзовите лишние устройства, затем уменьшите лимит.')
            sub['name'] = clean_name(request.get('name', sub['name']))
            sub['max_devices'] = limit
            sub['protocols'] = protocols(request.get('protocols', sub['protocols']))
            data['users'] = [u for u in data['users'] if u.get('subscription_id') != sub['id']
                             or u['protocol'] in sub['protocols']]
            for device in sub['devices']:
                if not device.get('revoked'):
                    provision(data, sub, device)
            for user in data['users']:
                if user.get('subscription_id') == sub['id']:
                    device = next(d for d in sub['devices'] if d['id'] == user['device_id'])
                    user['name'] = sub['name'] + ' / ' + device['name']
        elif operation in ('toggle', 'set-enabled'):
            if operation == 'set-enabled' and not isinstance(request.get('enabled'), bool):
                raise SubscriptionError('Некорректное состояние доступа.')
            sub['enabled'] = request['enabled'] if operation == 'set-enabled' else not sub['enabled']
            for user in data['users']:
                if user.get('subscription_id') == sub['id']:
                    user['enabled'] = sub['enabled']
        elif operation == 'rotate':
            sub['token'] = secrets.token_hex(32)
            sub['devices'] = []
            drop_profiles(data, sub['id'])
        elif operation in ('revoke', 'allow'):
            device = next((d for d in sub['devices'] if d['id'] == request.get('device_id')), None)
            if device is None:
                raise SubscriptionError('Устройство не найдено.', 404, 'not_found')
            drop_profiles(data, sub['id'], device['id'])
            if operation == 'revoke':
                device['revoked'] = True
            elif not device.get('revoked'):
                raise SubscriptionError('Устройство уже разрешено.')
            else:
                sub['devices'].remove(device)
        else:
            raise SubscriptionError('Неизвестная операция.')
    return data, {'ok': True, 'id': sub['id']}


def issue(data, request):
    """Issue per-device keys on first fetch, or return the same existing keys."""
    data = copy.deepcopy(data)
    token = str(request.get('token', ''))
    if not re.fullmatch(r'[a-f0-9]{64}', token):
        raise SubscriptionError('Подписка не найдена.', 404, 'not_found')
    sub = next((s for s in accounts(data) if secrets.compare_digest(s['token'], token)), None)
    if sub is None or not sub['enabled']:
        raise SubscriptionError('Подписка не найдена или отключена.', 404, 'not_found')
    if sub['max_devices']:
        hwid = str(request.get('hwid', '')).strip()
        if not 8 <= len(hwid) <= 256 or any(ord(c) < 33 or ord(c) > 126 for c in hwid):
            raise SubscriptionError('Нужен клиент, передающий X-HWID (например, Happ).', 403, 'hwid_required')
        # Do not store raw HWIDs; salt by account so records cannot be correlated.
        digest = hashlib.sha256((sub['id'] + ':' + hwid).encode()).hexdigest()
    else:
        digest = 'shared'
    device = next((d for d in sub['devices'] if d['hwid_hash'] == digest), None)
    if device is not None and device.get('revoked'):
        raise SubscriptionError('Доступ этого устройства отозван.', 403, 'revoked')
    if device is None:
        active = sum(not d.get('revoked') for d in sub['devices'])
        if sub['max_devices'] and active >= sub['max_devices']:
            raise SubscriptionError('Лимит устройств исчерпан. Обратитесь к администратору.', 403, 'device_limit')
        if len(sub['devices']) >= 100:
            raise SubscriptionError('Нужна ротация подписки: слишком много отозванных устройств.', 403, 'device_limit')
        did = secrets.token_hex(8)
        device = {'id': did, 'hwid_hash': digest,
                  'name': 'Общий профиль' if digest == 'shared' else 'Устройство ' + did[:6],
                  'created_at': int(time.time()), 'last_seen': 0, 'revoked': False}
        sub['devices'].append(device)
        provision(data, sub, device)
    device['last_seen'] = int(time.time())
    users = [u for u in data['users'] if u.get('subscription_id') == sub['id']
             and u.get('device_id') == device['id'] and u.get('enabled', True)]
    if len(users) != len(sub['protocols']):
        raise SubscriptionError('Профили устройства повреждены. Обратитесь к администратору.', 503, 'storage')
    return data, {'ok': True, 'name': sub['name'], 'limited': bool(sub['max_devices']), 'users': users}
