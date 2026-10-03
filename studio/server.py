#!/usr/bin/env python3
"""A loopback-only editor for a file-backed Hugo Moments timeline."""

from __future__ import annotations

import argparse
from datetime import date, datetime
from email import policy
from email.parser import BytesParser
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import tomllib
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path('/Users/xufeng/Workspace/Moments')
ZONE = ZoneInfo('Asia/Shanghai')
MAX_REQUEST = 64 * 1024 * 1024
MAX_PICTURE = 10 * 1024 * 1024
ID_RE = re.compile(r'^[a-zA-Z0-9_-]{1,80}$')


class Problem(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f'Cannot serialize {type(value).__name__}')


def read_document(path: Path):
    text = path.read_text(encoding='utf-8').lstrip('\ufeff')
    if text.startswith('+++\n'):
        header, separator, body = text[4:].partition('\n+++')
        if not separator:
            raise Problem(f'Missing closing front matter in {path.name}')
        return tomllib.loads(header), body.lstrip('\r\n')
    if text.startswith('{'):
        metadata, offset = json.JSONDecoder().raw_decode(text)
        if not isinstance(metadata, dict):
            raise Problem('Front matter must be an object')
        return metadata, text[offset:].lstrip('\r\n')
    raise Problem(f'{path.name} needs JSON or TOML front matter for editing')


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_document(path: Path, metadata: dict, body: str):
    header = json.dumps(metadata, ensure_ascii=False, indent=2, default=json_default)
    atomic_write(path, header + '\n\n' + body.rstrip('\n') + '\n')


