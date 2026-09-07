from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
import httpx

from .errors import APIError, AuthenticationError, ProtocolError


def credentials(env_file: str | Path) -> dict[str, str]:
    """Read without changing os.environ; environment overrides the file."""
    values = dotenv_values(env_file, interpolate=False) if Path(env_file).exists() else {}
    aliases = {
        "access_token": ("YANDEX_ACCESS_TOKEN", "yandex_access_token"),
        "client_id": ("YANDEX_CLIENT_ID", "yandex_ClientID"),
        "client_secret": ("YANDEX_CLIENT_SECRET", "yandex_Client_secret"),
        "redirect_uri": ("YANDEX_REDIRECT_URI", "yandex_Redirect_URI"),
    }
    return {
        key: next((str(source[name]).strip() for source in (os.environ, values)
                   for name in names if source.get(name)), "")
        for key, names in aliases.items()
    }


def check_status(response: httpx.Response) -> None:
    if not 200 <= response.status_code < 300:
        cls = AuthenticationError if response.status_code == 401 else APIError
        raise cls(response.status_code)


def json_object(response: httpx.Response) -> dict:
    try:
        obj = response.json()
    except ValueError:
        raise ProtocolError("Expected JSON response") from None
    if not isinstance(obj, dict):
        raise ProtocolError("Expected JSON object")
    return obj


def transfer_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        valid = (parts.scheme == "https" and parts.port in (None, 443)
                 and not parts.username and not parts.password
                 and any(host.endswith('.' + domain) for domain in
                         ("yandex.net", "yandex.ru", "yandex.com")))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ProtocolError("Unexpected file transfer host or protocol")
    return url
