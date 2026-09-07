from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
import fnmatch
import os
from pathlib import Path
import threading
import time
import unicodedata

from .management import remote_path
from .progress import Progress
from .errors import ProtocolError, YandexDiskError


@dataclass
class BatchResult:
    completed: list = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


def excluded(relative: str, patterns) -> bool:
    """Glob on relative POSIX paths, or basename when pattern contains no '/'."""
    parts = relative.strip('/').split('/')
    prefixes = ['/'.join(parts[:i]) for i in range(1, len(parts) + 1)]
    for pattern in patterns:
        pattern = pattern.strip('/')
        if not pattern:
            continue
        if '/' not in pattern:
            if any(fnmatch.fnmatchcase(part, pattern) for part in parts):
                return True
        elif any(fnmatch.fnmatchcase(prefix, pattern) for prefix in prefixes):
            return True
        elif pattern.endswith('/**') and relative == pattern[:-3]:
            return True
    return False


def safe_local(base: Path, relative: str) -> Path:
    parts = relative.split('/')
    if not relative or relative.startswith('/') or any(
            p in ('', '.', '..') or '\\' in p or '\x00' in p for p in parts):
        raise ProtocolError('Unsafe filename in remote directory')
    candidate = base.joinpath(*parts)
    # Reject symlinks in all existing components, including the destination root.
    for path in [candidate, *candidate.parents]:
        if path.is_symlink():
            raise ValueError('Recursive download destination must not contain symlinks')
        if path == base:
            break
    try:
        candidate.resolve().relative_to(base.resolve())
    except ValueError:
        raise ProtocolError('Remote filename escapes local destination') from None
    return candidate


