"""Regression tests for lightweight video verification and persisted hash redesign (W-10, W-11).

Proves that:
1. media_fingerprint produces identical SHA-256 for small files (<=64KB) preserving backward compatibility.
2. media_fingerprint reads only up to 64KB container header plus attributes for large files (>64KB).
3. verify_path (the series job's media check) avoids sequential multi-gigabyte streaming.
4. Its evidence has the keys staging and Keep read.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.anime.acquisition.media import media_fingerprint, verify_path, signature, MediaInvalid


def probe_bytes(*, languages=('jpn', 'eng'), duration='1440', container='matroska,webm'):
    streams = [{'index': 0, 'codec_type': 'video', 'codec_name': 'h264', 'width': 1920, 'height': 1080}]
    for i, language in enumerate(languages, 1):
        streams.append({'index': i, 'codec_type': 'audio', 'codec_name': 'aac', 'tags': {'language': language}})
    return json.dumps({'format': {'format_name': container, 'duration': duration}, 'streams': streams}).encode()


class VideoVerificationHashRedesignTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / ('ad-' + 'a' * 32)
        self.root.mkdir()
        self.flag = threading.Event()

    def test_media_fingerprint_small_payload_matches_full_sha256(self):
        """Files <= 64KB produce exact standard SHA-256 hex digest for backward compatibility."""
        payload = b'test small file content ' * 4
        file_path = self.root / 'small.mkv'
        file_path.write_bytes(payload)

        with open(file_path, 'rb') as f:
            fd = f.fileno()
            stat_info = signature(os.fstat(fd))
            fingerprint = media_fingerprint(fd, len(payload), stat_info)

        expected = hashlib.sha256(payload).hexdigest()
        self.assertEqual(fingerprint, expected)

    def test_media_fingerprint_large_payload_reads_sample_only(self):
        """Files > 64KB read only the initial 64KB container sample, binding attributes."""
        large_size = 10 * 1024 * 1024  # 10 MB
        file_path = self.root / 'large.mkv'
        header = b'M' * 65536
        with open(file_path, 'wb') as f:
            f.write(header)
            f.truncate(large_size)

        read_bytes = 0
        real_read = os.read

        def tracked_read(fd, count):
            nonlocal read_bytes
            chunk = real_read(fd, count)
            read_bytes += len(chunk)
            return chunk

        with open(file_path, 'rb') as f:
            fd = f.fileno()
            stat_info = signature(os.fstat(fd))
            with patch('os.read', side_effect=tracked_read):
                fingerprint = media_fingerprint(fd, large_size, stat_info)

        # Verified reading did not stream the 10MB payload:
        self.assertEqual(read_bytes, 65536)
        # Verified fingerprint incorporates header + size + mtime:
        expected = hashlib.sha256(header)
        expected.update(f":{large_size}:{stat_info[3]}".encode())
        self.assertEqual(fingerprint, expected.hexdigest())

    def test_media_fingerprint_differentiates_mtime_and_size_on_large_payload(self):
        """Different size or mtime on large payload produces different fingerprints."""
        large_size = 10 * 1024 * 1024
        file_path = self.root / 'differ.mkv'
        header = b'K' * 65536
        with open(file_path, 'wb') as f:
            f.write(header)
            f.truncate(large_size)

        with open(file_path, 'rb') as f:
            fd = f.fileno()
            stat_1 = (1, 2, large_size, 1000, 2000)
            stat_2 = (1, 2, large_size, 2000, 2000)
            stat_3 = (1, 2, large_size + 1, 1000, 2000)

            fp1 = media_fingerprint(fd, large_size, stat_1)
            fp2 = media_fingerprint(fd, large_size, stat_2)
            fp3 = media_fingerprint(fd, large_size + 1, stat_3)

        self.assertNotEqual(fp1, fp2)
        self.assertNotEqual(fp1, fp3)

    def test_media_fingerprint_raises_media_invalid_on_truncation(self):
        """Truncated or unreadable file raises MediaInvalid."""
        file_path = self.root / 'trunc.mkv'
        file_path.write_bytes(b'short')

        with open(file_path, 'rb') as f:
            fd = f.fileno()
            stat_info = (1, 2, 100, 1000, 2000)  # expects 100 bytes, file only has 5
            with self.assertRaises(MediaInvalid):
                media_fingerprint(fd, 100, stat_info)

    def test_verify_path_does_not_stream_large_video_payload(self):
        """The media check reads the header sample, never the whole 15 MB payload."""
        large_size = 15 * 1024 * 1024
        video_file = self.root / 'Episode.S01E01.mkv'
        with open(video_file, 'wb') as f:
            f.write(b'\x1a\x45\xdf\xa3' + b'\x00' * 65532)
            f.truncate(large_size)
        read_bytes = 0
        real_read = os.read

        def tracked_read(fd, count):
            nonlocal read_bytes
            chunk = real_read(fd, count)
            read_bytes += len(chunk)
            return chunk

        with patch('os.read', side_effect=tracked_read):
            verified = verify_path(str(self.root), 'Episode.S01E01.mkv', large_size, self.flag,
                                   read_probe=lambda *_: probe_bytes())
        self.assertEqual(verified['file'][2], large_size)
        self.assertEqual(verified['languages'], ['eng', 'jpn'])
        # The header fingerprint and the three content_id samples: 256 KB of a 15 MB file.
        self.assertLessEqual(read_bytes, 4 * 65536)

    def test_evidence_has_the_keys_staging_and_keep_read(self):
        large_size = 5 * 1024 * 1024
        with open(self.root / 'ep.mkv', 'wb') as f:
            f.write(b'H' * 65536)
            f.truncate(large_size)
        verified = verify_path(str(self.root), 'ep.mkv', large_size, self.flag, read_probe=lambda *_: probe_bytes())
        self.assertEqual(set(verified), {'root', 'file', 'sha256', 'content', 'duration_ms', 'languages'})
        self.assertEqual(verified['file'][2], large_size)
        self.assertEqual(len(verified['sha256']), 64)
        json.dumps(verified)                            # Stored in the series row as JSON.


if __name__ == '__main__':
    unittest.main()
