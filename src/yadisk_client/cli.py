"""Command line: yd put/get/cp/ls/du/df/stat/mkdir/mv/rename/rm/trash/share."""
import argparse
from dataclasses import asdict, is_dataclass
import getpass
import json
import os
from pathlib import Path
import posixpath
import sys
import tempfile
import time
import webbrowser

from .auth import OAuth
from .batch import BatchResult
from .backup import BackupResult
from .progress import Progress
from .client import YandexDisk
from .errors import YandexDiskError
from .management import remote_path


def _options(parser):
    parser.add_argument('--env-file', default=argparse.SUPPRESS, help='Path to credentials .env')
    parser.add_argument('--token-file', default=argparse.SUPPRESS, help='OAuth token JSON saved by auth login')
    parser.add_argument('--json', action='store_true', default=argparse.SUPPRESS, help='Machine-readable stdout')
    parser.add_argument('--no-progress', action='store_true', default=argparse.SUPPRESS)
    parser.add_argument('--timeout', type=float, default=argparse.SUPPRESS, help='Network inactivity timeout')
    parser.add_argument('--no-fast-upload', action='store_true', default=argparse.SUPPRESS)


def parser():
    root = argparse.ArgumentParser(prog='yd', description='Yandex Disk files and directories')
    root.add_argument('--version', action='version', version='yd 0.3.0')
    _options(root)
    root.set_defaults(env_file=os.environ.get('YADISK_ENV_FILE', '.env'), token_file=None,
                      json=False, no_progress=False, timeout=600, no_fast_upload=False)
    commands = root.add_subparsers(dest='command', required=True)

    def command(name, help):
        p = commands.add_parser(name, help=help)
        _options(p)
        return p

    for name in ('put', 'get', 'cp'):
        p = command(name, {'put': 'Upload local files', 'get': 'Download from Disk',
                           'cp': 'Copy: local ↔ disk:/ or disk:/ → disk:/'}[name])
        if name == 'put':
            p.add_argument('sources', nargs='+')
        else:
            p.add_argument('source')
        p.add_argument('destination')
        p.add_argument('-r', '--recursive', action='store_true')
        p.add_argument('--exclude', action='append', default=[], metavar='GLOB')
        p.add_argument('-j', '--workers', type=int, default=4)
        p.add_argument('--overwrite', action='store_true')
    p = command('backup', 'Incremental backup with old versions and retained deleted files')
    p.add_argument('source')
    p.add_argument('destination', help='Backup root; current/ and history/ will be managed inside')
    p.add_argument('--exclude', action='append', default=[])
    p.add_argument('-j', '--workers', type=int, default=4)
    p = command('ls', 'List files, optionally recursively')
    p.add_argument('path', nargs='?', default='/')
    p.add_argument('-r', '--recursive', action='store_true')
    for name in ('stat', 'du'):
        p = command(name, 'Metadata' if name == 'stat' else 'Recursive size and file count')
        p.add_argument('path', nargs='?', default='/')
    command('df', 'Used and free space')
    p = command('mkdir', 'Create directory')
    p.add_argument('path')
    p.add_argument('-p', '--parents', action='store_true')
    p = command('mv', 'Move or rename on Disk')
    p.add_argument('source'); p.add_argument('destination')
    p.add_argument('--overwrite', action='store_true')
    p = command('rename', 'Change basename on Disk')
    p.add_argument('path'); p.add_argument('name')
    p.add_argument('--overwrite', action='store_true')
    p = command('rm', 'Move to trash by default')
    p.add_argument('path')
    p.add_argument('-r', '--recursive', action='store_true')
    p.add_argument('--permanent', action='store_true')
    for name in ('share', 'unshare'):
        p = command(name, 'Create public URL' if name == 'share' else 'Disable public URL')
        p.add_argument('path')
    p = command('trash', 'List, restore or permanently remove one trash item')
    sub = p.add_subparsers(dest='trash_command', required=True)
    for name in ('ls', 'restore', 'rm'):
        q = sub.add_parser(name)
        _options(q)
        q.add_argument('path', **({'nargs': '?', 'default': '/'} if name == 'ls' else {}))
        if name == 'restore':
            q.add_argument('--name'); q.add_argument('--overwrite', action='store_true')
    p = command('auth', 'Check credentials or obtain/refresh OAuth tokens')
    sub = p.add_subparsers(dest='auth_command', required=True)
    for name in ('check', 'login', 'refresh'):
        q = sub.add_parser(name)
        _options(q)
        if name == 'login':
            q.add_argument('--browser', action='store_true')
            q.add_argument('--overwrite', action='store_true')
    return root


