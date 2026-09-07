"""Metadata and server-side operations. Mutations are never blindly replayed."""
import posixpath
import time
from urllib.parse import urlsplit

from ._common import json_object
from .errors import APIError, NetworkError, ProtocolError, YandexDiskError


def remote_path(path: str) -> str:
    if path.startswith('disk:'):
        path = path[5:]
    if '\x00' in path or any(p in ('.', '..') for p in path.split('/')):
        raise ValueError('Remote path must not contain NUL, . or .. components')
    return '/' + '/'.join(p for p in path.split('/') if p)


def non_root(path: str) -> str:
    path = remote_path(path)
    if path == '/':
        raise ValueError('This operation cannot target the Disk root')
    return path


def trash_item(path: str) -> str:
    value = path[6:] if path.startswith('trash:') else path
    if not value.strip('/') or any(p in ('.', '..') for p in value.split('/')):
        raise ValueError('Specify one item from trash_list(); trash root is not allowed')
    return path


class ManagementMixin:
    def info(self) -> dict:
        """Quota in bytes, including calculated free_space."""
        obj = json_object(self._request('GET', ''))
        return {key: obj.get(key) for key in
                ('total_space', 'used_space', 'trash_size', 'max_file_size', 'is_paid')} | {
                    'free_space': max(0, obj['total_space'] - obj['used_space'])}

    def stat(self, path: str) -> dict:
        return json_object(self._request('GET', '/resources', params={'path': remote_path(path),
            'fields': 'name,path,type,size,md5,sha256,created,modified,public_url,public_key'}))

    def exists(self, path: str) -> bool:
        try:
            self.stat(path)
            return True
        except APIError as exc:
            if exc.status_code == 404:
                return False
            raise

    def _list(self, path: str, endpoint: str):
        offset = 0
        while True:
            obj = json_object(self._request('GET', endpoint, params={
                'path': path, 'limit': 1000, 'offset': offset,
                'sort': 'created' if endpoint.startswith('/trash/') else 'name'}))
            if obj.get('type') == 'file':
                yield {k: v for k, v in obj.items() if k != '_embedded'}
                return
            embedded = obj.get('_embedded')
            if not isinstance(embedded, dict) or not isinstance(embedded.get('items'), list):
                raise ProtocolError('Directory listing has no items')
            items = embedded['items']
            yield from items
            offset += len(items)
            total = embedded.get('total')
            if not items or (isinstance(total, int) and offset >= total) or (total is None and len(items) < 1000):
                return

    def listdir(self, path: str = '/') -> list[dict]:
        return list(self._list(remote_path(path), '/resources'))

    def walk(self, path: str = '/'):
        """Yield descendants; iterative to support deeply nested directories."""
        stack = [remote_path(path)]
        seen = set()
        while stack:
            current = stack.pop()
            if current in seen:
                raise ProtocolError('Repeated directory in listing')
            seen.add(current)
            for item in self._list(current, '/resources'):
                yield item
                if item.get('type') == 'dir':
                    stack.append(remote_path(item['path']))

    def size(self, path: str = '/') -> dict:
        obj = self.stat(path)
        if obj['type'] == 'file':
            return {'bytes': obj['size'], 'files': 1, 'directories': 0}
        counts = {'bytes': 0, 'files': 0, 'directories': 0}
        for item in self.walk(path):
            if item['type'] == 'file':
                counts['files'] += 1
                counts['bytes'] += item['size']
            else:
                counts['directories'] += 1
        return counts

    def _operation(self, response, *, timeout=300):
        if response.status_code != 202:
            return
        link = json_object(response).get('href', '')
        parsed = urlsplit(link)
        if (parsed.scheme != 'https' or parsed.hostname not in
                ('cloud-api.yandex.net', 'cloud-api.yandex.com', 'cloud-api.yandex.ru')
                or parsed.username or parsed.password or parsed.port not in (443, None)
                or not parsed.path.startswith('/v1/disk/operations/') or parsed.query or parsed.fragment):
            raise ProtocolError('Invalid asynchronous operation URL')
        endpoint = parsed.path[len('/v1/disk'):]
        deadline = time.monotonic() + timeout
        while True:
            state = json_object(self._request('GET', endpoint)).get('status')
            if state == 'success':
                return
            if state == 'failed':
                raise YandexDiskError('Server-side operation failed')
            if state != 'in-progress':
                raise ProtocolError('Unknown operation status')
            if time.monotonic() >= deadline:
                raise NetworkError('Operation still running; inspect source and destination before retrying')
            time.sleep(0.5)

    def mkdir(self, path: str, *, parents: bool = False, exist_ok: bool = False) -> None:
        path = remote_path(path)
        if path == '/':
            return
        paths = [path]
        if parents:
            parts = path.strip('/').split('/')
            paths = ['/' + '/'.join(parts[:i]) for i in range(1, len(parts) + 1)]
        for current in paths:
            try:
                self._operation(self._request('PUT', '/resources', _retry=False, params={'path': current}))
            except APIError as exc:
                if exc.status_code != 409 or not (exist_ok or current != path):
                    raise
                if self.stat(current).get('type') != 'dir':
                    raise FileExistsError('A file occupies the requested directory path') from None

    def copy(self, source: str, destination: str, *, overwrite: bool = False) -> None:
        source, destination = self._move_paths(source, destination)
        self._operation(self._request('POST', '/resources/copy', _retry=False,
            params={'from': source, 'path': destination, 'overwrite': str(overwrite).lower()}))

    @staticmethod
    def _move_paths(source, destination):
        source, destination = non_root(source), non_root(destination)
        if destination == source or destination.startswith(source + '/'):
            raise ValueError('Destination must not equal or be inside the source')
        return source, destination

    def move(self, source: str, destination: str, *, overwrite: bool = False) -> None:
        source, destination = self._move_paths(source, destination)
        self._operation(self._request('POST', '/resources/move', _retry=False,
            params={'from': source, 'path': destination, 'overwrite': str(overwrite).lower()}))

    def rename(self, path: str, name: str, *, overwrite: bool = False) -> None:
        if not name or '/' in name or '\x00' in name or name in ('.', '..'):
            raise ValueError('New name must be a single filename')
        self.move(path, posixpath.join(posixpath.dirname(non_root(path)), name), overwrite=overwrite)

    def remove(self, path: str, *, recursive: bool = False, permanently: bool = False) -> None:
        path = non_root(path)
        if not recursive and self.stat(path)['type'] == 'dir':
            raise IsADirectoryError('Use recursive=True to remove a directory')
        self._operation(self._request('DELETE', '/resources', _retry=False,
            params={'path': path, 'permanently': str(permanently).lower()}))

    def trash_list(self, path: str = '/') -> list[dict]:
        # Trash paths are opaque API identifiers, not ordinary Disk paths.
        return list(self._list(path, '/trash/resources'))

    def restore(self, trash_path: str, *, name: str | None = None, overwrite: bool = False) -> None:
        trash_path = trash_item(trash_path)
        params = {'path': trash_path, 'overwrite': str(overwrite).lower()}
        if name is not None:
            if not name or '/' in name or name in ('.', '..') or '\x00' in name:
                raise ValueError('Restore name must be a filename')
            params['name'] = name
        self._operation(self._request('PUT', '/trash/resources/restore', _retry=False, params=params))

    def trash_delete(self, trash_path: str) -> None:
        trash_path = trash_item(trash_path)
        self._operation(self._request('DELETE', '/trash/resources', _retry=False, params={'path': trash_path}))

    def publish(self, path: str) -> str:
        path = non_root(path)
        self._operation(self._request('PUT', '/resources/publish', _retry=False, params={'path': path}))
        for attempt in range(8):
            link = self.stat(path).get('public_url')
            if link:
                return link
            if attempt < 7:
                time.sleep(0.5)
        raise ProtocolError('Published item has no public URL yet')

    def unpublish(self, path: str) -> None:
        self._operation(self._request('PUT', '/resources/unpublish', _retry=False,
                                      params={'path': non_root(path)}))
