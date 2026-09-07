"""Behavioural checks for the local worker; never contact Disk or install jobs."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from yadisk_client.backup import BackupProgress, BackupResult
from yadisk_client.errors import AuthenticationError, NetworkError
from yadisk_client.service import BackupService, load_config, main, read_json, write_json


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        source = self.directory / 'source'
        source.mkdir()
        self.config = {
            'source': str(source), 'remote_root': '/Backups/Test/Desktop',
            'env_file': str(self.directory / 'credentials.env'),
            'interval_seconds': 900, 'workers': 2,
            'network_retry_seconds': 60, 'timeout': 120, 'exclude': ['.env'],
        }
        write_json(self.directory / 'config.json', self.config)
        self.old_success = '2026-09-01T12:00:00+00:00'
        write_json(self.directory / 'status.json', {
            'last_success': self.old_success,
            'last_result': {'uploaded': 1},
        })
        self.disk = Mock()
        self.disk.__enter__ = Mock(return_value=self.disk)
        self.disk.__exit__ = Mock(return_value=False)
        self.factory = Mock(return_value=self.disk)

    def service(self):
        service = BackupService(self.directory, client_factory=self.factory)

        def close_handlers():
            for handler in service.logger.handlers[:]:
                handler.close()
                service.logger.removeHandler(handler)
        self.addCleanup(close_handlers)
        return service

    def status(self):
        return read_json(self.directory / 'status.json')

    def test_success_replaces_last_success_and_persists_result(self):
        service = self.service()
        result = BackupResult(uploaded=['new'], updated=['changed'], unchanged=['same'],
                              bytes_uploaded=13)
        with patch('yadisk_client.backup.backup_tree', return_value=result) as backup:
            self.assertTrue(service.run_once(self.config))
        self.disk.check_auth.assert_called_once_with()
        backup.assert_called_once()
        status = self.status()
        self.assertEqual(status['state'], 'idle')
        self.assertFalse(status['running'])
        self.assertNotEqual(status['last_success'], self.old_success)
        self.assertEqual(status['last_result']['bytes_uploaded'], 13)
        self.assertEqual(status['last_result']['updated'], 1)
        self.assertEqual(read_json(self.directory / 'last_result.json')['uploaded'], ['new'])

    def test_partial_failure_never_claims_a_complete_backup(self):
        result = BackupResult(uploaded=['good'], failed={'bad': 'PermissionError'})
        with patch('yadisk_client.backup.backup_tree', return_value=result):
            self.assertFalse(self.service().run_once(self.config))
        status = self.status()
        self.assertEqual(status['state'], 'error')
        self.assertFalse(status['running'])
        self.assertEqual(status['last_success'], self.old_success)
        self.assertEqual(status['last_result']['failed'], 1)

    def test_pending_or_interrupted_backups_do_not_advance_last_success(self):
        for result in [BackupResult(pending=['not yet']), BackupResult(interrupted=True)]:
            with self.subTest(result=result):
                with patch('yadisk_client.backup.backup_tree', return_value=result):
                    self.assertFalse(self.service().run_once(self.config))
                self.assertEqual(self.status()['last_success'], self.old_success)
                self.assertNotEqual(self.status()['state'], 'idle')

    def test_network_failure_reports_offline_and_preserves_previous_success(self):
        self.disk.check_auth.side_effect = NetworkError('signed-url?secret=do-not-log')
        with patch('yadisk_client.backup.backup_tree') as backup:
            self.assertFalse(self.service().run_once(self.config))
        backup.assert_not_called()
        self.assertEqual(self.status()['state'], 'offline')
        self.assertFalse(self.status()['running'])
        self.assertEqual(self.status()['last_success'], self.old_success)
        self.assertNotIn('do-not-log', (self.directory / 'backup.log').read_text())
        self.assertNotIn('do-not-log', (self.directory / 'status.json').read_text())

    def test_file_level_network_failure_reports_offline(self):
        result = BackupResult(failed={'a': 'NetworkError'}, pending=['b'], interrupted=True)
        with patch('yadisk_client.backup.backup_tree', return_value=result):
            self.assertFalse(self.service().run_once(self.config))
        self.assertEqual(self.status()['state'], 'offline')
        self.assertEqual(self.status()['last_success'], self.old_success)

    def test_expired_token_has_actionable_status_and_no_exception_details(self):
        self.disk.check_auth.side_effect = AuthenticationError(401)
        self.assertFalse(self.service().run_once(self.config))
        self.assertEqual(self.status()['state'], 'error')
        self.assertIn('токен', self.status()['message'])
        self.assertEqual(self.status()['last_success'], self.old_success)

    def test_paused_once_does_not_create_client_or_start_upload(self):
        self.assertEqual(main(['--config-dir', str(self.directory), '--pause']), 0)
        service = self.service()
        with patch('yadisk_client.backup.backup_tree') as backup:
            self.assertEqual(service.serve(once=True), 1)
        self.factory.assert_not_called()
        backup.assert_not_called()
        self.assertEqual(self.status()['state'], 'paused')
        self.assertFalse(self.status()['running'])
        self.assertEqual(self.status()['last_success'], self.old_success)

    def test_resume_removes_pause_and_requests_an_immediate_run(self):
        main(['--config-dir', str(self.directory), '--pause'])
        main(['--config-dir', str(self.directory), '--resume'])
        self.assertFalse((self.directory / 'paused').exists())
        self.assertTrue((self.directory / 'run-now').exists())
        service = self.service()
        result = BackupResult(unchanged=['same'])
        with patch('yadisk_client.backup.backup_tree', return_value=result):
            # Keep this bounded while still allowing the actual executor to run.
            real_wait = service.stop.wait
            with patch.object(service.stop, 'wait', side_effect=lambda _: real_wait(0.01)):
                self.assertEqual(service.serve(once=True), 0)
        self.factory.assert_called_once()
        self.assertFalse((self.directory / 'run-now').exists())

    def test_pause_during_backup_is_observed_and_does_not_advance_success(self):
        service = self.service()

        def backup(*args, should_stop, progress, **kwargs):
            self.assertFalse(should_stop())
            (self.directory / 'paused').touch()
            self.assertTrue(should_stop())
            progress(BackupProgress('paused', 'next', 4, 8, 1, 1, 2, paused=True, done=True))
            return BackupResult(uploaded=['first'], pending=['next'], paused=True)

        with patch('yadisk_client.backup.backup_tree', side_effect=backup):
            self.assertFalse(service.run_once(self.config))
        self.assertEqual(self.status()['state'], 'paused')
        self.assertFalse(self.status()['running'])
        self.assertEqual(self.status()['last_success'], self.old_success)

    def test_checking_phases_do_not_claim_uploading(self):
        service = self.service()

        def backup(*args, progress, **kwargs):
            for phase in ('scanning', 'listing', 'checking', 'preparing'):
                # Force delivery despite throttling; counters remain at zero.
                progress(BackupProgress(phase, 'sample', 0, 0, 1, 0, 1, done=True))
                self.assertEqual(self.status()['state'], 'scanning', phase)
            return BackupResult(unchanged=['sample'])

        with patch('yadisk_client.backup.backup_tree', side_effect=backup):
            self.assertTrue(service.run_once(self.config))

    def test_another_process_cannot_start_while_worker_lock_is_held(self):
        original_status = (self.directory / 'status.json').read_bytes()
        with (self.directory / 'worker.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code = (
                'from pathlib import Path\n'
                'import sys\n'
                'from yadisk_client.service import BackupService\n'
                'def forbidden(*a, **kw):\n'
                '    raise AssertionError("A second worker started")\n'
                'service = BackupService(Path(sys.argv[1]), client_factory=forbidden)\n'
                'raise SystemExit(service.serve(once=True))\n'
            )
            src = str(Path(__file__).resolve().parents[1] / 'src')
            process = subprocess.run([sys.executable, '-c', code, str(self.directory)],
                env=dict(os.environ, PYTHONPATH=src), capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual((self.directory / 'status.json').read_bytes(), original_status)

    def test_malformed_settings_publish_config_error_without_network(self):
        cases = [
            {'source': None}, {'source': ['bad']}, {'source': '/'},
            {'source': str(Path.home())}, {'source': 'relative'},
            {'remote_root': None}, {'remote_root': 1}, {'remote_root': '/'},
            {'env_file': None}, {'env_file': []}, {'exclude': 'not-a-list'},
            {'exclude': [1]}, {'workers': True}, {'workers': 0},
            {'interval_seconds': 1}, {'network_retry_seconds': 0},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                write_json(self.directory / 'config.json', self.config | changes)
                self.assertEqual(self.service().serve(once=True), 1)
                self.assertEqual(self.status()['state'], 'error')
                self.assertFalse(self.status()['running'])
                self.assertEqual(self.status()['last_success'], self.old_success)
        self.factory.assert_not_called()

    def test_invalid_config_releases_worker_lock_for_a_later_attempt(self):
        (self.directory / 'config.json').write_text('{bad json')
        self.assertEqual(self.service().serve(once=True), 1)
        with (self.directory / 'worker.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_non_object_cached_status_does_not_prevent_worker_start(self):
        for value in (None, [], 'invalid status'):
            with self.subTest(value=value):
                write_json(self.directory / 'status.json', value)
                service = self.service()
                service.publish(state='idle')
                self.assertIsNone(self.status()['last_success'])

    def test_atomic_write_failure_preserves_previous_status(self):
        status_path = self.directory / 'status.json'
        original = status_path.read_bytes()
        with patch('yadisk_client.service.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                write_json(status_path, {'state': 'updated'})
        self.assertEqual(status_path.read_bytes(), original)
        self.assertEqual(list(self.directory.glob('.status.json*')), [])

    def test_readers_never_observe_a_partially_written_status(self):
        status_path = self.directory / 'status.json'
        errors, observed = [], []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    value = json.loads(status_path.read_text())
                    observed.append(value)
                    if 'generation' in value and value['text'] != str(value['generation']) * 1000:
                        errors.append('mixed generations')
                except Exception as exc:
                    errors.append(type(exc).__name__)

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for generation in range(30):
                write_json(status_path, {'generation': generation, 'text': str(generation) * 1000})
        finally:
            stop.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(observed)
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
