#!/usr/bin/env python3
"""A file-backed Hugo Moments editor for localhost or your local network."""

from __future__ import annotations

import argparse
from datetime import date, datetime
from email import policy
from email.parser import BytesParser
import hashlib
import ipaddress
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import tomllib
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path('/Users/xufeng/Workspace/Moments')
ZONE = ZoneInfo('Asia/Shanghai')
MAX_REQUEST = 64 * 1024 * 1024
MAX_PICTURE = 10 * 1024 * 1024
ID_RE = re.compile(r'^[a-zA-Z0-9_-]{1,80}$')


def authority(value):
    """Return a normalized HTTP authority, rejecting malformed Host headers."""
    if not isinstance(value, str) or not value or any(ord(char) <= 32 or ord(char) >= 127 for char in value):
        return None
    try:
        parsed = urlsplit('//' + value)
        host = parsed.hostname
        port = parsed.port if parsed.port is not None else 80
        if not 1 <= port <= 65535:
            return None
        if not host or parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment:
            return None
        try:
            host = str(ipaddress.ip_address(host))
        except ValueError:
            host = host.lower().rstrip('.')
            if not re.fullmatch(r'[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?', host):
                return None
        return host, port
    except ValueError:
        return None


def network_hosts():
    """Discover this Mac's interface addresses and Bonjour name without DNS."""
    hosts = {'localhost', '127.0.0.1', '::1'}
    hostname = socket.gethostname().lower().rstrip('.')
    if authority(hostname):
        hosts.add(hostname)
    commands = [(['/sbin/ifconfig'], 'addresses'), (['/usr/sbin/scutil', '--get', 'LocalHostName'], 'name')]
    for command, kind in commands:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode:
            continue
        if kind == 'addresses':
            for address in re.findall(r'^\s*inet\s+(\d+\.\d+\.\d+\.\d+)\s', result.stdout, re.MULTILINE):
                hosts.add(str(ipaddress.ip_address(address)))
        else:
            name = result.stdout.strip().lower() + '.local'
            if authority(name):
                hosts.add(name)
    return hosts


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
        self.accounts = None

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
        author_id = self.author_id(post)
        author = None
        if self.accounts:
            try:
                author = self.accounts.user(author_id)
            except Problem as error:
                if error.status != 404:
                    raise
        return {
            'id': moment_id,
            'author_id': author_id,
            'name': author['name'] if author else metadata.get('name', ''),
            'avatar': author['avatar'] if author else metadata.get('avatar', ''),
            'date': parse_date(metadata['date']).isoformat(),
            'draft': bool(metadata.get('draft', False)),
            'hidden': bool(metadata.get('hidden', False)),
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

    def author_id(self, post):
        metadata = post[2]
        if metadata.get('author_id'):
            return metadata['author_id']
        if self.accounts:
            legacy = self.accounts.resolve(metadata.get('name', ''))
            return legacy['id'] if legacy else self.accounts.default_id
        return 'xf-zhao'

    def save(self, payload, files, moment_id=None, author_id=None):
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
            metadata['author_id'] = self.author_id(previous) if previous else (author_id or (self.accounts.default_id if self.accounts else 'xf-zhao'))
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

    def set_hidden(self, moment_id, payload):
        with self.lock:
            post = self.find(moment_id)
            path, metadata, body = post[1:]
            if payload.get('revision') != self.revision(path):
                raise Problem('This moment changed. Reload it before changing its visibility.', 409)
            hidden = payload.get('hidden')
            if not isinstance(hidden, bool):
                raise Problem('hidden must be true or false')
            if hidden == bool(metadata.get('hidden', False)):
                return self.summary(post)
            metadata = dict(metadata)
            stamp = datetime.now(ZONE).strftime('%Y%m%dT%H%M%S%f')
            atomic_write(self.safe(path.parent / '.history' / f'{stamp}.md'), path.read_text(encoding='utf-8'))
            if hidden:
                metadata['studio_hidden_build'] = dict(metadata.get('build', {}))
                metadata['build'] = {**metadata['studio_hidden_build'], 'render': 'never', 'list': 'never', 'publishResources': False}
            else:
                previous_build = metadata.pop('studio_hidden_build', {})
                metadata['build'] = {**previous_build, 'publishResources': False}
            metadata['hidden'] = hidden
            write_document(path, metadata, body)
            return self.summary((moment_id, path, metadata, body))

    def delete(self, moment_id, payload):
        with self.lock:
            post = self.find(moment_id)
            if payload.get('revision') != self.revision(post[1]):
                raise Problem('This moment changed. Reload it before deleting it.', 409)
            source = self.safe(post[1].parent)
            now = datetime.now(ZONE)
            destination = self.safe(self.root / '.trash' / f'{now.strftime("%Y%m%dT%H%M%S%f")}-{moment_id}')
            record = {'id': moment_id, 'original_folder': source.relative_to(self.root).as_posix(), 'deleted_at': now.isoformat()}
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.rename(destination)
            try:
                atomic_write(destination / 'trash.json', json.dumps(record, ensure_ascii=False, indent=2) + '\n')
            except OSError:
                destination.rename(source)
                raise
            return {'id': moment_id, 'trashed_folder': destination.relative_to(self.root).as_posix()}

    def picture(self, moment_id, name):
        with self.lock:
            post = self.find(moment_id)
            relative = 'pictures/' + name
            if relative not in post[2].get('pictures', []):
                raise Problem('Photo not found', 404)
            folder = self.safe(post[1].parent)
            target = self.safe(folder / relative)
            if not target.is_relative_to(folder) or not target.is_file() or any(part.startswith('.') for part in Path(relative).parts):
                raise Problem('Photo not found', 404)
            return target

    def interact(self, moment_id, action, payload, author, author_id=None):
        with self.lock:
            post = self.find(moment_id)
            social = self.social(post[1])
            if action == 'like':
                if not isinstance(payload.get('liked'), bool):
                    raise Problem('liked must be true or false')
                if author_id:
                    likes = social.get('likes', [self.accounts.default_id] if social.get('liked') and self.accounts else [])
                    likes = [user_id for user_id in likes if user_id != author_id]
                    if payload['liked']:
                        likes.append(author_id)
                    social['likes'] = likes
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
                    'author_id': author_id,
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
    def __init__(self, root: Path, port=1313, author='xf-zhao', avatar='default-avatar.png', bio='', host='127.0.0.1', allowed_hosts=None):
        from studio.accounts import Accounts
        self.store = Store(root)
        self.accounts = Accounts(self.store.root, name=author, avatar=avatar, bio=bio)
        self.store.accounts = self.accounts
        for post in self.store.posts():
            if post[2].get('name') and not self.accounts.resolve(post[2]['name']):
                self.accounts.import_legacy(post[2]['name'], post[2].get('avatar', avatar))
        self.port = port
        self.host = host
        self.extra_hosts = set()
        for value in allowed_hosts or []:
            parsed = authority(value)
            if not parsed or ':' in value or '/' in value:
                raise Problem('Use a hostname or IPv4 address for --allowed-host, without a port')
            self.extra_hosts.add(parsed[0])
        self.network_lock = threading.Lock()
        self.allowed_hosts = self.extra_hosts | {'localhost', '127.0.0.1', '::1', host.lower()}
        self.network_checked = 0
        self.refresh_hosts()
        self.author = author
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.temporary = tempfile.TemporaryDirectory(prefix='moments-studio-')
        self.site_root = None
        self.generation = 0
        self.build_error = None
        self.fingerprint = None
        self.stopped = threading.Event()

    def refresh_hosts(self):
        with self.network_lock:
            if self.host == '0.0.0.0' and time.monotonic() - self.network_checked >= 5:
                self.allowed_hosts = self.extra_hosts | network_hosts() | {'0.0.0.0'}
                self.network_checked = time.monotonic()

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

    def build(self, invalidate_on_error=False):
        with self.lock:
            for post in self.store.posts():
                if post[2].get('name') and not self.accounts.resolve(post[2]['name']):
                    self.accounts.import_legacy(post[2]['name'], post[2].get('avatar', 'default-avatar.png'))
            self.generation += 1
            output = Path(self.temporary.name).resolve() / str(self.generation)
            try:
                result = subprocess.run([
                    'hugo', '--environment', 'development', '--buildDrafts', '--buildFuture',
                    '--contentDir', str(self.store.root), '--destination', str(output),
                    '--baseURL', '/', '--noBuildLock',
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
            if self.build_error and invalidate_on_error:
                # A stale preview could still display a hidden or deleted moment.
                previous = self.site_root
                self.site_root = None
                if previous:
                    shutil.rmtree(previous, ignore_errors=True)
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
        if len(self.headers.get_all('Host', [])) != 1:
            return False
        self.app.refresh_hosts()
        parsed = authority(self.headers.get('Host'))
        return bool(parsed and parsed[0] in self.app.allowed_hosts and parsed[1] == self.app.port)

    def local_client(self):
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def authorize(self):
        if not self.valid_host():
            raise Problem('Open Moments through this Mac\'s address or configured hostname', 403)
        origin = self.headers.get('Origin')
        if origin:
            if any(ord(char) <= 32 or ord(char) >= 127 for char in origin):
                raise Problem('This request did not come from the Moments page you opened', 403)
            parsed = urlsplit(origin)
            if parsed.scheme != 'http' or parsed.path or parsed.query or parsed.fragment or authority(parsed.netloc) != authority(self.headers.get('Host')):
                raise Problem('This request did not come from the Moments page you opened', 403)
        if not secrets.compare_digest(self.headers.get('X-Moments-Token', ''), self.app.token):
            raise Problem('Reload the page to reconnect to the editor', 403)

    def session_token(self):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get('Cookie', ''))
        except Exception:
            return ''
        item = cookies.get('Moments-Session')
        return item.value if item else ''

    def current_user(self, required=False):
        user = self.app.accounts.session(self.session_token())
        if required and not user:
            raise Problem('Log in before making changes', 401)
        return user

    def manage(self, moment_id, user):
        post = self.app.store.find(moment_id)
        if not self.app.accounts.can_manage(user, self.app.store.author_id(post)):
            raise Problem('You can manage your own moments', 403)
        return post

    def visible(self, post, user):
        return not post[2].get('hidden') or self.app.accounts.can_manage(user, self.app.store.author_id(post))

    def social_view(self, social, user):
        result = dict(social)
        likes = social.get('likes', [self.app.accounts.default_id] if social.get('liked') else [])
        result['liked'] = bool(user and user['id'] in likes)
        result['like_count'] = len(likes)
        return result

    def moment_view(self, post, user):
        result = self.app.store.summary(post)
        result['social'] = self.social_view(result['social'], user)
        return result

    def respond(self, payload, status=200, session_cookie=None):
        body = json.dumps(payload, ensure_ascii=False, default=json_default).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        if session_cookie is not None:
            age = 43200 if session_cookie else 0
            self.send_header('Set-Cookie', f'Moments-Session={session_cookie}; Path=/; Max-Age={age}; HttpOnly; SameSite=Strict')
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
                raise Problem('Open Moments through this Mac\'s address or configured hostname', 403)
            route = urlsplit(self.path).path
            user = self.current_user()
            if method == 'GET' and route == '/api/session':
                self.respond({'token': self.app.token, 'author': user['name'] if user else self.app.accounts.user(self.app.accounts.default_id)['name'],
                    'user': user, 'authenticated': user is not None, 'setup_required': self.app.accounts.setup_required,
                    'users': self.app.accounts.users(), 'default_user_id': self.app.accounts.default_id,
                    'data_root': str(self.app.store.root), 'generation': self.app.generation, 'build_error': self.app.build_error})
                return
            if method == 'GET' and route == '/api/users':
                self.respond({'users': self.app.accounts.users()})
                return
            if method == 'GET' and route == '/api/moments':
                with self.app.store.lock:
                    moments = [self.moment_view(post, user) for post in self.app.store.posts() if self.visible(post, user)]
                self.respond({'moments': sorted(moments, key=lambda post: post['date'], reverse=True)})
                return
            picture_match = re.fullmatch(r'/api/moments/([a-zA-Z0-9_-]+)/pictures/(.+)', route)
            if method in ('GET', 'HEAD') and picture_match:
                with self.app.store.lock:
                    post = self.app.store.find(picture_match[1])
                    if not self.visible(post, user):
                        raise Problem('This moment is hidden', 403)
                    target = self.app.store.picture(picture_match[1], unquote(picture_match[2]))
                    with target.open('rb') as source:
                        extension = image_extension(source.read(40))
                        content_type = {'.jpg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp', '.avif': 'image/avif'}[extension]
                        self.send_response(200)
                        self.send_header('Content-Type', content_type)
                        self.send_header('Content-Length', str(target.stat().st_size))
                        self.end_headers()
                        if method == 'GET':
                            source.seek(0)
                            shutil.copyfileobj(source, self.wfile)
                return
            match = re.fullmatch(r'/api/moments/([a-zA-Z0-9_-]+)(?:/(like|comments|visibility)(?:/([a-zA-Z0-9_-]+))?)?', route)
            if method == 'GET' and match and not match[2]:
                with self.app.store.lock:
                    post = self.app.store.find(match[1])
                    if not self.visible(post, user):
                        raise Problem('This moment is hidden', 403)
                    self.respond(self.moment_view(post, user))
                return
            if method in ('POST', 'PUT', 'DELETE'):
                self.authorize()
                payload, files = self.read_payload()
                with self.app.lock:
                    if method == 'POST' and route in ('/api/auth/setup', '/api/auth/login'):
                        if route.endswith('/setup'):
                            if not self.local_client():
                                raise Problem('Set up the first login on this Mac at http://localhost:' + str(self.app.port), 403)
                            signed_in = self.app.accounts.setup(payload.get('password'))
                        else:
                            signed_in = self.app.accounts.login(payload.get('user_id'), payload.get('password'))
                        self.app.accounts.logout(self.session_token())
                        cookie = self.app.accounts.new_session(signed_in)
                        self.respond({'user': signed_in}, session_cookie=cookie)
                        return
                    if method == 'POST' and route == '/api/auth/logout':
                        self.app.accounts.logout(self.session_token())
                        self.respond({'ok': True}, session_cookie='')
                        return
                    user = self.current_user(required=True)
                    user_match = re.fullmatch(r'/api/users/([a-z0-9_-]{1,40})', route)
                    if method == 'POST' and route == '/api/users':
                        if user['role'] != 'owner':
                            raise Problem('Only the owner can create an account', 403)
                        created = self.app.accounts.create(payload, files)
                        error = self.app.build()
                        self.respond({'user': created, 'build_error': error}, 201)
                        return
                    if method == 'PUT' and user_match:
                        if not self.app.accounts.can_manage(user, user_match[1]):
                            raise Problem('You can edit your own profile', 403)
                        updated = self.app.accounts.update(user_match[1], payload, files)
                        error = self.app.build()
                        self.respond({'user': updated, 'build_error': error})
                        return
                    if method == 'POST' and route == '/api/moments':
                        moment = self.app.store.save(payload, files, author_id=user['id'])
                        error = self.app.build()
                        self.respond({'moment': moment, 'build_error': error}, 201)
                        return
                    if match and method == 'PUT' and not match[2]:
                        self.manage(match[1], user)
                        moment = self.app.store.save(payload, files, match[1])
                        error = self.app.build()
                        self.respond({'moment': moment, 'build_error': error})
                        return
                    if match and method == 'PUT' and match[2] == 'visibility' and not match[3]:
                        self.manage(match[1], user)
                        moment = self.app.store.set_hidden(match[1], payload)
                        error = self.app.build(invalidate_on_error=True)
                        self.respond({'moment': moment, 'build_error': error})
                        return
                    if match and method == 'DELETE' and not match[2]:
                        self.manage(match[1], user)
                        deleted = self.app.store.delete(match[1], payload)
                        error = self.app.build(invalidate_on_error=True)
                        self.respond({**deleted, 'build_error': error})
                        return
                    if match and method == 'POST' and match[2] in ('like', 'comments') and not match[3]:
                        post = self.app.store.find(match[1])
                        if not self.visible(post, user):
                            raise Problem('This moment is hidden', 403)
                        payload['author'] = user['name']
                        social = self.app.store.interact(match[1], match[2], payload, user['name'], user['id'])
                        self.respond(self.social_view(social, user))
                        return
                    if match and method == 'PUT' and match[2] == 'comments' and match[3]:
                        post = self.app.store.find(match[1])
                        if not self.visible(post, user):
                            raise Problem('This moment is hidden', 403)
                        comment = next((item for item in self.app.store.social(post[1])['comments'] if item['id'] == match[3]), None)
                        if not comment:
                            raise Problem('Comment not found', 404)
                        if not self.app.accounts.can_manage(user, comment.get('author_id') or self.app.accounts.default_id):
                            raise Problem('You can edit your own comments', 403)
                        self.respond(self.social_view(self.app.store.edit_comment(match[1], match[3], payload), user))
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

    def do_DELETE(self):
        self.dispatch('DELETE')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--port', type=int, default=1313)
    parser.add_argument('--host', default='127.0.0.1', help='Listen on 127.0.0.1 for this Mac, or 0.0.0.0 for other devices')
    parser.add_argument('--allowed-host', action='append', default=[], help='Additional trusted hostname, without a port; may be repeated')
    parser.add_argument('--name', help='Override the configured author name for local comments')
    parser.add_argument('--import-content', type=Path, help='Import existing Hugo drafts into the dated data folders')
    arguments = parser.parse_args()
    if not shutil.which('hugo'):
        parser.error('Install Hugo Extended before starting Moments')
    configuration = subprocess.run(['hugo', 'config', '--format', 'json'], cwd=REPO, capture_output=True, text=True, check=True)
    params = json.loads(configuration.stdout).get('params', {})
    author = arguments.name or params.get('name', 'Me')
    if not 1 <= arguments.port <= 65535:
        parser.error('Choose a port between 1 and 65535')
    try:
        app = App(arguments.data_dir, arguments.port, author, params.get('avatar', 'default-avatar.png'), params.get('signature', ''), host=arguments.host, allowed_hosts=arguments.allowed_host)
    except Problem as error:
        parser.error(str(error))
    if arguments.import_content:
        app.store.import_content(arguments.import_content)
    error = app.build()
    if error:
        parser.error(error)
    server = ThreadingHTTPServer((arguments.host, arguments.port), Handler)
    server.app = app
    threading.Thread(target=app.watch, daemon=True).start()
    print(f'Moments is ready at http://localhost:{arguments.port}/', flush=True)
    if arguments.host == '0.0.0.0':
        print(f'Listening on all IPv4 interfaces (0.0.0.0:{arguments.port})', flush=True)
        for hostname in sorted(app.allowed_hosts - {'localhost', '127.0.0.1', '::1', '0.0.0.0'}):
            print(f'Other devices: http://{hostname}:{arguments.port}/', flush=True)
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
    # Direct script execution and -m execution share the same exception classes.
    import sys
    sys.path.insert(0, str(REPO))
    sys.modules['studio.server'] = sys.modules[__name__]
    main()
