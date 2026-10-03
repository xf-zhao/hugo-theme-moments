"""Install a per-user macOS LaunchAgent for the local Moments app."""

import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile


REPO = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path('/Users/xufeng/Workspace/Moments')
LABEL = 'com.xfz.moments'
LAUNCHCTL = '/bin/launchctl'
LAUNCH_PATH = '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'


class SetupError(Exception):
    pass


def configuration(repo, data_root, host='0.0.0.0', port=1313, python=None):
    """Use argument arrays so paths and user input never become shell code."""
    repo = Path(repo).expanduser().resolve()
    data_root = Path(data_root).expanduser().resolve()
    # Keep a stable Homebrew symlink usable across Python upgrades.
    python = Path(python or sys.executable).expanduser().absolute()
    logs = data_root / '.studio' / 'logs'
    return {
        'Label': LABEL,
        'ProgramArguments': [str(repo / 'studio' / 'launch.sh'), '--host', host,
                             '--port', str(port), '--data-dir', str(data_root)],
        'WorkingDirectory': str(repo),
        'EnvironmentVariables': {
            'PATH': LAUNCH_PATH,
            'MOMENTS_PYTHON': str(python),
            'PYTHONUNBUFFERED': '1',
        },
        'RunAtLoad': True,
        'KeepAlive': True,
        'ThrottleInterval': 10,
        'StandardOutPath': str(logs / 'stdout.log'),
        'StandardErrorPath': str(logs / 'stderr.log'),
    }


def write_plist(path, value):
    """Replace the plist atomically; launchd requires no group/world writes."""
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.moments-', suffix='.plist', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as target:
            plistlib.dump(value, target, sort_keys=False)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class LaunchAgent:
    def __init__(self, home=None, uid=None, runner=None):
        self.path = Path(home or Path.home()) / 'Library' / 'LaunchAgents' / f'{LABEL}.plist'
        self.domain = f'gui/{os.getuid() if uid is None else uid}'
        self.target = f'{self.domain}/{LABEL}'
        self.runner = runner or subprocess.run

    def command(self, *arguments, required=True):
        result = self.runner([LAUNCHCTL, *arguments], capture_output=True, text=True, check=False)
        if required and result.returncode:
            details = (result.stderr or result.stdout).strip()
            raise SetupError(f'launchctl {arguments[0]} failed ({result.returncode}): {details}')
        return result

    def installed(self):
        if not self.path.exists():
            return None
        try:
            with self.path.open('rb') as source:
                value = plistlib.load(source)
        except (OSError, ValueError, plistlib.InvalidFileException) as error:
            raise SetupError(f'Cannot read {self.path}: {error}') from error
        if not isinstance(value, dict) or value.get('Label') != LABEL:
            raise SetupError(f'{self.path} does not belong to Moments; leave it untouched.')
        return value

    def install(self, value):
        self.installed()  # Validate before replacing an existing configuration.
        launch_script = Path(value['ProgramArguments'][0])
        if not launch_script.is_file() or not os.access(launch_script, os.X_OK):
            raise SetupError(f'Launch script is missing or not executable: {launch_script}')
        python = Path(value['EnvironmentVariables']['MOMENTS_PYTHON'])
        if not python.is_file() or not os.access(python, os.X_OK):
            raise SetupError(f'Python is missing or not executable: {python}')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        logs = Path(value['StandardOutPath']).parent
        logs.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        logs.mkdir(exist_ok=True, mode=0o700)
        # Target only this service, never its enclosing GUI domain.
        if self.command('print', self.target, required=False).returncode == 0:
            self.command('bootout', self.target)
        write_plist(self.path, value)
        self.command('enable', self.target)
        self.command('bootstrap', self.domain, str(self.path))
        print(f'Installed {self.path}')
        print('Moments starts now and whenever you log in to macOS.')
        self.print_paths(value)
        print(f'Restart: launchctl kickstart -k {self.target}')

    def print_paths(self, value):
        print(f'Stdout log: {value["StandardOutPath"]}')
        print(f'Stderr log: {value["StandardErrorPath"]}')

    def status(self):
        value = self.installed()
        if value is None:
            print(f'Autostart is not installed: {self.path}')
            return 1
        print(f'Configuration: {self.path}')
        self.print_paths(value)
        result = self.command('print', self.target, required=False)
        if result.returncode:
            print('The LaunchAgent is installed but is not loaded.')
            if result.stderr.strip():
                print(result.stderr.strip())
            return 1
        # Print launchctl's full diagnostic output, including state/PID/exit code.
        # Its output is intended for humans and should not be parsed as an API.
        print(result.stdout.rstrip())
        return 0

    def remove(self):
        self.installed()  # Do not delete a file with a different label.
        if self.command('print', self.target, required=False).returncode == 0:
            self.command('bootout', self.target)
        self.path.unlink(missing_ok=True)
        print('Moments autostart was removed and its service was stopped.')
        print('Your moments, account settings, and logs are kept.')


def port_number(value):
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError('port must be an integer') from error
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError('port must be between 1 and 65535')
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'status', 'remove'])
    parser.add_argument('--host', default='0.0.0.0', help='bind address for install (default: 0.0.0.0)')
    parser.add_argument('--port', type=port_number, default=1313)
    parser.add_argument('--data-dir', type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('autostart uses macOS launchd; start the app with studio/launch.sh on other systems')
    agent = LaunchAgent()
    try:
        if args.action == 'install':
            agent.install(configuration(REPO, args.data_dir, args.host, args.port))
        elif args.action == 'status':
            return agent.status()
        else:
            agent.remove()
    except (SetupError, OSError) as error:
        print(f'Moments autostart: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
