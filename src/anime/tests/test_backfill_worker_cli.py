"""The production worker enables backfill acquisition without an extra flag."""
import unittest
from unittest.mock import patch

from src.anime.__main__ import main, parser


class BackfillWorkerCliTests(unittest.TestCase):
    def test_sonarr_acquisition_flag_is_not_supported(self):
        with self.assertRaises(SystemExit) as error:
            parser().parse_args(['worker', '--config-dir', '/test/config',
                                 '--sonarr-acquire'])
        self.assertEqual(error.exception.code, 2)

    def test_default_worker_enables_backfill_acquisition(self):
        calls = []

        async def serve(config_dir, **options):
            calls.append(options)

        with patch('src.anime.__main__.check_local'), patch(
            'src.anime.providers.configuration.run_identity_worker', new=serve
        ):
            self.assertEqual(main(['worker', '--config-dir', '/test/config']), 0)

        self.assertEqual(calls, [{
            'backfill': True,
            'backfill_select': True,
            'backfill_acquire': True,
        }])
