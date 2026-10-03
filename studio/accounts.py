"""Local profiles and password sessions, with credentials outside Hugo content."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import secrets
import shutil
import threading
import time
import unicodedata
from urllib.parse import urlsplit

from studio.server import MAX_PICTURE, Problem, atomic_write, image_extension, read_document, text_field, write_document

USER_ID = re.compile(r'^[a-z0-9_-]{1,40}$')
SESSION_SECONDS = 12 * 60 * 60


class Accounts:
    def __init__(self, root: Path, name='xfz', avatar='default-avatar.png', bio=''):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._sessions = {}
        self.profile_root = self._safe(self.root / 'users')
        self.private_root = self._safe(self.root / '.studio')
        self.private_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_path = self._safe(self.private_root / 'accounts.json')
        if self.state_path.exists():
            try:
                self.state = json.loads(self.state_path.read_text(encoding='utf-8'))
                self._validate_state()
            except (ValueError, TypeError, KeyError):
                raise Problem('The accounts file is invalid; it has not been changed.', 500) from None
        else:
            display = self._name(name)
            user_id = self._slug(display)
            self.state = {'version': 1, 'default_id': user_id, 'users': {user_id: {'role': 'owner', 'credential': None}}}
            self._write_profile(user_id, {'name': display, 'avatar': self._avatar(avatar), 'bio': self._bio(bio)})
            self._persist()
        self.default_id = self.state['default_id']
        # An unknown login still runs the password derivation before failing.
        self._dummy_credential = self._credential(secrets.token_urlsafe(32))

    def _safe(self, path: Path):
        if path.is_symlink():
            raise Problem('Profile and account files cannot be symbolic links.', 403)
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root):
            raise Problem('The profile is outside the Moments folder.', 403)
        return resolved

    def _folder(self, user_id):
        if not isinstance(user_id, str) or not USER_ID.fullmatch(user_id):
            raise Problem('User not found.', 404)
        self._safe(self.root / 'users')
        return self._safe(self.profile_root / user_id)

    def _path(self, user_id):
        return self._safe(self._folder(user_id) / '_index.md')

    def _validate_state(self):
        if not isinstance(self.state, dict) or self.state.get('version') != 1:
            raise ValueError('Unsupported accounts format')
        users = self.state['users']
        default_id = self.state['default_id']
        if not isinstance(users, dict) or not USER_ID.fullmatch(default_id):
            raise ValueError('Invalid users')
        if users[default_id].get('role') != 'owner':
            raise ValueError('Missing owner')
        for user_id, record in users.items():
            if not isinstance(user_id, str) or not USER_ID.fullmatch(user_id) or not isinstance(record, dict):
                raise ValueError('Invalid user')
            if record.get('role') not in ('owner', 'user'):
                raise ValueError('Invalid role')
            credential = record.get('credential')
            if credential is not None and not self._valid_credential(credential):
                raise ValueError('Invalid password hash')

    def _persist(self):
        self._safe(self.root / '.studio')
        self._safe(self.state_path)
        atomic_write(self.state_path, json.dumps(self.state, ensure_ascii=False, indent=2) + '\n')

    @staticmethod
    def _slug(name):
        plain = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode().lower()
        slug = re.sub(r'[^a-z0-9_-]+', '-', plain).strip('-_')[:40]
        return slug or 'user-' + hashlib.sha256(name.encode()).hexdigest()[:12]

    @staticmethod
    def _name(value):
        result = text_field({'name': value}, 'name', 80).strip()
        if not result:
            raise Problem('Enter a display name.')
        return result

    @staticmethod
    def _bio(value):
        return text_field({'bio': value}, 'bio', 2000).strip()

    @staticmethod
    def _password(value):
        result = text_field({'password': value}, 'password', 1024)
        if len(result) < 8:
            raise Problem('Use a password with at least 8 characters.')
        return result

    @staticmethod
    def _avatar(value):
        result = text_field({'avatar': value}, 'avatar', 2000).strip()
        if not result:
            return 'default-avatar.png'
        parsed = urlsplit(result)
        if parsed.scheme:
            if parsed.scheme not in ('http', 'https') or not parsed.netloc:
                raise Problem('Use an http or https avatar URL, or upload a picture.')
        elif parsed.netloc or '\\' in result or any(part in ('.', '..') for part in parsed.path.split('/')):
            raise Problem('Use a valid avatar path.')
        return result

    @staticmethod
    def _valid_credential(credential):
        try:
            return (isinstance(credential, dict) and credential.get('scheme') == 'scrypt'
                    and len(bytes.fromhex(credential['salt'])) == 32
                    and len(bytes.fromhex(credential['hash'])) == 64)
        except (ValueError, TypeError, KeyError):
            return False

    @staticmethod
    def _credential(password):
        salt = secrets.token_bytes(32)
        derived = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=16384, r=8, p=1, dklen=64)
        return {'scheme': 'scrypt', 'salt': salt.hex(), 'hash': derived.hex()}

    @staticmethod
    def _matches(password, credential):
        derived = hashlib.scrypt(password.encode('utf-8'), salt=bytes.fromhex(credential['salt']), n=16384, r=8, p=1, dklen=64)
        return secrets.compare_digest(derived, bytes.fromhex(credential['hash']))

    def _profile(self, user_id):
        path = self._path(user_id)
        if not path.is_file():
            raise Problem('This user profile is missing from the Moments folder.', 500)
        metadata, body = read_document(path)
        return metadata, body

    def _avatar_url(self, user_id, avatar):
        if urlsplit(avatar).scheme or avatar.startswith('/'):
            return avatar
        candidate = self._safe(self._folder(user_id) / avatar)
        return f'/users/{user_id}/{avatar}' if candidate.is_file() else '/' + avatar

    def _write_profile(self, user_id, changes, files=()):
        path = self._path(user_id)
        if path.exists():
            metadata, body = read_document(path)
        else:
            metadata, body = {}, ''
        metadata = dict(metadata)
        metadata.update(changes)
        folder = self._folder(user_id)
        if files:
            if len(files) != 1:
                raise Problem('Choose one avatar picture.')
            _, data = files[0]
            if not isinstance(data, bytes) or len(data) > MAX_PICTURE:
                raise Problem('Your avatar must be an image of 10 MB or smaller.')
            filename = 'avatar-' + secrets.token_hex(8) + image_extension(data)
            target = self._safe(folder / filename)
            folder.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream:
                stream.write(data)
            metadata['avatar'] = filename
        metadata.update({'user_id': user_id, 'default_user': user_id == self.state['default_id'],
                         'title': metadata['name'], 'type': 'users', 'layout': 'user', 'url': f'/users/{user_id}/'})
        metadata['build'] = {**metadata.get('build', {}), 'publishResources': False}
        write_document(path, metadata, metadata.get('bio', body))

    @property
    def setup_required(self):
        with self.lock:
            return self.state['users'][self.default_id].get('credential') is None

    def user(self, user_id):
        with self.lock:
            if not isinstance(user_id, str) or user_id not in self.state['users']:
                raise Problem('User not found.', 404)
            metadata, body = self._profile(user_id)
            record = self.state['users'][user_id]
            aliases = metadata.get('previous_names', [])
            aliases = [name for name in aliases if isinstance(name, str)] if isinstance(aliases, list) else []
            return {'id': user_id, 'name': self._name(metadata.get('name', user_id)),
                    'avatar': self._avatar_url(user_id, self._avatar(metadata.get('avatar', 'default-avatar.png'))),
                    'bio': self._bio(metadata.get('bio', body)), 'url': f'/users/{user_id}/',
                    'previous_names': aliases,
                    'role': record['role'], 'can_login': bool(record.get('credential'))}

    def users(self):
        with self.lock:
            return sorted((self.user(user_id) for user_id in self.state['users']), key=lambda user: (user['role'] != 'owner', user['name'].casefold(), user['id']))

    def resolve(self, name):
        if not isinstance(name, str) or not name.strip():
            return None
        with self.lock:
            match = name.strip().casefold()
            users = self.users()
            for field in ('id', 'name'):
                for user in users:
                    if user[field].casefold() == match:
                        return user
            for user in users:
                if any(alias.casefold() == match for alias in user['previous_names']):
                    return user
        return None

    def setup(self, password):
        with self.lock:
            if not self.setup_required:
                raise Problem('The owner account is already configured. Log in instead.', 409)
            credential = self._credential(self._password(password))
            self.state['users'][self.default_id]['credential'] = credential
            self._persist()
            return self.user(self.default_id)

    def login(self, user_id, password):
        with self.lock:
            password = text_field({'password': password}, 'password', 1024)
            profile = self.resolve(user_id)
            record = self.state['users'].get(profile['id']) if profile else None
            credential = record.get('credential') if record else None
            valid = self._matches(password, credential or self._dummy_credential)
            if not credential or not valid:
                raise Problem('The user or password is incorrect.', 401)
            return self.user(profile['id'])

    def update(self, user_id, payload, files=()):
        with self.lock:
            previous = self.user(user_id)
            metadata, _ = self._profile(user_id)
            changes = {'name': self._name(payload.get('name', previous['name'])),
                       'bio': self._bio(payload.get('bio', previous['bio'])),
                       'avatar': self._avatar(payload.get('avatar', metadata.get('avatar', 'default-avatar.png')))}
            aliases = list(previous['previous_names'])
            if changes['name'] != previous['name'] and previous['name'].casefold() not in {alias.casefold() for alias in aliases}:
                aliases.append(previous['name'])
            changes['previous_names'] = aliases
            self._write_profile(user_id, changes, files)
            return self.user(user_id)

    def _add(self, user_id, name, avatar, bio, credential, files=()):
        folder = self._folder(user_id)
        if folder.exists():
            raise Problem('This user folder already exists.', 409)
        folder.mkdir(parents=True, exist_ok=False)
        try:
            self._write_profile(user_id, {'name': name, 'avatar': avatar, 'bio': bio}, files)
            self.state['users'][user_id] = {'role': 'user', 'credential': credential}
            self._persist()
        except Exception:
            self.state['users'].pop(user_id, None)
            shutil.rmtree(folder)
            raise
        return self.user(user_id)

    def create(self, payload, files=()):
        with self.lock:
            user_id = text_field(payload, 'id', 40).strip().lower()
            if not USER_ID.fullmatch(user_id):
                raise Problem('Use 1–40 lowercase letters, numbers, underscores, or hyphens for the user ID.')
            credential = self._credential(self._password(payload.get('password')))
            previous = self.state['users'].get(user_id)
            if previous:
                if previous.get('credential') or previous['role'] == 'owner':
                    raise Problem('This user already has an account.', 409)
                self.update(user_id, payload, files)
                previous['credential'] = credential
                self._persist()
                return self.user(user_id)
            name = self._name(payload.get('name', user_id))
            bio = self._bio(payload.get('bio', ''))
            avatar = self._avatar(payload.get('avatar', 'default-avatar.png'))
            return self._add(user_id, name, avatar, bio, credential, files)

    def import_legacy(self, name, avatar='default-avatar.png'):
        with self.lock:
            name = self._name(name)
            existing = self.resolve(name)
            if existing:
                return existing
            user_id = self._slug(name)
            if user_id in self.state['users'] or self._folder(user_id).exists():
                suffix = '-' + hashlib.sha256(name.encode()).hexdigest()[:8]
                user_id = user_id[:40 - len(suffix)] + suffix
            return self._add(user_id, name, self._avatar(avatar), '', None)

    def can_manage(self, session_user, author_id):
        if not isinstance(session_user, dict):
            return False
        with self.lock:
            record = self.state['users'].get(session_user.get('id'))
            return bool(record and (record['role'] == 'owner' or session_user['id'] == (author_id or self.default_id)))

    def new_session(self, user):
        with self.lock:
            profile = self.user(user['id'])
            if not profile['can_login']:
                raise Problem('This user has no password configured.', 401)
            now = time.monotonic()
            self._sessions = {token: record for token, record in self._sessions.items() if record['expires'] > now}
            token = secrets.token_urlsafe(32)
            self._sessions[token] = {'id': profile['id'], 'expires': now + SESSION_SECONDS}
            return token

    def session(self, token):
        with self.lock:
            if not isinstance(token, str):
                return None
            record = self._sessions.get(token)
            if not record:
                return None
            if record['expires'] <= time.monotonic():
                del self._sessions[token]
                return None
            return self.user(record['id'])

    def logout(self, token):
        with self.lock:
            if isinstance(token, str):
                self._sessions.pop(token, None)
