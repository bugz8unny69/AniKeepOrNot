"""Offline COPY-mode batch staging keeps the original torrent video intact."""
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest

from src.nextgen.acquisition.media import signature
from src.nextgen.staging import Publisher, StagingReview, specification, root_identity
from src.nextgen.tests.test_staging_publication import JournalDouble


class BatchStagingCopyTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root=Path(tmp.name)
        self.source=root/'torrent';self.source.mkdir()
        self.stage=root/'trial';self.stage.mkdir()
        self.video=self.source/'Show S01E01.mkv'
        self.video.write_bytes(b'video-episode-content'*50)
        self.sha=hashlib.sha256(self.video.read_bytes()).hexdigest()
        evidence={'path':self.video.name,'root':root_identity(str(self.source)),
                  'file':list(signature(self.video.stat())),'sha256':self.sha,
                  'duration_ms':1200000,'languages':['eng','jpn']}
        intent=SimpleNamespace(options=SimpleNamespace(download_root=str(self.source)))
        self.plan=specification(intent,evidence,(1,1001,2,1,1,2001,'Show'),
                                str(self.stage),1)
        self.plan['root_identity']=root_identity(str(self.stage))
        self.plan['transfer']='COPY'
        self.journal=JournalDouble()
        self.publisher=Publisher(self.journal,SimpleNamespace(job_id=123),
                                 threading.Event(),allow_root=True)

    def test_batch_staging_copies_without_rehashing_torrent_data(self):
        original_inode=self.video.stat().st_ino
        receipts=self.publisher.publish(self.plan)
        output=self.stage/self.plan['entries'][0]['path']
        self.assertEqual(output.read_bytes(),self.video.read_bytes())
        self.assertNotEqual(output.stat().st_ino,original_inode)
        self.assertEqual(self.video.stat().st_ino,original_inode)
        self.assertEqual(receipts[0]['sha256'],self.sha)
        self.assertFalse(receipts[0]['hash_checked'])
        self.assertTrue(all(p['hash_checked'] for p in receipts[1:]))
        again=self.publisher.publish(self.plan)
        self.assertEqual(receipts,again)
        self.assertTrue(self.video.exists())

    def test_existing_unknown_destination_is_never_adopted(self):
        output=self.stage/self.plan['entries'][0]['path']
        output.parent.mkdir(parents=True)
        output.write_bytes(self.video.read_bytes())
        with self.assertRaises(StagingReview):
            self.publisher.publish(self.plan)
        self.assertTrue(self.video.exists())

    def test_tampered_copied_media_cannot_reconcile_as_published(self):
        self.publisher.publish(self.plan)
        output=self.stage/self.plan['entries'][0]['path']
        output.write_bytes(b'changed')
        with self.assertRaises(StagingReview):
            self.publisher.publish(self.plan)
        self.assertTrue(self.video.exists())


if __name__=='__main__':
    unittest.main()
