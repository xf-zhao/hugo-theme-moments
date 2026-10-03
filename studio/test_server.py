"""Persistence, HTTP, and Hugo integration checks. All data is temporary."""

import base64
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from studio.server import App, Handler, MAX_REQUEST, Problem, REPO, Store, read_document, write_document

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jV1cAAAAASUVORK5CYII=')


def payload(**changes):
    result = {'date': '2026-10-03T14:30:00+08:00', 'body': 'A moment worth keeping.', 'tags': ['daily'], 'draft': False}
    result.update(changes)
    return result


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='moments-store-test-')
        self.root = Path(self.temporary.name).resolve()
        self.store = Store(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_post_creates_dated_bundle_and_preserves_unicode(self):
        moment = self.store.save(payload(body='Coffee, rain, and a walk. ☕ 下雨了'), [('rain.png', PNG)])
        folder = self.root / moment['folder']
        self.assertEqual(folder.parent, self.root / '2026/10/03')
        metadata, body = read_document(folder / 'index.md')
        self.assertIn('☕ 下雨了', body)
        self.assertEqual(metadata['date'], '2026-10-03T14:30:00+08:00')
        self.assertEqual((folder / metadata['pictures'][0]).read_bytes(), PNG)

    def test_edit_moves_bundle_and_preserves_photos_comments_and_url(self):
        moment = self.store.save(payload(), [('original.png', PNG)])
        self.store.interact(moment['id'], 'comments', {'text': 'Remember this'}, 'Me')
        before = (self.root / moment['folder'] / 'index.md').read_bytes()
        changed = self.store.save(payload(date='2027-01-02T10:20:00+08:00', body='Updated.', revision=moment['revision']), [], moment['id'])
        folder = self.root / changed['folder']
        self.assertEqual(folder.parent, self.root / '2027/01/02')
        self.assertEqual(changed['url'], moment['url'])
        self.assertEqual(changed['social']['comments'][0]['text'], 'Remember this')
        self.assertEqual((folder / changed['pictures'][0]).read_bytes(), PNG)
        self.assertEqual(next((folder / '.history').glob('*.md')).read_bytes(), before)
        self.assertFalse((self.root / moment['folder']).exists())

    def test_stale_edit_cannot_overwrite_a_newer_version(self):
        moment = self.store.save(payload(), [])
        self.store.save(payload(body='New version', revision=moment['revision']), [], moment['id'])
        with self.assertRaises(Problem) as raised:
            self.store.save(payload(body='Stale version', revision=moment['revision']), [], moment['id'])
        self.assertEqual(raised.exception.status, 409)
        self.assertIn('New version', self.store.summary(self.store.find(moment['id']))['body'])

    def test_concurrent_edits_have_one_winner(self):
        moment = self.store.save(payload(), [])
        def edit(text):
            try:
                self.store.save(payload(body=text, revision=moment['revision']), [], moment['id'])
                return 'saved'
            except Problem as error:
                return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(edit, ['First version', 'Second version']))
        self.assertCountEqual(result, ['saved', 409])

    def test_comments_replies_edits_and_likes_survive_restart(self):
        moment = self.store.save(payload(), [])
        social = self.store.interact(moment['id'], 'comments', {'text': '<script>plain text</script>'}, 'Me')
        first = social['comments'][0]
        self.store.interact(moment['id'], 'comments', {'text': 'A reply', 'parent_id': first['id']}, 'Me')
        self.store.edit_comment(moment['id'], first['id'], {'text': 'Edited comment'})
        self.store.interact(moment['id'], 'like', {'liked': True}, 'Me')
        reloaded = Store(self.root).summary(self.store.find(moment['id']))['social']
        self.assertTrue(reloaded['liked'])
        self.assertEqual(reloaded['comments'][0]['text'], 'Edited comment')
        self.assertEqual(reloaded['comments'][1]['parent_id'], first['id'])
        self.assertIn('edited_at', reloaded['comments'][0])

    def test_invalid_post_does_not_create_data(self):
        for changes, files in [({'body': ''}, []), ({'link': 'javascript:alert(1)'}, []), ({'date': '../2026'}, []), ({'tags': 'not a list'}, []), ({'draft': 'false'}, []), ({}, [('fake.jpg', b'not an image')])]:
            with self.subTest(changes=changes):
                with self.assertRaises(Problem):
                    self.store.save(payload(**changes), files)
                self.assertEqual(self.store.posts(), [])

    def test_uploaded_filename_and_symlinks_cannot_escape_root(self):
        moment = self.store.save(payload(), [('../../outside.png', PNG)])
        self.assertTrue((self.root / moment['folder'] / moment['pictures'][0]).resolve().is_relative_to(self.root))
        with tempfile.TemporaryDirectory(prefix='moments-outside-') as outside:
            link = self.root / '2027'
            link.symlink_to(Path(outside), target_is_directory=True)
            with self.assertRaises(Problem) as raised:
                self.store.save(payload(date='2027-01-01T00:00:00+08:00'), [])
            self.assertEqual(raised.exception.status, 403)
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_removed_picture_keeps_original_file(self):
        moment = self.store.save(payload(), [('original.png', PNG)])
        picture = self.root / moment['folder'] / moment['pictures'][0]
        edited = self.store.save(payload(pictures=[], revision=moment['revision']), [], moment['id'])
        self.assertEqual(edited['pictures'], [])
        self.assertEqual(picture.read_bytes(), PNG)

    def test_manual_toml_bundle_can_be_edited_without_changing_its_url(self):
        path = self.root / '2026/10/03/manual/index.md'
        path.parent.mkdir(parents=True)
        path.write_text('+++\ndate = "2026-10-03T12:00:00+08:00"\ndraft = true\ntags = []\n+++\n\nA manual draft.\n', encoding='utf-8')
        moment = self.store.summary(self.store.posts()[0])
        self.assertEqual(moment['url'], '/2026/10/03/manual/')
        edited = self.store.save(payload(date='2026-11-04T12:00:00+08:00', revision=moment['revision']), [], moment['id'])
        self.assertEqual(edited['url'], moment['url'])
        self.assertEqual(edited['folder'], '2026/11/04/manual')

    def test_hide_edit_and_unhide_preserve_draft_build_settings_and_history(self):
        moment = self.store.save(payload(draft=True), [('photo.png', PNG)])
        path = self.root / moment['folder'] / 'index.md'
        metadata, body = read_document(path)
        original_build = {'publishResources': False, 'render': 'link', 'list': 'local'}
        metadata['build'] = original_build
        write_document(path, metadata, body)
        moment = self.store.summary(self.store.find(moment['id']))
        self.assertFalse(moment['hidden'])
        original = path.read_bytes()
        hidden = self.store.set_hidden(moment['id'], {'hidden': True, 'revision': moment['revision']})
        metadata, _ = read_document(path)
        self.assertTrue(hidden['hidden'])
        self.assertTrue(hidden['draft'])
        self.assertEqual(metadata['studio_hidden_build'], original_build)
        self.assertEqual(metadata['build']['render'], 'never')
        self.assertEqual(metadata['build']['list'], 'never')
        self.assertEqual(next((path.parent / '.history').glob('*.md')).read_bytes(), original)
        edited = self.store.save(payload(body='Edited while hidden', draft=True, revision=hidden['revision'], date='2027-01-01T08:00:00+08:00'), [], moment['id'])
        self.assertTrue(edited['hidden'])
        path = self.root / edited['folder'] / 'index.md'
        self.assertEqual(read_document(path)[0]['build']['render'], 'never')
        shown = self.store.set_hidden(moment['id'], {'hidden': False, 'revision': edited['revision']})
        metadata, _ = read_document(path)
        self.assertFalse(shown['hidden'])
        self.assertTrue(shown['draft'])
        self.assertEqual(metadata['build'], original_build)
        self.assertNotIn('studio_hidden_build', metadata)
        self.assertEqual((path.parent / shown['pictures'][0]).read_bytes(), PNG)

    def test_visibility_and_delete_require_current_revision(self):
        moment = self.store.save(payload(), [])
        with self.assertRaises(Problem):
            self.store.set_hidden(moment['id'], {'hidden': 'true', 'revision': moment['revision']})
        self.assertEqual(self.store.summary(self.store.find(moment['id']))['revision'], moment['revision'])
        hidden = self.store.set_hidden(moment['id'], {'hidden': True, 'revision': moment['revision']})
        unchanged = self.store.set_hidden(moment['id'], {'hidden': True, 'revision': hidden['revision']})
        self.assertEqual(unchanged['revision'], hidden['revision'])
        for operation, changes in [(self.store.set_hidden, {'hidden': False}), (self.store.delete, {})]:
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(Problem) as raised:
                    operation(moment['id'], {**changes, 'revision': moment['revision']})
                self.assertEqual(raised.exception.status, 409)
        self.assertTrue(self.store.summary(self.store.find(moment['id']))['hidden'])

    def test_delete_preserves_entire_bundle_in_recoverable_trash(self):
        moment = self.store.save(payload(), [('photo.png', PNG)])
        self.store.interact(moment['id'], 'comments', {'text': 'Keep this comment'}, 'Me')
        edited = self.store.save(payload(body='Updated before deletion', revision=moment['revision']), [], moment['id'])
        folder = self.root / edited['folder']
        before = {file.relative_to(folder).as_posix(): file.read_bytes() for file in folder.rglob('*') if file.is_file()}
        deleted = self.store.delete(moment['id'], {'revision': edited['revision']})
        self.assertEqual(deleted['id'], moment['id'])
        self.assertFalse(folder.exists())
        self.assertEqual(self.store.posts(), [])
        trash = self.root / deleted['trashed_folder']
        self.assertTrue(trash.is_relative_to(self.root / '.trash'))
        for relative, data in before.items():
            self.assertEqual((trash / relative).read_bytes(), data)
        manifest = json.loads((trash / 'trash.json').read_text())
        self.assertEqual(manifest['id'], moment['id'])
        self.assertEqual(manifest['original_folder'], edited['folder'])
        self.assertIn('deleted_at', manifest)
        with self.assertRaises(Problem) as raised:
            self.store.find(moment['id'])
        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(Store(self.root).posts(), [])

    def test_trash_symlink_cannot_move_data_outside_root(self):
        moment = self.store.save(payload(), [])
        with tempfile.TemporaryDirectory(prefix='moments-trash-outside-') as outside:
            (self.root / '.trash').symlink_to(Path(outside), target_is_directory=True)
            with self.assertRaises(Problem) as raised:
                self.store.delete(moment['id'], {'revision': moment['revision']})
            self.assertEqual(raised.exception.status, 403)
            self.assertTrue((self.root / moment['folder'] / 'index.md').exists())
            self.assertEqual(list(Path(outside).iterdir()), [])


class HugoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='moments-hugo-test-')
        self.root = Path(self.temporary.name) / 'data'
        self.app = App(self.root, port=1327)

    def tearDown(self):
        self.app.temporary.cleanup()
        self.temporary.cleanup()

    def test_editor_build_and_static_export_include_correct_data(self):
        public = self.app.store.save(payload(body='Published text'), [('photo.png', PNG)])
        draft = self.app.store.save(payload(body='Private draft text', draft=True), [])
        self.app.store.interact(public['id'], 'comments', {'text': 'Local comment source'}, 'Me')
        self.app.store.save(payload(body='Published updated text', revision=public['revision']), [], public['id'])
        self.assertIsNone(self.app.build())
        html = (self.app.site_root / 'index.html').read_text()
        self.assertIn('id="studio-new"', html)
        self.assertIn('Private draft text', html)
        self.assertIn('studio-draft-badge', html)
        self.assertIn('Published updated text', (self.app.site_root / '2026/index.html').read_text())
        photo = public['url'].lstrip('/') + public['pictures'][0]
        self.assertEqual((self.app.site_root / photo).read_bytes(), PNG)
        output = Path(self.temporary.name) / 'public'
        result = subprocess.run(['hugo', '--minify', '--contentDir', str(self.root), '--destination', str(output)], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        html = (output / 'index.html').read_text()
        self.assertIn('Published updated text', html)
        self.assertNotIn('Private draft text', html)
        self.assertNotIn('studio-new', html)
        self.assertFalse((output / draft['url'].lstrip('/')).exists())
        self.assertEqual(list(output.rglob('social.json')), [])
        self.assertFalse(any('.history' in path.parts for path in output.rglob('*')))
        self.assertEqual((output / photo).read_bytes(), PNG)

    def test_bad_shortcode_is_saved_and_previous_preview_survives(self):
        moment = self.app.store.save(payload(), [])
        self.assertIsNone(self.app.build())
        old_preview = self.app.site_root
        saved = self.app.store.save(payload(body='{{< nonexistent-shortcode >}}', revision=moment['revision']), [], moment['id'])
        self.assertTrue(self.app.build())
        self.assertEqual(self.app.site_root, old_preview)
        self.assertIn('nonexistent-shortcode', self.app.store.summary(self.app.store.find(moment['id']))['body'])
        self.app.store.save(payload(body='Corrected', revision=saved['revision']), [], moment['id'])
        self.assertIsNone(self.app.build())
        self.assertIn('Corrected', (self.app.site_root / 'index.html').read_text())

    def test_hidden_and_trashed_posts_are_absent_from_preview_and_exports(self):
        public = self.app.store.save(payload(body='Visible public moment'), [])
        hidden = self.app.store.save(payload(body='HIDDEN_PRIVATE_TEXT', tags=['hidden-unique-tag']), [('hidden.png', PNG)])
        deleted = self.app.store.save(payload(body='DELETED_PRIVATE_TEXT'), [('deleted.png', PNG)])
        output = Path(self.temporary.name) / 'public'
        command = ['hugo', '--minify', '--contentDir', str(self.root), '--destination', str(output)]
        initial = subprocess.run(command, cwd=REPO, capture_output=True, text=True)
        self.assertEqual(initial.returncode, 0, initial.stderr)
        for moment in (hidden, deleted):
            self.assertTrue((output / moment['url'].lstrip('/') / 'index.html').exists())
        self.app.store.set_hidden(hidden['id'], {'hidden': True, 'revision': hidden['revision']})
        self.app.store.delete(deleted['id'], {'revision': deleted['revision']})
        self.assertIsNone(self.app.build())
        result = subprocess.run(command, cwd=REPO, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for generated in (self.app.site_root, output):
            with self.subTest(generated=generated):
                self.assertIn('Visible public moment', (generated / 'index.html').read_text())
                self.assertFalse((generated / 'tags/hidden-unique-tag').exists())
                for moment in (hidden, deleted):
                    self.assertFalse((generated / moment['url'].lstrip('/')).exists())
                    self.assertFalse(any((generated / moment['url'].lstrip('/') / picture).exists() for picture in moment['pictures']))
                self.assertFalse(any('.trash' in path.parts for path in generated.rglob('*')))
                for file in generated.rglob('*'):
                    if file.suffix in ('.html', '.xml', '.json'):
                        text = file.read_text(encoding='utf-8')
                        self.assertNotIn('HIDDEN_PRIVATE_TEXT', text, file)
                        self.assertNotIn('DELETED_PRIVATE_TEXT', text, file)
                        self.assertNotIn('hidden-unique-tag', text, file)
                        if file.suffix == '.xml':
                            self.assertNotIn(hidden['url'], text, file)
                            self.assertNotIn(deleted['url'], text, file)
        stored = self.app.store.summary(self.app.store.find(hidden['id']))
        self.assertTrue(stored['hidden'])
        self.app.store.set_hidden(hidden['id'], {'hidden': False, 'revision': stored['revision']})
        self.assertIsNone(self.app.build())
        self.assertIn('HIDDEN_PRIVATE_TEXT', (self.app.site_root / 'index.html').read_text())
        self.assertTrue((self.app.site_root / hidden['url'].lstrip('/') / 'index.html').exists())

    def test_visibility_change_invalidates_stale_preview_if_rebuild_fails(self):
        moment = self.app.store.save(payload(body='Previously visible'), [])
        self.assertIsNone(self.app.build())
        old_preview = self.app.site_root
        self.app.store.save(payload(body='{{< nonexistent-shortcode >}}'), [])
        self.app.store.set_hidden(moment['id'], {'hidden': True, 'revision': moment['revision']})
        self.assertTrue(self.app.build(invalidate_on_error=True))
        self.assertIsNone(self.app.site_root)
        self.assertFalse(old_preview.exists())
        self.assertTrue(self.app.store.summary(self.app.store.find(moment['id']))['hidden'])

    def test_import_preserves_existing_yaml_draft(self):
        source = Path(self.temporary.name) / 'old-content'
        source.mkdir()
        (source / 'first-moment.md').write_text('---\ndate: 2026-10-03T12:00:00+08:00\ndraft: true\ntags: [daily]\n---\n\nAn existing draft.\n', encoding='utf-8')
        self.app.store.import_content(source)
        post = self.app.store.summary(self.app.store.posts()[0])
        self.assertEqual(post['folder'], '2026/10/03/first-moment')
        self.assertTrue(post['draft'])
        self.assertEqual(post['tags'], ['daily'])
        self.assertIn('An existing draft.', post['body'])
        self.assertTrue((source / 'first-moment.md').exists())
        self.assertEqual((self.root / post['folder'] / '.history/imported.md').read_text(), (source / 'first-moment.md').read_text())
        self.app.store.import_content(source)
        self.assertEqual(len(self.app.store.posts()), 1)


class QuietHandler(Handler):
    def log_message(self, *args):
        pass


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='moments-http-test-')
        cls.app = App(Path(cls.temporary.name) / 'data')
        owner = cls.app.accounts.setup('fixture-password')
        session = cls.app.accounts.new_session(owner)
        cls.auth_cookie = f'Moments-Session={session}'
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        cls.app.port = cls.server.server_port
        cls.server.app = cls.app
        cls.app.build()
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
        headers.setdefault('Cookie', self.auth_cookie)
        if body is not None and isinstance(body, dict):
            body = json.dumps(body).encode()
            headers.setdefault('Content-Type', 'application/json')
        connection = HTTPConnection('127.0.0.1', self.app.port, timeout=15)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        content_type = response.getheader('Content-Type', '')
        result = json.loads(raw) if 'application/json' in content_type else raw
        status = response.status
        connection.close()
        return status, result

    def test_post_edit_comment_reply_and_like_through_http(self):
        boundary = 'moments-test-boundary'
        multipart = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="post"\r\n\r\n'.encode()
            + json.dumps(payload()).encode()
            + f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="pictures"; filename="photo.png"\r\nContent-Type: image/png\r\n\r\n'.encode()
            + PNG + f'\r\n--{boundary}--\r\n'.encode()
        )
        headers = {'X-Moments-Token': self.app.token, 'Content-Type': f'multipart/form-data; boundary={boundary}'}
        status, saved = self.call('POST', '/api/moments', multipart, headers)
        self.assertEqual(status, 201, saved)
        moment = saved['moment']
        headers = {'X-Moments-Token': self.app.token}
        url = f'/api/moments/{moment["id"]}'
        status, edited = self.call('PUT', url, payload(body='Edited through HTTP', revision=moment['revision']), headers)
        self.assertEqual(status, 200, edited)
        self.assertEqual(self.call('PUT', url, payload(revision=moment['revision']), headers)[0], 409)
        status, social = self.call('POST', url + '/comments', {'text': 'Persistent comment'}, headers)
        self.assertEqual(status, 200)
        first = social['comments'][0]['id']
        status, social = self.call('POST', url + '/comments', {'text': 'Persistent reply', 'parent_id': first}, headers)
        self.assertEqual(status, 200)
        self.assertEqual(social['comments'][1]['parent_id'], first)
        self.assertEqual(self.call('PUT', url + '/comments/' + first, {'text': 'Edited comment'}, headers)[0], 200)
        self.assertTrue(self.call('POST', url + '/like', {'liked': True}, headers)[1]['liked'])
        self.assertEqual(self.call('GET', moment['url'])[0], 200)
        self.assertEqual(self.call('GET', moment['url'] + moment['pictures'][0])[1], PNG)
        self.assertEqual(self.call('GET', moment['url'] + 'social.json')[0], 404)
        self.assertEqual(self.call('GET', moment['url'] + 'index.md')[0], 404)

    def test_foreign_origin_host_and_missing_token_cannot_write(self):
        self.assertEqual(self.call('POST', '/api/moments', payload())[0], 403)
        headers = {'X-Moments-Token': self.app.token, 'Origin': 'https://other-site.example'}
        self.assertEqual(self.call('POST', '/api/moments', payload(), headers)[0], 403)
        self.assertEqual(self.call('GET', '/api/session', headers={'Host': 'other-site.example'})[0], 403)
        self.assertEqual(self.call('GET', '/../../hugo.yaml')[0], 404)
        self.assertEqual(self.call('GET', '/.history/source.md')[0], 404)

    def test_hide_unhide_delete_and_private_photo_access_through_http(self):
        moment = self.app.store.save(payload(body='Hide and show via HTTP'), [('secret.png', PNG)])
        self.app.build()
        url = '/api/moments/' + moment['id']
        headers = {'X-Moments-Token': self.app.token}
        changes = {'hidden': True, 'revision': moment['revision']}
        self.assertEqual(self.call('PUT', url + '/visibility', changes)[0], 403)
        self.assertEqual(self.call('DELETE', url, {'revision': moment['revision']})[0], 403)
        status, saved = self.call('PUT', url + '/visibility', changes, headers)
        self.assertEqual(status, 200, saved)
        self.assertIsNone(saved['build_error'])
        hidden = saved['moment']
        self.assertTrue(hidden['hidden'])
        self.assertTrue(self.call('GET', url)[1]['hidden'])
        self.assertTrue(any(post['id'] == moment['id'] and post['hidden'] for post in self.call('GET', '/api/moments')[1]['moments']))
        self.assertEqual(self.call('GET', moment['url'])[0], 404)
        self.assertEqual(self.call('GET', moment['url'] + moment['pictures'][0])[0], 404)
        photo_route = url + '/' + moment['pictures'][0]
        self.assertEqual(self.call('GET', photo_route), (200, PNG))
        self.assertEqual(self.call('HEAD', photo_route), (200, b''))
        self.assertEqual(self.call('GET', url + '/pictures/../index.md')[0], 404)
        self.assertEqual(self.call('GET', url + '/pictures/%2e%2e/social.json')[0], 404)
        self.assertEqual(self.call('GET', url + '/pictures/missing.png')[0], 404)
        self.assertEqual(self.call('GET', photo_route, headers={'Host': 'other-site.example'})[0], 403)
        self.assertEqual(self.call('DELETE', url, {'revision': moment['revision']}, headers)[0], 409)
        status, shown = self.call('PUT', url + '/visibility', {'hidden': False, 'revision': hidden['revision']}, headers)
        self.assertEqual(status, 200, shown)
        self.assertFalse(shown['moment']['hidden'])
        self.assertEqual(self.call('GET', moment['url'])[0], 200)
        status, deleted = self.call('DELETE', url, {'revision': shown['moment']['revision']}, headers)
        self.assertEqual(status, 200, deleted)
        self.assertIsNone(deleted['build_error'])
        self.assertEqual(deleted['id'], moment['id'])
        self.assertEqual(self.call('GET', url)[0], 404)
        self.assertEqual(self.call('GET', moment['url'])[0], 404)
        self.assertEqual(self.call('GET', photo_route)[0], 404)
        self.assertEqual(self.call('GET', '/' + deleted['trashed_folder'] + '/index.md')[0], 404)
        self.assertTrue((self.app.store.root / deleted['trashed_folder'] / 'trash.json').exists())

    def test_malformed_and_oversized_requests_are_rejected(self):
        headers = {'X-Moments-Token': self.app.token, 'Content-Type': 'application/json'}
        self.assertEqual(self.call('POST', '/api/moments', b'not JSON', headers)[0], 400)
        headers['Content-Length'] = str(MAX_REQUEST + 1)
        self.assertEqual(self.call('POST', '/api/moments', b'', headers)[0], 413)


if __name__ == '__main__':
    unittest.main()
