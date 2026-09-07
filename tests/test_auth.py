import base64
import hashlib
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from yadisk_client import OAuth, AuthenticationError, Tokens


class AuthTests(unittest.TestCase):
    def test_pkce_state_and_token_exchange(self):
        calls = []
        def handler(r):
            calls.append(r)
            return httpx.Response(200, json={'access_token': 'access-secret',
                 'refresh_token': 'refresh-secret', 'expires_in': 3600})
        with OAuth('id', 'secret', 'http://localhost:8080/callback', transport=httpx.MockTransport(handler)) as auth:
            request = auth.authorization_url()
            params = parse_qs(urlsplit(request.url).query)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(request.code_verifier.encode()).digest()).rstrip(b'=').decode()
            self.assertEqual(params['code_challenge'], [challenge])
            self.assertEqual(params['code_challenge_method'], ['S256'])
            callback = request.redirect_uri + '?' + urlencode({'state': request.state, 'code': 'one-time-code'})
            token = auth.exchange_callback(callback, request)
        self.assertEqual(token.access_token, 'access-secret')
        self.assertNotIn('access-secret', repr(token))
        self.assertNotIn('refresh-secret', repr(token))
        body = parse_qs(calls[0].content.decode())
        self.assertEqual(body['code_verifier'], [request.code_verifier])
        self.assertEqual(body['grant_type'], ['authorization_code'])
        self.assertEqual(len(calls), 1)

    def test_bad_state_and_redirect_never_send_code(self):
        calls = []
        with OAuth('id', 'secret', 'http://localhost/callback', transport=httpx.MockTransport(lambda r: calls.append(r))) as auth:
            request = auth.authorization_url()
            for callback in ['http://localhost/callback?state=wrong&code=x',
                             'http://otherhost/callback?state='+request.state+'&code=x']:
                with self.assertRaises(ValueError): auth.exchange_callback(callback, request)
        self.assertFalse(calls)

    def test_refresh_uses_correct_grant_and_returns_new_tokens(self):
        def handler(r):
            self.assertEqual(parse_qs(r.content.decode()),
                             {'grant_type': ['refresh_token'], 'refresh_token': ['old']})
            self.assertTrue(r.headers['Authorization'].startswith('Basic '))
            return httpx.Response(200, json={'access_token': 'new', 'refresh_token': 'rotated'})
        with OAuth('id', 'secret', 'http://localhost/callback', transport=httpx.MockTransport(handler)) as auth:
            tokens = auth.refresh('old')
        self.assertEqual(tokens, Tokens('new', 'rotated'))

    def test_rejected_code_is_not_retried(self):
        calls = []
        with OAuth('id', 'secret', 'http://localhost/callback', transport=httpx.MockTransport(
                lambda r: calls.append(r) or httpx.Response(400, json={'error_description': 'sensitive'}))) as auth:
            request = auth.authorization_url()
            callback = request.redirect_uri + '?' + urlencode({'state': request.state, 'code': 'code'})
            with self.assertRaises(AuthenticationError) as ctx: auth.exchange_callback(callback, request)
        self.assertEqual(len(calls), 1)
        self.assertNotIn('sensitive', str(ctx.exception))


if __name__ == '__main__': unittest.main()
