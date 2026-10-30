"""Timeweb links use a file-like child URL; existing server paths stay put."""
import ast
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import wpp_cdn as cdn
import wpp_cdn_doctor as doctor
import wpp_routing as routing

ROOTS = ('/vless-' + 'a' * 24, '/assets/' + 'a' * 12 + '/' + 'b' * 12)
UUID = '11111111-1111-4111-8111-111111111111'
INSTALLER = (ROOT / 'install-panel.sh').read_text(encoding='utf-8')
PANEL = INSTALLER.split('cat > "$APP_FILE" <<\'PY\'\n', 1)[1].split('\nPY\n', 1)[0]
PROXY_LINK = next(node for node in ast.parse(PANEL).body
                  if isinstance(node, ast.FunctionDef) and node.name == 'proxy_link')


def make_link(root, provider='timeweb'):
    profile = None if provider == 'direct' else {
        'enabled': True, 'provider': provider, 'public_host': 'edge.example',
        'fingerprint': 'firefox',
    }
    namespace = {
        'cdn': cdn, 'DOMAIN': 'vpn.example', 'quote': quote,
        'urlencode': urlencode, 'json': json,
        'proxy_user': lambda *args: {'cdn': profile},
        'proxy_connection': lambda user: None,
        'xray_path': lambda: root,
    }
    module = ast.fix_missing_locations(ast.Module(body=[PROXY_LINK], type_ignores=[]))
    exec(compile(module, 'real-proxy-link.py', 'exec'), namespace)
    return namespace['proxy_link']('vless', UUID, name='🌐 Test')


class TimewebPaths(unittest.TestCase):
    def test_legacy_and_static_client_paths_append_index_json(self):
        for root in ROOTS:
            with self.subTest(root=root):
                server = cdn.endpoint_path(root, 'timeweb')
                self.assertTrue(server.endswith('/stream'))
                self.assertEqual(cdn.client_endpoint_path(root, 'timeweb'), server + '/index.json')

    def test_real_uri_generator_keeps_credentials_and_transport(self):
        for root in ROOTS:
            with self.subTest(root=root):
                parsed = urlsplit(make_link(root))
                values = parse_qs(parsed.query)
                self.assertEqual(parsed.username, UUID)
                self.assertEqual(parsed.hostname, 'edge.example')
                self.assertEqual(parsed.port, 443)
                self.assertEqual(values['path'], [cdn.client_endpoint_path(root, 'timeweb')])
                self.assertEqual(values['fp'], ['firefox'])
                self.assertEqual(values['mode'], ['packet-up'])
                self.assertEqual(json.loads(values['extra'][0]), cdn._timeweb_extra())

    def test_other_provider_and_direct_paths_are_unchanged(self):
        for root in ROOTS:
            for provider, variant in [('direct', 'direct'), ('yandex', 'cdn'),
                                      ('custom', 'cdn'), ('vk', 'vk'),
                                      ('beget', 'beget'), ('selectel', 'selectel')]:
                with self.subTest(root=root, provider=provider):
                    path = parse_qs(urlsplit(make_link(root, provider)).query)['path'][0]
                    self.assertEqual(path, cdn.endpoint_path(root, variant))

    def test_routed_plain_and_failover_json_keep_the_new_client_path(self):
        for root in ROOTS:
            link = make_link(root)
            configs = [routing.plain_xray_config(link),
                       routing.xray_config(link, routing.validate({})),
                       routing.failover_xray_config([link])]
            for config in configs:
                with self.subTest(root=root, config=config['remarks']):
                    outbound = next(row for row in config['outbounds'] if row['protocol'] == 'vless')
                    self.assertEqual(outbound['streamSettings']['xhttpSettings']['path'],
                                     cdn.client_endpoint_path(root, 'timeweb'))
                    self.assertEqual(outbound['settings']['vnext'][0]['users'][0]['id'], UUID)

    def test_server_and_caddy_keep_the_existing_endpoint_and_wildcard(self):
        for root in ROOTS:
            with self.subTest(root=root):
                server = cdn.endpoint_path(root, 'timeweb')
                self.assertEqual(cdn.server_xhttp(server, 'timeweb')['path'], server)
                caddy = doctor._managed_routes(root)
                matcher = '@web_panel_vless_timeweb path ' + server + ' ' + server + '/*'
                self.assertIn(matcher, caddy)
                self.assertNotIn('/index.json', caddy)
                self.assertEqual(cdn.rewrite_caddy_xhttp_routes(caddy, root), caddy)

    def test_doctor_reports_the_actual_client_path_without_network(self):
        profile = {'provider': 'timeweb', 'public_host': 'edge.example'}
        with patch.object(doctor, '_resolve', return_value=['192.0.2.1']), \
             patch.object(doctor, '_tls', return_value=('TLSv1.3', 100)), \
             patch.object(doctor, '_request', return_value=(
                 200, {'x-wpp-cdn-probe': 'ok'}, b'{}', 1)), \
             patch.object(doctor, '_local_port', return_value=True):
            report = doctor.run(profile, 'vpn.example', ROOTS[0])
        self.assertTrue(report['ok'])
        self.assertEqual(report['path'], cdn.client_endpoint_path(ROOTS[0], 'timeweb'))

    def test_client_helper_keeps_server_path_validation(self):
        for root, variant in [('/invalid', 'timeweb'), (ROOTS[0], 'unknown')]:
            with self.subTest(root=root, variant=variant):
                with self.assertRaises(cdn.CDNError):
                    cdn.client_endpoint_path(root, variant)


if __name__ == '__main__':
    unittest.main()
