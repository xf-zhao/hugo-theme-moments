"""LaunchAgent configuration checks without installing a real service."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import plistlib
import stat
import subprocess
import tempfile
import unittest

from studio.autostart import LABEL, LaunchAgent, SetupError, configuration, write_plist


class LaunchAgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='moments-launch-test-')
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / 'Repo with spaces & Unicode 雨'
        (self.repo / 'studio').mkdir(parents=True)
        (self.repo / 'studio' / 'launch.sh').write_text('#!/bin/bash\nexit 0\n')
        (self.repo / 'studio' / 'launch.sh').chmod(0o755)
        self.python = self.root / 'Python with spaces'
        self.python.write_text('#!/bin/bash\nexit 0\n')
        self.python.chmod(0o755)
        self.data_root = self.root / 'Moments with spaces & photos'
        self.value = configuration(self.repo, self.data_root, python=self.python)
        self.calls = []
        self.loaded = False
        self.fail_command = None
        self.agent = LaunchAgent(home=self.root / 'Home with spaces', uid=123, runner=self.run_command)

    def tearDown(self):
        self.temporary.cleanup()

    def run_command(self, args, **options):
        self.calls.append(args)
        self.assertTrue(options['capture_output'])
        self.assertTrue(options['text'])
        self.assertFalse(options['check'])
        command = args[1]
        if command == self.fail_command:
            return subprocess.CompletedProcess(args, 5, '', 'test failure')
        if command == 'print':
            return subprocess.CompletedProcess(args, 0 if self.loaded else 3,
                                               'state = running\npid = 456\n' if self.loaded else '', '')
        if command == 'bootstrap':
            self.loaded = True
        elif command == 'bootout':
            self.loaded = False
        return subprocess.CompletedProcess(args, 0, '', '')

    def install(self):
        with redirect_stdout(io.StringIO()):
            self.agent.install(self.value)

    def test_install_keeps_paths_as_separate_arguments_and_writes_valid_plist(self):
        self.install()
        with self.agent.path.open('rb') as source:
            value = plistlib.load(source)
        self.assertEqual(value, self.value)
        self.assertEqual(value['ProgramArguments'], [str(self.repo / 'studio' / 'launch.sh'), '--host',
                                                    '0.0.0.0', '--port', '1313', '--data-dir', str(self.data_root)])
        self.assertEqual(value['EnvironmentVariables']['MOMENTS_PYTHON'], str(self.python))
        self.assertTrue(value['KeepAlive'])
        self.assertTrue(value['RunAtLoad'])
        self.assertEqual(value['ThrottleInterval'], 10)
        self.assertTrue((self.data_root / '.studio/logs').is_dir())
        self.assertFalse(stat.S_IMODE(self.agent.path.stat().st_mode) & 0o022)
        self.assertIn(['/bin/launchctl', 'bootstrap', 'gui/123', str(self.agent.path)], self.calls)

    def test_reinstall_unloads_only_the_moments_service_before_replacing(self):
        self.install()
        self.calls.clear()
        self.value = configuration(self.repo, self.data_root, host='127.0.0.1', port=1327, python=self.python)
        self.install()
        self.assertEqual(self.calls[0], ['/bin/launchctl', 'print', f'gui/123/{LABEL}'])
        self.assertEqual(self.calls[1], ['/bin/launchctl', 'bootout', f'gui/123/{LABEL}'])
        self.assertEqual(self.agent.installed()['ProgramArguments'][2:5], ['127.0.0.1', '--port', '1327'])

    def test_bootout_failure_preserves_original_configuration(self):
        self.install()
        before = self.agent.path.read_bytes()
        self.value['ProgramArguments'][4] = '1327'
        self.fail_command = 'bootout'
        with self.assertRaisesRegex(SetupError, 'bootout failed.*test failure'):
            self.install()
        self.assertEqual(self.agent.path.read_bytes(), before)

    def test_bootstrap_failure_is_reported(self):
        self.fail_command = 'bootstrap'
        with self.assertRaisesRegex(SetupError, 'bootstrap failed.*test failure'):
            self.install()

    def test_remove_keeps_data_and_logs(self):
        self.install()
        log = self.data_root / '.studio/logs/stderr.log'
        log.write_text('A useful diagnostic')
        moment = self.data_root / '2026/10/03/post/index.md'
        moment.parent.mkdir(parents=True)
        moment.write_text('Keep this moment')
        with redirect_stdout(io.StringIO()):
            self.agent.remove()
        self.assertFalse(self.agent.path.exists())
        self.assertFalse(self.loaded)
        self.assertEqual(log.read_text(), 'A useful diagnostic')
        self.assertEqual(moment.read_text(), 'Keep this moment')
        self.assertIn(['/bin/launchctl', 'bootout', f'gui/123/{LABEL}'], self.calls)

    def test_unrelated_configuration_is_not_replaced_or_unloaded(self):
        self.agent.path.parent.mkdir(parents=True)
        write_plist(self.agent.path, {'Label': 'com.someone.else'})
        before = self.agent.path.read_bytes()
        for action in (lambda: self.agent.install(self.value), self.agent.remove):
            with self.assertRaisesRegex(SetupError, 'does not belong to Moments'):
                action()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.agent.path.read_bytes(), before)
        self.assertFalse(self.data_root.exists())

    def test_status_does_not_modify_data_or_launch_the_service(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.agent.status(), 1)
        self.assertFalse(self.agent.path.parent.exists())
        self.assertEqual(self.calls, [])
        self.install()
        self.calls.clear()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(self.agent.status(), 0)
        self.assertIn('pid = 456', output.getvalue())
        self.assertEqual(self.calls, [['/bin/launchctl', 'print', f'gui/123/{LABEL}']])


if __name__ == '__main__':
    unittest.main()
