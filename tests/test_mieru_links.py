"""Client-specific Mieru exports must preserve the same server credentials."""
import base64
from html.parser import HTMLParser
import json
from pathlib import Path
import sys
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wpp_extra_protocols as extra
import wpp_ui as ui


class Buttons(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.buttons = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        if tag == 'button':
            self.buttons.append(dict(attrs))


class MieruLinks(unittest.TestCase):
    def setUp(self):
        self.user = {'id': 'a' * 16, 'name': 'Телефон · iOS', 'protocol': 'mieru',
                     'username': 'wpp1234567890', 'secret': 'test_secret_123456',
                     'connection_id': 'mieru-primary', 'backend_port': 54000,
                     'transport': 'TCP', 'enabled': True}
        self.connection = {'id': 'mieru-primary', 'protocol': 'mieru',
                           'config': {'port': 54000, 'transport': 'TCP'}}

    def test_shadowrocket_export_contains_authority_port_and_profile(self):
        link = extra.client_link(self.user, self.connection, 'vpn.example', self.user['name'])
        parsed = urlsplit(link)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, 'mierus')
        self.assertEqual(parsed.hostname, 'vpn.example')
        self.assertEqual(parsed.port, 54000)
        self.assertEqual(query['port'], ['54000'])
        self.assertEqual(query['protocol'], ['TCP'])
        self.assertEqual(query['profile'], [self.user['name']])
        self.assertEqual(unquote(parsed.fragment), self.user['name'])
        self.assertNotIn('+', parsed.query)

    def test_special_credentials_and_ipv6(self):
        user = {**self.user, 'username': 'user@example', 'secret': 'p@ss:/?&+#'}
        self.connection['config']['transport'] = 'UDP'
        parsed = urlsplit(extra.client_link(user, self.connection, '2001:db8::1', 'IPv6'))
        self.assertEqual(unquote(parsed.username), user['username'])
        self.assertEqual(unquote(parsed.password), user['secret'])
        self.assertEqual(parsed.hostname, '2001:db8::1')
        self.assertEqual(parsed.port, 54000)
        self.assertEqual(parse_qs(parsed.query)['protocol'], ['UDP'])

    def test_official_export_remains_available(self):
        link = extra.native_client_link(self.user, self.connection, 'vpn.example')
        self.assertTrue(link.startswith('mieru://'))
        payload = base64.b64decode(link[8:], validate=True)
        self.assertIn(self.user['username'].encode(), payload)
        self.assertIn(self.user['secret'].encode(), payload)
        self.assertIn(b'vpn.example', payload)

    def test_main_copy_and_both_editor_qrs_use_matching_formats(self):
        def proxy(protocol, secret, port, name, username=''):
            return extra.client_link(self.user, self.connection, 'vpn.example', name)
        records = ui.client_records([], [self.user], {}, 'vpn.example', proxy)
        self.assertTrue(records[0]['link'].startswith('mierus://'))
        markup = ui.client_editor(records[0], [self.user], '/panel', 'vpn.example',
                                  'csrf', {}, proxy, [self.connection])
        self.assertIn('Mieru · Shadowrocket', markup)
        buttons = Buttons(markup).buttons
        copied = [b['data-copy'] for b in buttons if 'data-copy' in b]
        self.assertEqual([urlsplit(s).scheme for s in copied], ['mierus', 'mieru'])
        qr = [b for b in buttons if 'data-qr-info' in b]
        self.assertEqual(len(qr), 2)
        self.assertTrue(json.loads(qr[0]['data-qr-info'])['link'].startswith('mierus://'))
        self.assertNotIn('format=native', qr[0]['data-qr'])
        native = json.loads(qr[1]['data-qr-info'])
        self.assertTrue(native['link'].startswith('mieru://'))
        self.assertEqual(native['port'], 54000)
        self.assertIn('format=native', qr[1]['data-qr'])


if __name__ == '__main__':
    unittest.main()
