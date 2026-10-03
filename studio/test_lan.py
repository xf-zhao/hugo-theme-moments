"""LAN access, portable rendering, and same-origin checks with temporary data."""

from html.parser import HTMLParser
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import urlsplit

from studio.server import App, Handler
from studio.test_server import PNG, payload


class QuietHandler(Handler):
    def log_message(self, *args):
        pass


class RemoteAddressHandler(QuietHandler):
    """Keep real socket requests, but exercise the remote-client setup boundary."""

    def __init__(self, request, client_address, server):
        super().__init__(request, ('192.168.50.21', client_address[1]), server)


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in ('href', 'src') and value:
                self.urls.append(value)


class LanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='moments-lan-test-')
        cls.app = App(Path(cls.temporary.name) / 'data', author='xfz', host='0.0.0.0',
                      allowed_hosts=['192.168.50.20', 'xfz-mac.local'])
        cls.owner = cls.app.accounts.setup('fixture-lan-password')
        cls.cookie = 'Moments-Session=' + cls.app.accounts.new_session(cls.owner)
        cls.profile = cls.app.accounts.update(cls.owner['id'], {}, [('avatar.png', PNG)])
        cls.moment = cls.app.store.save(payload(body='Portable LAN photo.'), [('photo.png', PNG)],
                                       author_id=cls.owner['id'])
        # Pagination must work from a device whose localhost is a different computer.
        for index in range(11):
            cls.app.store.save(payload(body=f'Portable LAN moment {index}.'), [], author_id=cls.owner['id'])
        cls.server = ThreadingHTTPServer(('0.0.0.0', 0), QuietHandler)
        cls.app.port = cls.server.server_port
        cls.server.app = cls.app
        error = cls.app.build()
        if error:
            cls.server.server_close()
            cls.app.temporary.cleanup()
            cls.temporary.cleanup()
            raise AssertionError(error)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.app.temporary.cleanup()
        cls.temporary.cleanup()

    def call(self, method, path, body=None, headers=None):
        headers = dict(headers or {})
        headers.setdefault('Host', f'192.168.50.20:{self.app.port}')
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            headers.setdefault('Content-Type', 'application/json')
        connection = HTTPConnection('127.0.0.1', self.app.port, timeout=15)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        response_headers = dict(response.getheaders())
        result = json.loads(raw) if 'application/json' in response.getheader('Content-Type', '') else raw
        status = response.status
        connection.close()
        return status, result, response_headers

    def mutation_headers(self, host=None, cookie=None):
        host = host or f'192.168.50.20:{self.app.port}'
        return {'Host': host, 'Origin': f'http://{host}', 'X-Moments-Token': self.app.token,
                'Cookie': self.cookie if cookie is None else cookie}

    def test_known_lan_ip_and_hostname_can_browse_without_exposing_credentials(self):
        for hostname in ('192.168.50.20', 'xfz-mac.local', 'XFZ-MAC.LOCAL'):
            with self.subTest(hostname=hostname):
                headers = {'Host': f'{hostname}:{self.app.port}'}
                status, session, _ = self.call('GET', '/api/session', headers=headers)
                self.assertEqual(status, 200, session)
                self.assertFalse(session['authenticated'])
                self.assertEqual(session['default_user_id'], self.owner['id'])
                self.assertNotIn('credential', json.dumps(session))
                self.assertNotIn('scrypt', json.dumps(session))
                self.assertEqual(self.call('GET', '/', headers=headers)[0], 200)
                self.assertEqual(self.call('GET', self.profile['url'], headers=headers)[0], 200)

    def test_lan_login_cookie_profile_and_multipart_photo_post(self):
        hostname = f'xfz-mac.local:{self.app.port}'
        headers = self.mutation_headers(hostname, cookie='')
        status, signed_in, response_headers = self.call('POST', '/api/auth/login',
                                                       {'user_id': self.owner['id'], 'password': 'fixture-lan-password'}, headers)
        self.assertEqual(status, 200, signed_in)
        cookie_header = response_headers.get('Set-Cookie', '')
        self.assertIn('HttpOnly', cookie_header)
        self.assertIn('SameSite=Strict', cookie_header)
        cookie = cookie_header.split(';', 1)[0]
        self.assertTrue(cookie.startswith('Moments-Session='))
        headers = self.mutation_headers(hostname, cookie)
        status, session, _ = self.call('GET', '/api/session', headers=headers)
        self.assertEqual(status, 200)
        self.assertTrue(session['authenticated'])
        self.assertEqual(session['user']['id'], self.owner['id'])
        status, profile, _ = self.call('PUT', '/api/users/' + self.owner['id'], {'bio': 'Updated from another device.'}, headers)
        self.assertEqual(status, 200, profile)
        self.assertEqual(profile['user']['bio'], 'Updated from another device.')
        boundary = 'moments-lan-boundary'
        multipart = (f'--{boundary}\r\nContent-Disposition: form-data; name="post"\r\n\r\n'.encode()
                     + json.dumps(payload(body='Posted from the LAN.')).encode()
                     + f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="pictures"; filename="lan.png"\r\nContent-Type: image/png\r\n\r\n'.encode()
                     + PNG + f'\r\n--{boundary}--\r\n'.encode())
        headers['Content-Type'] = f'multipart/form-data; boundary={boundary}'
        status, saved, _ = self.call('POST', '/api/moments', multipart, headers)
        self.assertEqual(status, 201, saved)
        self.assertIsNone(saved['build_error'])
        moment = saved['moment']
        self.assertEqual(self.call('GET', moment['url'], headers={'Host': hostname})[0], 200)
        status, photo, _ = self.call('GET', moment['url'] + moment['pictures'][0], headers={'Host': hostname})
        self.assertEqual((status, photo), (200, PNG))

    def test_allowed_hosts_still_require_same_origin_and_login_to_write(self):
        headers = self.mutation_headers()
        for origin in (f'http://xfz-mac.local:{self.app.port}', 'http://foreign.example',
                       f'https://192.168.50.20:{self.app.port}', f'http://192.168.50.20:{self.app.port}/path',
                       f'http://evil.example@192.168.50.20:{self.app.port}'):
            with self.subTest(origin=origin):
                status, result, _ = self.call('POST', '/api/moments', payload(), {**headers, 'Origin': origin})
                self.assertEqual(status, 403, result)
        status, result, _ = self.call('POST', '/api/moments', payload(), self.mutation_headers(cookie=''))
        self.assertEqual(status, 401, result)
        del headers['X-Moments-Token']
        self.assertEqual(self.call('POST', '/api/moments', payload(), headers)[0], 403)

    def test_unknown_and_malformed_host_authorities_are_rejected(self):
        port = self.app.port
        for host in ('foreign.example', f'foreign.example:{port}', f'192.168.50.20:{port + 1}',
                     f'foreign.example@192.168.50.20:{port}', f'192.168.50.20:{port}@foreign.example',
                     f'http://192.168.50.20:{port}', f'192.168.50.20:{port}/path',
                     f'192.168.50.20:{port}?query', f'192.168.50.20:{port}#fragment',
                     '192.168.50.20:invalid', f'192.168.50.20:{port}:80'):
            with self.subTest(host=host):
                status, result, _ = self.call('GET', '/api/session', headers={'Host': host})
                self.assertEqual(status, 403, result)

    def test_posts_avatars_and_pagination_links_work_without_localhost_urls(self):
        all_links = []
        for path in ('/', self.moment['url'], self.profile['url'], '/2/'):
            status, html, _ = self.call('GET', path)
            self.assertEqual(status, 200, html[:200])
            links = Links()
            links.feed(html.decode())
            all_links.extend(links.urls)
            for value in links.urls:
                self.assertNotIn(urlsplit(value).hostname, ('localhost', '127.0.0.1', '0.0.0.0'), value)
        # Follow the generated links, rather than only checking their spelling.
        self.assertTrue(any(urlsplit(value).path == '/2/' for value in all_links), all_links)
        self.assertEqual(self.call('GET', self.profile['avatar'])[1], PNG)
        self.assertEqual(self.call('GET', self.moment['url'] + self.moment['pictures'][0])[1], PNG)


