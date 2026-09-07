import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import posixpath
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote

import httpx

from yadisk_client import YandexDisk, Progress, APIError, NetworkError, ProtocolError
from yadisk_client.batch import excluded
from yadisk_client.cli import main, parser, _save_tokens
from yadisk_client.auth import Tokens


class FakeServer:
    def __init__(self):
        self.nodes = {'/': None}
        self.calls = []
        self.lock = threading.RLock()
        self.peak, self.active = 0, 0
        self.fail = set()

    def meta(self, path):
        data = self.nodes[path]
        return {'path': 'disk:' + path, 'name': posixpath.basename(path),
                'type': 'dir' if data is None else 'file',
                **({} if data is None else {'size': len(data), 'md5': hashlib.md5(data).hexdigest()})}

    def handler(self, r):
        endpoint, params = r.url.path, dict(r.url.params)
        path = params.get('path', '/')
        if r.url.host == 'uploader.disk.yandex.net':
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            time.sleep(0.01)
            with self.lock:
                self.active -= 1
                if path in self.fail:
                    return httpx.Response(400)
                self.nodes[path] = r.read()
            assert 'Authorization' not in r.headers
            return httpx.Response(201)
        if r.url.host == 'downloader.disk.yandex.net':
            return httpx.Response(200, content=self.nodes[path])
        with self.lock:
            self.calls.append((r.method, endpoint, params))
            if endpoint == '/v1/disk':
                return httpx.Response(200, json={'total_space': 10000, 'used_space': 100, 'trash_size': 0})
            if endpoint.endswith('/upload'):
                if path in self.nodes and params['overwrite'] == 'false':
                    return httpx.Response(409)
                return httpx.Response(200, json={'href': 'https://uploader.disk.yandex.net/file?path='+quote(path)})
            if endpoint.endswith('/download'):
                return httpx.Response(200, json={'href': 'https://downloader.disk.yandex.net/file?path='+quote(path)})
            if endpoint == '/v1/disk/resources':
                if r.method == 'PUT':
                    if path in self.nodes: return httpx.Response(409)
                    if posixpath.dirname(path) not in self.nodes: return httpx.Response(404)
                    self.nodes[path] = None
                    return httpx.Response(201)
                if r.method == 'GET':
                    if path not in self.nodes: return httpx.Response(404)
                    obj = self.meta(path)
                    if self.nodes[path] is None:
                        children = sorted(p for p in self.nodes if p != path and posixpath.dirname(p) == path)
                        offset = int(params.get('offset', 0))
                        obj['_embedded'] = {'total': len(children), 'items': [self.meta(p) for p in children[offset:offset+2]]}
                    return httpx.Response(200, json=obj)
            return httpx.Response(204)


class ExtendedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = FakeServer()
        self.disk = YandexDisk('test', max_attempts=1, transport=httpx.MockTransport(self.server.handler))

    def tearDown(self):
        self.disk.close()
        self.temp.cleanup()

    def test_pagination_walk_size_and_quota(self):
        self.server.nodes.update({'/a': b'a', '/b': b'bb', '/c': b'ccc', '/dir': None, '/dir/leaf': b'1234'})
        self.assertEqual(len(self.disk.listdir()), 4)
        self.assertEqual(len(list(self.disk.walk())), 5)
        self.assertEqual(self.disk.size(), {'bytes': 10, 'files': 4, 'directories': 1})
        self.assertEqual(self.disk.info()['free_space'], 9900)
        self.assertFalse(self.disk.exists('/missing'))

    def test_recursive_parallel_roundtrip_exclusions_and_empty_dirs(self):
        source = self.root/'source'; source.mkdir()
        (source/'empty').mkdir(); (source/'nested').mkdir(); (source/'node_modules').mkdir()
        (source/'node_modules'/'skip').write_text('no')
        (source/'nested'/'x.tmp').write_text('skip')
        (source/'.env').write_text('not uploaded')
        for i in range(8): (source/'nested'/f'{i}.bin').write_bytes(bytes([i])*100)
        events = []
        result = self.disk.upload_tree(source, '/tree', exclude=['node_modules', '*.tmp', '.env'], workers=3, progress=events.append)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.completed), 8)
        self.assertEqual(len(result.skipped), 3)
        self.assertGreater(self.server.peak, 1)
        self.assertLessEqual(self.server.peak, 3)
        self.assertTrue(events[-1].done)
        self.assertEqual(events[-1].files_completed, 8)
        self.assertEqual(events[-1].percent, 100)
        self.assertEqual(events[-1].transferred, 800)
        target = self.root/'download'
        result = self.disk.download_tree('/tree', target, workers=3, exclude=['0.bin'])
        self.assertTrue(result.ok)
        self.assertTrue((target/'empty').is_dir())
        self.assertFalse((target/'nested'/'0.bin').exists())
        for i in range(1, 8):
            self.assertEqual((target/'nested'/f'{i}.bin').read_bytes(), bytes([i])*100)

    def test_partial_queue_failure_is_reported_and_other_files_finish(self):
        jobs = []
        for i in range(3):
            source = self.root/f'{i}'
            source.write_bytes(b'content')
            jobs.append((source, f'/file{i}'))
        self.server.fail.add('/file1')
        result = self.disk.upload_many(jobs, workers=2)
        self.assertFalse(result.ok)
        self.assertEqual(len(result.failed), 1)
        self.assertEqual(len(result.completed), 2)

    def test_duplicate_queue_destinations_and_invalid_workers_rejected(self):
        p = self.root/'source'; p.write_bytes(b'x')
        with self.assertRaises(ValueError): self.disk.upload_many([(p, '/x'), (p, 'disk:/x')])
        for workers in [0, 33, 1.5]:
            with self.assertRaises(ValueError): self.disk.upload_tree(self.root, '/tree', workers=workers)
        self.assertFalse(self.server.calls)

    def test_existing_symlink_in_download_tree_is_rejected(self):
        self.server.nodes.update({'/tree': None, '/tree/dir': None, '/tree/dir/a': b'a'})
        target = self.root/'target'; target.mkdir()
        outside = self.root/'outside'; outside.mkdir()
        (target/'dir').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError): self.disk.download_tree('/tree', target)
        self.assertFalse(list(outside.iterdir()))

    def test_case_collisions_rejected_before_writing(self):
        self.server.nodes.update({'/tree': None, '/tree/A': b'a', '/tree/a': b'b'})
        target = self.root/'out'
        with self.assertRaises(ProtocolError): self.disk.download_tree('/tree', target)
        self.assertFalse(target.exists())

    def test_symlink_upload_skipped(self):
        source = self.root/'source'; source.mkdir()
        (source/'link').symlink_to('/does-not-exist')
        result = self.disk.upload_tree(source, '/tree')
        self.assertEqual(result.skipped, ['link'])
        self.assertEqual(result.completed, [])

    def test_globs_prune_nested_folders(self):
        for rel, pattern in [('nested/a.tmp','*.tmp'), ('node_modules/a','node_modules'),
                             ('nested/build/a','nested/build/**'), ('nested/build','nested/build/**')]:
            self.assertTrue(excluded(rel, [pattern]))
        self.assertFalse(excluded('abc/report.csv', ['*.tmp']))

    def test_async_operation_is_polled_and_mutation_not_replayed(self):
        calls = []
        def handler(r):
            calls.append(r)
            if r.method == 'POST': return httpx.Response(202, json={'href':'https://cloud-api.yandex.net/v1/disk/operations/id'})
            return httpx.Response(200, json={'status':'in-progress' if len(calls)==2 else 'success'})
        with YandexDisk('test', transport=httpx.MockTransport(handler)) as d, patch('yadisk_client.management.time.sleep'):
            d.move('/old', '/new')
        self.assertEqual([r.method for r in calls], ['POST','GET','GET'])
        calls.clear()
        def fail(r):
            calls.append(r)
            raise httpx.ReadError('sensitive URL')
        with YandexDisk('test', transport=httpx.MockTransport(fail)) as d:
            with self.assertRaises(NetworkError): d.copy('/old','/new')
        self.assertEqual(len(calls), 1)

    def test_failed_operation_and_invalid_operation_url(self):
        for link in ['https://evil.example/v1/disk/operations/id', 'http://cloud-api.yandex.net/v1/disk/operations/id']:
            with YandexDisk('test', transport=httpx.MockTransport(lambda r: httpx.Response(202,json={'href':link}))) as d:
                with self.assertRaises(ProtocolError): d.copy('/a','/b')

    def test_management_paths_trash_flags_and_public_link(self):
        calls = []
        def handler(r):
            calls.append((r.method,r.url.path,dict(r.url.params)))
            if r.method == 'GET':
                return httpx.Response(200,json={'type':'file','public_url':'https://disk.yandex.ru/d/test'})
            return httpx.Response(204)
        with YandexDisk('test',transport=httpx.MockTransport(handler)) as d:
            d.copy('/a','/b'); d.rename('/b','new'); d.remove('/new')
            d.restore('trash:/opaque',name='restored'); d.trash_delete('trash:/opaque')
            self.assertEqual(d.publish('/a'),'https://disk.yandex.ru/d/test')
            d.unpublish('/a')
            for action in [lambda:d.remove('/',recursive=True),lambda:d.move('/a','/a/b'),
                           lambda:d.trash_delete('/'),lambda:d.rename('/a','../b')]:
                with self.assertRaises(ValueError): action()
        remove = next(c for c in calls if c[0]=='DELETE' and c[1]=='/v1/disk/resources')
        self.assertEqual(remove[2]['permanently'],'false')
        restore = next(c for c in calls if c[1].endswith('/restore'))
        self.assertEqual(restore[2]['path'],'trash:/opaque')

    def test_directory_delete_requires_recursive(self):
        self.server.nodes['/dir'] = None
        with self.assertRaises(IsADirectoryError): self.disk.remove('/dir')
        self.assertFalse(any(m=='DELETE' for m,_,_ in self.server.calls))

    def test_trash_uses_supported_sort_and_rejects_root_aliases(self):
        def handler(r):
            self.assertEqual(r.url.params['sort'], 'created')
            return httpx.Response(200, json={'type': 'dir', '_embedded': {'total': 0, 'items': []}})
        with YandexDisk('test', transport=httpx.MockTransport(handler)) as d:
            self.assertEqual(d.trash_list(), [])
            for path in ['//', 'trash://', '/', '', 'trash:/../']:
                with self.assertRaises(ValueError): d.trash_delete(path)

    def test_progress_not_complete_until_verified(self):
        p = self.root/'file'; p.write_bytes(b'123')
        events = []
        self.disk.upload(p,'/file',progress=events.append)
        self.assertEqual(events[0].transferred,0)
        self.assertTrue(events[-1].done)
        self.assertFalse(any(e.done for e in events[:-1]))
        event = Progress('p','upload',50,100,2)
        self.assertEqual(event.percent,50)
        self.assertEqual(event.bytes_per_second,25)
        self.assertEqual(event.eta_seconds,2)

    def test_cli_global_options_and_json_failure_exit(self):
        for args in [['--json','trash','ls'],['trash','ls','--json']]:
            self.assertTrue(parser().parse_args(args).json)
        output = io.StringIO()
        with patch('yadisk_client.cli.YandexDisk.from_env', return_value=self.disk), contextlib.redirect_stdout(output):
            self.assertEqual(main(['df','--json']),0)
        self.assertEqual(json.loads(output.getvalue())['free_space'],9900)
        output = io.StringIO()
        with patch('yadisk_client.cli.YandexDisk.from_env', return_value=self.disk), contextlib.redirect_stdout(output):
            self.assertEqual(main(['cp','local-a','local-b','--json']),1)
        self.assertIn('error',json.loads(output.getvalue()))

    def test_cli_copy_direction_routing(self):
        from unittest.mock import MagicMock
        for source,dest,method in [('a','disk:/b','upload'),('disk:/a','b','download'),('disk:/a','disk:/b','copy')]:
            disk = MagicMock()
            disk.__enter__.return_value = disk
            disk.stat.return_value={'type':'file'}
            disk.exists.return_value=False
            getattr(disk,method).return_value=None
            with patch('yadisk_client.cli.YandexDisk.from_env',return_value=disk), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['cp',source,dest]),0)
            getattr(disk,method).assert_called_once()

    def test_token_store_exclusive_and_private(self):
        path = self.root/'token.json'
        _save_tokens(path,Tokens('secret','refresh'),overwrite=False)
        if os.name=='posix': self.assertEqual(path.stat().st_mode & 0o777,0o600)
        with self.assertRaises(FileExistsError): _save_tokens(path,Tokens('changed'),overwrite=False)
        self.assertEqual(json.loads(path.read_text())['access_token'],'secret')
        _save_tokens(path,Tokens('changed'),overwrite=True)
        self.assertEqual(json.loads(path.read_text())['access_token'],'changed')


if __name__ == '__main__': unittest.main()
