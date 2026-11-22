"""Transcoded batch episodes are staged by MOVE from the staging root (prod 10961, Call of the Night)."""
import json
import unittest

from src.anime.batch_decision_cleanup import protected_staging_roots
from src.anime.cancellation import CancellationError
from src.anime.staging import torrent_left_in_place

TRIAL = '/mnt/media/Anime-Trial'
DOWNLOADS = '/mnt/media/Torrents/anime/ad-' + 'a' * 32


def plan(transfer, source_root):
    return {'transfer': transfer, 'root': TRIAL, 'source_root': source_root}


class TranscodedBatchStagingTests(unittest.TestCase):
    def test_only_a_copy_or_a_transcoded_move_leaves_the_torrent_in_place(self):
        self.assertTrue(torrent_left_in_place(plan('COPY', DOWNLOADS)))
        self.assertTrue(torrent_left_in_place(plan('MOVE', TRIAL)))       # Transcoded output, renamed into place.
        self.assertFalse(torrent_left_in_place(plan('MOVE', DOWNLOADS)))  # Would move the torrent's own file.
        self.assertFalse(torrent_left_in_place(plan('LINK', TRIAL)))
        self.assertFalse(torrent_left_in_place({'transfer': 'MOVE'}))
        self.assertFalse(torrent_left_in_place(None))

    def test_batch_cleanup_protects_the_staging_root_of_a_transcoded_episode(self):
        rows = [(1, 0, json.dumps(plan('COPY', DOWNLOADS))), (1, 1, json.dumps(plan('MOVE', TRIAL)))]
        self.assertEqual(protected_staging_roots(rows), (TRIAL,))
        with self.assertRaises(CancellationError):
            protected_staging_roots([(1, 0, json.dumps(plan('MOVE', DOWNLOADS)))])


if __name__ == '__main__':
    unittest.main()
