"""Exercise the real public JSON handler and standalone Happ import UI."""
import ast
import base64
import copy
import hashlib
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import wpp_connections as connections
import wpp_cdn as cdn
import wpp_routing as routing
import wpp_ui as ui

INSTALLER = (ROOT / 'install-panel.sh').read_text(encoding='utf-8')
PANEL = INSTALLER.split('cat > "$APP_FILE" <<\'PY\'\n', 1)[1].split('\nPY\n', 1)[0]


class Controls(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.items = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        if tag in ('button', 'a'):
            self.items.append(dict(attrs))


class DirectRouting(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.connection_file = root / 'connections.json'
        self.routing_file = root / 'routing.json'
        self.registry = {'schema': 1, 'connections': connections.defaults()}
        connections.save(str(self.connection_file), self.registry)
        self.profile = routing.validate({**{key: False for key in routing.ROUTING_FLAGS},
                                         'enabled': True, 'split_dns': True,
                                         'direct_domains': ['direct.example'],
                                         'proxy_domains': ['vpn-only.example']})
        self.state = {'schema': 1, 'profiles': {'vless-direct': self.profile}}
        self.save_routing()
        self.user = {'id': 'a' * 16, 'name': 'Телефон', 'protocol': 'vless',
                     'secret': '11111111-1111-4111-8111-111111111111',
                     'connection_id': 'vless-direct', 'backend_port': 443, 'enabled': True}
        self.profiles = [self.user]
        self.qr_payloads = []

        def proxy(protocol, secret, port=443, name='Proxy', username=''):
            if protocol == 'hysteria':
                return f'hysteria2://{secret}@vpn.example:{port}/?sni=vpn.example'
            return f'vless://{secret}@vpn.example:443?security=tls&type=xhttp&path=%2Ftest&mode=auto'

        namespace = dict(copy=copy, base64=base64, hashlib=hashlib, secrets=secrets,
                         json=json, re=re, parse_qs=parse_qs, urlparse=urlparse,
                         BaseHTTPRequestHandler=BaseHTTPRequestHandler,
                         node_api=SimpleNamespace(API_PREFIX='/wpp-api'),
                         SUB_PREFIX='/wpp-sub/', DIRECT_PROFILE_PREFIX='/wpp-direct/',
                         DOMAIN='vpn.example', PANEL_PATH='/panel-test',
                         CONNECTIONS_FILE=str(self.connection_file), ROUTING_FILE=str(self.routing_file),
                         connection_store=connections, routing_store=routing,
                         users=lambda: copy.deepcopy(self.profiles), proxy_link=proxy,
                         allow_subscription_request=lambda client: True, client_id=lambda handler: 'local',
                         SUB_FETCH_SLOTS=threading.BoundedSemaphore(2),
                         happ_import_link=ui.happ_import_link,
                         load=lambda: {}, primary=lambda: '',
                         awg=SimpleNamespace(PROTOCOLS=('awg20', 'awg31')), AWG_AVAILABLE=True)
        tree = ast.parse(PANEL)
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in ('direct_profile_token', 'direct_profile_url', 'direct_profile_config')]
        handler = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Handler')
        methods = [node for node in handler.body if isinstance(node, ast.FunctionDef)
                   and node.name in ('do_GET', 'serve_direct_profile', 'send_data', 'send_png', 'send_html')]
        subset = ast.ClassDef(name='Handler', bases=handler.bases, keywords=[], body=methods, decorator_list=[])
        module = ast.fix_missing_locations(ast.Module(body=functions + [subset], type_ignores=[]))
        exec(compile(module, 'panel-test.py', 'exec'), namespace)
        namespace['qr_png_bytes'] = lambda value: self.qr_payloads.append(value) or b'qr-image'
        cls = namespace['Handler']
        cls.log_message = lambda *args: None
        cls.auth = lambda handler: True
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), cls)
        self.worker = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.01}, daemon=True)
        self.worker.start()
        self.addCleanup(self.close_server)
        self.namespace = namespace
        self.proxy = proxy

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)

    def save_routing(self):
        self.routing_file.write_text(json.dumps(self.state), encoding='utf-8')

    def profile_url(self, user=None, secret=None):
        return self.namespace['direct_profile_url'](user or self.user, secret)

    def get(self, path):
        url = 'http://127.0.0.1:' + str(self.server.server_port) + path
        try:
            response = urlopen(Request(url, headers={'User-Agent': 'Happ/4.0',
                                                     'Accept': 'application/json'}), timeout=3)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def fetch(self, suffix='', user=None, secret=None):
        return self.get(urlsplit(self.profile_url(user, secret)).path + suffix)

    def assert_routed(self, config, secret=None):
        self.assertIn('dns', config)
        rules = config['routing']['rules']
        self.assertTrue(any(rule.get('outboundTag') == 'direct'
                            and 'domain:direct.example' in rule.get('domain', []) for rule in rules))
        outbound = next(value for value in config['outbounds'] if value['tag'] == 'proxy')
        self.assertEqual(outbound['settings']['vnext'][0]['users'][0]['id'], secret or self.user['secret'])

    def test_happ_fetches_one_complete_routed_config_without_panel_login(self):
        self.namespace['Handler'].auth = lambda handler: self.fail('Public JSON must not require panel login')
        status, headers, body = self.fetch()
        self.assertEqual(status, 200)
        self.assertIn('application/json', headers['Content-Type'])
        configs = json.loads(body)
        self.assertEqual(len(configs), 1)
        self.assert_routed(configs[0])

    def test_download_returns_single_json_object_with_routing(self):
        status, headers, body = self.fetch('?format=single&download=1')
        self.assertEqual(status, 200)
        self.assertIn('attachment;', headers['Content-Disposition'])
        self.assert_routed(json.loads(body))

    def use_real_timeweb_link_generator(self):
        connection = connections.validate({
            'id': 'timeweb-test', 'name': 'Timeweb', 'protocol': 'vless',
            'config': {'route': 'cdn', 'provider': 'timeweb',
                       'public_host': 'edge.example', 'fingerprint': 'firefox'},
        })
        self.registry['connections'].append(connection)
        connections.save(str(self.connection_file), self.registry)
        connections.apply_to_user(self.user, connection)
        self.state['profiles'] = {'timeweb-test': self.profile}
        self.save_routing()
        self.namespace.update(cdn=cdn, quote=quote, urlencode=urlencode,
                              LOCATION_FILE='unused-location-file')
        self.namespace['node_api'].location_prefix = lambda value: '🌐 Test'
        self.namespace['node_api'].load_location = lambda path: {}
        names = {'proxy_user', 'proxy_connection', 'proxy_connection_name', 'proxy_link'}
        functions = [node for node in ast.parse(PANEL).body
                     if isinstance(node, ast.FunctionDef) and node.name in names]
        exec(compile(ast.Module(body=functions, type_ignores=[]),
                     'real-timeweb-link.py', 'exec'), self.namespace)
        self.namespace['xray_path'] = lambda: '/vless-' + 'a' * 24

    def test_timeweb_routed_public_json_uses_file_like_path(self):
        self.use_real_timeweb_link_generator()
        status, _, body = self.fetch()
        self.assertEqual(status, 200)
        config = json.loads(body)[0]
        self.assert_routed(config)
        outbound = next(row for row in config['outbounds'] if row['tag'] == 'proxy')
        self.assertEqual(outbound['streamSettings']['xhttpSettings']['path'],
                         '/vless-' + 'a' * 24 + '-timeweb/api/v2/sync/stream/index.json')
        self.assertEqual(outbound['settings']['vnext'][0]['address'], 'edge.example')

    def test_timeweb_single_json_download_keeps_new_path_without_routing(self):
        self.use_real_timeweb_link_generator()
        self.state['profiles'] = {}
        self.save_routing()
        status, headers, body = self.fetch('?format=single&download=1')
        self.assertEqual(status, 200)
        self.assertIn('attachment;', headers['Content-Disposition'])
        config = json.loads(body)
        self.assertNotIn('routing', config)
        self.assertTrue(config['outbounds'][0]['streamSettings']['xhttpSettings']['path']
                        .endswith('/stream/index.json'))

    def test_hysteria_profile_keeps_dns_rules_and_authentication(self):
        self.user.update(protocol='hysteria', connection_id='hysteria-primary', backend_port=8443)
        self.state['profiles'] = {'hysteria-primary': self.profile}
        self.save_routing()
        status, _, body = self.fetch()
        self.assertEqual(status, 200)
        config = json.loads(body)[0]
        self.assertIn('dns', config)
        self.assertIn('routing', config)
        outbound = next(value for value in config['outbounds'] if value['tag'] == 'proxy')
        self.assertEqual(outbound['protocol'], 'hysteria')
        self.assertEqual(outbound['streamSettings']['hysteriaSettings']['auth'], self.user['secret'])

    def test_legacy_user_without_connection_id_keeps_routing(self):
        self.user.pop('connection_id')
        status, _, body = self.fetch()
        self.assertEqual(status, 200)
        self.assert_routed(json.loads(body)[0])
        self.assertNotIn('connection_id', self.user)

    def test_second_device_gets_its_own_uuid(self):
        secret = '22222222-2222-4222-8222-222222222222'
        self.user['device_secrets'] = [self.user['secret'], secret]
        status, _, body = self.fetch(secret=secret)
        self.assertEqual(status, 200)
        self.assert_routed(json.loads(body)[0], secret)

    def test_disabled_deleted_unknown_keys_are_unavailable(self):
        self.user['enabled'] = False
        self.assertEqual(self.fetch()[0], 404)
        self.profiles.clear()
        self.assertEqual(self.fetch()[0], 404)
        self.assertEqual(self.get('/wpp-direct/' + 'f' * 64)[0], 404)

    def test_disabled_server_connection_is_unavailable(self):
        connection = connections.get(self.registry, 'vless-direct')
        connection['enabled'] = False
        connections.save(str(self.connection_file), self.registry)
        self.assertEqual(self.fetch()[0], 404)

    def test_corrupt_routing_does_not_silently_return_plain_profile(self):
        self.routing_file.write_text('{broken', encoding='utf-8')
        self.assertEqual(self.fetch()[0], 503)
        self.save_routing()
        self.assertEqual(self.fetch()[0], 200)

    def test_no_profile_does_not_inherit_another_connections_rules(self):
        self.state['profiles'] = {'hysteria-primary': self.profile}
        self.save_routing()
        status, _, body = self.fetch()
        self.assertEqual(status, 200)
        self.assertNotIn('routing', json.loads(body)[0])

    def test_table_copy_and_qr_import_the_json_profile(self):
        records = ui.client_records([], self.profiles, {}, 'vpn.example', self.proxy,
                                    self.state['profiles'], self.namespace['direct_profile_url'])
        self.assertEqual(records[0]['link'], ui.happ_import_link(self.profile_url()))
        self.assertTrue(records[0]['raw_link'].startswith('vless://'))
        markup = ui.users_ui([], self.profiles, {}, '/panel-test', 'vpn.example', 'csrf', self.proxy,
                             connections=self.registry['connections'], routing_profiles=self.state['profiles'],
                             direct_profile_url=self.namespace['direct_profile_url'])
        buttons = Controls(markup).items
        self.assertEqual(next(value['data-copy'] for value in buttons if 'data-copy' in value), records[0]['link'])
        qr = next(value for value in buttons if 'data-qr-info' in value)
        info = json.loads(qr['data-qr-info'])
        self.assertEqual(info['kind'], 'routing')
        self.assertEqual(info['host'], 'vpn.example')
        self.assertIsNone(info['port'])
        self.assertEqual(self.get(qr['data-qr'])[0], 200)
        self.assertEqual(self.qr_payloads[-1], records[0]['link'])
        raw_json = next(value['data-copy'] for value in buttons
                        if value.get('data-copy', '').startswith('{'))
        self.assert_routed(json.loads(raw_json))
        download = next(value['href'] for value in buttons if 'format=single' in value.get('href', ''))
        status, _, body = self.get(urlparse(download).path + '?' + urlparse(download).query)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), json.loads(raw_json))

    def test_turning_routing_off_restores_plain_vless_copy(self):
        self.profile['enabled'] = False
        records = ui.client_records([], self.profiles, {}, 'vpn.example', self.proxy,
                                    self.state['profiles'], self.namespace['direct_profile_url'])
        self.assertTrue(records[0]['link'].startswith('vless://'))
        self.assertEqual(records[0]['routing_url'], '')


if __name__ == '__main__':
    unittest.main()
