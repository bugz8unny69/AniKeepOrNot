"""Each attempt works on a copy of the worker's context with its own settings (owner, 2026-11-28).

With two lanes the worker copies the context per attempt. The backfill catalog was a
closure over the original context, whose settings stay None: every search failed with
AttributeError in options_from_configuration (Psyren, Firefly Wedding).
"""
import copy
import threading
import unittest

from src.anime.series_job.context import build


class Reached(Exception):
    pass


class Settings:
    def value(self, key):
        raise Reached(key)


class SeriesContextCopyTests(unittest.TestCase):
    def test_a_copy_builds_its_catalog_from_its_own_settings(self):
        context = copy.copy(build(secrets=None, cancelled=threading.Event()))
        context.configuration = Settings()
        catalog = context.catalogs(context.configuration)
        with self.assertRaises(Reached):        # It read this attempt's settings, not None's.
            catalog(19765, 1)


if __name__ == "__main__":
    unittest.main()
