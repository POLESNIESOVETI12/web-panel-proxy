"""Secure WPP node registry, API authentication and federation client."""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request

API_PREFIX = '/wpp-api/v1'
CONNECTION_TOKEN_PREFIX = 'wppnode1_'
MAX_NODES = 16
MAX_RESPONSE = 1024 * 1024
GEO_RESPONSE_LIMIT = 64 * 1024


class NodeError(ValueError):
    pass


def atomic_json(path, value):
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.wpp-nodes-', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def ensure_api_key(path):
    try:
        with open(path, encoding='ascii') as stream:
            value = stream.read().strip()
    except FileNotFoundError:
        value = ''
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}', value):
        value = secrets.token_urlsafe(32)
        directory = os.path.dirname(path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='ascii') as stream:
            stream.write(value + '\n')
        os.chmod(path, 0o600)
    return value


def make_connection_token(domain, key):
    domain = str(domain or '').strip().lower()
    url = normalize_url(domain if '://' in domain else 'https://' + domain)
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}', str(key or '')):
        raise NodeError('Некорректный ключ API ноды.')
    packed = json.dumps({'url': url, 'key': key}, ensure_ascii=True,
                        separators=(',', ':')).encode('utf-8')
    return CONNECTION_TOKEN_PREFIX + base64.urlsafe_b64encode(packed).decode('ascii').rstrip('=')


def parse_connection_token(value):
    value = str(value or '').strip()
    if not value.startswith(CONNECTION_TOKEN_PREFIX):
        raise NodeError('Вставьте полный Node API token из устанавливаемой ноды.')
    encoded = value[len(CONNECTION_TOKEN_PREFIX):]
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,512}', encoded):
        raise NodeError('Некорректный Node API token.')
    try:
        raw = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
        data = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise NodeError('Некорректный Node API token.') from exc
    if not isinstance(data, dict) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', str(data.get('key', ''))):
        raise NodeError('Некорректный Node API token.')
    return {'url': normalize_url(data.get('url')), 'token': str(data['key'])}


def bearer_valid(header, key):
    prefix = 'Bearer '
    if not isinstance(header, str) or not header.startswith(prefix):
        return False
    candidate = header[len(prefix):].strip()
    if candidate.startswith(CONNECTION_TOKEN_PREFIX):
        try:
            candidate = parse_connection_token(candidate)['token']
        except NodeError:
            return False
    return hmac.compare_digest(candidate, key)


def clean_text(value, label, maximum=80):
    value = str(value or '').strip()
    if not value or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise NodeError('%s: от 1 до %d символов.' % (label, maximum))
    return value


def flag(code):
    code = str(code or '').strip().upper()
    if not re.fullmatch(r'[A-Z]{2}', code):
        raise NodeError('Код страны должен состоять из двух латинских букв, например DE.')
    return ''.join(chr(127397 + ord(char)) for char in code)


def location(value):
    value = value if isinstance(value, dict) else {}
    code = str(value.get('country_code', 'UN')).upper()
    if code == 'UN':
        return {'country_code': 'UN', 'country_name': 'Сервер', 'name': clean_text(value.get('name', 'Основная локация'), 'Локация')}
    flag(code)
    return {'country_code': code, 'country_name': clean_text(value.get('country_name'), 'Страна'),
            'name': clean_text(value.get('name'), 'Локация')}


def location_prefix(value):
    value = location(value)
    icon = '🌐' if value['country_code'] == 'UN' else flag(value['country_code'])
    parts = [icon, value['country_name']]
    if value['name'] and value['name'].casefold() != value['country_name'].casefold():
        parts.append(value['name'])
    return ' '.join(parts[:2]) + (' · ' + parts[2] if len(parts) > 2 else '')


def load_location(path):
    try:
        with open(path, encoding='utf-8') as stream:
            return location(json.load(stream))
    except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError):
        return location({})


def save_location(path, value):
    value = location(value)
    atomic_json(path, value)
    return value


