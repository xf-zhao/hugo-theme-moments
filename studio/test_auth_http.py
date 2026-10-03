"""Exercise local account permissions and public profile exports with temporary data."""

from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from studio.server import App, Handler, REPO, read_document
from studio.test_server import PNG, payload


class QuietHandler(Handler):
    def log_message(self, *args):
        pass


class AccountHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='moments-auth-http-')
        self.root = Path(self.temporary.name).resolve() / 'data'
        self.app = App(self.root, author='xfz', bio='Life and discoveries.')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        self.app.port = self.server.server_port
        self.server.app = self.app
        self.assertIsNone(self.app.build())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.app.temporary.cleanup()
        self.temporary.cleanup()

    def call(self, method, path, body=None, cookie=None, headers=None):
        headers = {'X-Moments-Token': self.app.token, **(headers or {})}
        if cookie:
            headers['Cookie'] = cookie
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            headers.setdefault('Content-Type', 'application/json')
        connection = HTTPConnection('127.0.0.1', self.app.port, timeout=15)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        if 'application/json' in response.getheader('Content-Type', ''):
            data = json.loads(data)
        result = response.status, data, response.getheader('Set-Cookie')
        connection.close()
        return result

    def owner(self):
        status, user, cookie = self.call('POST', '/api/auth/setup', {'password': 'owner-password'})
        self.assertEqual(status, 200, user)
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        return cookie.split(';')[0]

    def member(self, owner):
        status, created, _ = self.call('POST', '/api/users', {'id': 'friend', 'name': 'A Friend', 'bio': 'Another perspective.', 'password': 'friend-password'}, owner)
        self.assertEqual(status, 201, created)
        status, user, cookie = self.call('POST', '/api/auth/login', {'user_id': 'friend', 'password': 'friend-password'})
        self.assertEqual(status, 200, user)
        return cookie.split(';')[0]

    def test_setup_login_logout_and_session_guards(self):
        status, session, _ = self.call('GET', '/api/session')
        self.assertEqual(status, 200)
        self.assertTrue(session['setup_required'])
        self.assertFalse(session['authenticated'])
        self.assertEqual(self.call('POST', '/api/moments', payload())[0], 401)
        self.assertEqual(self.call('POST', '/api/auth/setup', {'password': 'short'})[0], 400)
        owner = self.owner()
        self.assertEqual(self.call('POST', '/api/auth/setup', {'password': 'another-password'})[0], 409)
        session = self.call('GET', '/api/session', cookie=owner)[1]
        self.assertEqual(session['user']['id'], 'xfz')
        self.assertFalse(session['setup_required'])
        self.assertEqual(self.call('POST', '/api/auth/login', {'user_id': 'xfz', 'password': 'incorrect'})[0], 401)
        self.assertEqual(self.call('POST', '/api/auth/logout', {}, owner)[0], 200)
        self.assertFalse(self.call('GET', '/api/session', cookie=owner)[1]['authenticated'])
        self.assertEqual(self.call('POST', '/api/moments', payload(), owner)[0], 401)
        self.assertEqual(self.call('POST', '/api/auth/login', {'user_id': 'xfz', 'password': 'owner-password'}, headers={'Origin': 'https://other.example'})[0], 403)
        status, _, cookie = self.call('POST', '/api/auth/login', {'user_id': 'xfz', 'password': 'owner-password'})
        self.assertEqual(status, 200)
        self.assertTrue(self.call('GET', '/api/session', cookie=cookie.split(';')[0])[1]['authenticated'])

    def test_user_ownership_hidden_privacy_and_comment_identity(self):
        owner = self.owner()
        friend = self.member(owner)
        _, saved, _ = self.call('POST', '/api/moments', payload(author_id='friend'), owner)
        moment = saved['moment']
        self.assertEqual(moment['author_id'], 'xfz')
        url = '/api/moments/' + moment['id']
        for method, path, body in [
            ('PUT', url, payload(revision=moment['revision'])),
            ('DELETE', url, {'revision': moment['revision']}),
            ('PUT', url + '/visibility', {'hidden': True, 'revision': moment['revision']}),
            ('PUT', '/api/users/xfz', {'name': 'Impersonated'}),
            ('POST', '/api/users', {'id': 'intruder', 'name': 'Intruder', 'password': 'test-password'}),
        ]:
            self.assertEqual(self.call(method, path, body, friend)[0], 403)
        status, social, _ = self.call('POST', url + '/comments', {'text': 'Hello', 'author': 'xfz', 'author_id': 'xfz'}, friend)
        self.assertEqual(status, 200)
        self.assertEqual(social['comments'][0]['author'], 'A Friend')
        self.assertEqual(social['comments'][0]['author_id'], 'friend')
        _, social, _ = self.call('POST', url + '/comments', {'text': 'Welcome'}, owner)
        comment_id = social['comments'][-1]['id']
        self.assertEqual(self.call('PUT', url + '/comments/' + comment_id, {'text': 'Impersonated'}, friend)[0], 403)
        self.call('POST', url + '/like', {'liked': True}, friend)
        _, social, _ = self.call('POST', url + '/like', {'liked': True}, owner)
        self.assertEqual(social['like_count'], 2)
        self.call('POST', url + '/like', {'liked': False}, friend)
        self.assertEqual(self.call('GET', url, cookie=owner)[1]['social']['like_count'], 1)
        self.assertFalse(self.call('GET', url, cookie=friend)[1]['social']['liked'])
        _, hidden, _ = self.call('PUT', url + '/visibility', {'hidden': True, 'revision': moment['revision']}, owner)
        self.assertTrue(hidden['moment']['hidden'])
        self.assertEqual(self.call('GET', url, cookie=friend)[0], 403)
        self.assertEqual(self.call('GET', url)[0], 403)
        self.assertNotIn(moment['id'], [item['id'] for item in self.call('GET', '/api/moments', cookie=friend)[1]['moments']])
        self.assertEqual(self.call('PUT', url + '/comments/' + social['comments'][0]['id'], {'text': 'Hidden edit'}, friend)[0], 403)
        _, saved, _ = self.call('POST', '/api/moments', payload(body='Friend posted.'), friend)
        member_post = saved['moment']
        self.assertEqual(member_post['author_id'], 'friend')
        self.assertEqual(self.call('PUT', '/api/moments/' + member_post['id'], payload(body='Friend edited.', revision=member_post['revision']), friend)[0], 200)

    def test_profile_upload_links_legacy_names_and_public_export(self):
        owner = self.owner()
        moment = self.app.store.save(payload(), [])
        old_owner = self.root / '2026/10/03/old-owner/index.md'
        old_owner.parent.mkdir(parents=True)
        old_owner.write_text('+++\ndate="2026-10-03T11:00:00+08:00"\nname="xfz"\n+++\n\nOwner legacy name.\n')
        boundary = 'profile-upload'
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="post"\r\n\r\n'.encode()
                + json.dumps({'name': 'Xufeng', 'bio': 'Coffee, code, and walks.'}).encode()
                + f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="pictures"; filename="avatar.png"\r\nContent-Type: image/png\r\n\r\n'.encode()
                + PNG + f'\r\n--{boundary}--\r\n'.encode())
        status, saved, _ = self.call('PUT', '/api/users/xfz', body, owner, {'Content-Type': f'multipart/form-data; boundary={boundary}'})
        self.assertEqual(status, 200, saved)
        self.assertIsNone(saved['build_error'])
        avatar = saved['user']['avatar']
        self.assertEqual(self.call('GET', avatar)[1], PNG)
        html = self.call('GET', '/')[1].decode()
        self.assertIn('href="/users/xfz/"', html)
        self.assertIn('Xufeng', html)
        profile = self.call('GET', '/users/xfz/')[1].decode()
        self.assertIn('Coffee, code, and walks.', profile)
        self.assertIn(moment['id'], profile)
        self.assertIn('Owner legacy name.', profile)
        self.assertEqual(self.app.accounts.resolve('xfz')['name'], 'Xufeng')
        legacy = self.root / '2026/10/03/legacy/index.md'
        legacy.parent.mkdir(parents=True)
        legacy.write_text('+++\ndate="2026-10-03T12:00:00+08:00"\nname="Frank"\navatar="default-avatar.png"\n+++\n\nLegacy user moment.\n')
        self.assertIsNone(self.app.build())
        self.assertIn('Legacy user moment.', self.call('GET', '/users/frank/')[1].decode())
        self.assertNotIn('Legacy user moment.', self.call('GET', '/users/xfz/')[1].decode())
        output = Path(self.temporary.name) / 'export'
        built = subprocess.run(['hugo', '--minify', '--contentDir', str(self.root), '--destination', str(output)], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(built.returncode, 0, built.stderr)
        self.assertIn('Coffee, code, and walks.', (output / 'users/xfz/index.html').read_text())
        self.assertEqual((output / avatar.lstrip('/')).read_bytes(), PNG)
        self.assertFalse((output / '.studio').exists())
        self.assertEqual(list(output.rglob('accounts.json')), [])
        public_text = ''.join(path.read_text() for path in output.rglob('*') if path.suffix in ('.html', '.xml', '.json'))
        self.assertNotIn('owner-password', public_text)
        self.assertNotIn('"credential"', public_text)


if __name__ == '__main__':
    unittest.main()