def _encode(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError('Unsupported JSON output value')


def _print(value, args):
    if args.json:
        print(json.dumps(value, default=_encode, ensure_ascii=False))
    elif isinstance(value, BatchResult):
        print(f'Completed: {len(value.completed)}; failed: {len(value.failed)}; skipped: {len(value.skipped)}')
        for path, error in value.failed.items():
            print(f'{path}: {error}', file=sys.stderr)
    elif isinstance(value, BackupResult):
        print(f'New: {len(value.uploaded)}; updated: {len(value.updated)}; unchanged: {len(value.unchanged)}; '
              f'failed: {len(value.failed)}; pending: {len(value.pending)}; skipped: {len(value.skipped)}')
        for path, error in value.failed.items():
            print(f'{path}: {error}', file=sys.stderr)
    elif isinstance(value, list):
        for item in value:
            print(f"{item.get('type', '?'):4} {item.get('size', 0):>12}  {item.get('path', item.get('name', ''))}")
    elif isinstance(value, dict):
        for key, item in value.items():
            print(f'{key}: {item}')
    elif is_dataclass(value):
        print(f'{value.size} bytes; {value.seconds:.2f}s; {value.remote_path}')
    else:
        print(value if value is not None else 'OK')


class TerminalProgress:
    def __init__(self, enabled):
        self.enabled, self.last, self.visible = enabled, 0, False

    def __call__(self, event):
        now = time.monotonic()
        if not self.enabled or (not event.done and now - self.last < 0.15):
            return
        self.last = now
        eta = '?' if event.eta_seconds is None else f'{event.eta_seconds:.0f}s'
        print(f'\r{event.direction}: {event.percent:6.1f}%  '
              f'{event.bytes_per_second / 1e6:.2f} MB/s  ETA {eta}  '
              f'files {event.files_completed}/{event.files_total}     ', end='', file=sys.stderr, flush=True)
        self.visible = True

    def close(self):
        if self.visible:
            print(file=sys.stderr)


def _is_remote(path):
    return path.startswith('disk:')


def _remote_destination(disk, destination, name):
    path = remote_path(destination)
    if destination.endswith('/') or (disk.exists(path) and disk.stat(path)['type'] == 'dir'):
        return path.rstrip('/') + '/' + name
    return path


def _put(disk, sources, destination, args, progress):
    paths = [Path(p).expanduser() for p in sources]
    if len(paths) == 1 and paths[0].is_dir():
        if not args.recursive:
            raise ValueError('A directory requires -r/--recursive')
        return disk.upload_tree(paths[0], destination, exclude=args.exclude, workers=args.workers,
                                overwrite=args.overwrite, progress=progress)
    if args.exclude:
        raise ValueError('--exclude is supported for recursive directory transfers')
    if len(paths) == 1:
        return disk.upload(paths[0], _remote_destination(disk, destination, paths[0].name),
                           overwrite=args.overwrite, progress=progress)
    if disk.stat(destination)['type'] != 'dir':
        raise ValueError('Multiple sources require an existing remote directory')
    return disk.upload_many([(p, remote_path(destination).rstrip('/') + '/' + p.name) for p in paths],
                            workers=args.workers, overwrite=args.overwrite, progress=progress)


def _get(disk, source, destination, args, progress):
    if disk.stat(source)['type'] == 'dir':
        if not args.recursive:
            raise ValueError('A directory requires -r/--recursive')
        return disk.download_tree(source, destination, exclude=args.exclude, workers=args.workers,
                                  overwrite=args.overwrite, progress=progress)
    if args.exclude:
        raise ValueError('--exclude is supported for recursive directory transfers')
    target = Path(destination).expanduser()
    if target.is_dir():
        target /= posixpath.basename(remote_path(source))
    return disk.download(source, target, overwrite=args.overwrite, progress=progress)


def _save_tokens(path, tokens, *, overwrite):
    path = Path(path).expanduser()
    data = json.dumps(asdict(tokens), indent=2) + '\n'
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.yadisk-tokens-')
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temp, path)
        else:
            os.link(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def _read_tokens(path):
    obj = json.loads(Path(path).expanduser().read_text())
    if not isinstance(obj, dict):
        raise ValueError('Token configuration must be a JSON object')
    return obj


def _auth(args):
    path = Path(args.token_file or '.yadisk-token.json').expanduser()
    if args.auth_command == 'login' and path.exists() and not args.overwrite:
        raise FileExistsError('Token file exists; use --overwrite to replace it')
    with OAuth.from_env(args.env_file) as oauth:
        if args.auth_command == 'login':
            request = oauth.authorization_url()
            print('Open this URL to authorize:\n' + request.url, file=sys.stderr)
            if args.browser:
                webbrowser.open(request.url)
            callback = getpass.getpass('Paste full callback URL (hidden): ')
            tokens = oauth.exchange_callback(callback.strip(), request)
        else:
            saved = _read_tokens(path)
            tokens = oauth.refresh(saved.get('refresh_token', ''))
        _save_tokens(path, tokens, overwrite=args.auth_command == 'refresh' or args.overwrite)
    return {'saved_to': str(path), 'expires_in': tokens.expires_in}


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    display = TerminalProgress(not args.no_progress and not args.json and sys.stderr.isatty())
    try:
        if args.command == 'auth' and args.auth_command != 'check':
            _print(_auth(args), args)
            return 0
        options = {'fast_upload': not args.no_fast_upload, 'timeout': args.timeout}
        if args.token_file:
            token = _read_tokens(args.token_file).get('access_token', '')
            disk = YandexDisk(token, **options)
        else:
            disk = YandexDisk.from_env(args.env_file, **options)
        with disk:
            c = args.command
            if c == 'backup':
                def show_backup(event):
                    display(Progress(event.path, 'backup', event.transferred, event.total, event.elapsed,
                                     done=event.done, files_completed=event.files_completed, files_total=event.files_total))
                result = disk.backup_tree(args.source, args.destination, exclude=args.exclude,
                                          workers=args.workers, progress=show_backup)
            elif c == 'put':
                if any(_is_remote(p) for p in args.sources):
                    raise ValueError('put sources must be local; use cp for remote copy')
                result = _put(disk, args.sources, args.destination, args, display)
            elif c == 'get':
                if _is_remote(args.destination):
                    raise ValueError('get destination must be local')
                result = _get(disk, args.source, args.destination, args, display)
            elif c == 'cp':
                source_remote, dest_remote = _is_remote(args.source), _is_remote(args.destination)
                if source_remote and dest_remote:
                    if args.exclude:
                        raise ValueError('--exclude cannot filter a server-side copy; use get/put -r')
                    if disk.stat(args.source)['type'] == 'dir' and not args.recursive:
                        raise ValueError('Copying a directory requires -r')
                    # Exact destination for server-side copies (documented).
                    result = disk.copy(args.source, args.destination, overwrite=args.overwrite)
                elif source_remote:
                    result = _get(disk, args.source, args.destination, args, display)
                elif dest_remote:
                    result = _put(disk, [args.source], args.destination, args, display)
                else:
                    raise ValueError('cp requires disk:/ on at least one side')
            elif c == 'ls':
                result = list(disk.walk(args.path)) if args.recursive else disk.listdir(args.path)
            elif c == 'stat': result = disk.stat(args.path)
            elif c == 'du': result = disk.size(args.path)
            elif c == 'df': result = disk.info()
            elif c == 'mkdir': result = disk.mkdir(args.path, parents=args.parents, exist_ok=args.parents)
            elif c == 'mv': result = disk.move(args.source, args.destination, overwrite=args.overwrite)
            elif c == 'rename': result = disk.rename(args.path, args.name, overwrite=args.overwrite)
            elif c == 'rm': result = disk.remove(args.path, recursive=args.recursive, permanently=args.permanent)
            elif c == 'share': result = disk.publish(args.path)
            elif c == 'unshare': result = disk.unpublish(args.path)
            elif c == 'trash':
                if args.trash_command == 'ls': result = disk.trash_list(args.path)
                elif args.trash_command == 'restore':
                    result = disk.restore(args.path, name=args.name, overwrite=args.overwrite)
                else: result = disk.trash_delete(args.path)
            elif c == 'auth': result = {'authenticated': disk.check_auth()}
        display.close()
        display.visible = False
        _print(result, args)
        return 1 if isinstance(result, (BatchResult, BackupResult)) and not result.ok else 0
    except (YandexDiskError, OSError, ValueError, KeyError) as exc:
        # JSON parse errors can include credential text: output a safe fixed form.
        message = f'{type(exc).__name__}: ' + (
            'Invalid JSON configuration' if isinstance(exc, json.JSONDecodeError) else str(exc))
        if args.json:
            print(json.dumps({'error': message}, ensure_ascii=False))
        else:
            print(message, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Interrupted', file=sys.stderr)
        return 130
    finally:
        display.close()


if __name__ == '__main__':
    raise SystemExit(main())