def detect_public_location(timeout=8):
    """Return a best-effort location for the VPS public egress IP."""
    providers = (
        ('https://ipwho.is/?lang=ru', 'country_code', 'country', 'city', 'success'),
        ('https://ipapi.co/json/', 'country_code', 'country_name', 'city', None),
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    for url, code_key, country_key, city_key, success_key in providers:
        request_object = urllib.request.Request(url, method='GET', headers={
            'Accept': 'application/json', 'User-Agent': 'WEB-PANEL-PROXY/2.2',
        })
        try:
            with opener.open(request_object, timeout=timeout) as response:
                raw = response.read(GEO_RESPONSE_LIMIT + 1)
            if len(raw) > GEO_RESPONSE_LIMIT:
                continue
            value = json.loads(raw.decode('utf-8'))
            if not isinstance(value, dict) or (success_key and value.get(success_key) is not True):
                continue
            code = str(value.get(code_key, '')).strip().upper()
            country = str(value.get(country_key, '')).strip()
            city = str(value.get(city_key, '')).strip()
            if not re.fullmatch(r'[A-Z]{2}', code) or code == 'UN':
                continue
            return location({'country_code': code, 'country_name': country,
                             'name': city or 'Основная локация'})
        except (OSError, TimeoutError, ValueError, TypeError, json.JSONDecodeError,
                urllib.error.URLError, urllib.error.HTTPError):
            continue
    return None


def load_nodes(path):
    try:
        with open(path, encoding='utf-8') as stream:
            value = json.load(stream)
    except FileNotFoundError:
        return []
    if not isinstance(value, list):
        raise NodeError('Реестр нод повреждён.')
    return value


def save_nodes(path, nodes):
    atomic_json(path, nodes)


def public_node(node):
    return {key: value for key, value in node.items() if key != 'token'}


def normalize_url(value):
    try:
        parsed = urllib.parse.urlsplit(str(value or '').strip())
    except ValueError as exc:
        raise NodeError('Некорректный URL ноды.') from exc
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or
            parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        raise NodeError('URL ноды должен иметь вид https://node.example.com без пути, логина и параметров.')
    hostname = parsed.hostname.lower()
    if (len(hostname) > 253 or hostname.endswith('.') or
            any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part)
                for part in hostname.split('.'))):
        raise NodeError('Укажите корректное публичное доменное имя ноды.')
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        raise NodeError('Для ноды используйте доменное имя с действующим HTTPS-сертификатом.')
    if '.' not in parsed.hostname or parsed.hostname.lower() == 'localhost':
        raise NodeError('Укажите публичное доменное имя ноды.')
    try:
        port = parsed.port
    except ValueError as exc:
        raise NodeError('Некорректный HTTPS-порт ноды.') from exc
    if port not in (None, 443):
        raise NodeError('API ноды доступен только через стандартный HTTPS-порт 443.')
    return 'https://' + hostname


def resolve_public(hostname):
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise NodeError('Не удалось определить IP-адрес ноды.') from exc
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise NodeError('Домен ноды должен указывать только на публичные IP-адреса.')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def request(node, method, path, payload=None, timeout=8):
    base = normalize_url(node.get('url'))
    parsed = urllib.parse.urlsplit(base)
    resolve_public(parsed.hostname)
    body = None if payload is None else json.dumps(payload, ensure_ascii=True).encode()
    req = urllib.request.Request(base + path, data=body, method=method,
        headers={'Authorization': 'Bearer ' + str(node.get('token', '')), 'Accept': 'application/json',
                 'Content-Type': 'application/json', 'User-Agent': 'WPP-Controller/2.2'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise NodeError('Ответ ноды слишком большой.')
            value = json.loads(raw.decode('utf-8'))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        raise NodeError('Нода не отвечает или отклонила API-токен.') from exc
    if not isinstance(value, dict) or not value.get('ok'):
        raise NodeError(str(value.get('message', 'Некорректный ответ ноды.')) if isinstance(value, dict) else 'Некорректный ответ ноды.')
    return value


def add_node(path, request_data):
    nodes = load_nodes(path)
    if len(nodes) >= MAX_NODES:
        raise NodeError('Достигнут лимит из %d нод.' % MAX_NODES)
    bundled = str(request_data.get('connection_token', '')).strip()
    if bundled:
        details = parse_connection_token(bundled)
        url, token = details['url'], details['token']
    else:
        # Backward compatibility for controllers configured before bundled tokens.
        url = normalize_url(request_data.get('url'))
        token = str(request_data.get('token', '')).strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        raise NodeError('Некорректный API-токен ноды.')
    if any(node.get('url') == url for node in nodes):
        raise NodeError('Эта нода уже добавлена.')
    candidate = {'url': url, 'token': token}
    status = request(candidate, 'GET', API_PREFIX + '/status')
    reported_url = normalize_url('https://' + str(status.get('domain', '')).strip().lower())
    if reported_url != url:
        raise NodeError('Домен в токене не совпадает с доменом, который сообщила нода.')
    loc = location(status.get('location'))
    node = {'id': secrets.token_hex(8), 'url': url, 'token': token, **loc, 'enabled': True}
    node['version'] = str(status.get('version', ''))[:32]
    nodes.append(node)
    save_nodes(path, nodes)
    return public_node(node)


def sync_profile(node, external_id, name, protocols):
    if not re.fullmatch(r'[a-f0-9]{32,64}', external_id):
        raise NodeError('Некорректный идентификатор федеративного профиля.')
    return request(node, 'POST', API_PREFIX + '/federation/sync', {
        'external_id': external_id, 'name': clean_text(name, 'Имя профиля'),
        'protocols': list(protocols),
    }, timeout=20)


def delete_profile(node, external_id):
    if not re.fullmatch(r'[a-f0-9]{32,64}', external_id):
        raise NodeError('Некорректный идентификатор федеративного профиля.')
    return request(node, 'POST', API_PREFIX + '/federation/delete', {'external_id': external_id}, timeout=20)


def purge_profiles(node):
    """Remove profiles issued through the federation API on one managed node."""
    return request(node, 'POST', API_PREFIX + '/federation/purge', {}, timeout=30)
