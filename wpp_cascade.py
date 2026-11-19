"""Managed server chains with VLESS relay links and fail-closed routing.

The entry is the local panel. One or two authenticated WPP nodes provide the
remaining hops. Relay credentials never appear in browser data or user links.
All mutating functions are called under the existing manager process lock.
"""
import copy
import hashlib
import wpp_cdn
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import subprocess
import tempfile
import time
import uuid
from urllib.parse import urlsplit

import wpp_nodes as nodes_api

CAPABILITY = 'cascade-v1'
ENTRY_PROTOCOLS = frozenset({'vless', 'hysteria'})
BLOCK_TAG = 'wpp-cascade-block'
MAX_RELAYS = 128


class CascadeError(ValueError):
    pass


def clean_plan(raw=None):
    if raw is None:
        return {'mode': 'direct', 'nodes': []}
    if not isinstance(raw, dict):
        raise CascadeError('Некорректные параметры цепочки серверов.')
    mode = raw.get('mode', 'direct')
    if mode not in ('direct', 'two', 'three'):
        raise CascadeError('Неизвестный режим моста.')
    ids = raw.get('nodes', [])
    count = {'direct': 0, 'two': 1, 'three': 2}[mode]
    if (not isinstance(ids, list) or len(ids) != count or
            any(not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{16}', value) for value in ids)):
        raise CascadeError('Выберите сервер для каждого участка цепочки.')
    if len(set(ids)) != len(ids):
        raise CascadeError('Каждый сервер можно использовать в цепочке только один раз.')
    return {'mode': mode, 'nodes': list(ids)}


def plan_hash(plan):
    return hashlib.sha256(json.dumps(clean_plan(plan), sort_keys=True).encode()).hexdigest()


def empty_state():
    return {'schema': 1, 'entries': {}, 'relays': {}, 'garbage': []}


def load(path):
    try:
        with open(path, encoding='utf-8') as stream:
            state = json.load(stream)
    except FileNotFoundError:
        return empty_state()
    except (OSError, ValueError) as exc:
        raise CascadeError('Файл мостов повреждён. Проверьте настройки сервера.') from exc
    if (not isinstance(state, dict) or state.get('schema') != 1 or
            not isinstance(state.get('entries'), dict) or not isinstance(state.get('relays'), dict) or
            not isinstance(state.get('garbage'), list)):
        raise CascadeError('Некорректный формат файла мостов.')
    return state


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.cascade-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=True, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def hostname(value):
    try:
        return urlsplit(nodes_api.normalize_url('https://' + str(value))).hostname
    except nodes_api.NodeError as exc:
        raise CascadeError('Для моста требуется публичный домен с HTTPS.') from exc


def endpoint(raw, with_key=True):
    if not isinstance(raw, dict):
        raise CascadeError('Нода вернула некорректное VLESS-подключение.')
    host = hostname(raw.get('host', ''))
    path = raw.get('path', '')
    if (not isinstance(path, str) or not re.fullmatch(r'/[A-Za-z0-9_./~%+-]{1,500}', path) or
            '..' in path or raw.get('port', 443) != 443):
        raise CascadeError('Некорректный путь или порт VLESS на ноде.')
    result = {'host': host, 'port': 443, 'path': path}
    if with_key:
        try:
            key = str(uuid.UUID(str(raw.get('uuid', ''))))
        except ValueError as exc:
            raise CascadeError('Некорректный служебный ключ моста.') from exc
        result['uuid'] = key
    return result


def outbound(tag, target):
    target = endpoint(target)
    return {'tag': tag, 'protocol': 'vless',
            'settings': {'vnext': [{'address': target['host'], 'port': 443,
                                   'users': [{'id': target['uuid'], 'encryption': 'none'}]}]},
            'streamSettings': {'network': 'xhttp', 'security': 'tls',
                               'tlsSettings': {'serverName': target['host'], 'allowInsecure': False,
                                               'alpn': ['h2']},
                               'xhttpSettings': {'host': target['host'], 'path': target['path'],
                                                 'mode': 'packet-up'}}}


def hysteria_outbound(tag, host, port, key, server_name=None):
    return {'tag': tag, 'protocol': 'hysteria',
            'settings': {'version': 2, 'address': host, 'port': port},
            'streamSettings': {'network': 'hysteria', 'security': 'tls',
                               'tlsSettings': {'serverName': server_name or host, 'alpn': ['h3']},
                               'hysteriaSettings': {'version': 2, 'auth': key}}}


def relay_record(request, local_host):
    if not isinstance(request, dict):
        raise CascadeError('Некорректный запрос моста.')
    chain_id, revision = request.get('chain_id', ''), request.get('revision', '')
    hosts, position = request.get('hosts'), request.get('position')
    if (not isinstance(chain_id, str) or not re.fullmatch(r'[a-f0-9]{32}', chain_id) or
            not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{16}', revision) or
            not isinstance(hosts, list) or len(hosts) not in (2, 3) or
            type(position) is not int or not 1 <= position < len(hosts)):
        raise CascadeError('Некорректный состав цепочки.')
    hosts = [hostname(value) for value in hosts]
    if len(set(hosts)) != len(hosts) or hosts[position] != hostname(local_host):
        raise CascadeError('Цепочка содержит повторный сервер или неверную ноду.')
    target = request.get('next')
    if position == len(hosts) - 1:
        if target is not None:
            raise CascadeError('У выходного сервера не должно быть следующего участка.')
    else:
        target = endpoint(target)
        if target['host'] != hosts[position + 1]:
            raise CascadeError('Следующий сервер не совпадает со схемой цепочки.')
        nodes_api.resolve_public(target['host'])
    # UUID is validated without accepting any transport settings from the caller.
    key = endpoint({'host': hosts[position], 'path': '/cascade', 'uuid': request.get('uuid')})['uuid']
    rid = chain_id + '-' + revision + '-' + str(position)
    return {'id': rid, 'uuid': key, 'email': 'cascade:' + rid,
            'hosts': hosts, 'next': target, 'created_at': int(time.time())}


def relay_clients(state, protocol='vless'):
    clients = []
    for rid, relay in state['relays'].items():
        if (not re.fullmatch(r'[a-f0-9]{32}-[a-f0-9]{16}-[12]', rid) or
                not isinstance(relay, dict) or relay.get('id') != rid or relay.get('email') != 'cascade:' + rid):
            raise CascadeError('Служебное подключение моста повреждено.')
        try:
            key = str(uuid.UUID(relay['uuid']))
        except (KeyError, ValueError, TypeError) as exc:
            raise CascadeError('Служебный ключ моста повреждён.') from exc
        hosts = relay.get('hosts', [])
        position = int(rid[-1])
        if (not isinstance(hosts, list) or len(hosts) not in (2, 3) or position >= len(hosts) or
                len(set(hosts)) != len(hosts) or 'next' not in relay):
            raise CascadeError('Состав служебного участка повреждён.')
        if position == len(hosts) - 1:
            if relay['next'] is not None:
                raise CascadeError('Выходной сервер содержит лишний участок.')
        elif endpoint(relay['next'])['host'] != hosts[position + 1]:
            raise CascadeError('Следующий участок моста повреждён.')
        probe_protocol = relay.get('probe_protocol', 'vless')
        if probe_protocol not in ENTRY_PROTOCOLS or (probe_protocol != 'vless' and not relay.get('probe_only')):
            raise CascadeError('Некорректный протокол служебного подключения.')
        if protocol == probe_protocol:
            clients.append({'auth' if protocol == 'hysteria' else 'id': key,
                            'email': relay['email'], 'level': 0})
    return clients


def hysteria_probe_clients(state, registry):
    enabled = {row['id'] for row in registry['connections']
               if row.get('protocol') == 'hysteria' and row.get('enabled', True)}
    return [client for client in relay_clients(state, 'hysteria')
            if state['relays'][client['email'].removeprefix('cascade:')].get('probe_connection') in enabled]


def overlay(config, state, registry, users, secrets_for_user):
    """Route by user email so other templates sharing this inbound stay direct."""
    result = copy.deepcopy(config)
    rules, extra = [], []
    for connection in registry.get('connections', []):
        protocol = connection.get('protocol')
        if protocol not in ENTRY_PROTOCOLS:
            continue
        plan = clean_plan(connection.get('config', {}).get('cascade'))
        if plan['mode'] == 'direct':
            continue
        emails = ['panel:' + user['id'] + ':' + str(index)
                  for user in users if user.get('connection_id') == connection['id'] and
                  user.get('protocol') == protocol and user.get('enabled', True)
                  for index, _ in enumerate(secrets_for_user(user), 1)]
        if not emails:
            continue
        entry = state['entries'].get(connection['id'], {})
        tag = BLOCK_TAG
        if entry.get('plan_hash') == plan_hash(plan):
            tag = 'cascade-entry-' + connection['id']
            extra.append(outbound(tag, entry.get('target')))
        rules.append({'type': 'field', 'user': emails, 'outboundTag': tag})
    relay_clients(state)  # Fail instead of accepting malformed internal records.
    for rid, relay in state['relays'].items():
        tag = 'direct'
        target=relay.get('next')
        if relay.get('probe_only'):
            # The persistent health identity follows this connection's committed
            # routing, including failover; it must never bypass a broken entry.
            connection=next((row for row in registry.get('connections',[]) if row['id']==relay.get('probe_connection')),None)
            plan=clean_plan(connection.get('config',{}).get('cascade')) if connection else clean_plan()
            if not connection or not connection.get('enabled',True) or connection.get('protocol') != relay.get('probe_protocol','vless'):
                tag=BLOCK_TAG
            elif relay.get('probe_target'):
                target=relay['probe_target']
            elif plan['mode']!='direct':
                entry=state['entries'].get(connection['id'],{})
                if entry.get('plan_hash')==plan_hash(plan):target=entry['target']
                else:tag=BLOCK_TAG
        if target is not None:
            tag = 'cascade-relay-' + rid
            extra.append(outbound(tag, target))
        rules.append({'type': 'field', 'user': [relay['email']], 'outboundTag': tag})
    if rules:
        result.setdefault('outbounds', []).extend(extra + [{'tag': BLOCK_TAG, 'protocol': 'blackhole'}])
        routing = result.setdefault('routing', {})
        routing['rules'] = rules + routing.get('rules', [])
    return result


def listener_hash(inbound):
    stream = copy.deepcopy(inbound.get('streamSettings', {}))
    settings = stream.get('xhttpSettings', {})
    compatible = wpp_cdn.direct_extra()
    # This exact preset keeps old path/session/method framing. Preserve the
    # existing bridge pin across updates; all other transport edits stay pinned.
    if stream.get('network') == 'xhttp' and all(settings.get(key) == value for key, value in compatible.items()):
        for key in compatible:
            if key != 'mode': settings.pop(key, None)
    return hashlib.sha256(json.dumps(stream, sort_keys=True).encode()).hexdigest()


def mutate_relay(path, request, local_host, apply, delete=False, local_endpoint=None, local_listener_hash=None, probe_connection=None, probe_protocol='vless', probe_target=None):
    before = load(path)
    after = copy.deepcopy(before)
    if delete:
        rid = str(request.get('id', ''))
        if not re.fullmatch(r'[a-f0-9]{32}-[a-f0-9]{16}-[12]', rid):
            raise CascadeError('Некорректный идентификатор участка.')
        if rid not in after['relays']:
            return {'ok': True, 'id': rid}
        del after['relays'][rid]
    else:
        relay = relay_record(request, local_host)
        if probe_connection is not None:
            # Only trusted local code sets this argument; Node API JSON cannot.
            relay.update(probe_only=True,probe_connection=str(probe_connection),probe_protocol=probe_protocol)
            if probe_target is not None:
                relay['probe_target'] = endpoint(probe_target)
        if local_endpoint is not None:
            relay['local_endpoint'] = endpoint(local_endpoint, with_key=False)
        if local_listener_hash is not None:
            relay['listener_hash'] = local_listener_hash
        rid = relay['id']
        existing = after['relays'].get(rid)
        if existing:
            if any(existing.get(key) != relay.get(key) for key in ('uuid', 'email', 'hosts', 'next')):
                raise CascadeError('Эта ревизия моста уже содержит другие настройки.')
            return {'ok': True, 'id': rid}
        if len(after['relays']) >= MAX_RELAYS:
            raise CascadeError('На сервере достигнут лимит служебных участков моста.')
        after['relays'][rid] = relay
    save(path, after)
    try:
        apply()
    except Exception:
        save(path, before)
        apply()
        raise
    return {'ok': True, 'id': rid}


def check_relay_listener(config, state, direct_tag):
    """An editor must not invalidate URLs already used by another server."""
    if not state['relays']:
        return
    inbound = next((row for row in config.get('inbounds', []) if row.get('tag') == direct_tag), {})
    path = inbound.get('streamSettings', {}).get('xhttpSettings', {}).get('path')
    for relay in state['relays'].values():
        if relay.get('probe_only'):
            continue
        pinned = relay.get('local_endpoint')
        if pinned and endpoint(pinned, with_key=False)['path'] != path:
            raise CascadeError('Вход VLESS используется другими мостами. Сначала отключите эти цепочки на их входных серверах, затем измените путь XHTTP.')
        raw_hash = hashlib.sha256(json.dumps(inbound.get('streamSettings', {}), sort_keys=True).encode()).hexdigest()
        if relay.get('listener_hash') and relay['listener_hash'] not in (listener_hash(inbound), raw_hash):
            raise CascadeError('Параметры транспорта VLESS используются другими мостами. Сначала отключите эти цепочки на их входных серверах.')


class Controller:
    def __init__(self, state_path, registry_path, nodes_path, local_host, apply,
                 probe, request=nodes_api.request, verify_entry=None):
        self.path, self.registry_path, self.nodes_path = state_path, registry_path, nodes_path
        self.host = hostname(local_host)
        self.apply_runtime, self.probe, self.request = apply, probe, request
        self.verify_entry = verify_entry
        self.journal = str(state_path) + '.transaction'

    def cleanup(self):
        state = load(self.path)
        current_nodes = {node.get('url'): node for node in nodes_api.load_nodes(self.nodes_path)}
        pending = []
        for index, peer in enumerate(state['garbage']):
            if index >= 4:
                pending.append(peer)
                continue
            try:
                node = current_nodes.get(peer['node']['url'], peer['node'])
                self.request(node, 'POST', nodes_api.API_PREFIX + '/cascade/delete', {'id': peer['id']}, timeout=15)
            except Exception:
                pending.append(peer)
        state['garbage'] = pending
        save(self.path, state)
        return len(pending)

    def recover(self):
        """Restore both logical files after a crash in the local commit window."""
        if not os.path.exists(self.journal):
            return
        import wpp_connections as connections
        with open(self.journal, encoding='utf-8') as stream:
            old = json.load(stream)
        connections.save(self.registry_path, old['registry'])
        save(self.path, old['state'])
        self.apply_runtime()
        os.unlink(self.journal)

    def _prepare(self, cid, plan):
        nodes = {node['id']: node for node in nodes_api.load_nodes(self.nodes_path)}
        selected = []
        for nid in plan['nodes']:
            node = nodes.get(nid)
            if not node or not node.get('enabled', True):
                raise CascadeError('Одна из выбранных нод удалена или выключена.')
            selected.append(node)
        hosts = [self.host] + [urlsplit(nodes_api.normalize_url(node.get('url'))).hostname for node in selected]
        if len(set(hosts)) != len(hosts):
            raise CascadeError('Нельзя использовать одну панель в цепочке несколько раз.')
        descriptions = []
        for node, host in zip(selected, hosts[1:]):
            try:
                report = self.request(node, 'GET', nodes_api.API_PREFIX + '/cascade/status', timeout=25)
            except nodes_api.NodeError as exc:
                raise CascadeError('Нода ' + host + ' недоступна или не поддерживает мост. Проверьте её доступность и установите эту версию панели на всех участках.') from exc
            if CAPABILITY not in report.get('capabilities', []):
                raise CascadeError('Обновите все выбранные ноды до версии с поддержкой моста.')
            description = endpoint(report.get('endpoint'), with_key=False)
            if description['host'] != host:
                raise CascadeError('Домен VLESS не совпадает с доменом выбранной ноды.')
            descriptions.append(description)
        chain = hashlib.sha256((self.host + ':' + cid).encode()).hexdigest()[:32]
        revision = secrets.token_hex(8)
        target = None
        peers = []
        for index in reversed(range(len(selected))):
            position = index + 1
            key = str(uuid.uuid4())
            rid = chain + '-' + revision + '-' + str(position)
            peer = {'node': {'url': selected[index]['url'], 'token': selected[index]['token']}, 'id': rid}
            # Remember the ID before sending: an HTTP timeout can follow a successful remote commit.
            state = load(self.path)
            state['garbage'].append(peer)
            save(self.path, state)
            peers.append(peer)
            payload = {'chain_id': chain, 'revision': revision, 'hosts': hosts,
                       'position': position, 'uuid': key, 'next': target}
            result = self.request(selected[index], 'POST', nodes_api.API_PREFIX + '/cascade/prepare', payload, timeout=65)
            if result.get('id') != rid:
                raise CascadeError('Нода не подтвердила применение участка моста.')
            target = {**descriptions[index], 'uuid': key}
        return target, peers

    def execute(self, request, check_only=False):
        import wpp_connections as connections
        registry = connections.load(self.registry_path)
        cid = str(request.get('id', ''))
        current = connections.get(registry, cid)
        if not current or current.get('protocol') not in ENTRY_PROTOCOLS:
            raise CascadeError('Сначала создайте входное подключение VLESS или Hysteria2 на этой панели.')
        plan = clean_plan(request.get('cascade'))
        if current.get('config',{}).get('ssh_entry') and plan['mode']!='direct':
            raise CascadeError('Сначала отключите SSH-вход RU у этого подключения.')
        candidate = copy.deepcopy(registry)
        item = connections.get(candidate, cid)
        item['config']['cascade'] = plan
        item['name'] = request.get('name', current['name'])
        item = connections.validate(item, existing=current)
        candidate['connections'] = [item if row['id'] == cid else row for row in candidate['connections']]
        target, peers, report = None, [], {'ok': True, 'direct': True}
        result = None
        try:
            if plan['mode'] != 'direct':
                target, peers = self._prepare(cid, plan)
                if item['protocol'] == 'hysteria' and item.get('enabled', True) and self.verify_entry:
                    # Journal the temporary health key. Real clients stay on their
                    # existing route until the new chain has passed over UDP.
                    save(self.journal, {'registry': registry, 'state': load(self.path)})
                    try:
                        report = self.verify_entry(item, target)
                    finally:
                        self.recover()
                else:
                    report = self.probe(target)
                if not isinstance(report, dict) or not report.get('ok'):
                    raise CascadeError('Цепочка не прошла проверку выхода в интернет.')
            if check_only:
                result = {'ok': True, 'checked': True, **report}
                return result
            before = load(self.path)
            after = copy.deepcopy(before)
            old = after['entries'].pop(cid, None)
            if old:
                after['garbage'].extend(old.get('peers', []))
            if plan['mode'] != 'direct':
                after['entries'][cid] = {'plan_hash': plan_hash(plan), 'target': target,
                                         'peers': peers, 'checked_at': int(time.time())}
            new_ids = {peer['id'] for peer in peers}
            after['garbage'] = [peer for peer in after['garbage'] if peer['id'] not in new_ids]
            save(self.journal, {'registry': registry, 'state': before})
            try:
                connections.save(self.registry_path, candidate)
                save(self.path, after)
                self.apply_runtime()
            except Exception:
                self.recover()
                raise
            os.unlink(self.journal)
            result = {'ok': True, 'saved': True, 'mode': plan['mode'], **report}
            return result
        finally:
            # Peers not referenced by a committed entry remain in garbage.
            # Failed deletions are durable and retried on the next operation.
            if not os.path.exists(self.journal):
                try:
                    pending = self.cleanup()
                    if result is not None:
                        result['cleanup_pending'] = pending
                except OSError:
                    if result is not None:
                        result['cleanup_pending'] = True

    def detach(self, cid):
        state = load(self.path)
        old = state['entries'].pop(cid, None)
        if old:
            state['garbage'].extend(old.get('peers', []))
            save(self.path, state)
        keys=[key for key,row in state['relays'].items() if row.get('probe_only') and row.get('probe_connection')==cid]
        if keys:
            for key in keys:state['relays'].pop(key,None)
            save(self.path,state)
        return self.cleanup()


def _receive(sock, count):
    data = b''
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise OSError('Proxy closed the connection')
        data += chunk
    return data


def socks_connect(port, host, target_port=443, timeout=8):
    sock = socket.create_connection(('127.0.0.1', port), timeout=timeout)
    try:
        sock.sendall(b'\x05\x01\x00')
        if _receive(sock, 2) != b'\x05\x00':
            raise OSError('SOCKS negotiation failed')
        address = host.encode('idna')
        sock.sendall(b'\x05\x01\x00\x03' + bytes([len(address)]) + address + target_port.to_bytes(2, 'big'))
        response = _receive(sock, 4)
        if response[:2] != b'\x05\x00':
            raise OSError('SOCKS target unavailable')
        size = {1: 4, 4: 16}.get(response[3])
        if response[3] == 3:
            size = _receive(sock, 1)[0]
        if size is None:
            raise OSError('Invalid SOCKS response')
        _receive(sock, size + 2)
        return sock
    except Exception:
        sock.close()
        raise


def https_via_socks(port, host, path, timeout=6):
    """HTTPS and HTTP framing stay verified; both metadata providers use this tunnel."""
    connection=http.client.HTTPSConnection(host,timeout=timeout,context=ssl.create_default_context())
    raw=socks_connect(port,host,timeout=timeout)
    try:
        connection.sock=ssl.create_default_context().wrap_socket(raw,server_hostname=host)
        connection.request('GET',path,headers={'Host':host,'User-Agent':'WPP-Cascade-Check','Connection':'close'})
        response=connection.getresponse();body=response.read(65537)
        if len(body)>65536:raise ValueError('Response too large')
        return response.status,body
    finally:
        connection.close();raw.close()


def exit_information(port, fetch=https_via_socks):
    for host,path in [('api.country.is','/'),('www.cloudflare.com','/cdn-cgi/trace')]:
        try:
            status,body=fetch(port,host,path)
            if status!=200:continue
            if host=='api.country.is':
                value=json.loads(body)
                if not isinstance(value,dict):continue
                address=value.get('ip');country=value.get('country')
            else:
                value=dict(line.split('=',1) for line in body.decode('utf-8').splitlines() if '=' in line)
                address=value.get('ip');country=value.get('loc')
            parsed=ipaddress.ip_address(address)
            if not parsed.is_global or parsed.is_multicast:continue
            if not isinstance(country,str) or not re.fullmatch(r'[A-Z]{2}',country):country=None
            return {'exit_ip':str(parsed),'country_code':country,'exit_checked_at':int(time.time())}
        except (OSError,ValueError,TypeError,http.client.HTTPException):continue
    return {'exit_ip':None,'country_code':None,'exit_error':'Не удалось определить IP и страну через мост.'}


def probe(xray_binary, target, client_outbound=None, details=False):
    """Use a temporary Xray client to test the entire prepared chain over HTTPS."""
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    config = {'log': {'loglevel': 'none'},
              'inbounds': [{'listen': '127.0.0.1', 'port': port, 'protocol': 'socks',
                            'settings': {'auth': 'noauth', 'udp': False}}],
              'outbounds': [client_outbound or outbound('probe-chain', target)]}
    with tempfile.TemporaryDirectory(prefix='wpp-cascade-probe-') as folder:
        file = Path(folder) / 'config.json'
        save(file, config)
        process = subprocess.Popen([str(xray_binary), 'run', '-config', str(file)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            started=time.monotonic()
            deadline = time.monotonic() + 5
            while True:
                if process.poll() is not None:
                    raise CascadeError('Не удалось запустить проверку Xray.')
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.2):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise CascadeError('Проверка Xray не запустилась вовремя.')
                    time.sleep(.1)
            for host, path, expected in [('www.gstatic.com', '/generate_204', 204),
                                         ('cp.cloudflare.com', '/generate_204', 204)]:
                try:
                    with socks_connect(port, host, timeout=7) as raw:
                        with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as secure:
                            secure.sendall(('GET ' + path + ' HTTP/1.1\r\nHost: ' + host + '\r\nConnection: close\r\n\r\n').encode('ascii'))
                            line = b''
                            while b'\r\n' not in line and len(line) < 1024:
                                line += _receive(secure, 1)
                            if int(line.split(b' ', 2)[1]) == expected:
                                report={'ok': True, 'internet': True, 'checked_at': int(time.time()),'route_ms':round((time.monotonic()-started)*1000,1)}
                                if details:report.update(exit_information(port))
                                return report
                except (OSError, ValueError, IndexError):
                    continue
            raise CascadeError('Нет выхода в интернет через цепочку. Предыдущие настройки сохранены.')
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
