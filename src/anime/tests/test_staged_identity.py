"""Owner, 2026-11-27: a file is known by its size and content, not its mtime; a path AKON
planned is AKON's (production: 5 videos of rejected series were left because their
publication never reached PUBLISHED, and tvshow.nfo because its mtime moved)."""
import os
from pathlib import Path
import tempfile
import unittest

from src.anime.acquisition.media import SAMPLE, content_id, same_content
from src.anime.series_job.reject import remove_staged

SERIES = "Show [tvdbid-1] [anidbid-2]"
VIDEO = SERIES + "/Season 01/Show - S01E01.mkv"
NFO = SERIES + "/Season 01/Show - S01E01.nfo"
SHOW = SERIES + "/tvshow.nfo"


def cid(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        return content_id(fd, os.fstat(fd).st_size)
    finally:
        os.close(fd)


class Configuration:
    def __init__(self, root):
        self.root = root

    def value(self, key):
        return {"storage.user_evaluation_root": self.root}[key]


class ContentIdTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)

    def test_touch_and_copy_keep_it_a_change_of_content_does_not(self):
        big = self.base / "big.mkv"
        data = bytearray(os.urandom(10 * SAMPLE))
        big.write_bytes(data)
        before = cid(big)
        os.utime(big, ns=(1, 1))                                  # Touched: same file.
        self.assertEqual(cid(big), before)
        copy = self.base / "copy.mkv"
        copy.write_bytes(big.read_bytes())                        # Copied: same file.
        self.assertEqual(cid(copy), before)
        for where in (0, len(data) // 2, len(data) - 1):          # One byte in any sample: another file.
            changed = bytearray(data)
            changed[where] ^= 1
            copy.write_bytes(changed)
            self.assertNotEqual(cid(copy), before)
        copy.write_bytes(data[:-1])                                # Truncated: another file.
        self.assertNotEqual(cid(copy), before)

    def test_small_files_are_read_whole(self):
        small = self.base / "s"
        small.write_bytes(b"a" * 1000)
        other = self.base / "o"
        other.write_bytes(b"a" * 999 + b"b")
        self.assertNotEqual(cid(small), cid(other))

    def test_same_content_by_size_and_recorded_content(self):
        video = self.base / "v.mkv"
        video.write_bytes(b"x" * 100)
        self.assertTrue(same_content(str(self.base), "v.mkv", 100, cid(video)))
        self.assertTrue(same_content(str(self.base), "v.mkv", 100, None))        # Old rows: size only.
        self.assertFalse(same_content(str(self.base), "v.mkv", 99, None))
        video.write_bytes(b"y" * 100)
        self.assertFalse(same_content(str(self.base), "v.mkv", 100, cid(self.base / "v.mkv") + "0"))
        self.assertIsNone(same_content(str(self.base), "gone.mkv", 100, None))


class RemoveStagedTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / SERIES / "Season 01").mkdir(parents=True)
        (self.root / VIDEO).write_bytes(b"v" * 100)
        (self.root / NFO).write_text("<episodedetails/>")
        (self.root / SHOW).write_text("<tvshow/>")
        self.plan = {"entries": [
            {"role": "media", "path": VIDEO, "size": 100, "content": cid(self.root / VIDEO)},
            {"role": "episode", "path": NFO, "size": len("<episodedetails/>")},
            {"role": "series", "path": SHOW, "size": len("<tvshow/>")}]}
        # A plan that never reached PUBLISHED is still AKON's: it was recorded before writing.
        self.torrent = {"staging": {"plans": {"S01E01": self.plan}}}

    def test_reject_removes_a_planned_copy_its_nfos_and_the_empty_folders(self):
        os.utime(self.root / SHOW, ns=(5, 5))                      # tvshow.nfo rewritten later: still ours.
        self.assertEqual(remove_staged(Configuration(str(self.root)), self.torrent), 0)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_a_file_that_is_not_the_one_akon_wrote_is_left(self):
        (self.root / VIDEO).write_bytes(b"w" * 100)                # Same size, other content.
        self.assertEqual(remove_staged(Configuration(str(self.root)), self.torrent), 1)
        self.assertTrue((self.root / VIDEO).exists())
        self.assertFalse((self.root / NFO).exists())
        self.assertTrue((self.root / SERIES).exists())             # Not empty: kept.

    def test_keep_leaves_tvshow_nfo_while_episodes_are_still_staged(self):
        remove_staged(Configuration(str(self.root)), self.torrent, {"S01E01"}, series_nfo=False)
        self.assertEqual([p.name for p in (self.root / SERIES).iterdir()], ["tvshow.nfo"])

    def test_your_own_files_in_the_folder_are_never_removed(self):
        (self.root / SERIES / "notes.txt").write_text("mine")
        remove_staged(Configuration(str(self.root)), self.torrent)
        self.assertEqual([p.name for p in (self.root / SERIES).iterdir()], ["notes.txt"])
