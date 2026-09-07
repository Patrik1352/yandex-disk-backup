"""Incremental, versioned one-way backups into an exclusively managed Disk folder.

Local deletions never delete the current cloud copy. Each changed file is uploaded
and verified in a unique staging path; an existing version is copied and verified
in history before the staged file replaces it. There is no remote compare-and-swap
API: use a dedicated destination and do not run two writers against it.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import posixpath
import re
import stat
import threading
import time
import uuid

from .batch import excluded
from .errors import APIError, AuthenticationError, IntegrityError, NetworkError, ProtocolError
from .management import non_root, remote_path


@dataclass
class BackupResult:
    run_id: str = ''
    uploaded: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    pending: list[str] = field(default_factory=list)
    staging_leftovers: list[str] = field(default_factory=list)
    bytes_uploaded: int = 0
    bytes_total: int = 0
    paused: bool = False
    interrupted: bool = False

    @property
    def ok(self) -> bool:
        return not (self.failed or self.pending or self.paused or self.interrupted)


@dataclass(frozen=True)
class BackupProgress:
    """Serialized callback event; paths are relative, counters are aggregate.

    Phases: scanning, listing, checking, preparing, uploading, archiving,
    committing, finished, paused, interrupted. ``total`` is the planned upload
    byte count discovered as directories are checked; ``transferred`` includes in-flight bytes.
    ``done`` means this invocation has ended, including failure or pause; inspect
    the returned BackupResult.ok before declaring a successful backup. Callbacks
    are synchronous, serialized and throttled to approximately five per second.
    """
    phase: str
    path: str
    transferred: int
    total: int
    elapsed: float
    files_completed: int
    files_total: int
    paused: bool = False
    done: bool = False

    @property
    def percent(self) -> float:
        return min(100.0, self.transferred / self.total * 100) if self.total else (
            100.0 if self.done and not self.paused and self.phase == 'finished' else 0.0)

    @property
    def bytes_per_second(self) -> float:
        return self.transferred / self.elapsed if self.elapsed > 0 else 0.0

    @property
    def eta_seconds(self) -> float | None:
        speed = self.bytes_per_second
        return max(0, self.total - self.transferred) / speed if speed else (
            0.0 if self.done and self.phase == 'finished' else None)


class SourceChangedError(IntegrityError):
    """The local file changed after it was scanned; retry on the next run."""


class RemoteChangedError(IntegrityError):
    """Another writer changed the current cloud file; replacement was stopped."""


class _Stopped(Exception):
    pass


@dataclass(frozen=True)
class _Source:
    path: Path
    relative: str
    size: int
    signature: tuple


@dataclass(frozen=True)
class _Job:
    source: _Source
    md5: str
    previous: tuple[int, str] | None


def _signature(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _safe_error(exc):
    # Do not expose exception messages: third-party errors may include signed URLs.
    code = getattr(exc, 'status_code', None)
    return type(exc).__name__ + (f' (HTTP {code})' if isinstance(code, int) else '')


def _fingerprint(meta):
    if meta is None:
        return None
    size, digest = meta.get('size'), meta.get('md5')
    if meta.get('type') != 'file':
        raise IsADirectoryError('A directory occupies the destination file path')
    if (not isinstance(size, int) or isinstance(size, bool) or size < 0
            or not isinstance(digest, str) or not re.fullmatch('[0-9a-fA-F]{32}', digest)):
        raise ProtocolError('File metadata does not contain a valid size and MD5')
    return size, digest.lower()


def _stat_optional(disk, path):
    try:
        return disk.stat(path)
    except APIError as exc:
        if exc.status_code == 404:
            return None
        raise


def _assert_source(source):
    value = source.path.lstat()
    if not stat.S_ISREG(value.st_mode) or _signature(value) != source.signature:
        raise SourceChangedError('Source changed after scanning')


def _checksum(source, stopped, heartbeat=None):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    digest = hashlib.md5()
    with os.fdopen(os.open(source.path, flags), 'rb') as stream:
        value = os.fstat(stream.fileno())
        if not stat.S_ISREG(value.st_mode) or _signature(value) != source.signature:
            raise SourceChangedError('Source changed before hashing')
        while True:
            if stopped():
                raise _Stopped()
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            if heartbeat is not None:
                heartbeat()
        if _signature(os.fstat(stream.fileno())) != source.signature:
            raise SourceChangedError('Source changed while hashing')
    _assert_source(source)
    return digest.hexdigest()


def backup_tree(disk, local_dir, remote_root, *, exclude=(), workers=4,
                progress=None, should_stop=None, network_retries=0, retry_delay=10) -> BackupResult:
    """Back up directory contents under ``remote_root/current`` with old versions.

    Exclusions have the same relative glob semantics as upload_tree. Symlinks and
    special files are skipped; unreadable regular files/directories are failures.
    Every run hashes local files (same-size edits are detected). Remote directories
    are listed on demand; their files are copied before visiting the next directory.
    ``should_stop()`` cooperatively pauses scanning/hashing and prevents new
    transfers. In-flight verified transfers finish. Authentication/network errors
    stop scheduling more work; completed copies remain and the next run resumes
    by comparing content. Each worker owns an independent client/session.
    """
    if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 32:
        raise ValueError('workers must be between 1 and 32')
    if not isinstance(network_retries, int) or not 0 <= network_retries <= 20:
        raise ValueError('network_retries must be between 0 and 20')
    if not isinstance(retry_delay, (int, float)) or not 0 <= retry_delay <= 300:
        raise ValueError('retry_delay must be between 0 and 300 seconds')
    if isinstance(exclude, str):
        raise TypeError('exclude must be an iterable of glob patterns, not one string')
    patterns = tuple(exclude)
    if any(not isinstance(pattern, str) for pattern in patterns):
        raise TypeError('Exclusion patterns must be strings')
    base = Path(local_dir).expanduser().absolute()
    remote = non_root(remote_root)
    current = remote + '/current'
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ-') + uuid.uuid4().hex[:12]
    staging, history = remote + '/.staging/' + run_id, remote + '/history/' + run_id
    result = BackupResult(run_id=run_id)
    lock, thread = threading.RLock(), threading.local()
    circuit = threading.Event()
    started, last_event = time.monotonic(), 0.0
    transferred, clients, sources, directories = {}, [], [], ['']
    seen_completed = set()

    def stopped():
        if should_stop is not None and should_stop():
            with lock:
                result.paused = True
            return True
        return circuit.is_set()

    def fail(relative, exc):
        with lock:
            result.failed[relative] = _safe_error(exc)
            seen_completed.add(relative)
            if isinstance(exc, (NetworkError, AuthenticationError)):
                result.interrupted = True
                circuit.set()

    def emit(phase, path='', *, force=False, done=False):
        nonlocal last_event
        if progress is None:
            return
        with lock:
            now = time.monotonic()
            if not force and now - last_event < 0.2:
                return
            last_event = now
            progress(BackupProgress(phase, path, sum(transferred.values()),
                result.bytes_total, now - started, min(len(seen_completed), len(sources)),
                len(sources), result.paused, done))

    def finish():
        result.pending = sorted(source.relative for source in sources
                                if source.relative not in seen_completed)
        for values in (result.uploaded, result.updated, result.unchanged,
                       result.skipped, result.staging_leftovers):
            values.sort()
        phase = 'paused' if result.paused else ('interrupted' if result.interrupted else 'finished')
        emit(phase, force=True, done=True)
        return result

    emit('scanning', force=True)
    try:
        root_stat = base.lstat()
        if not stat.S_ISDIR(root_stat.st_mode):
            raise NotADirectoryError('Backup source must be a real directory')
        # A symlink in an ancestor would bypass symlink exclusions for the whole tree.
        if any(parent.is_symlink() for parent in base.parents):
            raise ValueError('Backup source must not have symlink ancestors')
    except OSError as exc:
        fail('.', exc)
        return finish()

    def walk_error(exc):
        try:
            relative = Path(exc.filename).relative_to(base).as_posix()
        except (TypeError, ValueError):
            relative = '.'
        fail(relative, exc)

    for root, dirs, files in os.walk(base, followlinks=False, onerror=walk_error):
        if stopped():
            break
        folder = Path(root)
        for name in list(dirs):
            path = folder / name
            relative = path.relative_to(base).as_posix()
            try:
                value = path.lstat()
                if excluded(relative, patterns) or stat.S_ISLNK(value.st_mode):
                    dirs.remove(name)
                    result.skipped.append(relative)
                elif not stat.S_ISDIR(value.st_mode):
                    dirs.remove(name)
                    raise SourceChangedError('Directory changed during scanning')
                else:
                    directories.append(relative)
            except (OSError, SourceChangedError) as exc:
                if name in dirs:
                    dirs.remove(name)
                fail(relative, exc)
        for name in files:
            if stopped():
                break
            path = folder / name
            relative = path.relative_to(base).as_posix()
            try:
                value = path.lstat()
                if excluded(relative, patterns) or not stat.S_ISREG(value.st_mode):
                    result.skipped.append(relative)
                    continue
                sources.append(_Source(path, relative, value.st_size, _signature(value)))
            except OSError as exc:
                fail(relative, exc)
            emit('scanning', relative)
    if stopped():
        return finish()

    # Process one directory at a time: upload its files before visiting siblings.
    # A network retry therefore retains this run's queue and checked metadata.
    index, listing_errors = {}, {}

    def retry(operation, path=''):
        for attempt in range(network_retries + 1):
            try:
                return operation()
            except NetworkError:
                if attempt == network_retries:
                    raise
                deadline = time.monotonic() + retry_delay
                emit('retrying', path, force=True)
                while time.monotonic() < deadline:
                    if stopped():
                        raise _Stopped()
                    time.sleep(min(0.2, max(0, deadline - time.monotonic())))
                if stopped():
                    raise _Stopped()

    class ReliableReads:
        def __init__(self, raw):
            self.raw = raw

        def __getattr__(self, name):
            method = getattr(self.raw, name)
            if name in ('stat', 'listdir', 'mkdir'):
                return lambda *args, **kwargs: retry(lambda: method(*args, **kwargs), args[0] if args else '')
            return method

    raw_disk = disk
    disk = ReliableReads(raw_disk)

    def list_directory(relative_dir):
        directory = current + ('/' + relative_dir if relative_dir else '')
        items = disk.listdir(directory)
        seen = set()
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get('path'), str):
                raise ProtocolError('Directory listing contains invalid metadata')
            path = remote_path(item['path'])
            if posixpath.dirname(path) != directory or path in seen:
                raise ProtocolError('Directory listing contains an unsafe or repeated path')
            seen.add(path)
            if item.get('type') not in ('file', 'dir'):
                raise ProtocolError('Directory listing has an unknown resource type')
            index[path[len(current) + 1:]] = item
        emit('listing', relative_dir)

    def client():
        if not hasattr(thread, 'client'):
            thread.client = ReliableReads(type(raw_disk)(**raw_disk._worker_options))
            with lock:
                clients.append(thread.client)
        return thread.client

    def blocked_parent(relative, errors):
        parts = relative.split('/')
        return next((errors[parent] for parent in [''] + [
            '/'.join(parts[:i]) for i in range(1, len(parts))] if parent in errors), None)

    def check(source):
        if stopped():
            raise _Stopped()
        error = blocked_parent(source.relative, listing_errors)
        if error is not None:
            raise error
        digest = _checksum(source, stopped, lambda: emit('checking', source.relative))
        previous = _fingerprint(index.get(source.relative))
        if previous == (source.size, digest):
            with lock:
                result.unchanged.append(source.relative)
                seen_completed.add(source.relative)
            return None
        return _Job(source, digest, previous)

    def run_bounded(executor, function, items, phase):
        iterator, pending = iter(items), {}
        # At most workers futures are scheduled: pause/offline stops the queue
        # immediately after the already in-flight batch finishes.
        def enqueue():
            while len(pending) < workers and not stopped():
                try:
                    item = next(iterator)
                except StopIteration:
                    break
                pending[executor.submit(function, item)] = item
        enqueue()
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                item = pending.pop(future)
                source = item.source if isinstance(item, _Job) else item
                try:
                    outcome = future.result()
                    if outcome is not None:
                        yield outcome
                except _Stopped:
                    pass
                except Exception as exc:
                    fail(source.relative, exc)
                emit(phase, source.relative)
            enqueue()

    def cleanup(worker, stage):
        try:
            meta = _stat_optional(worker, stage)
            if meta is not None:
                if meta.get('type') != 'file':
                    raise ProtocolError('Unexpected staging object type')
                worker.remove(stage, permanently=True)
        except Exception:
            with lock:
                result.staging_leftovers.append(stage)

    def transfer(job):
        source, previous = job.source, job.previous
        if stopped():
            raise _Stopped()
        worker = client()
        target = current + '/' + source.relative
        stage = staging + '/' + source.relative
        archive = history + '/' + source.relative
        expected = source.size, job.md5
        move_attempted = upload_attempted = False
        def report(event):
            with lock:
                transferred[source.relative] = min(event.transferred, source.size)
            emit('uploading', source.relative)
        try:
            _assert_source(source)
            upload_attempted = True
            def send():
                # Only this run owns this stage path. Repeating a lost upload is
                # safe here; current/history are never overwritten by this retry.
                return worker.upload(source.path, stage, overwrite=network_retries > 0, progress=report)
            uploaded = retry(send, source.relative)
            if (uploaded.size, uploaded.md5.lower()) != expected:
                raise SourceChangedError('Source changed between hashing and upload')
            if _fingerprint(worker.stat(stage)) != expected:
                raise IntegrityError('Staged upload does not match source')
            _assert_source(source)
            if _fingerprint(_stat_optional(worker, target)) != previous:
                raise RemoteChangedError('Current destination changed during upload')
            if previous is not None:
                emit('archiving', source.relative)
                try:
                    worker.copy(target, archive, overwrite=False)
                except NetworkError:
                    # A lost response is safe to reconcile at our unique destination.
                    if _fingerprint(_stat_optional(worker, archive)) != previous:
                        raise
                if _fingerprint(worker.stat(archive)) != previous:
                    raise IntegrityError('Archived previous version did not verify')
                if _fingerprint(_stat_optional(worker, target)) != previous:
                    raise RemoteChangedError('Current destination changed during archiving')
            emit('committing', source.relative)
            move_attempted = True
            try:
                worker.move(stage, target, overwrite=previous is not None)
            except NetworkError:
                if (_fingerprint(_stat_optional(worker, target)) != expected
                        or _stat_optional(worker, stage) is not None):
                    raise
            if _fingerprint(worker.stat(target)) != expected:
                raise IntegrityError('Published current file did not verify')
            with lock:
                (result.updated if previous is not None else result.uploaded).append(source.relative)
                result.bytes_uploaded += source.size
                transferred[source.relative] = source.size
                seen_completed.add(source.relative)
        except Exception:
            if upload_attempted and not move_attempted:
                cleanup(worker, stage)
            elif move_attempted:
                # An asynchronous move might still be running. Do not delete its source.
                with lock:
                    result.staging_leftovers.append(stage)
            raise

    jobs = []
    prepared = set()

    def ensure(path):
        if path in prepared:
            return
        if path != remote:
            parent = posixpath.dirname(path)
            if parent.startswith(remote):
                ensure(parent)
        disk.mkdir(path, parents=path == remote, exist_ok=True)
        prepared.add(path)

    grouped = {}
    for source in sources:
        grouped.setdefault(posixpath.dirname(source.relative), []).append(source)
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            root_meta = _stat_optional(disk, current)
            if root_meta is not None and root_meta.get('type') != 'dir':
                raise NotADirectoryError('The current backup destination must be a directory')
            for relative in directories:
                if stopped():
                    break
                try:
                    error = blocked_parent(relative + '/__child__', listing_errors)
                    if error is not None:
                        raise error
                    meta = root_meta if not relative else index.get(relative)
                    if meta is not None:
                        if meta.get('type') != 'dir':
                            raise FileExistsError('A file occupies a backup directory')
                        list_directory(relative)
                        prepared.add(current + ('/' + relative if relative else ''))
                    else:
                        ensure(current + ('/' + relative if relative else ''))
                    batch = list(run_bounded(executor, check, grouped.get(relative, []), 'checking'))
                    jobs.extend(batch)
                    result.bytes_total += sum(job.source.size for job in batch)
                    if batch and not stopped():
                        ensure(staging + ('/' + relative if relative else ''))
                        if any(job.previous is not None for job in batch):
                            ensure(history + ('/' + relative if relative else ''))
                        list(run_bounded(executor, transfer, batch, 'uploading'))
                except _Stopped:
                    break
                except Exception as exc:
                    listing_errors[relative] = exc
                    fail(relative or '.', exc)
                    for source in grouped.get(relative, []):
                        fail(source.relative, exc)
    except _Stopped:
        pass
    except Exception as exc:
        fail('.', exc)
    finally:
        for worker in clients:
            worker.close()
    if (jobs and not (result.failed or result.paused or result.interrupted
                      or result.staging_leftovers)
            and len(result.uploaded) + len(result.updated) == len(jobs)):
        # Every staged file was verified and moved successfully. Only this run's
        # random, exclusively created subtree remains, containing empty folders.
        # Never remove the shared .staging root or an uncertain operation source.
        try:
            disk.remove(staging, recursive=True, permanently=True)
        except Exception:
            result.staging_leftovers.append(staging)
    return finish()
