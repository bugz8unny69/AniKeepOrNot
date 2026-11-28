"""The worker command: the series worker, with no acquisition flags."""
import unittest
from unittest.mock import patch

from src.anime.__main__ import main, parser


class BackfillWorkerCliTests(unittest.TestCase):
    def test_sonarr_acquisition_flag_is_not_supported(self):
        with self.assertRaises(SystemExit) as error:
            parser().parse_args(['worker', '--config-dir', '/test/config',
                                 '--sonarr-acquire'])
        self.assertEqual(error.exception.code, 2)

    def test_the_worker_command_runs_the_series_worker_with_no_flags(self):
        """The series job runs its own trial and backfill searches: no option switches them on."""
        calls = []

        async def run(config_dir):
            calls.append(config_dir)

        with patch('src.anime.__main__.check_local'), patch('src.anime.worker.run_worker', new=run):
            self.assertEqual(main(['worker', '--config-dir', '/test/config']), 0)
        self.assertEqual(calls, ['/test/config'])
