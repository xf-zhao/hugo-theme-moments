"""Profile, credential, ownership, and session checks using temporary data."""

import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from studio.accounts import Accounts, SESSION_SECONDS
from studio.server import MAX_PICTURE, Problem, read_document, write_document

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jV1cAAAAASUVORK5CYII=')
PASSWORD = 'correct horse battery staple'


class AccountsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='moments-accounts-test-')
        self.root = Path(self.temporary.name).resolve()
        self.accounts = Accounts(self.root, name='xfz', bio='Life, thoughts, and discoveries.')

    def tearDown(self):
        self.temporary.cleanup()

    def member(self, **changes):
        payload = {'id': 'alice', 'name': 'Alice', 'password': PASSWORD, 'bio': 'Small daily discoveries.'}
        payload.update(changes)
        return self.accounts.create(payload)

    def test_owner_setup_stores_hash_and_keeps_public_profile_separate(self):
        self.assertTrue(self.accounts.setup_required)
        owner = self.accounts.setup(PASSWORD)
        self.assertEqual(owner['id'], 'xfz')
        self.assertTrue(owner['can_login'])
        self.assertFalse(self.accounts.setup_required)
        state = json.loads((self.root / '.studio/accounts.json').read_text())
        self.assertNotIn(PASSWORD, json.dumps(state))
        credential = state['users']['xfz']['credential']
        self.assertEqual(credential['scheme'], 'scrypt')
        self.assertEqual(len(bytes.fromhex(credential['salt'])), 32)
        metadata, body = read_document(self.root / 'users/xfz/_index.md')
        self.assertTrue(metadata['default_user'])
        self.assertEqual(body.strip(), owner['bio'])
        self.assertEqual(metadata['url'], '/users/xfz/')
        self.assertNotIn('credential', metadata)
        self.assertNotIn('role', metadata)
        self.assertNotIn('password', metadata)
        self.assertEqual(self.accounts.login('XFZ', PASSWORD)['id'], 'xfz')
        with self.assertRaises(Problem) as raised:
            self.accounts.setup(PASSWORD)
        self.assertEqual(raised.exception.status, 409)

    def test_login_failure_and_password_validation(self):
        for invalid in ('short', '', None, 'x' * 1025):
            with self.assertRaises(Problem):
                self.accounts.setup(invalid)
        self.assertTrue(self.accounts.setup_required)
        self.accounts.setup(PASSWORD)
        for user_id, password in [('missing', PASSWORD), ('xfz', 'incorrect'), ('xfz', '')]:
            with self.assertRaises(Problem) as raised:
                self.accounts.login(user_id, password)
            self.assertEqual(raised.exception.status, 401)

    def test_owner_id_password_and_profiles_survive_restart_and_rename(self):
        self.accounts.setup(PASSWORD)
        self.accounts.update('xfz', {'name': 'Xufeng Zhao', 'bio': 'Updated profile'})
        self.member()
        restarted = Accounts(self.root, name='Different Config Name', bio='different config bio')
        self.assertEqual(restarted.default_id, 'xfz')
        self.assertEqual(restarted.user('xfz')['name'], 'Xufeng Zhao')
        self.assertEqual(restarted.login('Xufeng Zhao', PASSWORD)['id'], 'xfz')
        self.assertEqual(restarted.login('alice', PASSWORD)['role'], 'user')
        self.assertEqual(len(restarted.users()), 2)

    def test_equal_passwords_get_unique_salts(self):
        self.accounts.setup(PASSWORD)
        self.member()
        credentials = self.accounts.state['users']
        self.assertNotEqual(credentials['xfz']['credential']['salt'], credentials['alice']['credential']['salt'])
        self.assertNotEqual(credentials['xfz']['credential']['hash'], credentials['alice']['credential']['hash'])

    def test_sessions_expire_logout_and_do_not_survive_restart(self):
        owner = self.accounts.setup(PASSWORD)
        with patch('studio.accounts.time.monotonic', return_value=1000):
            token = self.accounts.new_session(owner)
            self.assertEqual(self.accounts.session(token)['id'], 'xfz')
            self.assertIsNone(self.accounts.session('unknown'))
            self.assertIsNone(Accounts(self.root).session(token))
        with patch('studio.accounts.time.monotonic', return_value=1000 + SESSION_SECONDS):
            self.assertIsNone(self.accounts.session(token))
        token = self.accounts.new_session(owner)
        self.accounts.logout(token)
        self.assertIsNone(self.accounts.session(token))

    def test_owner_manages_all_posts_members_only_their_own(self):
        owner = self.accounts.setup(PASSWORD)
        member = self.member()
        self.assertTrue(self.accounts.can_manage(owner, 'alice'))
        self.assertTrue(self.accounts.can_manage(owner, None))
        self.assertTrue(self.accounts.can_manage(member, 'alice'))
        self.assertFalse(self.accounts.can_manage(member, 'xfz'))
        self.assertFalse(self.accounts.can_manage(member, None))
        self.assertFalse(self.accounts.can_manage(None, 'alice'))
        self.assertFalse(self.accounts.can_manage({'id': 'missing', 'role': 'owner'}, 'alice'))
        self.assertFalse(self.accounts.can_manage({**member, 'role': 'owner'}, 'xfz'))

    def test_profile_update_keeps_extras_and_original_avatar(self):
        path = self.root / 'users/xfz/_index.md'
        metadata, body = read_document(path)
        metadata['custom'] = 'preserve me'
        write_document(path, metadata, body)
        first = self.accounts.update('xfz', {'name': 'Xufeng', 'bio': '☕ A good morning'}, [('me.png', PNG)])
        second = self.accounts.update('xfz', {'bio': 'Next day'}, [('new.png', PNG)])
        first_path = self.root / first['avatar'].lstrip('/')
        second_path = self.root / second['avatar'].lstrip('/')
        self.assertEqual(first_path.read_bytes(), PNG)
        self.assertEqual(second_path.read_bytes(), PNG)
        self.assertNotEqual(first_path, second_path)
        metadata, body = read_document(path)
        self.assertEqual(metadata['custom'], 'preserve me')
        self.assertEqual(metadata['name'], 'Xufeng')
        self.assertEqual(body.strip(), 'Next day')
        self.assertFalse(metadata['build']['publishResources'])
        self.assertTrue(metadata['default_user'])

    def test_legacy_profiles_can_be_viewed_then_registered(self):
        legacy = self.accounts.import_legacy('Coffee Friend', 'friend.png')
        self.assertEqual(legacy['id'], 'coffee-friend')
        self.assertFalse(legacy['can_login'])
        self.assertEqual(legacy['avatar'], '/friend.png')
        self.assertEqual(self.accounts.resolve('COFFEE FRIEND')['id'], legacy['id'])
        self.assertEqual(self.accounts.import_legacy('Coffee Friend', 'new.png'), legacy)
        self.assertFalse(read_document(self.root / 'users/coffee-friend/_index.md')[0]['default_user'])
        with self.assertRaises(Problem) as raised:
            self.accounts.login('coffee-friend', PASSWORD)
        self.assertEqual(raised.exception.status, 401)
        registered = self.accounts.create({'id': legacy['id'], 'password': PASSWORD})
        self.assertEqual(registered['name'], legacy['name'])
        self.assertEqual(registered['avatar'], legacy['avatar'])
        self.assertTrue(registered['can_login'])
        self.assertEqual(self.accounts.login(registered['id'], PASSWORD)['id'], registered['id'])
        with self.assertRaises(Problem) as raised:
            self.accounts.create({'id': legacy['id'], 'password': PASSWORD})
        self.assertEqual(raised.exception.status, 409)

    def test_legacy_slug_collisions_and_unicode_names_get_distinct_profiles(self):
        alice = self.accounts.import_legacy('Alice Smith')
        other = self.accounts.import_legacy('Alice.Smith')
        chinese = self.accounts.import_legacy('小李')
        self.assertNotEqual(alice['id'], other['id'])
        self.assertTrue(other['id'].startswith('alice-smith-'))
        self.assertTrue(chinese['id'].startswith('user-'))
        self.assertEqual(self.accounts.resolve('小李')['id'], chinese['id'])

    def test_renamed_users_keep_legacy_name_aliases_after_restart(self):
        legacy = self.accounts.import_legacy('Coffee Friend')
        self.accounts.update(legacy['id'], {'name': 'Morning Friend'})
        self.accounts.update(legacy['id'], {'name': 'Rainy Friend'})
        self.accounts.update('xfz', {'name': 'Xufeng Zhao'})
        self.accounts.update('xfz', {'name': 'Future Owner Name'})
        restarted = Accounts(self.root)
        self.assertEqual(restarted.resolve('COFFEE FRIEND')['id'], legacy['id'])
        self.assertEqual(restarted.resolve('Morning Friend')['id'], legacy['id'])
        self.assertEqual(restarted.resolve('Xufeng Zhao')['id'], 'xfz')
        self.assertEqual(restarted.resolve('xfz')['id'], 'xfz')
        self.assertEqual(restarted.import_legacy('Coffee Friend')['id'], legacy['id'])
        metadata, _ = read_document(self.root / f"users/{legacy['id']}/_index.md")
        self.assertEqual(metadata['previous_names'], ['Coffee Friend', 'Morning Friend'])
        self.assertEqual(len(restarted.users()), 2)

    def test_invalid_profile_changes_do_not_change_existing_profile(self):
        path = self.root / 'users/xfz/_index.md'
        original = path.read_bytes()
        invalid = [({'name': ''}, []), ({'bio': 'x' * 2001}, []), ({'avatar': '../.studio/accounts.json'}, []),
                   ({'avatar': 'javascript:alert(1)'}, []), ({}, [('bad.png', b'not an image')]),
                   ({}, [('big.png', PNG + b'x' * MAX_PICTURE)]), ({}, [('one.png', PNG), ('two.png', PNG)])]
        for payload, files in invalid:
            with self.assertRaises(Problem):
                self.accounts.update('xfz', payload, files)
            self.assertEqual(path.read_bytes(), original)

    def test_invalid_user_ids_and_symlinks_cannot_write_outside_profiles(self):
        for user_id in ('../outside', 'a/b', '.private', 'a' * 41):
            with self.assertRaises(Problem):
                self.member(id=user_id)
        with tempfile.TemporaryDirectory(prefix='moments-profile-outside-') as outside:
            (self.root / 'users/escape').symlink_to(Path(outside), target_is_directory=True)
            with self.assertRaises(Problem) as raised:
                self.member(id='escape')
            self.assertEqual(raised.exception.status, 403)
            self.assertEqual(list(Path(outside).iterdir()), [])
        with self.assertRaises(Problem) as raised:
            self.accounts.user('unknown')
        self.assertEqual(raised.exception.status, 404)

    def test_corrupt_account_file_is_preserved(self):
        path = self.root / '.studio/accounts.json'
        path.write_text('{invalid json')
        with self.assertRaises(Problem) as raised:
            Accounts(self.root)
        self.assertEqual(raised.exception.status, 500)
        self.assertEqual(path.read_text(), '{invalid json')


if __name__ == '__main__':
    unittest.main()