class FirstSetupTests(unittest.TestCase):
    def test_owner_can_still_set_up_from_this_mac_while_lan_binding_is_enabled(self):
        with tempfile.TemporaryDirectory(prefix='moments-local-setup-test-') as temporary:
            app = App(Path(temporary) / 'data', author='xfz', host='0.0.0.0', allowed_hosts=['192.168.50.20'])
            server = ThreadingHTTPServer(('0.0.0.0', 0), QuietHandler)
            server.app = app
            app.port = server.server_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = HTTPConnection('127.0.0.1', app.port, timeout=15)
                authority = f'127.0.0.1:{app.port}'
                connection.request('POST', '/api/auth/setup', body=json.dumps({'password': 'local-owner-password'}),
                                   headers={'Host': authority, 'Origin': 'http://' + authority,
                                            'Content-Type': 'application/json', 'X-Moments-Token': app.token})
                response = connection.getresponse()
                result = json.loads(response.read())
                self.assertEqual(response.status, 200, result)
                self.assertEqual(result['user']['id'], app.accounts.default_id)
                self.assertIn('HttpOnly', response.getheader('Set-Cookie', ''))
                connection.close()
                self.assertFalse(app.accounts.setup_required)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
                app.temporary.cleanup()

    def test_remote_device_cannot_claim_an_unconfigured_owner_account(self):
        with tempfile.TemporaryDirectory(prefix='moments-lan-setup-test-') as temporary:
            app = App(Path(temporary) / 'data', author='xfz', host='0.0.0.0', allowed_hosts=['192.168.50.20'])
            server = ThreadingHTTPServer(('0.0.0.0', 0), RemoteAddressHandler)
            server.app = app
            app.port = server.server_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = HTTPConnection('127.0.0.1', app.port, timeout=15)
                authority = f'192.168.50.20:{app.port}'
                connection.request('POST', '/api/auth/setup', body=json.dumps({'password': 'remote-claim-password'}),
                                   headers={'Host': authority, 'Origin': 'http://' + authority,
                                            'Content-Type': 'application/json', 'X-Moments-Token': app.token})
                response = connection.getresponse()
                result = response.read()
                self.assertEqual(response.status, 403, result)
                connection.close()
                self.assertTrue(app.accounts.setup_required)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
                app.temporary.cleanup()


if __name__ == '__main__':
    unittest.main()
