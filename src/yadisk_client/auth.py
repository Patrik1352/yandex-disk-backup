"""OAuth authorization code flow with PKCE; no embedded web server."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
from pathlib import Path
import secrets
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from ._common import credentials, json_object
from .errors import AuthenticationError, NetworkError, ProtocolError


@dataclass(frozen=True)
class Tokens:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_in: int | None = None


@dataclass(frozen=True)
class AuthorizationRequest:
    url: str
    state: str = field(repr=False)
    code_verifier: str = field(repr=False)
    redirect_uri: str


class OAuth:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str,
                 *, transport: httpx.BaseTransport | None = None):
        if not client_id or not client_secret or not redirect_uri:
            raise ValueError("client_id, client_secret and redirect_uri are required")
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._http = httpx.Client(transport=transport, timeout=30, follow_redirects=False)

    @classmethod
    def from_env(cls, env_file: str | Path = ".env", **kwargs) -> OAuth:
        config = credentials(env_file)
        return cls(config['client_id'], config['client_secret'], config['redirect_uri'], **kwargs)

    def authorization_url(self) -> AuthorizationRequest:
        verifier = secrets.token_urlsafe(48)
        state = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        params = {'response_type': 'code', 'client_id': self._client_id,
                  'redirect_uri': self._redirect_uri, 'state': state,
                  'code_challenge': challenge, 'code_challenge_method': 'S256',
                  'scope': 'cloud_api:disk.read cloud_api:disk.write'}
        return AuthorizationRequest('https://oauth.yandex.ru/authorize?' + urlencode(params),
                                    state, verifier, self._redirect_uri)

    def exchange_callback(self, callback_url: str, request: AuthorizationRequest) -> Tokens:
        """Validate callback state and URI, then exchange its code once."""
        if request.redirect_uri != self._redirect_uri:
            raise ValueError("Authorization request belongs to a different redirect_uri")
        expected, actual = urlsplit(request.redirect_uri), urlsplit(callback_url)
        if (expected.scheme, expected.netloc, expected.path) != (actual.scheme, actual.netloc, actual.path):
            raise ValueError("OAuth callback URI does not match redirect_uri")
        query = parse_qs(actual.query)
        state = query.get('state', [])
        if len(state) != 1 or not hmac.compare_digest(state[0], request.state):
            raise ValueError("OAuth state mismatch")
        if 'error' in query:
            raise AuthenticationError(400)
        code = query.get('code', [])
        if len(code) != 1:
            raise ValueError("OAuth callback must contain one authorization code")
        return self._token({'grant_type': 'authorization_code', 'code': code[0],
                            'code_verifier': request.code_verifier,
                            'redirect_uri': request.redirect_uri})

    def refresh(self, refresh_token: str) -> Tokens:
        """Return current/new tokens; caller is responsible for saving them."""
        if not refresh_token:
            raise ValueError("refresh_token is required")
        return self._token({'grant_type': 'refresh_token', 'refresh_token': refresh_token})

    def _token(self, data: dict[str, str]) -> Tokens:
        # Code exchange is deliberately not retried: the code is single-use.
        try:
            response = self._http.post('https://oauth.yandex.ru/token', data=data,
                                       auth=(self._client_id, self._client_secret))
        except httpx.HTTPError:
            raise NetworkError("OAuth request failed; credentials were not logged") from None
        if response.status_code != 200:
            raise AuthenticationError(response.status_code)
        obj = json_object(response)
        if not isinstance(obj.get('access_token'), str) or not obj['access_token']:
            raise ProtocolError("OAuth response has no access token")
        return Tokens(obj['access_token'], obj.get('refresh_token'), obj.get('expires_in'))

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> OAuth:
        return self

    def __exit__(self, *_args) -> None:
        self.close()
