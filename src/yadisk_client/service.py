"""Local macOS backup worker; reads config and menu-bar control markers."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import tempfile
import threading
import time

from .client import YandexDisk
from .errors import AuthenticationError, NetworkError
from .management import non_root

DEFAULT_EXCLUDES = [
    'node_modules', '.venv*', 'venv', 'site-packages', '__pycache__', '*.pyc', '.cache', '.pytest_cache',
    '.mypy_cache', '.ruff_cache', '.tox', '.next', '.nuxt', 'dist', 'build',
    '.DS_Store', '.env', '.env.*', '.ssh', '.aws', '*.pem', '*.key',
    '.yadisk-token.json', 'disk-token.json',
]
DEFAULT_DIR = Path.home() / 'Library/Application Support/YandexBackup'


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_json(path, default=None):
    try:
        with Path(path).open() as stream:
            return json.load(stream)
    except (FileNotFoundError, ValueError):
        if default is not None:
            return default
        raise


def load_config(directory: Path):
    obj = read_json(directory / 'config.json')
    if not isinstance(obj, dict):
        raise ValueError('Configuration must be an object')
    for key in ('source', 'remote_root', 'env_file'):
        if not isinstance(obj.get(key), str) or not obj[key].strip() or '\x00' in obj[key]:
            raise ValueError(f'Invalid configuration setting: {key}')
    source = Path(obj['source']).expanduser()
    if not source.is_absolute() or source.resolve() in (Path('/'), Path.home().resolve()):
        raise ValueError('Choose an explicit source directory, not the home or filesystem root')
    obj['source'] = str(source)
    obj['remote_root'] = non_root(obj['remote_root'])
    for key, default, minimum, maximum in [
        ('interval_seconds', 900, 60, 86400), ('workers', 4, 1, 16),
        ('network_retry_seconds', 60, 10, 3600), ('timeout', 120, 10, 7200),
    ]:
        value = obj.get(key, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f'Invalid configuration setting: {key}')
        obj[key] = value
    exclude = obj.get('exclude', DEFAULT_EXCLUDES)
    if not isinstance(exclude, list) or not all(isinstance(p, str) for p in exclude):
        raise ValueError('exclude must be a list of patterns')
    obj['exclude'] = exclude
    obj['env_file'] = str(Path(obj['env_file']).expanduser())
    return obj


class BackupService:
    def __init__(self, directory: Path, *, client_factory=YandexDisk.from_env):
        self.directory = Path(directory).expanduser()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.client_factory = client_factory
        self.stop = threading.Event()
        self._lock = threading.RLock()
        previous = read_json(self.directory / 'status.json', {})
        if not isinstance(previous, dict):
            previous = {}
        self.status = {'schema_version': 1, 'state': 'idle', 'running': False,
                       'message': 'Ожидание запуска', 'last_success': previous.get('last_success'),
                       'last_result': previous.get('last_result')}
        self.logger = logging.getLogger('yadisk_backup.' + str(id(self)))
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        handler = RotatingFileHandler(self.directory/'backup.log', maxBytes=2_000_000, backupCount=3)
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        self.logger.addHandler(handler)

    def publish(self, **changes):
        with self._lock:
            self.status.update(changes)
            self.status['updated_at'] = utc_now()
            write_json(self.directory/'status.json', self.status)

    def paused(self):
        return (self.directory/'paused').exists()

    def should_stop(self):
        return self.stop.is_set() or self.paused()

    def run_once(self, config):
        from .backup import backup_tree
        self.publish(state='scanning', running=True, message='Проверка файлов и облачной копии',
                     progress=None, started_at=utc_now(), source=config['source'],
                     remote_root=config['remote_root'])
        self.logger.info('Backup started')
        last_progress = 0.0

        def progress(event):
            nonlocal last_progress
            if time.monotonic()-last_progress < 0.4 and not event.done:
                return
            last_progress = time.monotonic()
            phase = getattr(event, 'phase', 'uploading')
            state = 'scanning' if phase in ('scanning', 'planning', 'hashing', 'listing', 'checking', 'preparing') else 'uploading'
            if phase == 'retrying':
                state = 'offline'
            if self.paused():
                state = 'paused'
            self.publish(state=state,
                message='Пауза после текущих файлов' if state == 'paused' else
                        ('Сеть недоступна; повтор текущей операции через 10 секунд' if phase == 'retrying' else
                         'Проверка следующей папки' if state == 'scanning' else 'Сохранение резервной копии'),
                progress={'transferred': event.transferred, 'total': event.total,
                          'percent': None, 'total_is_estimate': True, 'files_completed': event.files_completed,
                          'files_total': event.files_total, 'path': event.path,
                          'speed_bytes_per_second': getattr(event, 'bytes_per_second', None),
                          'eta_seconds': None})

        try:
            with self.client_factory(config['env_file'], timeout=config['timeout'], max_attempts=2) as disk:
                disk.check_auth()
                result = backup_tree(disk, config['source'], config['remote_root'],
                    exclude=config['exclude'], workers=config['workers'],
                    progress=progress, should_stop=self.should_stop, network_retries=5, retry_delay=10)
            write_json(self.directory/'last_result.json', asdict(result))
            summary = {key: len(getattr(result, key)) for key in
                       ('uploaded', 'updated', 'unchanged', 'skipped', 'pending', 'failed')}
            summary['bytes_uploaded'] = result.bytes_uploaded
            if result.ok:
                self.publish(state='idle', running=False, message='Все выбранные файлы сохранены',
                             last_success=utc_now(), last_result=summary)
                self.logger.info('Backup completed: %s', summary)
                return True
            if self.paused() or self.stop.is_set() or result.paused:
                state, message = 'paused', 'Копирование приостановлено; завершённые файлы сохранены'
            elif any('NetworkError' in str(value) for value in result.failed.values()):
                state, message = 'offline', 'Нет соединения; повтор будет выполнен автоматически'
            else:
                state, message = 'error', f'Не удалось сохранить {len(result.failed)} файлов; см. журнал'
            self.publish(state=state, running=False, message=message, last_result=summary)
            self.logger.warning('Incomplete backup: %s; errors: %s', summary, list(result.failed.items())[:20])
            return False
        except Exception as exc:
            state = 'offline' if isinstance(exc, NetworkError) else 'error'
            if isinstance(exc, AuthenticationError):
                message = 'Нужно обновить токен Яндекс.Диска'
            elif isinstance(exc, PermissionError):
                message = 'macOS не разрешает доступ к рабочему столу; проверьте разрешения'
            elif state == 'offline':
                message = 'Нет соединения; повтор будет выполнен автоматически'
            else:
                message = f'Ошибка резервного копирования: {type(exc).__name__}; см. журнал'
            self.publish(state=state, running=False, message=message)
            self.logger.error('Backup failed: %s', type(exc).__name__)
            return False

    def serve(self, *, once=False):
        lock_file = (self.directory/'worker.lock').open('a')
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            return 0
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = None
                next_run = 0.0
                was_paused = False
                try:
                    while not self.stop.is_set():
                        now = time.time()
                        if future is None:
                            try:
                                config = load_config(self.directory)
                            except (OSError, ValueError, KeyError) as exc:
                                self.publish(state='error', running=False, message='Ошибка настроек; откройте config.json')
                                self.logger.error('Invalid configuration: %s', type(exc).__name__)
                                if once:
                                    return 1
                                self.stop.wait(5)
                                continue
                        if future is not None and future.done():
                            success = future.result()
                            future = None
                            if once:
                                return 0 if success else 1
                            next_run = now + (config['interval_seconds'] if success else config['network_retry_seconds'])
                        paused = self.paused()
                        if paused:
                            self.publish(state='paused', message='Пауза после текущих файлов' if future else 'Копирование на паузе')
                        elif future is None:
                            request = self.directory/'run-now'
                            if now >= next_run or request.exists() or was_paused:
                                request.unlink(missing_ok=True)
                                future = executor.submit(self.run_once, config)
                            else:
                                self.publish(next_run=datetime.fromtimestamp(next_run, timezone.utc).isoformat())
                        else:
                            self.publish()
                        was_paused = paused
                        if once and paused and future is None:
                            return 1
                        self.stop.wait(2)
                finally:
                    self.stop.set()
                    if future is not None:
                        future.result()
            self.publish(running=False, state='idle', message='Фоновый процесс остановлен')
            return 0
        finally:
            lock_file.close()
            for handler in self.logger.handlers[:]:
                handler.close()
                self.logger.removeHandler(handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Yandex backup background worker')
    parser.add_argument('--config-dir', type=Path, default=DEFAULT_DIR)
    group = parser.add_mutually_exclusive_group()
    for name in ('once', 'status', 'pause', 'resume', 'now'):
        group.add_argument('--'+name, action='store_true')
    args = parser.parse_args(argv)
    directory = args.config_dir.expanduser()
    if args.status:
        print(json.dumps(read_json(directory/'status.json', {}), ensure_ascii=False, indent=2))
        return 0
    if args.pause or args.resume or args.now:
        directory.mkdir(parents=True, exist_ok=True)
        if args.pause:
            (directory/'paused').touch(mode=0o600)
        if args.resume:
            (directory/'paused').unlink(missing_ok=True)
            (directory/'run-now').touch(mode=0o600)
        if args.now:
            (directory/'run-now').touch(mode=0o600)
        return 0
    service = BackupService(directory)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: service.stop.set())
    return service.serve(once=args.once)


if __name__ == '__main__':
    raise SystemExit(main())
