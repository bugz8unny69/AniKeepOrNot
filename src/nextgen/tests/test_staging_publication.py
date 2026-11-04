"""Offline filesystem publication/recovery doubles; authored, unrun."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
import xml.etree.ElementTree as ET

from src.nextgen.acquisition.media import signature
from src.nextgen.staging import (Publisher, StagingReview, StagingConfiguration,
    specification, root_identity, disjoint)


class JournalDouble:
    def __init__(self):self.rows={};self.next_id=1;self.fail_confirm=False
    def guard(self,claim):pass
    def artifact(self,claim,root,name):return self.rows.get((root,name))
    def register(self,claim,root,name):
        folder=os.stat(root);info=os.stat(Path(root)/name,follow_symlinks=False)
        value={'id':self.next_id,'root':[folder.st_dev,folder.st_ino],'file':[info.st_dev,info.st_ino],'phase':'TEMP'}
        self.next_id+=1;self.rows[root,name]=value;return value
    def register_move(self,claim,root,name,source):
        folder=os.stat(root)
        value={'id':self.next_id,'root':[folder.st_dev,folder.st_ino],'file':list(source[:2]),'phase':'TEMP'}
        self.next_id+=1;self.rows[root,name]=value;return value
    def intent(self,claim,artifact):
        row=next(r for r in self.rows.values() if r['id']==artifact)
        if row['phase']!='TEMP':raise StagingReview()
        row['phase']='PUBLISH_UNCERTAIN'
    def confirm(self,claim,artifact):
        if self.fail_confirm:self.fail_confirm=False;raise OSError('lost confirmation')
        row=next(r for r in self.rows.values() if r['id']==artifact)
        if row['phase']!='PUBLISH_UNCERTAIN':raise StagingReview()
        row['phase']='PUBLISHED'


class StagingPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        base=Path(self.temp.name);source=base/'source';source.mkdir();self.target=base/'staging';self.target.mkdir()
        self.file=source/'episode.mkv';self.file.write_bytes(b'x'*100)
        evidence={'path':self.file.name,'root':root_identity(str(source)),'file':list(signature(self.file.stat())),
                  'sha256':hashlib.sha256(b'x'*100).hexdigest(),'duration_ms':1440000,'languages':['eng','jpn']}
        intent=SimpleNamespace(options=SimpleNamespace(download_root=str(source)))
        self.plan=specification(intent,evidence,(100,101,200,0,10,201,'Title <& / dangerous>'),str(self.target),1)
        self.plan['root_identity']=root_identity(str(self.target))
        self.assertEqual(self.plan['transfer'],'COPY')
        self.plan['transfer']='MOVE'  # Existing tests also cover persisted MOVE plans.
        self.journal=JournalDouble();self.flag=threading.Event()
        self.publisher=Publisher(self.journal,SimpleNamespace(job_id=1),self.flag,allow_root=True)

    def video(self):return self.target/self.plan['entries'][0]['path']

    def test_same_filesystem_moves_inode_and_publishes_metadata(self):
        before=self.file.stat();receipts=self.publisher.publish(self.plan)
        self.assertEqual(self.video().read_bytes(),b'x'*100)
        self.assertEqual(self.video().stat().st_ino,before.st_ino)
        self.assertEqual(self.video().stat().st_mtime_ns,before.st_mtime_ns)
        self.assertEqual(len(receipts),3)
        self.assertEqual({r['phase'] for r in self.journal.rows.values()},{'PUBLISHED'})
        episode=ET.parse(self.target/self.plan['entries'][1]['path']).getroot()
        self.assertEqual(episode.findtext('season'),'0');self.assertEqual(episode.findtext('episode'),'10')
        self.assertEqual(episode.findtext('showtitle'),'Title <& / dangerous>')
        self.assertEqual(episode.find("uniqueid[@type='tvdb']").text,'201')
        self.assertFalse(self.file.exists())
        self.assertEqual(list(self.target.rglob('*.partial')),[])

    def test_retry_reconciles_published_files_without_recopy(self):
        first=self.publisher.publish(self.plan);ino=self.video().stat().st_ino
        again=self.publisher.publish(self.plan)
        self.assertEqual(first,again);self.assertEqual(self.video().stat().st_ino,ino)

    def test_lost_confirmation_recovers_the_original_renamed_inode(self):
        self.journal.fail_confirm=True
        with self.assertRaises(OSError):self.publisher.publish(self.plan)
        inode=self.video().stat().st_ino
        self.assertEqual(next(iter(self.journal.rows.values()))['phase'],'PUBLISH_UNCERTAIN')
        self.publisher.publish(self.plan)
        self.assertEqual(self.video().stat().st_ino,inode)

    def test_publication_uses_standard_rename_without_linux_specific_syscall(self):
        from unittest.mock import patch
        with patch('ctypes.CDLL',side_effect=AssertionError('staging must use standard file operations')):
            self.publisher.publish(self.plan)
        self.assertEqual(self.video().read_bytes(),b'x'*100)
        self.assertFalse(self.file.exists())

    def test_failed_final_rename_recovers_recorded_partial_without_recopy(self):
        from unittest.mock import patch
        real_rename=os.rename
        failed=False
        def interrupted(source,target,**kwargs):
            nonlocal failed
            if str(target).endswith('.mkv') and not failed:
                failed=True
                raise OSError('interrupted final rename')
            return real_rename(source,target,**kwargs)
        # Force cross-filesystem MOVE, as on separate download/staging mounts.
        from src.nextgen import staging
        real_signature=staging.signature
        source_inode=self.file.stat().st_ino
        def other_device(info):
            value=real_signature(info)
            return (value[0]+1,*value[1:]) if value[1]==source_inode else value
        self.plan['source']['file'][0]+=1
        with patch('src.nextgen.staging.signature',side_effect=other_device):
            with patch('src.nextgen.staging.os.rename',side_effect=interrupted):
                with self.assertRaises(OSError):self.publisher.publish(self.plan)
            partial=self.video().parent/self.plan['entries'][0]['temporary']
            self.assertEqual(partial.read_bytes(),b'x'*100)
            inode=partial.stat().st_ino
            self.assertEqual(next(iter(self.journal.rows.values()))['phase'],'PUBLISH_UNCERTAIN')
            original_copy=self.publisher._copy
            def metadata_only(plan,entry,fd):
                if entry['role']=='media':raise AssertionError('recorded video must not be recopied')
                return original_copy(plan,entry,fd)
            with patch.object(self.publisher,'_copy',side_effect=metadata_only):
                self.publisher.publish(self.plan)
        self.assertEqual(self.video().stat().st_ino,inode)
        self.assertFalse(partial.exists())
        self.assertFalse(self.file.exists())

    def test_stale_nfs_signatures_do_not_block_new_publication(self):
        # Re-mounted shares can report different device/inode values for the
        # exact same source file and staging root. Do not re-hash the video.
        self.plan['root_identity']=[999999,888888]
        self.plan['source']['root']=[999999,777777]
        self.plan['source']['file'][:2]=[999999,666666]
        receipts=self.publisher.publish(self.plan)
        self.assertEqual(len(receipts),3)
        self.assertEqual(self.video().read_bytes(),b'x'*100)
        self.assertFalse(self.file.exists())

    def test_retried_publication_ignores_stale_artifact_inode(self):
        self.publisher.publish(self.plan)
        for record in self.journal.rows.values():
            record['root']=[999999,999999]
            record['file']=[888888,888888]
        receipts=self.publisher.publish(self.plan)
        self.assertEqual(len(receipts),3)
        self.assertEqual(self.video().read_bytes(),b'x'*100)

    def test_unknown_existing_video_is_never_overwritten_even_with_matching_bytes(self):
        target=self.video();target.parent.mkdir(parents=True);target.write_bytes(b'x'*100)
        inode=target.stat().st_ino
        with self.assertRaises(StagingReview):self.publisher.publish(self.plan)
        self.assertEqual(target.stat().st_ino,inode)

    def test_cleanup_retry_allows_source_permission_repair_without_recopy(self):
        from unittest.mock import patch
        from src.nextgen import staging
        real_signature=staging.signature
        source_inode=self.file.stat().st_ino
        def other_device(info):
            value=real_signature(info)
            return (value[0]+1,*value[1:]) if value[1]==source_inode else value
        self.plan['source']['file'][0]+=1
        with patch('src.nextgen.staging.signature',side_effect=other_device):
            with patch.object(self.publisher,'_remove_source',side_effect=PermissionError('delete denied')):
                with self.assertRaises(PermissionError):self.publisher.publish(self.plan)
            target_inode=self.video().stat().st_ino
            # chmod/ACL repair changes ctime, but leaves video bytes/mtime alone.
            self.file.chmod(0o660)
            self.assertEqual(self.file.stat().st_mtime_ns,self.plan['source']['file'][3])
            self.assertNotEqual(self.file.stat().st_ctime_ns,self.plan['source']['file'][4])
            with patch.object(self.publisher,'_copy',side_effect=AssertionError('must reuse published files')):
                receipts=self.publisher.publish(self.plan)
        self.assertEqual(len(receipts),3)
        self.assertEqual(self.video().stat().st_ino,target_inode)
        self.assertFalse(self.file.exists())

    def test_cleanup_still_rejects_source_modification(self):
        self.file.write_bytes(b'y'*100)
        self.file.chmod(0o660)
        expected=self.plan['source']['file']
        os.utime(self.file,ns=(expected[3]+1000000,expected[3]+1000000))
        with self.assertRaises(StagingReview):self.publisher._remove_source(self.plan)
        self.assertTrue(self.file.exists())

    def test_changed_source_and_destination_are_preserved_for_review(self):
        self.file.write_bytes(b'y'*100)
        with self.assertRaises(StagingReview):self.publisher.publish(self.plan)
        self.assertFalse(self.video().exists())

    def test_unregistered_partial_is_preserved_after_creation_receipt_crash(self):
        entry=self.plan['entries'][0];folder=self.video().parent;folder.mkdir(parents=True)
        temp=folder/entry['temporary'];temp.write_bytes(b'unknown')
        with self.assertRaises(StagingReview):self.publisher.publish(self.plan)
        self.assertEqual(temp.read_bytes(),b'unknown')

    def test_symlink_destination_and_overlapping_roots_fail(self):
        external=self.target.parent/'external';external.mkdir()
        series=self.target/Path(self.plan['entries'][0]['path']).parts[0];series.symlink_to(external,target_is_directory=True)
        with self.assertRaises(OSError):self.publisher.publish(self.plan)
        self.assertEqual(list(external.iterdir()),[])
        with self.assertRaises(StagingConfiguration):disjoint(str(self.target),(str(self.target.parent),))

    def test_changed_nfo_leaves_already_published_video(self):
        target=self.target/self.plan['entries'][1]['path'];target.parent.mkdir(parents=True);target.write_text('other metadata')
        with self.assertRaises(StagingReview) as raised:self.publisher.publish(self.plan)
        self.assertTrue(raised.exception.failed_check.endswith('NFO_SIZE'))
        self.assertEqual(raised.exception.failed_path,str(target))
        self.assertEqual(self.video().read_bytes(),b'x'*100)
        self.assertEqual(target.read_text(),'other metadata')

    def test_new_copy_preserves_source_and_does_not_rehash_video(self):
        from unittest.mock import patch
        from src.nextgen import staging
        self.plan['transfer']='COPY'
        real_digest=staging.digest_fd
        def metadata_only(fd,size,flag):
            if size==100:raise AssertionError('must not re-read the video')
            return real_digest(fd,size,flag)
        with patch('src.nextgen.staging.digest_fd',side_effect=metadata_only):
            receipts=self.publisher.publish(self.plan)
        self.assertEqual(self.video().read_bytes(),self.file.read_bytes())
        self.assertNotEqual(self.video().stat().st_ino,self.file.stat().st_ino)
        self.assertFalse(receipts[0]['hash_checked'])
        self.assertTrue(all(r['hash_checked'] for r in receipts[1:]))
        self.assertTrue(self.file.exists())

    def test_copy_succeeds_despite_changed_source_inode_and_device(self):
        """W-07, W-10: Staging copy succeeds when source file inode/device differ (NFS remount)."""
        self.plan['transfer']='COPY'
        orig_file = self.plan['source']['file']
        # Simulate remount: different device and inode, but identical size and mtime
        self.plan['source']['file'] = [999999, 888888, orig_file[2], orig_file[3], orig_file[4]]
        receipts = self.publisher.publish(self.plan)
        self.assertEqual(self.video().read_bytes(), self.file.read_bytes())
        self.assertEqual(len(receipts), 3)

    def test_copy_retry_discards_incomplete_owned_temporary(self):
        from unittest.mock import patch
        self.plan['transfer']='COPY'
        original=self.publisher._copy
        once=[True]
        def interrupted(plan,entry,fd):
            if entry['role']=='media' and once[0]:
                once[0]=False
                os.write(fd,b'incomplete')
                raise OSError('interrupted copy')
            return original(plan,entry,fd)
        with patch.object(self.publisher,'_copy',side_effect=interrupted):
            with self.assertRaises(OSError):self.publisher.publish(self.plan)
        temporary=self.video().parent/self.plan['entries'][0]['temporary']
        self.assertEqual(temporary.read_bytes(),b'incomplete')
        self.publisher.publish(self.plan)
        self.assertEqual(self.video().read_bytes(),self.file.read_bytes())
        self.assertFalse(temporary.exists())

    def test_copy_retry_recovers_short_uncertain_temporary(self):
        from unittest.mock import patch
        self.plan['transfer']='COPY'
        real_rename=os.rename
        once=[True]
        def interrupted(source,target,**kw):
            if str(target).endswith('.mkv') and once[0]:
                once[0]=False
                raise OSError('rename interrupted')
            return real_rename(source,target,**kw)
        with patch('src.nextgen.staging.os.rename',side_effect=interrupted):
            with self.assertRaises(OSError):self.publisher.publish(self.plan)
        temporary=self.video().parent/self.plan['entries'][0]['temporary']
        self.assertEqual(temporary.stat().st_size,100)
        temporary.write_bytes(b'short')
        self.publisher.publish(self.plan)
        self.assertEqual(self.video().read_bytes(),self.file.read_bytes())
        self.assertFalse(temporary.exists())

    def test_published_episode_nfo_recreated_without_moving_video_again(self):
        self.publisher.publish(self.plan)
        video_inode=self.video().stat().st_ino
        nfo=self.target/self.plan['entries'][1]['path']
        nfo.write_text('stale metadata')
        self.publisher.publish(self.plan)
        self.assertEqual(nfo.read_text(),self.plan['entries'][1]['text'])
        self.assertEqual(self.video().stat().st_ino,video_inode)
        nfo.unlink()
        self.publisher.publish(self.plan)
        self.assertEqual(nfo.read_text(),self.plan['entries'][1]['text'])

    def test_shared_series_nfo_is_regenerated(self):
        self.publisher.publish(self.plan)
        nfo=self.target/self.plan['entries'][2]['path']
        nfo.write_text('wrong series metadata')
        self.publisher.publish(self.plan)
        self.assertEqual(nfo.read_text(),self.plan['entries'][2]['text'])

    def test_cancel_during_copy_does_not_publish(self):
        import asyncio
        self.flag.set()
        with self.assertRaises(asyncio.CancelledError):self.publisher.publish(self.plan)
        self.assertFalse(self.video().exists())

    def test_video_staging_never_hashes_or_reads_back_destination(self):
        from unittest.mock import patch
        from src.nextgen import staging
        real_digest=staging.digest_fd
        def metadata_only(fd,size,flag):
            if size==100:raise AssertionError('Video destination must not be read back for hashing')
            return real_digest(fd,size,flag)
        with patch('src.nextgen.staging.digest_fd',side_effect=metadata_only):self.publisher.publish(self.plan)
        self.assertFalse(self.file.exists())
        self.assertEqual(self.video().read_bytes(),b'x'*100)

    def test_cross_filesystem_fallback_streams_once_and_removes_source(self):
        # Force the device comparison to choose its cross-filesystem path without
        # requiring a second mounted filesystem in an offline case.
        from unittest.mock import patch
        from src.nextgen import staging
        real_signature=staging.signature
        source_inode=self.file.stat().st_ino
        def other_device(info):
            value=real_signature(info)
            return (value[0]+1,*value[1:]) if value[1]==source_inode else value
        self.plan['source']['file'][0]+=1
        with patch('src.nextgen.staging.signature',side_effect=other_device):
            self.publisher.publish(self.plan)
        self.assertFalse(self.file.exists())
        self.assertNotEqual(self.video().stat().st_ino,source_inode)
        self.assertEqual(self.video().read_bytes(),b'x'*100)
