import hashlib
from pathlib import Path
import posixpath
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from yadisk_client.backup import backup_tree
from yadisk_client.errors import APIError, AuthenticationError, IntegrityError, NetworkError
from yadisk_client.progress import Progress


class MemoryDisk:
    """Independent fake client sessions with shared remote storage."""
    def __init__(self, state=None):
        self.state = state if state is not None else {
            'nodes': {'/': None}, 'calls': [], 'lock': threading.RLock(),
            'clients': [], 'active': 0, 'peak': 0,
        }
        self._worker_options = {'state': self.state}
        self.closed = False
        self.state['clients'].append(self)

    def close(self):
        self.closed = True

    def stat(self, path):
        with self.state['lock']:
            self.state['calls'].append(('stat', path))
            if path not in self.state['nodes']:
                raise APIError(404)
            value = self.state['nodes'][path]
            return {'path': 'disk:' + path, 'type': 'dir' if value is None else 'file',
                    **({} if value is None else {'size': len(value), 'md5': hashlib.md5(value).hexdigest()})}

    def listdir(self, path):
        with self.state['lock']:
            self.state['calls'].append(('listdir', path))
            if self.state.get('bad_listing'):
                return [{'path': 'disk:/unrelated/file', 'type': 'file', 'size': 1, 'md5': '0' * 32}]
            if path not in self.state['nodes']:
                raise APIError(404)
            result = []
            for child in list(self.state['nodes']):
                if child != path and posixpath.dirname(child) == path:
                    # Listing includes metadata without calling the public stat API.
                    data = self.state['nodes'][child]
                    result.append({'path': 'disk:' + child, 'type': 'dir' if data is None else 'file',
                        **({} if data is None else {'size': len(data), 'md5': hashlib.md5(data).hexdigest()})})
            return result

    def mkdir(self, path, *, parents=False, exist_ok=False):
        with self.state['lock']:
            self.state['calls'].append(('mkdir', path))
            paths = ['/' + '/'.join(path.strip('/').split('/')[:i])
                     for i in range(1, len(path.strip('/').split('/')) + 1)] if parents else [path]
            for part in paths:
                if part in self.state['nodes']:
                    if self.state['nodes'][part] is not None or not (exist_ok or part != path):
                        raise FileExistsError()
                    continue
                parent = posixpath.dirname(part)
                if parent not in self.state['nodes'] or self.state['nodes'][parent] is not None:
                    raise APIError(404)
                self.state['nodes'][part] = None

    def upload(self, local, remote, *, overwrite=False, progress=None):
        with self.state['lock']:
            self.state['calls'].append(('upload', remote))
            self.state['active'] += 1
            self.state['peak'] = max(self.state['peak'], self.state['active'])
        try:
            if self.state.get('delay'):
                time.sleep(self.state['delay'])
            if self.state.get('upload_error'):
                raise self.state['upload_error']
            value = Path(local).read_bytes()
            with self.state['lock']:
                if remote in self.state['nodes'] and not overwrite:
                    raise APIError(409)
                self.state['nodes'][remote] = value
            if progress:
                progress(Progress(remote, 'upload', len(value), len(value), 0.1, done=True))
            if self.state.get('after_upload'):
                self.state['after_upload'](local, remote)
            if self.state.get('corrupt_stage'):
                self.state['nodes'][remote] = b'corrupted'
                raise IntegrityError('Upload verification failed')
            return SimpleNamespace(size=len(value), md5=hashlib.md5(value).hexdigest())
        finally:
            with self.state['lock']:
                self.state['active'] -= 1

    def copy(self, source, target, *, overwrite=False):
        with self.state['lock']:
            self.state['calls'].append(('copy', source, target))
            if self.state.get('copy_error'):
                raise self.state['copy_error']
            if target in self.state['nodes'] and not overwrite:
                raise APIError(409)
            self.state['nodes'][target] = self.state['nodes'][source]
            if self.state.get('corrupt_archive'):
                self.state['nodes'][target] = b'broken'

    def move(self, source, target, *, overwrite=False):
        with self.state['lock']:
            self.state['calls'].append(('move', source, target))
            if self.state.get('move_error'):
                raise self.state['move_error']
            if target in self.state['nodes'] and not overwrite:
                raise APIError(409)
            self.state['nodes'][target] = self.state['nodes'].pop(source)
            if self.state.get('move_lost_response'):
                raise NetworkError('https://signed.example/secret')

    def remove(self, path, *, permanently=False, recursive=False):
        with self.state['lock']:
            self.state['calls'].append(('remove', path))
            self.state['nodes'].pop(path, None)
            if recursive:
                for child in list(self.state['nodes']):
                    if child.startswith(path + '/'):
                        del self.state['nodes'][child]


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.disk = MemoryDisk()

    def tearDown(self):
        self.temp.cleanup()

    def source(self, name, data=b'contents'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def previous(self, name, data):
        path = '/backup/current/' + name
        self.disk.mkdir(posixpath.dirname(path), parents=True, exist_ok=True)
        self.disk.state['nodes'][path] = data

    def backup(self, **kwargs):
        return backup_tree(self.disk, self.root, '/backup', **kwargs)

    def test_first_upload_then_unchanged_uses_listing_and_no_upload(self):
        self.source('project/file.txt', b'abc')
        first = self.backup()
        self.assertTrue(first.ok)
        self.assertEqual(first.uploaded, ['project/file.txt'])
        self.assertFalse(any(path.startswith('/backup/.staging/' + first.run_id)
                             for path in self.disk.state['nodes']))
        self.assertIn('/backup/.staging', self.disk.state['nodes'])
        self.disk.state['calls'].clear()
        second = self.backup()
        self.assertTrue(second.ok)
        self.assertEqual(second.unchanged, ['project/file.txt'])
        self.assertFalse(any(call[0] == 'upload' for call in self.disk.state['calls']))
        self.assertEqual([call for call in self.disk.state['calls'] if call[0] == 'stat'],
                         [('stat', '/backup/current')])

    def test_same_size_change_archives_verified_previous_before_move(self):
        self.source('file.txt', b'new')
        self.previous('file.txt', b'old')
        result = self.backup()
        self.assertTrue(result.ok)
        self.assertEqual(result.updated, ['file.txt'])
        self.assertEqual(self.disk.state['nodes']['/backup/current/file.txt'], b'new')
        archive = '/backup/history/' + result.run_id + '/file.txt'
        self.assertEqual(self.disk.state['nodes'][archive], b'old')
        actions = [call[0] for call in self.disk.state['calls']]
        self.assertLess(actions.index('copy'), actions.index('move'))
        self.assertEqual(result.bytes_uploaded, 3)

    def test_copy_failure_preserves_current_and_cleans_only_own_stage(self):
        self.source('file.txt', b'new')
        self.previous('file.txt', b'old')
        self.disk.state['copy_error'] = APIError(507)
        result = self.backup()
        self.assertFalse(result.ok)
        self.assertEqual(result.failed, {'file.txt': 'APIError (HTTP 507)'})
        self.assertEqual(self.disk.state['nodes']['/backup/current/file.txt'], b'old')
        removed = [call[1] for call in self.disk.state['calls'] if call[0] == 'remove']
        self.assertEqual(removed, ['/backup/.staging/' + result.run_id + '/file.txt'])
        self.assertFalse(any(call[0] == 'move' for call in self.disk.state['calls']))

    def test_corrupted_stage_and_archive_never_replace_current(self):
        self.source('file.txt', b'new')
        self.previous('file.txt', b'old')
        for key in ('corrupt_stage', 'corrupt_archive'):
            with self.subTest(key=key):
                self.disk.state[key] = True
                result = self.backup()
                self.assertFalse(result.ok)
                self.assertEqual(self.disk.state['nodes']['/backup/current/file.txt'], b'old')
                self.disk.state[key] = False

    def test_local_deletion_preserves_remote_and_does_not_traverse_remote_only_tree(self):
        self.previous('deleted.txt', b'keep')
        self.previous('deleted-directory/also-keep.txt', b'keep too')
        result = self.backup()
        self.assertTrue(result.ok)
        self.assertEqual(self.disk.state['nodes']['/backup/current/deleted.txt'], b'keep')
        self.assertEqual(self.disk.state['nodes']['/backup/current/deleted-directory/also-keep.txt'], b'keep too')
        self.assertNotIn(('listdir', '/backup/current/deleted-directory'), self.disk.state['calls'])
        self.assertFalse(any(call[0] == 'remove' for call in self.disk.state['calls']))

    def test_exclusions_symlinks_special_files_and_empty_directories(self):
        self.source('kept.txt')
        self.source('node_modules/cache.txt')
        self.source('.env', b'secret')
        (self.root / 'empty').mkdir()
        (self.root / 'link').symlink_to(self.root / 'kept.txt')
        (self.root / 'linked-dir').symlink_to(self.root / 'node_modules', target_is_directory=True)
        result = self.backup(exclude=['node_modules', '.env'])
        self.assertTrue(result.ok)
        self.assertEqual(result.uploaded, ['kept.txt'])
        self.assertEqual(result.skipped, ['.env', 'link', 'linked-dir', 'node_modules'])
        self.assertIn('/backup/current/empty', self.disk.state['nodes'])

    def test_pause_before_scan_is_not_success(self):
        self.source('file.txt')
        events = []
        result = self.backup(should_stop=lambda: True, progress=events.append)
        self.assertFalse(result.ok)
        self.assertTrue(result.paused)
        self.assertTrue(events[-1].done)
        self.assertEqual(events[-1].phase, 'paused')
        self.assertFalse(self.disk.state['calls'])

    def test_pause_between_files_finishes_inflight_and_reports_pending(self):
        for i in range(8):
            self.source(f'{i}.txt', b'new')
        stop = threading.Event()
        self.disk.state['after_upload'] = lambda *_: stop.set()
        result = self.backup(workers=1, should_stop=stop.is_set)
        self.assertFalse(result.ok)
        self.assertTrue(result.paused)
        self.assertEqual(len(result.uploaded), 1)
        self.assertEqual(len(result.pending), 7)

    def test_network_and_auth_errors_stop_scheduling_and_omit_secret_messages(self):
        for i in range(10):
            self.source(f'{i}.txt')
        for error in (NetworkError('https://signed.example/secret'), AuthenticationError(401)):
            with self.subTest(error=type(error).__name__):
                self.disk.state['upload_error'] = error
                self.disk.state['calls'].clear()
                result = self.backup(workers=1)
                self.assertFalse(result.ok)
                self.assertTrue(result.interrupted)
                self.assertEqual(len(result.pending), 9)
                self.assertEqual(len([call for call in self.disk.state['calls'] if call[0] == 'upload']), 1)
                self.assertNotIn('secret', str(result))

    def test_unreadable_source_is_failure_not_success(self):
        path = self.source('file.txt')
        original = Path.lstat
        def denied(candidate, *args, **kwargs):
            if candidate == path:
                raise PermissionError('private local text')
            return original(candidate, *args, **kwargs)
        with patch.object(Path, 'lstat', denied):
            result = self.backup()
        self.assertFalse(result.ok)
        self.assertEqual(result.failed, {'file.txt': 'PermissionError'})
        self.assertEqual(result.uploaded, [])

    def test_unreadable_directory_is_failure(self):
        self.source('blocked/file.txt')
        import os
        original = os.scandir
        def denied(candidate):
            if Path(candidate) == self.root / 'blocked':
                raise PermissionError(13, 'private local text', str(candidate))
            return original(candidate)
        with patch('yadisk_client.backup.os.scandir', denied):
            result = self.backup()
        self.assertFalse(result.ok)
        self.assertEqual(result.failed['blocked'], 'PermissionError')

    def test_source_changes_before_commit_preserve_current(self):
        self.source('file.txt', b'new')
        self.previous('file.txt', b'old')
        self.disk.state['after_upload'] = lambda local, _: Path(local).write_bytes(b'changed')
        result = self.backup()
        self.assertFalse(result.ok)
        self.assertEqual(result.failed['file.txt'], 'SourceChangedError')
        self.assertEqual(self.disk.state['nodes']['/backup/current/file.txt'], b'old')

    def test_external_change_during_upload_is_not_overwritten(self):
        self.source('file.txt', b'new')
        self.previous('file.txt', b'old')
        def change(*_):
            self.disk.state['nodes']['/backup/current/file.txt'] = b'external'
        self.disk.state['after_upload'] = change
        result = self.backup()
        self.assertFalse(result.ok)
        self.assertEqual(result.failed['file.txt'], 'RemoteChangedError')
        self.assertEqual(self.disk.state['nodes']['/backup/current/file.txt'], b'external')

    def test_lost_successful_move_response_is_reconciled(self):
        self.source('file.txt', b'new')
        self.previous('file.txt', b'old')
        self.disk.state['move_lost_response'] = True
        result = self.backup()
        self.assertTrue(result.ok)
        self.assertEqual(result.updated, ['file.txt'])
        self.assertEqual(result.staging_leftovers, [])

    def test_uncertain_move_keeps_stage_and_history(self):
        self.source('file.txt', b'new')
        self.previous('file.txt', b'old')
        self.disk.state['move_error'] = NetworkError('operation still running')
        result = self.backup()
        self.assertFalse(result.ok)
        self.assertTrue(result.interrupted)
        stage = '/backup/.staging/' + result.run_id + '/file.txt'
        self.assertEqual(result.staging_leftovers, [stage])
        self.assertIn(stage, self.disk.state['nodes'])
        self.assertEqual(self.disk.state['nodes']['/backup/history/' + result.run_id + '/file.txt'], b'old')

    def test_parallelism_is_bounded_and_sessions_are_independent(self):
        for i in range(12):
            self.source(f'folder/{i}.txt')
        self.disk.state['delay'] = 0.01
        result = self.backup(workers=3)
        self.assertTrue(result.ok)
        self.assertGreater(self.disk.state['peak'], 1)
        self.assertLessEqual(self.disk.state['peak'], 3)
        self.assertEqual(len(self.disk.state['clients']), 4)
        self.assertFalse(self.disk.closed)
        self.assertTrue(all(worker.closed for worker in self.disk.state['clients'][1:]))
        stage_parents = [call for call in self.disk.state['calls'] if call == (
            'mkdir', '/backup/.staging/' + result.run_id + '/folder')]
        self.assertEqual(len(stage_parents), 1)

    def test_unsafe_remote_listing_never_changes_outside_backup(self):
        self.source('file.txt')
        self.previous('file.txt', b'old')
        self.disk.state['bad_listing'] = True
        result = self.backup()
        self.assertFalse(result.ok)
        self.assertEqual(result.failed['.'], 'ProtocolError')
        self.assertFalse(any(call[0] in ('upload', 'copy', 'move', 'remove')
                             for call in self.disk.state['calls']))

    def test_file_directory_conflicts_are_reported_without_deletion(self):
        self.source('folder/file.txt')
        self.previous('folder', b'cloud file')
        result = self.backup()
        self.assertFalse(result.ok)
        self.assertEqual(self.disk.state['nodes']['/backup/current/folder'], b'cloud file')
        self.assertFalse(any(call[0] in ('upload', 'remove') for call in self.disk.state['calls']))

    def test_same_size_and_preserved_mtime_still_detects_content_change(self):
        import os
        path = self.source('file.txt', b'old')
        self.assertTrue(self.backup().ok)
        value = path.stat()
        path.write_bytes(b'new')
        os.utime(path, ns=(value.st_atime_ns, value.st_mtime_ns))
        result = self.backup()
        self.assertTrue(result.ok)
        self.assertEqual(result.updated, ['file.txt'])
        self.assertEqual(self.disk.state['nodes']['/backup/history/' + result.run_id + '/file.txt'], b'old')

    def test_paused_run_preserves_old_version_until_transfer_finishes(self):
        path = self.source('file.txt', b'new')
        self.previous('file.txt', b'old')
        stop = threading.Event()
        def stop_during_transfer(*_):
            stop.set()
            self.assertEqual(self.disk.state['nodes']['/backup/current/file.txt'], b'old')
        self.disk.state['after_upload'] = stop_during_transfer
        result = self.backup(workers=1, should_stop=stop.is_set)
        self.assertTrue(result.paused)
        self.assertFalse(result.ok)
        self.assertEqual(result.updated, ['file.txt'])
        self.assertEqual(result.pending, [])
        self.assertEqual(self.disk.state['nodes']['/backup/current/file.txt'], path.read_bytes())
        self.assertEqual(self.disk.state['nodes']['/backup/history/' + result.run_id + '/file.txt'], b'old')


if __name__ == '__main__':
    unittest.main()

class StreamingBackupTests(unittest.TestCase):
    setUp = BackupTests.setUp
    tearDown = BackupTests.tearDown
    source = BackupTests.source
    previous = BackupTests.previous
    backup = BackupTests.backup

    def test_files_in_different_directories_transfer_concurrently(self):
        for i in range(12):
            self.source(f'dir{i}/file.txt')
        self.disk.state['delay'] = 0.02
        result = self.backup(workers=4)
        self.assertTrue(result.ok)
        self.assertGreater(self.disk.state['peak'], 1)
        self.assertLessEqual(self.disk.state['peak'], 4)

    def test_no_redundant_stage_stat_after_verified_upload(self):
        self.source('file.txt')
        result = self.backup()
        self.assertTrue(result.ok)
        stage = '/backup/.staging/' + result.run_id + '/file.txt'
        self.assertNotIn(('stat', stage), self.disk.state['calls'])

    def test_transient_listing_failure_retries_without_reupload(self):
        self.source('first.txt')
        self.source('later/second.txt')
        self.previous('later/old.txt', b'old')
        original = MemoryDisk.listdir
        attempts = []
        def flaky(client, path):
            if path == '/backup/current/later':
                attempts.append(path)
                if len(attempts) == 1:
                    raise NetworkError('offline')
            return original(client, path)
        with patch.object(MemoryDisk, 'listdir', flaky):
            result = self.backup(workers=1, network_retries=2, retry_delay=0)
        self.assertTrue(result.ok)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len([c for c in self.disk.state['calls'] if c[0] == 'upload']), 2)

    def test_lost_upload_response_is_retried_and_history_preserved(self):
        self.source('file.txt', b'new')
        self.previous('file.txt', b'old')
        attempts = []
        def lost(*_):
            attempts.append(1)
            if len(attempts) == 1:
                raise NetworkError('offline')
        self.disk.state['after_upload'] = lost
        result = self.backup(network_retries=2, retry_delay=0)
        self.assertTrue(result.ok)
        self.assertEqual(self.disk.state['nodes']['/backup/history/' + result.run_id + '/file.txt'], b'old')
        self.assertEqual(result.bytes_uploaded, 3)

    def test_pause_interrupts_retry_wait(self):
        self.source('file.txt')
        stop = threading.Event()
        self.disk.state['upload_error'] = NetworkError('offline')
        result = self.backup(network_retries=2, retry_delay=10, should_stop=stop.is_set,
            progress=lambda e: stop.set() if e.phase == 'retrying' else None)
        self.assertTrue(result.paused)
        self.assertFalse(result.ok)
