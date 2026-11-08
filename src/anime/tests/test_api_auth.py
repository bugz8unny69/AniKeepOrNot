"""Offline credential separation, browser session/CSRF and rotation; unexecuted."""
import unittest
from src.anime.api.auth import ApiError, COOKIE, Sessions, webhook_key, public_origin, check_origin, valid_host

WEB='w'*43
ADMIN='m'*43


class ApiAuthenticationTests(unittest.TestCase):
    def test_legacy_webhook_locations_and_conflicting_credentials(self):
        for header,query,body in (({}, {}, {'token':WEB}),({'authorization':'Bearer '+WEB},{},{}),({}, {'token':[WEB]}, {})):
            webhook_key(header,query,body,WEB)
        for header,query,body in (({}, {}, {}),({'authorization':'Bearer '+ADMIN},{},{'token':WEB}),
                                  ({},{'token':[WEB,WEB]},{}),({},{},{'token':None}),({},{},{'token':123})):
            with self.assertRaises(ApiError):webhook_key(header,query,body,WEB)
        with self.assertRaises(ApiError) as error:webhook_key({}, {}, {'token':WEB},'')
        self.assertEqual(error.exception.status,503)

    def test_sessions_require_csrf_and_rotate_with_management_key(self):
        clock=[0];sessions=Sessions(clock=lambda:clock[0],ttl=30)
        token,csrf=sessions.create(ADMIN)
        headers={'cookie':COOKIE+'='+token}
        digest,session=sessions.require(headers,ADMIN)
        with self.assertRaises(ApiError):sessions.require(headers,ADMIN,mutation=True)
        sessions.require({**headers,'x-csrf-token':csrf},ADMIN,mutation=True)
        with self.assertRaises(ApiError):sessions.require(headers,'x'*43)
        sessions.remove(digest)
        with self.assertRaises(ApiError):sessions.require(headers,ADMIN)
        token,csrf=sessions.create(ADMIN);clock[0]=30
        with self.assertRaises(ApiError):sessions.require({'cookie':COOKIE+'='+token},ADMIN)

    def test_bounded_sessions_and_per_peer_login_attempts(self):
        sessions=Sessions(capacity=1)
        sessions.create(ADMIN)
        with self.assertRaises(ApiError):sessions.create(ADMIN)
        for _ in range(10):sessions.attempt('direct-peer')
        with self.assertRaises(ApiError):sessions.attempt('direct-peer')

    def test_origin_is_explicit_and_forwarded_headers_cannot_authorize(self):
        self.assertEqual(public_origin('https://anime.example/'),'https://anime.example')
        for origin in ('http://evil.example','https://user:pass@anime.example','https://anime.example/path','null','https://anime.example?token=x'):
            with self.assertRaises(ApiError):public_origin(origin)
        with self.assertRaises(ApiError):check_origin({'origin':'https://evil.example','x-forwarded-host':'anime.example'},'https://anime.example')
        with self.assertRaises(ApiError):check_origin({},'https://anime.example',required=True)
        self.assertTrue(valid_host('anime.proxy.example'))
        check_origin({'host':'anime.proxy.example','origin':'https://anime.proxy.example'},'http://127.0.0.1:8765',required=True)
        with self.assertRaises(ApiError):check_origin({'host':'anime.proxy.example','origin':'https://evil.example'},'http://127.0.0.1:8765',required=True)