def parse_date(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        raise Problem('Choose a valid date and time') from None
    if result.tzinfo is None:
        result = result.replace(tzinfo=ZONE)
    return result.astimezone(ZONE)


def text_field(payload, key, limit, default=''):
    value = payload.get(key, default)
    if not isinstance(value, str) or len(value) > limit:
        raise Problem(f'{key} must be text with at most {limit:,} characters')
    return value


def image_extension(data: bytes):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if data.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return '.gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return '.webp'
    if data[4:8] == b'ftyp' and any(brand in data[8:40] for brand in (b'avif', b'avis')):
        return '.avif'
    raise Problem('Use JPEG, PNG, GIF, WebP, or AVIF photos')


class Store:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def safe(self, path: Path):
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root):
            raise Problem('The file is outside the Moments folder', 403)
        return resolved

    def posts(self):
        posts = []
        for path in self.root.rglob('index.md'):
            relative = path.relative_to(self.root)
            if any(part.startswith('.') for part in relative.parts):
                continue
            self.safe(path)
            metadata, body = read_document(path)
            moment_id = metadata.get('moment_id') or hashlib.sha256(str(relative).encode()).hexdigest()[:24]
            if not isinstance(moment_id, str) or not ID_RE.fullmatch(moment_id):
                raise Problem(f'Invalid moment_id in {relative}')
            posts.append((moment_id, path, metadata, body))
        if len({post[0] for post in posts}) != len(posts):
            raise Problem('Two moments have the same moment_id')
        return posts

    def find(self, moment_id):
        if not ID_RE.fullmatch(moment_id):
            raise Problem('Moment not found', 404)
        for post in self.posts():
            if post[0] == moment_id:
                return post
        raise Problem('Moment not found', 404)

    @staticmethod
    def revision(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def social(self, path: Path):
        file = self.safe(path.parent / 'social.json')
        if file.exists():
            result = json.loads(file.read_text(encoding='utf-8'))
            if not isinstance(result, dict) or not isinstance(result.get('comments'), list):
                raise Problem('The comments file is invalid; it has not been changed', 500)
            return result
        return {'liked': False, 'comments': []}

    def summary(self, post):
        moment_id, path, metadata, body = post
        return {
            'id': moment_id,
            'date': parse_date(metadata['date']).isoformat(),
            'draft': bool(metadata.get('draft', False)),
            'body': body,
            'tags': metadata.get('tags', []),
            'pictures': metadata.get('pictures', []),
            'note': metadata.get('note', ''),
            'link': metadata.get('link', ''),
            'link_text': metadata.get('link_text', ''),
            'top': metadata.get('top', 0),
            'revision': self.revision(path),
            'url': metadata.get('url') or '/' + path.parent.relative_to(self.root).as_posix() + '/',
            'folder': str(path.parent.relative_to(self.root)),
            'social': self.social(path),
        }

    def save(self, payload, files, moment_id=None):
        with self.lock:
            previous = self.find(moment_id) if moment_id else None
            metadata = dict(previous[2]) if previous else {}
            if previous and payload.get('revision') != self.revision(previous[1]):
                raise Problem('This moment changed since you opened it. Reopen Edit before saving.', 409)
            body = text_field(payload, 'body', 100_000)
            when = parse_date(payload.get('date', datetime.now(ZONE).isoformat()))
            tags = payload.get('tags', [])
            if not isinstance(tags, list) or len(tags) > 30 or any(not isinstance(tag, str) or len(tag) > 80 for tag in tags):
                raise Problem('Use at most 30 tags, with at most 80 characters each')
            draft = payload.get('draft', False)
            if not isinstance(draft, bool):
                raise Problem('draft must be true or false')
            existing = metadata.get('pictures', [])
            keep = payload.get('pictures', existing if previous else [])
            if not isinstance(keep, list) or any(not isinstance(picture, str) or picture not in existing for picture in keep):
                raise Problem('Only photos already in this moment can be kept')
            if len(keep) + len(files) > 20:
                raise Problem('A moment can contain up to 20 photos')
            prepared = []
            for name, data in files:
                if len(data) > MAX_PICTURE:
                    raise Problem('Each photo must be 10 MB or smaller')
                extension = image_extension(data)
                original = name.replace('\\', '/').split('/')[-1]
                stem = re.sub(r'[^\w-]+', '-', Path(original).stem, flags=re.UNICODE).strip('-')[:60] or 'photo'
                prepared.append((f'pictures/{secrets.token_hex(4)}-{stem}{extension}', data))
            link = text_field(payload, 'link', 2000).strip()
            if link and (urlsplit(link).scheme not in ('http', 'https') or not urlsplit(link).netloc):
                raise Problem('Shared links must start with https:// or http://')
            top = payload.get('top', 0)
            if not isinstance(top, int) or isinstance(top, bool) or top < 0 or top > 1000:
                raise Problem('The pin order must be between 0 and 1000')
            if not body.strip() and not keep and not prepared and not link:
                raise Problem('Write something, choose a photo, or share a link')
            metadata.update({
                'date': when.isoformat(), 'draft': draft,
                'tags': list(dict.fromkeys(tag.strip().lstrip('#') for tag in tags if tag.strip().lstrip('#'))),
                'note': text_field(payload, 'note', 1000),
                'link': link, 'link_text': text_field(payload, 'link_text', 500), 'top': top,
            })
            moment_id = moment_id or secrets.token_hex(12)
            metadata['moment_id'] = moment_id
            metadata['url'] = metadata.get('url') or (self.summary(previous)['url'] if previous else f'/moments/{moment_id}/')
            metadata['pictures'] = list(keep) + [item[0] for item in prepared]
            # Only referenced resources are published; comments/history remain source data.
            metadata['build'] = {**metadata.get('build', {}), 'publishResources': False}
            day = self.safe(self.root / when.strftime('%Y/%m/%d'))
            folder = self.safe(day / (previous[1].parent.name if previous else moment_id))
            if previous:
                old_folder = previous[1].parent
                if folder != old_folder and folder.exists():
                    raise Problem('A moment already exists at the new date', 409)
                stamp = datetime.now(ZONE).strftime('%Y%m%dT%H%M%S%f')
                backup = self.safe(old_folder / '.history' / f'{stamp}.md')
                atomic_write(backup, previous[1].read_text(encoding='utf-8'))
                if folder != old_folder:
                    day.mkdir(parents=True, exist_ok=True)
                    old_folder.rename(folder)
            else:
                folder.mkdir(parents=True, exist_ok=False)
            for relative, data in prepared:
                target = self.safe(folder / relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as stream:
                    stream.write(data)
            path = self.safe(folder / 'index.md')
            write_document(path, metadata, body)
            return self.summary((moment_id, path, metadata, body))

    def interact(self, moment_id, action, payload, author):
        with self.lock:
            post = self.find(moment_id)
            social = self.social(post[1])
            if action == 'like':
                if not isinstance(payload.get('liked'), bool):
                    raise Problem('liked must be true or false')
                social['liked'] = payload['liked']
            elif action == 'comments':
                text = text_field(payload, 'text', 5000).strip()
                if not text:
                    raise Problem('Write a comment first')
                parent = payload.get('parent_id') or None
                if parent and parent not in {comment['id'] for comment in social['comments']}:
                    raise Problem('The comment you are replying to is no longer available', 409)
                name = text_field(payload, 'author', 80, author).strip() or author
                social['comments'].append({
                    'id': secrets.token_hex(12), 'author': name, 'text': text,
                    'date': datetime.now(ZONE).isoformat(), 'parent_id': parent,
                })
            else:
                raise Problem('Action not found', 404)
            atomic_write(self.safe(post[1].parent / 'social.json'), json.dumps(social, ensure_ascii=False, indent=2) + '\n')
            return social

    def edit_comment(self, moment_id, comment_id, payload):
        with self.lock:
            post = self.find(moment_id)
            social = self.social(post[1])
            comment = next((item for item in social['comments'] if item['id'] == comment_id), None)
            if not comment:
                raise Problem('Comment not found', 404)
            text = text_field(payload, 'text', 5000).strip()
            if not text:
                raise Problem('Write a comment first')
            comment['text'] = text
            comment['edited_at'] = datetime.now(ZONE).isoformat()
            atomic_write(self.safe(post[1].parent / 'social.json'), json.dumps(social, ensure_ascii=False, indent=2) + '\n')
            return social

    def import_content(self, source: Path):
        source = source.resolve()
        if self.posts() or not source.exists() or not any(source.rglob('*.md')):
            return
        with tempfile.TemporaryDirectory(prefix='moments-import-') as temporary:
            stage = Path(temporary)
            config = stage / 'hugo.toml'
            config.write_text('contentDir = ' + json.dumps(str(source.resolve())) + '\n', encoding='utf-8')
            converted = stage / 'converted'
            subprocess.run(['hugo', 'convert', 'toTOML', '--config', str(config), '--output', str(converted)], cwd=REPO, check=True, capture_output=True, text=True)
            for path in converted.rglob('*.md'):
                if path.name == '_index.md':
                    continue
                metadata, body = read_document(path)
                when = parse_date(metadata['date'])
                name = path.parent.name if path.name == 'index.md' else path.stem
                moment_id = re.sub(r'[^a-zA-Z0-9_-]', '-', name)[:60] or secrets.token_hex(12)
                folder = self.safe(self.root / when.strftime('%Y/%m/%d') / moment_id)
                folder.mkdir(parents=True, exist_ok=False)
                metadata.update({'date': when.isoformat(), 'moment_id': moment_id, 'url': f'/moments/{moment_id}/', 'build': {'publishResources': False}})
                write_document(folder / 'index.md', metadata, body)
                original = next((item for item in source.rglob('*.md') if path.as_posix().endswith('/' + item.relative_to(source).as_posix())), None)
                if original:
                    atomic_write(self.safe(folder / '.history' / 'imported.md'), original.read_text(encoding='utf-8'))


class App:
    def __init__(self, root: Path, port=1313, author='xf-zhao'):
        self.store = Store(root)
        self.port = port
        self.author = author
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.temporary = tempfile.TemporaryDirectory(prefix='moments-studio-')
        self.site_root = None
        self.generation = 0
        self.build_error = None
        self.fingerprint = None
        self.stopped = threading.Event()

    def source_fingerprint(self):
        digest = hashlib.sha256()
        for base in (self.store.root, REPO / 'layouts', REPO / 'assets', REPO / 'static', REPO / 'config'):
            if not base.exists():
                continue
            for path in sorted(base.rglob('*')):
                if path.is_file() and not any(part.startswith('.') for part in path.relative_to(base).parts) and path.name != 'social.json':
                    stat = path.stat()
                    digest.update(f'{path}:{stat.st_mtime_ns}:{stat.st_size}'.encode())
        digest.update((REPO / 'hugo.yaml').read_bytes())
        return digest.hexdigest()

    def build(self):
        with self.lock:
            self.generation += 1
            output = Path(self.temporary.name).resolve() / str(self.generation)
            try:
                result = subprocess.run([
                    'hugo', '--environment', 'development', '--buildDrafts', '--buildFuture',
                    '--contentDir', str(self.store.root), '--destination', str(output),
                    '--baseURL', f'http://localhost:{self.port}/', '--noBuildLock',
                ], cwd=REPO, capture_output=True, text=True, timeout=45)
                if result.returncode:
                    self.build_error = (result.stderr or result.stdout).strip()[-4000:]
                    shutil.rmtree(output, ignore_errors=True)
                else:
                    previous = self.site_root
                    self.site_root = output
                    self.build_error = None
                    if previous:
                        shutil.rmtree(previous, ignore_errors=True)
                self.fingerprint = self.source_fingerprint()
            except (OSError, subprocess.TimeoutExpired) as error:
                self.build_error = str(error)
            return self.build_error

    def watch(self):
        while not self.stopped.wait(1):
            try:
                if self.source_fingerprint() != self.fingerprint:
                    with self.lock:
                        if self.source_fingerprint() != self.fingerprint:
                            self.build()
            except OSError as error:
                print(f'Watch error: {error}', flush=True)


class Handler(SimpleHTTPRequestHandler):
    server_version = 'Moments/1'

    @property
    def app(self):
        return self.server.app

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        super().end_headers()

    def valid_host(self):
        return self.headers.get('Host') in (f'localhost:{self.app.port}', f'127.0.0.1:{self.app.port}')

    def authorize(self):
        if not self.valid_host():
            raise Problem('Open the editor through localhost', 403)
        origin = self.headers.get('Origin')
        allowed = (f'http://localhost:{self.app.port}', f'http://127.0.0.1:{self.app.port}')
        if origin and origin not in allowed:
            raise Problem('This request did not come from the local editor', 403)
        if not secrets.compare_digest(self.headers.get('X-Moments-Token', ''), self.app.token):
            raise Problem('Reload the page to reconnect to the editor', 403)

    def respond(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, default=json_default).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_payload(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise Problem('Invalid request size') from None
        if length < 1 or length > MAX_REQUEST:
            raise Problem('The upload is too large (maximum 64 MB)', 413)
        content = self.rfile.read(length)
        content_type = self.headers.get('Content-Type', '')
        files = []
        if content_type.startswith('multipart/form-data'):
            message = BytesParser(policy=policy.default).parsebytes(
                f'Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n'.encode() + content)
            if not message.is_multipart():
                raise Problem('Invalid photo upload')
            payload = None
            for part in message.iter_parts():
                name = part.get_param('name', header='content-disposition')
                data = part.get_payload(decode=True) or b''
                if name == 'post':
                    payload = json.loads(data)
                elif name == 'pictures' and part.get_filename():
                    files.append((part.get_filename(), data))
            if payload is None:
                raise Problem('The moment was missing from the upload')
        elif content_type.startswith('application/json'):
            payload = json.loads(content)
        else:
            raise Problem('Use JSON or a photo upload')
        if not isinstance(payload, dict):
            raise Problem('The request must contain an object')
        return payload, files

    def dispatch(self, method):
        try:
            if not self.valid_host():
                raise Problem('Open the editor through localhost', 403)
            route = urlsplit(self.path).path
            if method == 'GET' and route == '/api/session':
                self.respond({'token': self.app.token, 'author': self.app.author, 'data_root': str(self.app.store.root), 'generation': self.app.generation, 'build_error': self.app.build_error})
                return
            if method == 'GET' and route == '/api/moments':
                with self.app.store.lock:
                    moments = [self.app.store.summary(post) for post in self.app.store.posts()]
                self.respond({'moments': sorted(moments, key=lambda post: post['date'], reverse=True)})
                return
            match = re.fullmatch(r'/api/moments/([a-zA-Z0-9_-]+)(?:/(like|comments)(?:/([a-zA-Z0-9_-]+))?)?', route)
            if method == 'GET' and match:
                with self.app.store.lock:
                    self.respond(self.app.store.summary(self.app.store.find(match[1])))
                return
            if method in ('POST', 'PUT'):
                self.authorize()
                payload, files = self.read_payload()
                with self.app.lock:
                    if method == 'POST' and route == '/api/moments':
                        moment = self.app.store.save(payload, files)
                        error = self.app.build()
                        self.respond({'moment': moment, 'build_error': error}, 201)
                        return
                    if match and method == 'PUT' and not match[2]:
                        moment = self.app.store.save(payload, files, match[1])
                        error = self.app.build()
                        self.respond({'moment': moment, 'build_error': error})
                        return
                    if match and method == 'POST' and match[2] and not match[3]:
                        self.respond(self.app.store.interact(match[1], match[2], payload, self.app.author))
                        return
                    if match and method == 'PUT' and match[2] == 'comments' and match[3]:
                        self.respond(self.app.store.edit_comment(match[1], match[3], payload))
                        return
                raise Problem('Action not found', 404)
            if route.startswith('/api/'):
                raise Problem('Action not found', 404)
            if method not in ('GET', 'HEAD'):
                raise Problem('Method not allowed', 405)
            # Serve only the generated preview, never the source data directory.
            with self.app.lock:
                if self.app.site_root is None:
                    raise Problem('The preview could not build. Check the terminal.', 503)
                path = unquote(route)
                target = (self.app.site_root / path.lstrip('/')).resolve()
                if not target.is_relative_to(self.app.site_root) or any(part.startswith('.') for part in Path(path).parts):
                    raise Problem('File not found', 404)
                self.directory = str(self.app.site_root)
                if target.is_dir() and not (target / 'index.html').exists():
                    raise Problem('File not found', 404)
                if method == 'HEAD':
                    super().do_HEAD()
                else:
                    super().do_GET()
        except Problem as error:
            self.respond({'error': str(error)}, error.status)
        except (ValueError, TypeError, KeyError, tomllib.TOMLDecodeError) as error:
            self.respond({'error': f'Could not read the moment: {error}'}, 400)
        except (OSError, subprocess.SubprocessError) as error:
            self.respond({'error': f'The files could not be saved: {error}'}, 500)

    def do_GET(self):
        self.dispatch('GET')

    def do_HEAD(self):
        self.dispatch('HEAD')

    def do_POST(self):
        self.dispatch('POST')

    def do_PUT(self):
        self.dispatch('PUT')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--port', type=int, default=1313)
    parser.add_argument('--name', help='Override the configured author name for local comments')
    parser.add_argument('--import-content', type=Path, help='Import existing Hugo drafts into the dated data folders')
    arguments = parser.parse_args()
    if not shutil.which('hugo'):
        parser.error('Install Hugo Extended before starting Moments')
    configuration = subprocess.run(['hugo', 'config', '--format', 'json'], cwd=REPO, capture_output=True, text=True, check=True)
    author = arguments.name or json.loads(configuration.stdout).get('params', {}).get('name', 'Me')
    app = App(arguments.data_dir, arguments.port, author)
    if arguments.import_content:
        app.store.import_content(arguments.import_content)
    error = app.build()
    if error:
        parser.error(error)
    server = ThreadingHTTPServer(('127.0.0.1', arguments.port), Handler)
    server.app = app
    threading.Thread(target=app.watch, daemon=True).start()
    print(f'Moments is ready at http://localhost:{arguments.port}/', flush=True)
    print(f'Your moments are saved in {app.store.root}/YYYY/MM/DD/<moment>/', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stopped.set()
        server.server_close()
        app.temporary.cleanup()


if __name__ == '__main__':
    main()
