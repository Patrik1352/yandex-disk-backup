from __future__ import annotations

from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urljoin
import uuid

import httpx

from ._common import check_status, credentials, json_object, transfer_url
from .errors import APIError, IntegrityError, NetworkError, ProtocolError
from .management import ManagementMixin
from .batch import BatchMixin
from .progress import Reporter, ProgressCallback

BASE = 'https://cloud-api.yandex.net/v1/disk'
RETRYABLE = {408, 423, 429, 500, 502, 503, 504, 509}
CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class TransferResult:
    local_path: Path
    remote_path: str
    size: int
    seconds: float
    md5: str

    @property
    def megabytes_per_second(self) -> float:
        return self.size / 1_000_000 / self.seconds if self.seconds else 0.0


def _remote(path: str) -> str:
    if path.startswith('disk:'):
        path = path[5:]
    if not path or path.endswith('/') or '\x00' in path:
        raise ValueError("Specify a remote file path, including its name")
    return '/' + path.lstrip('/')


def _checksum(path: Path) -> tuple[int, str]:
    digest = hashlib.md5()
    size = 0
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(CHUNK_SIZE), b''):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


class YandexDisk(ManagementMixin, BatchMixin):
    """Synchronous Disk client. Batch operations manage their own worker clients."""

    def __init__(self, token: str, *, fast_upload: bool = True,
                 timeout: float = 600, max_attempts: int = 4,
                 transport: httpx.BaseTransport | None = None):
        if not isinstance(token, str) or not token.strip():
            raise ValueError("A non-empty OAuth token is required")
        if not isinstance(max_attempts, int) or max_attempts < 1 or timeout <= 0:
            raise ValueError("max_attempts and timeout must be positive")
        self._token = token.strip()
        self._attempts = max_attempts
        self._worker_options = dict(token=token, fast_upload=fast_upload, timeout=timeout,
                                    max_attempts=max_attempts, transport=transport)
        # Compatibility setting used by rclone's Yandex backend; configurable
        # because service behavior can change. It is not a speed guarantee.
        ua = ('Yandex.Disk ' + json.dumps({
            'os': 'windows', 'dtype': 'ydisk3', 'vsn': '3.2.37.4977',
            'id': '6BD01244C7A94456BBCEE7EEC990AEAD',
            'id2': '0F370CD40C594A4783BC839C846B999C',
            'session_id': uuid.uuid4().hex,
        }, separators=(',', ':'))) if fast_upload else 'yadisk-client-local/0.2.0'
        options = dict(headers={'User-Agent': ua, 'Accept-Encoding': 'identity'},
                       timeout=httpx.Timeout(timeout, connect=min(30, timeout)),
                       follow_redirects=False, transport=transport)
        self._api = httpx.Client(**options)
        # A separate client ensures OAuth never reaches signed upload/download URLs.
        self._files = httpx.Client(**options)

    @classmethod
    def from_env(cls, env_file: str | Path = '.env', **kwargs) -> YandexDisk:
        """Supports YANDEX_ACCESS_TOKEN and the existing yandex_access_token."""
        return cls(credentials(env_file)['access_token'], **kwargs)

    def check_auth(self) -> bool:
        """Return True on API access; raise AuthenticationError on HTTP 401."""
        self._request('GET', '')
        return True

    def backup_tree(self, local_dir, remote_root, *, exclude=(), workers=4,
                    progress=None, should_stop=None):
        """Content-based copy with current/ and history/; local deletions are retained."""
        from .backup import backup_tree
        return backup_tree(self, local_dir, remote_root, exclude=exclude, workers=workers,
                           progress=progress, should_stop=should_stop)

    @staticmethod
    def _wait(attempt: int, response: httpx.Response | None = None) -> None:
        delay = min(2 ** attempt, 30)
        value = response.headers.get('Retry-After') if response is not None else None
        if value:
            try:
                delay = max(0, float(value))
            except ValueError:
                try:
                    delay = max(0, parsedate_to_datetime(value).timestamp() - time.time())
                except (TypeError, ValueError, OverflowError):
                    pass
        time.sleep(delay)

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        retry = kwargs.pop("_retry", True)
        attempts = self._attempts if retry else 1
        for attempt in range(attempts):
            try:
                response = self._api.request(method, BASE + path,
                    headers={'Authorization': 'OAuth ' + self._token}, **kwargs)
            except httpx.HTTPError:
                if attempt + 1 == attempts:
                    if not retry:
                        raise NetworkError('Operation response lost; completion unknown. Inspect Disk before retrying') from None
                    raise NetworkError("Yandex API connection failed") from None
                self._wait(attempt)
                continue
            if response.status_code in RETRYABLE and attempt + 1 < attempts:
                self._wait(attempt, response)
                continue
            check_status(response)
            return response
        raise AssertionError("Unreachable")

    def _meta(self, remote: str) -> dict:
        return json_object(self._request('GET', '/resources',
            params={'path': remote, 'fields': 'type,size,md5'}))

    def _link(self, endpoint: str, remote: str, **params) -> str:
        info = json_object(self._request('GET', endpoint, params={'path': remote, **params}))
        return transfer_url(info.get('href'))

    def _verify_upload(self, remote: str, size: int, md5: str) -> None:
        # Yandex may finalize uploaded metadata asynchronously.
        for attempt in range(8):
            try:
                meta = self._meta(remote)
                if meta.get('type') == 'file' and meta.get('size') == size and meta.get('md5') == md5:
                    return
            except APIError as exc:
                if exc.status_code != 404:
                    raise
            if attempt < 7:
                time.sleep(min(0.25 * 2 ** attempt, 2))
        raise IntegrityError("Uploaded file could not be confirmed by size and MD5")

    def upload(self, local_path: str | Path, remote_path: str, *,
               overwrite: bool = False, progress: ProgressCallback | None = None) -> TransferResult:
        """Upload one file; remote parent must exist. Retry from the beginning."""
        local, remote = Path(local_path).expanduser(), _remote(remote_path)
        if not local.is_file():
            raise FileNotFoundError("Upload source must be an existing regular file")
        start = time.monotonic()
        size, expected_md5 = _checksum(local)
        reporter = Reporter(progress, remote, 'upload', size)
        sent_once = False
        for attempt in range(self._attempts):
            reporter.reset(attempt)
            # A previous PUT can succeed even if its response was lost. Only
            # reconcile after an attempted PUT; never accept a pre-existing file
            # silently when overwrite=False.
            if sent_once:
                try:
                    meta = self._meta(remote)
                    if meta.get('type') == 'file' and meta.get('size') == size and meta.get('md5') == expected_md5:
                        return self._finished(reporter, local, remote, size, start, expected_md5)
                except APIError as exc:
                    if exc.status_code != 404:
                        raise
            href = self._link('/resources/upload', remote, overwrite=str(overwrite).lower())
            response = None
            try:
                with local.open('rb') as source:
                    digest = hashlib.md5()
                    count = 0

                    def chunks():
                        nonlocal count
                        for chunk in iter(lambda: source.read(CHUNK_SIZE), b''):
                            count += len(chunk)
                            digest.update(chunk)
                            yield chunk
                            reporter.emit(count)

                    sent_once = True
                    response = self._files.put(href, content=chunks(),
                        headers={'Content-Length': str(size), 'Content-Type': 'application/octet-stream'})
                if response.status_code not in (201, 202):
                    check_status(response)
                    raise ProtocolError("Unexpected upload success status")
                if count != size or digest.hexdigest() != expected_md5:
                    raise IntegrityError("Upload source changed during transfer")
            except httpx.HTTPError:
                if attempt + 1 == self._attempts:
                    # Final reconciliation still protects against lost success responses.
                    try:
                        self._verify_upload(remote, size, expected_md5)
                    except (APIError, IntegrityError, NetworkError):
                        raise NetworkError("Upload connection failed; remote completion is unconfirmed") from None
                    return self._finished(reporter, local, remote, size, start, expected_md5)
            except APIError as exc:
                # Expired signed URLs are renewed; other permanent failures stop.
                if exc.status_code not in RETRYABLE | {401, 403, 404} or attempt + 1 == self._attempts:
                    raise
            else:
                self._verify_upload(remote, size, expected_md5)
                return self._finished(reporter, local, remote, size, start, expected_md5)
            self._wait(attempt, response)
        raise NetworkError("Upload did not complete")

    def download(self, remote_path: str, local_path: str | Path, *,
                 overwrite: bool = False, progress: ProgressCallback | None = None) -> TransferResult:
        """Download, verify, then atomically publish the destination file."""
        remote, local = _remote(remote_path), Path(local_path).expanduser()
        if not overwrite and os.path.lexists(local):
            raise FileExistsError("Download destination already exists")
        if not local.parent.is_dir():
            raise FileNotFoundError("Download parent directory must exist")
        start = time.monotonic()
        reporter = Reporter(progress, remote, 'download', 0)
        for attempt in range(self._attempts):
            meta = self._meta(remote)
            if (meta.get('type') != 'file' or not isinstance(meta.get('size'), int)
                    or not isinstance(meta.get('md5'), str)):
                raise ProtocolError("Download requires a file with size and MD5 metadata")
            reporter.total = meta['size']
            reporter.reset(attempt)
            href = self._link('/resources/download', remote)
            temp_path = None
            response = None
            try:
                with tempfile.NamedTemporaryFile(dir=local.parent, prefix='.yadisk-', suffix='.part', delete=False) as out:
                    temp_path = Path(out.name)
                    digest = hashlib.md5()
                    size = 0
                    for redirect in range(6):
                        with self._files.stream('GET', href) as response:
                            if response.status_code in (301, 302, 303, 307, 308):
                                if redirect == 5 or not response.headers.get('Location'):
                                    raise ProtocolError("Invalid or excessive download redirects")
                                href = transfer_url(urljoin(href, response.headers['Location']))
                                continue
                            check_status(response)
                            if response.status_code != 200:
                                raise ProtocolError("Expected complete download response")
                            for chunk in response.iter_bytes(CHUNK_SIZE):
                                size += len(chunk)
                                if size > meta['size']:
                                    raise IntegrityError("Download exceeds expected size")
                                digest.update(chunk)
                                out.write(chunk)
                                reporter.emit(size)
                            break
                    if size != meta['size'] or digest.hexdigest() != meta['md5']:
                        raise IntegrityError("Downloaded file does not match size and MD5")
                    out.flush()
                    os.fsync(out.fileno())
                if overwrite:
                    os.replace(temp_path, local)
                else:
                    # Atomic no-clobber, even when another process creates local
                    # after the initial existence check. Temp is on the same FS.
                    os.link(temp_path, local)
                    temp_path.unlink()
                return self._finished(reporter, local, remote, size, start, digest.hexdigest())
            except httpx.HTTPError:
                if attempt + 1 == self._attempts:
                    raise NetworkError("Download connection failed") from None
            except APIError as exc:
                if exc.status_code not in RETRYABLE | {401, 403, 404} or attempt + 1 == self._attempts:
                    raise
            except IntegrityError:
                if attempt + 1 == self._attempts:
                    raise
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
            self._wait(attempt, response)
        raise NetworkError("Download did not complete")

    @staticmethod
    def _finished(reporter, local, remote, size, start, md5):
        reporter.emit(size, done=True)
        return TransferResult(local, remote, size, time.monotonic() - start, md5)

    def close(self) -> None:
        self._files.close()
        self._api.close()

    def __enter__(self) -> YandexDisk:
        return self

    def __exit__(self, *_args) -> None:
        self.close()
