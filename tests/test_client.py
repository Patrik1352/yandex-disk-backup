import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from yadisk_client import (YandexDisk, APIError, AuthenticationError,
                           IntegrityError, NetworkError, ProtocolError)

DATA = b'test contents \x00\xff' * 20


class BrokenStream(httpx.SyncByteStream):
    def __iter__(self):
        yield DATA[:20]
        raise httpx.ReadError('sensitive signed URL')


class DiskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.local = self.root / 'source.bin'
        self.local.write_bytes(DATA)
        self.stored = {}
        self.seen = []
        self.sleeper = patch('yadisk_client.client.time.sleep')
        self.sleep = self.sleeper.start()

    def tearDown(self):
        self.sleeper.stop()
        self.temp.cleanup()

    def handler(self, request):
        self.seen.append(request)
        if request.url.host == 'cloud-api.yandex.net':
            self.assertEqual(request.headers['Authorization'], 'OAuth test-token')
            remote = request.url.params.get('path')
            if request.url.path.endswith('/resources/upload'):
                if remote in self.stored and request.url.params['overwrite'] == 'false':
                    return httpx.Response(409)
                self.upload_path = remote
                return httpx.Response(200, json={'href': 'https://uploader.disk.yandex.net/file'})
            if request.url.path.endswith('/resources/download'):
                self.download_path = remote
                return httpx.Response(200, json={'href': 'https://downloader.disk.yandex.net/file'})
            if request.url.path.endswith('/resources'):
                if remote not in self.stored:
                    return httpx.Response(404)
                data = self.stored[remote]
                return httpx.Response(200, json={'type': 'file', 'size': len(data),
                                               'md5': hashlib.md5(data).hexdigest()})
            return httpx.Response(200, json={})
        self.assertNotIn('authorization', request.headers)
        if request.method == 'PUT':
            self.stored[self.upload_path] = request.read()
            return httpx.Response(201)
        return httpx.Response(200, content=self.stored[self.download_path])

    def client(self, handler=None, **kwargs):
        return YandexDisk('test-token', transport=httpx.MockTransport(handler or self.handler), **kwargs)

    def test_roundtrip_unicode_empty_and_binary(self):
        for data in (DATA, b''):
            self.local.write_bytes(data)
            target = self.root / 'copy.bin'
            target.unlink(missing_ok=True)
            self.stored.clear()
            with self.client() as disk:
                self.assertTrue(disk.check_auth())
                result = disk.upload(self.local, 'disk:/тест # ?/файл.bin')
                downloaded = disk.download('/тест # ?/файл.bin', target)
            self.assertEqual(target.read_bytes(), data)
            self.assertEqual(result.md5, downloaded.md5)
            self.assertEqual(result.size, len(data))
        self.assertTrue(all(r.headers['User-Agent'].startswith('Yandex.Disk ') for r in self.seen))

    def test_auth_failure_has_no_secret_or_body(self):
        with self.client(lambda r: httpx.Response(401, text='secret signed URL')) as disk:
            with self.assertRaises(AuthenticationError) as ctx:
                disk.check_auth()
        self.assertEqual(str(ctx.exception), 'Yandex API returned HTTP 401')

    def test_retry_429_and_503(self):
        statuses = iter([429, 503, 200])
        with self.client(lambda r: httpx.Response(next(statuses), headers={'Retry-After': '2'})) as disk:
            self.assertTrue(disk.check_auth())
        self.assertEqual(self.sleep.call_count, 2)
        self.assertEqual(self.sleep.call_args.args, (2.0,))

    def test_permission_failure_not_retried(self):
        calls = []
        with self.client(lambda r: calls.append(r) or httpx.Response(403)) as disk:
            with self.assertRaises(APIError): disk.check_auth()
        self.assertEqual(len(calls), 1)

    def test_network_failure_bounded_and_sanitized(self):
        calls = []
        def fail(r):
            calls.append(r)
            raise httpx.ConnectError('sensitive token in URL')
        with self.client(fail, max_attempts=2) as disk:
            with self.assertRaises(NetworkError) as ctx: disk.check_auth()
        self.assertEqual(len(calls), 2)
        self.assertNotIn('sensitive', str(ctx.exception))
        self.assertTrue(ctx.exception.__suppress_context__)

    def test_existing_remote_requires_overwrite(self):
        self.stored['/file'] = b'old'
        with self.client() as disk:
            with self.assertRaises(APIError) as ctx: disk.upload(self.local, '/file')
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(self.stored['/file'], b'old')
            disk.upload(self.local, '/file', overwrite=True)
        self.assertEqual(self.stored['/file'], DATA)

    def test_upload_retry_rewinds_and_renews_url(self):
        puts = []
        def handler(r):
            if r.method == 'PUT':
                puts.append(r.read())
                if len(puts) == 1: return httpx.Response(503)
            return self.handler(r)
        with self.client(handler) as disk:
            disk.upload(self.local, '/file')
        self.assertEqual(puts, [DATA, DATA])
        self.assertEqual(sum(r.url.path.endswith('/upload') for r in self.seen), 2)

    def test_lost_upload_response_reconciles_success(self):
        puts = []
        def handler(r):
            result = self.handler(r)
            if r.method == 'PUT':
                puts.append(r)
                raise httpx.ReadError('signed URL')
            return result
        with self.client(handler) as disk:
            result = disk.upload(self.local, '/file')
        self.assertEqual(result.md5, hashlib.md5(DATA).hexdigest())
        self.assertEqual(len(puts), 1)

    def test_final_lost_response_can_still_confirm_success(self):
        def handler(r):
            result = self.handler(r)
            if r.method == 'PUT': raise httpx.ReadError('signed URL')
            return result
        with self.client(handler, max_attempts=1) as disk:
            self.assertEqual(disk.upload(self.local, '/file').size, len(DATA))

    def test_final_upload_failure_suppresses_sensitive_context(self):
        def handler(r):
            if r.method == 'PUT': raise httpx.WriteError('sensitive signed URL')
            return self.handler(r)
        with self.client(handler, max_attempts=1) as disk:
            with self.assertRaises(NetworkError) as ctx: disk.upload(self.local, '/file')
        self.assertTrue(ctx.exception.__suppress_context__)
        self.assertNotIn('sensitive', str(ctx.exception))

    def test_202_upload_waits_for_metadata(self):
        reads = []
        def handler(r):
            result = self.handler(r)
            if r.method == 'PUT': return httpx.Response(202)
            if r.url.path.endswith('/resources'):
                reads.append(r)
                if len(reads) == 1: return httpx.Response(404)
            return result
        with self.client(handler) as disk:
            disk.upload(self.local, '/file')
        self.assertEqual(len(reads), 2)

    def test_upload_does_not_accept_wrong_hash(self):
        def handler(r):
            result = self.handler(r)
            if r.method == 'PUT': self.stored[self.upload_path] = b'x' * len(DATA)
            return result
        with self.client(handler) as disk:
            with self.assertRaises(IntegrityError): disk.upload(self.local, '/file')

    def test_download_failure_preserves_existing_file(self):
        self.stored['/file'] = DATA
        target = self.root / 'existing'
        target.write_bytes(b'keep me')
        def handler(r):
            if r.url.host == 'downloader.disk.yandex.net':
                return httpx.Response(200, stream=BrokenStream())
            return self.handler(r)
        with self.client(handler, max_attempts=2) as disk:
            with self.assertRaises(NetworkError): disk.download('/file', target, overwrite=True)
        self.assertEqual(target.read_bytes(), b'keep me')
        self.assertFalse(list(self.root.glob('*.part')))

    def test_download_hash_failure_no_partial_file(self):
        self.stored['/file'] = DATA
        def handler(r):
            if r.url.host == 'downloader.disk.yandex.net': return httpx.Response(200, content=b'x' * len(DATA))
            return self.handler(r)
        with self.client(handler, max_attempts=1) as disk:
            with self.assertRaises(IntegrityError): disk.download('/file', self.root / 'out')
        self.assertFalse((self.root / 'out').exists())
        self.assertFalse(list(self.root.glob('*.part')))

    def test_download_retry_restarts_without_duplicating_bytes(self):
        self.stored['/file'] = DATA
        reads = []
        def handler(r):
            if r.url.host == 'downloader.disk.yandex.net':
                reads.append(r)
                if len(reads) == 1: return httpx.Response(200, stream=BrokenStream())
            return self.handler(r)
        target = self.root / 'out'
        with self.client(handler) as disk: disk.download('/file', target)
        self.assertEqual(len(reads), 2)
        self.assertEqual(target.read_bytes(), DATA)

    def test_download_follows_valid_redirect_without_oauth(self):
        self.stored['/file'] = DATA
        def handler(r):
            if r.url.host == 'downloader.disk.yandex.net':
                return httpx.Response(302, headers={'Location': 'https://storage.yandex.net/final'})
            return self.handler(r)
        with self.client(handler) as disk: disk.download('/file', self.root / 'out')
        self.assertEqual((self.root / 'out').read_bytes(), DATA)

    def test_expired_upload_link_is_requested_again(self):
        puts = []
        def handler(r):
            if r.method == 'PUT':
                puts.append(r)
                if len(puts) == 1: return httpx.Response(403)
            return self.handler(r)
        with self.client(handler) as disk: disk.upload(self.local, '/file')
        self.assertEqual(len(puts), 2)

    def test_local_destination_not_overwritten(self):
        with self.client() as disk:
            with self.assertRaises(FileExistsError): disk.download('/file', self.local)
        self.assertEqual(self.local.read_bytes(), DATA)
        self.assertFalse(self.seen)

    def test_destination_race_does_not_overwrite(self):
        self.stored['/file'] = DATA
        target = self.root / 'race'
        def handler(r):
            if r.url.host == 'downloader.disk.yandex.net': target.write_bytes(b'other process')
            return self.handler(r)
        with self.client(handler) as disk:
            with self.assertRaises(FileExistsError): disk.download('/file', target)
        self.assertEqual(target.read_bytes(), b'other process')

    def test_download_redirect_rejects_non_yandex_host(self):
        self.stored['/file'] = DATA
        def handler(r):
            if r.url.host == 'downloader.disk.yandex.net':
                return httpx.Response(302, headers={'Location': 'https://evil.example/file'})
            return self.handler(r)
        with self.client(handler) as disk:
            with self.assertRaises(ProtocolError): disk.download('/file', self.root / 'out')

    def test_unsafe_upload_link_rejected(self):
        def handler(r):
            if r.url.path.endswith('/upload'):
                return httpx.Response(200, json={'href': 'http://uploader.disk.yandex.net/file'})
            return self.handler(r)
        with self.client(handler) as disk:
            with self.assertRaises(ProtocolError): disk.upload(self.local, '/file')

    def test_legacy_env_quotes_and_env_override(self):
        env = self.root / '.env'
        env.write_text('yandex_access_token="file-token"\n')
        with patch.dict(os.environ, {}, clear=True):
            with YandexDisk.from_env(env, transport=httpx.MockTransport(lambda r: httpx.Response(200))) as disk:
                self.assertEqual(disk._token, 'file-token')
        with patch.dict(os.environ, {'YANDEX_ACCESS_TOKEN': 'environment-token'}, clear=True):
            with YandexDisk.from_env(env) as disk:
                self.assertEqual(disk._token, 'environment-token')

    def test_missing_token_and_invalid_options(self):
        for kwargs in ({'token': ''}, {'token': 'a', 'max_attempts': 0}, {'token': 'a', 'timeout': 0}):
            with self.assertRaises(ValueError): YandexDisk(**kwargs)


if __name__ == '__main__': unittest.main()