class BatchMixin:
    def _batch(self, jobs, direction, *, workers, overwrite, progress, skipped=()):
        if not isinstance(workers, int) or not 1 <= workers <= 32:
            raise ValueError('workers must be between 1 and 32')
        result = BatchResult(skipped=list(skipped))
        total = sum(size for _, _, size in jobs)
        started = time.monotonic()
        lock = threading.Lock()
        state = {}
        finished = set()
        clients = []
        thread = threading.local()

        def notify(index, event):
            if progress is None:
                return
            with lock:
                state[index] = event.transferred
                if event.done:
                    finished.add(index)
                progress(Progress(event.path, direction, sum(state.values()), total,
                                  time.monotonic() - started, event.attempt,
                                  len(finished) == len(jobs), len(finished), len(jobs)))

        def run(index, job):
            if not hasattr(thread, 'client'):
                thread.client = type(self)(**self._worker_options)
                with lock:
                    clients.append(thread.client)
            local, remote, _ = job
            callback = (lambda e: notify(index, e)) if progress else None
            if direction == 'upload':
                return thread.client.upload(local, remote, overwrite=overwrite, progress=callback)
            return thread.client.download(remote, local, overwrite=overwrite, progress=callback)

        if not jobs:
            if progress:
                progress(Progress('', direction, 0, 0, 0, done=True, files_total=0))
            return result
        try:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                pending = {}
                iterator = iter(enumerate(jobs))

                def enqueue():
                    for index, job in iterator:
                        pending[executor.submit(run, index, job)] = job
                        if len(pending) >= 2 * workers:
                            break

                enqueue()
                while pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        local, remote, _ = pending.pop(future)
                        try:
                            result.completed.append(future.result())
                        except (YandexDiskError, OSError, ValueError) as exc:
                            # Stable safe summary; don't put signed URLs in batch output.
                            result.failed[str(local) if direction == 'upload' else remote] = (
                                f'{type(exc).__name__}' + (f' (HTTP {exc.status_code})' if hasattr(exc, 'status_code') else ''))
                    enqueue()
        finally:
            for client in clients:
                client.close()
        return result

    def upload_many(self, files, *, workers=4, overwrite=False, progress=None) -> BatchResult:
        """Queue (local_file, exact_remote_file) pairs. Parent directories must exist."""
        jobs, targets = [], set()
        for local, remote in files:
            local, remote = Path(local).expanduser(), remote_path(remote)
            if not local.is_file() or local.is_symlink():
                raise ValueError('Queue sources must be regular non-symlink files')
            if remote == '/' or remote in targets:
                raise ValueError('Queue destinations must be distinct file paths')
            targets.add(remote)
            jobs.append((local, remote, local.stat().st_size))
        return self._batch(jobs, 'upload', workers=workers, overwrite=overwrite, progress=progress)

    def upload_tree(self, local_dir, remote_dir, *, exclude=(), workers=4,
                    overwrite=False, progress=None) -> BatchResult:
        """Copy contents of local_dir into remote_dir, preserving empty directories."""
        if not isinstance(workers, int) or not 1 <= workers <= 32:
            raise ValueError('workers must be between 1 and 32')
        base, remote = Path(local_dir).expanduser(), remote_path(remote_dir)
        if not base.is_dir() or base.is_symlink():
            raise NotADirectoryError('Source must be a real directory')
        jobs, directories, skipped = [], [''], []
        def onerror(error):
            raise error
        for root, dirs, files in os.walk(base, followlinks=False, onerror=onerror):
            current = Path(root)
            for name in list(dirs):
                path = current / name
                relative = path.relative_to(base).as_posix()
                if path.is_symlink() or excluded(relative, exclude):
                    dirs.remove(name)
                    skipped.append(relative)
                else:
                    directories.append(relative)
            for name in files:
                path = current / name
                relative = path.relative_to(base).as_posix()
                if path.is_symlink() or excluded(relative, exclude):
                    skipped.append(relative)
                    continue
                if not path.is_file():
                    raise ValueError('Directory contains a special non-regular file')
                jobs.append((path, remote.rstrip('/') + '/' + relative, path.stat().st_size))
        # Complete local plan before changing the remote tree.
        self.mkdir(remote, parents=True, exist_ok=True)
        for relative in directories[1:]:
            self.mkdir(remote.rstrip('/') + '/' + relative, exist_ok=True)
        return self._batch(jobs, 'upload', workers=workers, overwrite=overwrite, progress=progress, skipped=skipped)

    def download_tree(self, remote_dir, local_dir, *, exclude=(), workers=4,
                      overwrite=False, progress=None) -> BatchResult:
        """Copy directory contents; excluded folders aren't traversed."""
        if not isinstance(workers, int) or not 1 <= workers <= 32:
            raise ValueError('workers must be between 1 and 32')
        remote, base = remote_path(remote_dir), Path(local_dir).expanduser()
        if self.stat(remote)['type'] != 'dir':
            raise NotADirectoryError('Remote source must be a directory')
        # Validate every path before creating any destination directories.
        safe_local(base, '__path_check__')
        prefix = remote.rstrip('/') + '/'
        stack, seen, jobs, directories, skipped = [remote], set(), [], [], []
        while stack:
            current = stack.pop()
            for item in self.listdir(current):
                path = remote_path(item['path'])
                if not path.startswith(prefix) or path == remote:
                    raise ProtocolError('Remote listing escapes source directory')
                relative = path[len(prefix):]
                target = safe_local(base, relative)
                if excluded(relative, exclude):
                    skipped.append(relative)
                    continue
                # Conservative collision protection for case-insensitive macOS/Windows.
                key = unicodedata.normalize('NFC', relative).casefold()
                if key in seen:
                    raise ProtocolError('Remote names collide on a case-insensitive filesystem')
                seen.add(key)
                if item['type'] == 'dir':
                    directories.append(target)
                    stack.append(path)
                elif item['type'] == 'file':
                    jobs.append((target, path, item['size']))
                else:
                    raise ProtocolError('Unknown resource type')
        base.mkdir(parents=True, exist_ok=True)
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
        return self._batch(jobs, 'download', workers=workers, overwrite=overwrite, progress=progress, skipped=skipped)
