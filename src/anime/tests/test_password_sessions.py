"""Management session security regressions without starting the worker."""
import unittest

from src.anime.api.auth import ApiError, Sessions
from src.anime.api.passwords import hash_password, verify_password


class PasswordSessionTests(unittest.TestCase):
    def test_rotation_invalidates_cookie_and_csrf(self):
        sessions = Sessions()
        first = hash_password("correct horse battery staple")
        token, csrf = sessions.create(first)
        headers = {"cookie": "ad_nextgen_session=" + token, "x-csrf-token": csrf}
        sessions.require(headers, first, mutation=True)
        second = hash_password("new correct horse battery staple")
        self.assertTrue(verify_password("new correct horse battery staple", second))
        with self.assertRaises(ApiError) as error:
            sessions.require(headers, second, mutation=True)
        self.assertEqual(error.exception.status, 401)

    def test_csrf_is_required_for_mutations(self):
        sessions = Sessions()
        digest = hash_password("correct horse battery staple")
        token, csrf = sessions.create(digest)
        with self.assertRaises(ApiError) as error:
            sessions.require({"cookie": "ad_nextgen_session=" + token}, digest, mutation=True)
        self.assertEqual(error.exception.status, 403)
        sessions.require({"cookie": "ad_nextgen_session=" + token, "x-csrf-token": csrf}, digest, mutation=True)

    def test_explicit_session_clear(self):
        sessions = Sessions()
        digest = hash_password("correct horse battery staple")
        token, csrf = sessions.create(digest)
        sessions.clear()
        with self.assertRaises(ApiError):
            sessions.require({"cookie": "ad_nextgen_session=" + token, "x-csrf-token": csrf}, digest)


if __name__ == "__main__":
    unittest.main()
