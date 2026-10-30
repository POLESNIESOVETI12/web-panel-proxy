"""Portable tests for the panel's real OpenFlux link functions (no systemd)."""
import ast
import base64
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'wpp_openflux.py').read_text(encoding='utf-8')


class OpenFluxLinks(unittest.TestCase):
    def setUp(self):
        names = {'_transport_for', '_share_payload', '_python_share_link', 'make_import_uri'}
        nodes = [node for node in ast.parse(SOURCE).body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
        self.assertEqual(len(nodes), len(names))
        self.ns = {'json': json, 'zlib': zlib, 'base64': base64,
                   'TRANSPORT': 'yandex', 'TRANSPORTS': {'yandex', 'telemost', 'mailru', 'oneme'},
                   '_LINK_CACHE': {}, 'BIN': Path('/not-installed/openflux'),
                   'OpenFluxError': RuntimeError}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'actual-openflux-links.py', 'exec'), self.ns)
        self.fixture = {'name': 'Тест iPhone', 'transport': 'telemost', 'platform': 'ios',
                        'url': 'https://telemost.yandex.ru/j/diagnostic-fixture',
                        'key': 'dummy-secret-not-a-live-key-00000000000'}

    def decode(self, link):
        self.assertTrue(link.startswith('openflux://v1/'))
        encoded = link.split('openflux://v1/', 1)[1]
        packed = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
        return json.loads(zlib.decompress(packed, -zlib.MAX_WBITS))

    def test_fallback_is_raw_deflate_and_preserves_unicode(self):
        payload = self.ns['_share_payload'](self.fixture)
        link = self.ns['_python_share_link'](payload)
        self.assertEqual(self.decode(link), payload)
        self.assertEqual(self.decode(link)['name'], 'Тест iPhone')

    def test_old_zlib_wrapper_reproduces_rejection(self):
        payload = self.ns['_share_payload'](self.fixture)
        packed = zlib.compress(json.dumps(payload).encode(), 9)
        with self.assertRaises(zlib.error):
            zlib.decompress(packed, -zlib.MAX_WBITS)

    def test_ios_export_has_no_key_matching_current_plain_service_mode(self):
        payload = self.ns['_share_payload'](self.fixture)
        self.assertNotIn('secret', payload)
        self.assertNotIn('context', payload)
        self.assertEqual(payload['transports'], [{'type': 'telemost', 'url': self.fixture['url']}])

    def test_android_export_preserves_secret_and_context(self):
        payload = self.ns['_share_payload']({**self.fixture, 'platform': 'android'})
        decoded = self.decode(self.ns['_python_share_link'](payload))
        self.assertEqual(decoded['secret'], self.fixture['key'])
        self.assertEqual(decoded['context'], self.fixture['url'])

    def test_missing_cli_uses_compatible_fallback(self):
        def unavailable(*args, **kwargs):
            raise RuntimeError('binary unavailable')
        self.ns['_run'] = unavailable
        decoded = self.decode(self.ns['make_import_uri'](self.fixture))
        self.assertEqual(decoded, self.ns['_share_payload'](self.fixture))

    def test_valid_cli_link_is_not_reencoded(self):
        expected = self.ns['_python_share_link'](self.ns['_share_payload'](self.fixture))
        self.ns['_run'] = lambda *a, **k: SimpleNamespace(stdout=json.dumps({'link': expected}))
        self.assertEqual(self.ns['make_import_uri'](self.fixture), expected)

    def test_malformed_cli_response_falls_back(self):
        for text in ('not-json', '{}', '{"link":"https://wrong.example"}'):
            self.ns['_LINK_CACHE'].clear()
            self.ns['_run'] = lambda *a, _text=text, **k: SimpleNamespace(stdout=_text)
            with self.subTest(response=text):
                self.assertEqual(self.decode(self.ns['make_import_uri'](self.fixture)), self.ns['_share_payload'](self.fixture))

    def test_distinct_platforms_and_urls_do_not_reuse_wrong_cached_key(self):
        self.ns['_run'] = lambda *a, **k: SimpleNamespace(stdout='{}')
        ios = self.decode(self.ns['make_import_uri'](self.fixture))
        android = self.decode(self.ns['make_import_uri']({**self.fixture, 'platform': 'android'}))
        self.assertNotIn('secret', ios)
        self.assertEqual(android['secret'], self.fixture['key'])

    def test_max_token_never_enters_document_link(self):
        self.assertIsNone(self.ns['_share_payload']({**self.fixture, 'transport': 'oneme', 'max_server_token': 'private'}))


if __name__ == '__main__': unittest.main()
