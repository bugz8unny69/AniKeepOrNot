"""Tests for filesystem trust boundaries in Keep and Cleanup (W-07, W-09).

Proves that:
1. check_source succeeds even if historical inode/device numbers change (NFS remount).
2. check_source fails only if the file is missing or has a mismatched size.
3. remove_selected deletes video when mtime changed without full video rehash.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from src.anime.keep_selection import KeepSelection
from src.anime.sonarr import KeepReview
from src.anime.staging import StagingReview
from src.anime.storage.keep_imports import check_source
from src.anime.storage.review_decisions import remove_selected


class KeepFilesystemTrustTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.staging_root = self.root / 'staging'
        self.staging_root.mkdir()
        self.keep_root = self.root / 'keep'
        self.keep_root.mkdir()

        # Create staged media file
        self.media_file = self.staging_root / 'episode.mkv'
        self.media_content = b'test-video-content-12345'
        self.media_file.write_bytes(self.media_content)

        stat = self.media_file.stat()
        self.data = {
            'selection': {
                'anidb_id': 1,
                'anidb_episode_id': 100,
                'tvdb_series_id': 10,
                'tvdb_episode_id': 1000,
                'tvdb_season': 1,
                'tvdb_episode': 1,
                'preparation_plan_id': 1,
                'staging_root': str(self.staging_root),
                'media_relative_path': 'episode.mkv',
            },
            # Historical inode/device values that differ from current local filesystem:
            'root_identity': [999999, 888888],
            'source_parent': [999999, 888888],
            'source_file': [999999, 777777, len(self.media_content), 1000, 2000],
        }

    def test_check_source_succeeds_despite_changed_inode_and_device(self):
        """W-07: Historical inode/device changes (e.g. NFS remount) do not block Keep."""
        # Does not raise KeepReview despite root_identity / source_file having different st_dev/st_ino
        check_source(self.data)

    def test_check_source_fails_if_file_missing(self):
        """Missing staged file is rejected."""
        self.media_file.unlink()
        with self.assertRaises(KeepReview):
            check_source(self.data)

    def test_check_source_fails_if_size_mismatched(self):
        """Tampered file with different size is rejected."""
        self.media_file.write_bytes(b'different-size')
        with self.assertRaises(KeepReview):
            check_source(self.data)

    def test_remove_selected_succeeds_when_video_mtime_touched(self):
        """W-09: Video deletion does not rehash full video merely because mtime changed."""
        episode_nfo = self.staging_root / 'episode.nfo'
        nfo_content = b'<episodedetails><title>Ep 1</title></episodedetails>'
        episode_nfo.write_bytes(nfo_content)

        manifest = {
            'root': str(self.staging_root),
            'entries': [
                {'role': 'media', 'path': 'episode.mkv', 'size': len(self.media_content),
                 'sha256': hashlib.sha256(self.media_content).hexdigest()},
                {'role': 'episode', 'path': 'episode.nfo', 'size': len(nfo_content),
                 'sha256': hashlib.sha256(nfo_content).hexdigest()},
                {'role': 'series', 'path': 'tvshow.nfo', 'size': 10, 'sha256': 'a' * 64},
            ]
        }
        receipts = [
            {'role': 'media', 'path': 'episode.mkv', 'file': [1, 2, len(self.media_content), 1000, 2000]},
            {'role': 'episode', 'path': 'episode.nfo', 'file': [1, 3, len(nfo_content), 1000, 2000]},
            {'role': 'series', 'path': 'tvshow.nfo', 'file': [1, 4, 10, 1000, 2000]},
        ]

        # Touch the video file's mtime so it differs from receipt's mtime (1000)
        os.utime(self.media_file, (5000, 5000))

        # remove_selected should delete the media and episode NFO without failing on mtime change
        guard_called = []
        remove_selected(json.dumps(manifest), json.dumps(receipts),
                        lambda: guard_called.append(True), str(self.keep_root))

        self.assertFalse(self.media_file.exists())
        self.assertFalse(episode_nfo.exists())
        self.assertTrue(len(guard_called) >= 2)


if __name__ == '__main__':
    unittest.main()
