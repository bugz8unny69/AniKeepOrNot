"""Regression tests for browser origin validation behind a reverse proxy."""
import unittest

from src.anime.api.auth import ApiError, check_origin


class BrowserOriginTests(unittest.TestCase):
    def test_default_https_port_is_equivalent(self):
        check_origin({"host": "anime.example.test",
                      "origin": "https://anime.example.test:443",
                      "sec-fetch-site": "same-origin"},
                     "https://anime.example.test", required=True)

    def test_explicit_default_port_on_host(self):
        check_origin({"host": "anime.example.test:443",
                      "origin": "https://anime.example.test"},
                     "https://anime.example.test", required=True)

    def test_host_mismatch_is_rejected(self):
        with self.assertRaises(ApiError):
            check_origin({"host": "other.example.test",
                          "origin": "https://anime.example.test"},
                         "https://anime.example.test", required=True)

    def test_non_default_port_mismatch_is_rejected(self):
        with self.assertRaises(ApiError):
            check_origin({"host": "anime.example.test:8443",
                          "origin": "https://anime.example.test"},
                         "https://anime.example.test", required=True)

    def test_cross_site_fetch_is_rejected(self):
        with self.assertRaises(ApiError):
            check_origin({"host": "anime.example.test",
                          "origin": "https://anime.example.test",
                          "sec-fetch-site": "cross-site"},
                         "https://anime.example.test", required=True)


if __name__ == "__main__":
    unittest.main()
