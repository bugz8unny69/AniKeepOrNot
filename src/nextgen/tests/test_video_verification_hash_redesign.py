"""Regression tests for lightweight video verification and persisted hash redesign (W-10, W-11).

Proves that:
1. media_fingerprint produces identical SHA-256 for small files (<=64KB) preserving backward compatibility.
2. media_fingerprint reads only up to 64KB container header plus attributes for large files (>64KB).
3. verify_file, verify_batch_probe_file, and verify_batch_episode_file avoid sequential multi-gigabyte streaming.
4. Serialized VerifiedMedia evidence remains 100% schema-compatible with downstream consumers.
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

from src.nextgen.acquisition.media import (
    media_fingerprint, verify_file, signature, VerifiedMedia, MediaInvalid,
)
from src.nextgen.acquisition.metadata import TorrentFile, Manifest, BatchInventory
from src.nextgen.acquisition.settings import DownloadOptions
from src.nextgen.acquisition.submission import SubmissionPlan, Intent
from src.nextgen.batch.media import verify_batch_probe_file, verify_batch_episode_file
from src.nextgen.storage.batch_acquisition_candidate import BatchAcquisitionCandidate
from src.nextgen.storage.batch_submission_journal import BatchSubmissionIntent
from src.nextgen.storage.batch_transfer_journal import BatchTransferRecord
from src.nextgen.storage.batch_unlock_journal import BatchUnlockRecord, BatchUnlockIntent


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

    def test_verify_file_does_not_stream_large_video_payload(self):
        """verify_file avoids sequential multi-gigabyte disk streaming."""
        large_size = 15 * 1024 * 1024  # 15 MB
        video_file = self.root / 'Episode.S01E01.mkv'
        header = b'\x1a\x45\xdf\xa3' + b'\x00' * 65532
        with open(video_file, 'wb') as f:
            f.write(header)
            f.truncate(large_size)

        manifest = Manifest('a' * 40, 'b' * 64, (TorrentFile(0, 'Episode.S01E01.mkv', large_size),), 0)
        plan = SubmissionPlan(1, 2, 3, 4, 'a' * 32, manifest)
        intent = Intent(plan, DownloadOptions('https://qb.example', str(self.root), 'anime', 30),
                        'OWNED_STOPPED', 'b' * 64, 5)
        record = SimpleNamespace(fingerprint='c' * 64)

        read_bytes = 0
        real_read = os.read

        def tracked_read(fd, count):
            nonlocal read_bytes
            chunk = real_read(fd, count)
            read_bytes += len(chunk)
            return chunk

        with patch('os.read', side_effect=tracked_read):
            verified = verify_file(intent, record, 5, self.flag,
                                   read_probe=lambda *_: probe_bytes())

        self.assertIsNotNone(verified)
        self.assertEqual(verified.file_signature[2], large_size)
        self.assertEqual(verified.languages, ('eng', 'jpn'))
        # Total bytes read for fingerprinting should not exceed 64KB (never reading the 15MB payload)
        self.assertLessEqual(read_bytes, 65536)

    def test_verify_batch_probe_file_does_not_stream_large_video_payload(self):
        """verify_batch_probe_file avoids sequential multi-gigabyte disk streaming."""
        large_size = 20 * 1024 * 1024  # 20 MB
        video_file = self.root / 'Show.S01E01.mkv'
        header = b'\x1a\x45\xdf\xa3' + b'\x00' * 65532
        with open(video_file, 'wb') as f:
            f.write(header)
            f.truncate(large_size)

        files = (TorrentFile(0, 'Show.S01E01.mkv', large_size),)
        inventory = BatchInventory('a' * 40, 'b' * 64, files)
        options = DownloadOptions('http://localhost:8080', str(self.root), 'trial', 10)
        candidate = BatchAcquisitionCandidate(1, 2, inventory.info_hash, inventory.info_sha256, 0,
                                             ((1, 0),), (), 200)
        intent = BatchSubmissionIntent(candidate, inventory, options, 'a' * 32, 'OWNED_STOPPED', 'b' * 64, 5)

        from src.nextgen.batch.probe_transfer import selection_fingerprint
        piece_size = 200
        fp = selection_fingerprint(intent, piece_size)
        record = BatchTransferRecord(
            fingerprint=fp, phase='COMPLETE', piece_size=piece_size, selected_index=0,
            transfer_job_id=3, transfer_attempt=1, start_job_id=2, start_attempt=1,
            progress=1.0, reason_code='DOWNLOAD_PROGRESS'
        )

        read_bytes = 0
        real_read = os.read

        def tracked_read(fd, count):
            nonlocal read_bytes
            chunk = real_read(fd, count)
            read_bytes += len(chunk)
            return chunk

        with patch('os.read', side_effect=tracked_read):
            verified = verify_batch_probe_file(intent, record, 3, self.flag,
                                               read_probe=lambda *_: probe_bytes())

        self.assertIsNotNone(verified)
        self.assertEqual(verified.file_signature[2], large_size)
        self.assertLessEqual(read_bytes, 65536)

    def test_verify_batch_episode_file_does_not_stream_large_video_payload(self):
        """verify_batch_episode_file avoids sequential multi-gigabyte disk streaming."""
        large_size = 20 * 1024 * 1024  # 20 MB
        video_file = self.root / 'Show.S01E01.mkv'
        header = b'\x1a\x45\xdf\xa3' + b'\x00' * 65532
        with open(video_file, 'wb') as f:
            f.write(header)
            f.truncate(large_size)

        files = (TorrentFile(0, 'Show.S01E01.mkv', large_size),)
        inventory = BatchInventory('a' * 40, 'b' * 64, files)
        options = DownloadOptions('http://localhost:8080', str(self.root), 'trial', 10)
        candidate = BatchAcquisitionCandidate(1, 2, inventory.info_hash, inventory.info_sha256, 0,
                                             ((1, 0),), (), 200)
        intent = BatchSubmissionIntent(candidate, inventory, options, 'a' * 32, 'OWNED_STOPPED', 'b' * 64, 5)

        from src.nextgen.batch.unlock import planned_priorities
        priorities = planned_priorities(intent)
        unlock_intent = BatchUnlockIntent(
            plan_id=intent.candidate.plan_id,
            fingerprint='d' * 64,
            priorities=priorities,
            piece_size=200,
            verification_job_id=3
        )
        record = BatchUnlockRecord(
            intent=unlock_intent,
            phase='COMPLETE',
            first_job_id=7,
            first_attempt=1,
            start_job_id=5,
            start_attempt=1,
            reason_code='BATCH_UNLOCKED'
        )

        read_bytes = 0
        real_read = os.read

        def tracked_read(fd, count):
            nonlocal read_bytes
            chunk = real_read(fd, count)
            read_bytes += len(chunk)
            return chunk

        with patch('os.read', side_effect=tracked_read):
            verified = verify_batch_episode_file(intent, record, 7, 0, self.flag,
                                                 read_probe=lambda *_: probe_bytes())

        self.assertIsNotNone(verified)
        self.assertEqual(verified.file_signature[2], large_size)
        self.assertLessEqual(read_bytes, 65536)

    def test_verified_media_evidence_json_and_storage_roundtrip(self):
        """VerifiedMedia serialization matches exact keys and formats expected by storage and staging."""
        large_size = 5 * 1024 * 1024
        video_file = self.root / 'ep.mkv'
        header = b'H' * 65536
        with open(video_file, 'wb') as f:
            f.write(header)
            f.truncate(large_size)

        manifest = Manifest('a' * 40, 'b' * 64, (TorrentFile(0, 'ep.mkv', large_size),), 0)
        plan = SubmissionPlan(1, 2, 3, 4, 'a' * 32, manifest)
        intent = Intent(plan, DownloadOptions('https://qb.example', str(self.root), 'anime', 30),
                        'OWNED_STOPPED', 'b' * 64, 5)
        record = SimpleNamespace(fingerprint='c' * 64)

        verified = verify_file(intent, record, 5, self.flag, read_probe=lambda *_: probe_bytes())
        serialized = verified.json()
        parsed = json.loads(serialized)

        self.assertEqual(set(parsed.keys()), {'path', 'root', 'file', 'sha256', 'duration_ms', 'languages'})
        self.assertEqual(parsed['path'], 'ep.mkv')
        self.assertEqual(parsed['file'][2], large_size)
        self.assertEqual(len(parsed['sha256']), 64)

        # Roundtrip into a fresh VerifiedMedia instance
        reconstructed = VerifiedMedia(
            verified.preparation_job_id, verified.download_job_id, verified.plan_id,
            verified.submission_fingerprint, verified.transfer_fingerprint,
            parsed['path'], tuple(parsed['root']), tuple(parsed['file']),
            parsed['sha256'], parsed['duration_ms'], tuple(parsed['languages'])
        )
        self.assertEqual(reconstructed.json(), serialized)


if __name__ == '__main__':
    unittest.main()
