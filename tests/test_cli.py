"""CLI contracts: isolated configs, no live account access."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import goddard_sync as gs
import goddard_gdrive as gd


class CLIContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / 'config.json'
        self.config.write_text('{}')
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('GODDARD_')}
        self.env['HOME'] = self.tmp.name

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(Path(gs.__file__)), *args],
                              env=self.env, input='', text=True, capture_output=True, timeout=10)

    def test_all_help_without_config(self):
        for command in ('', 'login', 'sync', 'documents', 'lesson-text', 'drive-login',
                        'drive-upload', 'status', 'students', 'gphotos-login', 'albums', 'upload'):
            with self.subTest(command=command):
                result = self.run_cli(*([command] if command else []), '--help')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('--config', result.stdout)
                self.assertIn('--debug', result.stdout)
                if command:
                    self.assertIn('Example:', result.stdout)
                self.assertEqual(result.stderr, '')

    def test_usage_errors(self):
        for args in ([], ['bogus'], ['sync', '--workers', '0'], ['sync', '--workers', '-1'],
                     ['upload', '--workers', 'lots'], ['upload', '--limit', '0'],
                     ['drive-upload', '--prepare-only', '--dry-run'], ['sync', '--work', '2'],
                     ['upload', '--mode', 'wrong'], ['sync', '--output-dir']):
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn('error:', result.stderr)
                self.assertNotIn('Traceback', result.stderr)

    def test_bad_configs(self):
        for content, expected in (('{secret', 'Invalid JSON'), ('[]', 'JSON object'),
                                  ('{"per_student":"false"}', 'boolean'),
                                  ('{"students":[]}', 'keyed by student ID'),
                                  ('{"students":{"x":null}}', 'JSON object'),
                                  ('{"output_dir":42}', 'string'),
                                  ('{"gphotos_mode":"invalid"}', 'off, library, or album')):
            with self.subTest(content=content):
                self.config.write_text(content)
                result = self.run_cli('status', '--config', str(self.config))
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(expected, result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                self.assertNotIn('secret', result.stderr)

    def test_missing_config_and_token(self):
        result = self.run_cli('status', '--config', str(self.config) + '.missing')
        self.assertEqual(result.returncode, 2)
        self.assertIn('Config file not found', result.stderr)
        for command in ('sync', 'documents', 'students'):
            result = self.run_cli(command, '--config', str(self.config))
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('login', result.stderr)
            self.assertNotIn('Traceback', result.stderr)

    def test_ignored_upload_options_rejected(self):
        for flags, expected in ((['--student', 'Max'], 'per_student'),
                                (['--mode', 'library', '--album', 'X'], '--mode album')):
            result = self.run_cli('upload', '--config', str(self.config), *flags)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn(expected, result.stderr)

    def test_runtime_errors_and_debug(self):
        for exc, expected, code in ((PermissionError(13, 'Permission denied', '/example'), 'permissions', 1),
                                    (gs.urllib.error.URLError('secret-url'), 'connection', 1),
                                    (RuntimeError('secret-token'), '--debug', 1),
                                    (KeyboardInterrupt(), 'Interrupted', 130)):
            with self.subTest(exc=type(exc)), patch.object(gs, 'cmd_status', side_effect=exc):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(gs.main(['status', '--config', str(self.config)]), code)
                self.assertIn(expected, stderr.getvalue())
                self.assertNotIn('secret', stderr.getvalue())
                self.assertNotIn('Traceback', stderr.getvalue())
        for args in (['--debug', 'status'], ['status', '--debug']):
            with patch.object(gs, 'cmd_status', side_effect=RuntimeError('diagnostic')):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(gs.main(args), 1)
                self.assertIn('Traceback', stderr.getvalue())

    def test_noninteractive_login_does_not_request_code(self):
        with patch.object(gs, '_http') as http, patch.object(gs.sys.stdin, 'isatty', return_value=False):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(gs.main(['login', '--config', str(self.config), '--user', 'example@example.com']), 2)
            http.assert_not_called()

    def test_existing_code_only_exchanges_and_saves(self):
        with patch.object(gs, '_http', return_value={'access_token': 'test'}) as http:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(gs.main(['login', '--config', str(self.config), '--user', 'a@example.com', '--code', '1234']), 0)
            self.assertEqual(http.call_count, 1)
            self.assertEqual(http.call_args.args[0], gs.AUTH_URL)
            self.assertEqual(json.loads(self.config.read_text())['token'], 'test')

    def test_drive_missing_credentials_before_render(self):
        from types import SimpleNamespace
        with patch.object(gd, 'destinations', return_value=[('Child', 'folder', '/unused')]), patch.object(gd, 'prepare') as prepare:
            with self.assertRaisesRegex(gd.DriveError, 'OAuth'):
                gd.upload(SimpleNamespace(config=str(self.config), prepare_only=False, dry_run=False))
            prepare.assert_not_called()

    def test_relative_config_save(self):
        old = os.getcwd()
        try:
            os.chdir(self.tmp.name)
            gs.save_config('relative.json', {'username': 'example'})
            self.assertTrue(Path('relative.json').is_file())
            self.assertEqual(Path('relative.json').stat().st_mode & 0o777, 0o600)
        finally:
            os.chdir(old)
