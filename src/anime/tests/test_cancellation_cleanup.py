"""Cancellation cleanup deletes what the work owns; real local files, fake qBittorrent."""
from pathlib import Path
import tempfile
import unittest

from src.anime.acquisition.submission import ClientUnavailable, ClientAuthenticationFailed, Observation
from src.anime.cancellation_service import CancellationService
from src.anime import cancel_cleanup
from src.anime.tests import test_torrent_transfer as fixture
from src.anime.tests.test_torrent_submission import Secrets


class CancellationCleanupTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.TorrentTransferTests.asyncSetUp
    rows=fixture.TorrentTransferTests.rows
    source=fixture.TorrentTransferTests.source
    worker=fixture.TorrentTransferTests.worker
    queued=fixture.TorrentTransferTests.queued

    async def setup_download(self):
        worker=self.worker()
        self.store.update({'storage':{'download_root':str(self.root/'downloads')}},expected_revision=2)
        await self.queued(worker);await worker.attempt()
        self.job=self.rows("SELECT job_id FROM jobs WHERE kind='DOWNLOAD_TORRENT'")[0][0]
        self.payload_root=Path(self.snapshot.observation.save_path)
        self.payload_root.mkdir(parents=True)
        self.video=self.payload_root/self.snapshot.observation.files[0].path
        self.video.write_bytes(b'v'*100)
        (self.payload_root/'leftover.part').write_bytes(b'x')  # Anything in ad-* goes too.
        self.deleted=[]
        return worker

    def service(self,mode='normal'):
        test=self
        class FakeClient:
            def __init__(self,*args):pass
            def login(self):
                if mode=='auth':raise ClientAuthenticationFailed()
            def delete_owned(self,info_hash,marker,save_path):
                if mode=='down':raise ClientUnavailable()
                if mode in ('foreign','moved'):return mode
                test.deleted.append((info_hash,marker));return 'deleted'
        return CancellationService(self.store.read(),Secrets(),client_factory=FakeClient)

    async def test_cancel_deletes_torrent_and_whole_download_folder_and_frees_slot(self):
        await self.setup_download()
        self.assertEqual(self.service().cancel(self.job)['cleanup_state'],'COMPLETE')
        self.assertEqual(len(self.deleted),1)
        self.assertFalse(self.payload_root.exists())
        self.assertEqual(self.rows("SELECT state FROM jobs WHERE kind='DOWNLOAD_TORRENT'"),[('CANCELED',)])
        self.assertEqual(self.rows('SELECT state FROM episode_reservations'),[('RELEASED',)])

    async def test_qbittorrent_down_waits_then_finishes(self):
        await self.setup_download()
        self.assertEqual(self.service('down').cancel(self.job)['cleanup_state'],'PENDING')
        self.assertTrue(self.video.exists())
        self.assertEqual(self.service().cleanup(self.job)['cleanup_state'],'COMPLETE')
        self.assertFalse(self.payload_root.exists())

    async def test_missing_folder_counts_as_done(self):
        await self.setup_download()
        import shutil;shutil.rmtree(self.payload_root)
        self.assertEqual(self.service().cancel(self.job)['cleanup_state'],'COMPLETE')

    async def test_foreign_torrent_keeps_its_folder(self):
        await self.setup_download()
        self.assertEqual(self.service('foreign').cancel(self.job)['cleanup_state'],'COMPLETE')
        self.assertTrue(self.video.exists())

    async def test_moved_torrent_deletes_nothing_and_stops_for_review(self):
        await self.setup_download()
        result=self.service('moved').cancel(self.job)
        self.assertEqual(result['cleanup_state'],'REVIEW')
        self.assertIn('TORRENT_MOVED',result['failed_check'])
        self.assertTrue(self.video.exists())

    async def test_wrong_password_stops_for_review_naming_the_cause(self):
        await self.setup_download()
        result=self.service('auth').cancel(self.job)
        self.assertEqual(result['cleanup_state'],'REVIEW')
        self.assertIn('login',result['failed_check'])
        self.assertTrue(self.video.exists())


class OwnershipGuardTests(unittest.TestCase):
    def test_only_a_folder_named_for_its_marker_is_deleted(self):
        marker='a'*32
        with tempfile.TemporaryDirectory() as root:
            other=Path(root)/'downloads';other.mkdir();(other/'keep.mkv').write_bytes(b'x')
            cancel_cleanup.remove_download_folder(str(other),marker)
            self.assertTrue((other/'keep.mkv').exists())
            mine=Path(root)/('ad-'+marker);mine.mkdir();(mine/'v.mkv').write_bytes(b'x')
            cancel_cleanup.remove_download_folder(str(mine),marker)
            self.assertFalse(mine.exists())
            cancel_cleanup.remove_download_folder(str(mine),marker)  # Already gone: fine.

    def test_files_are_deleted_only_when_tag_and_folder_agree(self):
        from src.anime.torrent_client.qbittorrent import Client
        client=Client.__new__(Client);client.protocol=object();sent=[]
        client._request=lambda path,**kw:sent.append(path) or (b'',{})
        h='b'*40;marker='c'*32;folder='/d/ad-'+marker
        def seen(tags,path=folder):return Observation(h,'stoppedDL',path,'anime',tags,False,())
        cases=((None,'absent'),(seen(('sonarr',)),'foreign'),
               (seen(('ad-'+marker,),'/elsewhere'),'moved'),(seen(('ad-'+marker,),folder+'/'),'deleted'))
        for observed,expected in cases:
            client.inspect=lambda *a,o=observed,**k:o
            self.assertEqual(client.delete_owned(h,marker,folder),expected)
        self.assertEqual(sent,['torrents/delete'])

    def test_staging_outside_its_root_or_in_keep_is_never_touched(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging=Path(tmp)/'staging';keep=Path(tmp)/'keep'
            season=staging/'Show'/'Season 01';season.mkdir(parents=True);keep.mkdir()
            video=season/'Show - S01E01.mkv';video.write_bytes(b'v')
            (staging/'Show'/'tvshow.nfo').write_bytes(b'n')
            outside=Path(tmp)/'other.mkv';outside.write_bytes(b'o')
            kept=keep/'k.mkv';kept.write_bytes(b'k')
            cancel_cleanup.remove_staging(
                [(str(video),str(staging)),(str(outside),str(staging)),(str(kept),str(staging))],
                [(str(staging/'Show'),str(staging))],keep_root=str(keep))
            self.assertFalse((staging/'Show').exists())  # Only tvshow.nfo was left.
            self.assertTrue(outside.exists());self.assertTrue(kept.exists())
