"""Manual episode submission regression cases (offline; never run by CI here)."""
import base64
import hashlib
import json
import unittest

from src.nextgen.storage.sqlite import connect
from src.nextgen.storage.manual_release import (
    submit, metainfo_for, ManualSubmissionInvalid, ManualSubmissionUnavailable)
from src.nextgen.tests.test_management_actions import reserve_without_metadata
from src.nextgen.tests.test_api_v1 import ApiV1Tests, ADMIN, WEB


def bencode(obj):
    if type(obj) is bytes:
        return str(len(obj)).encode()+b':'+obj
    if type(obj) is int:
        return b'i'+str(obj).encode()+b'e'
    if type(obj) is dict:
        return b'd'+b''.join(bencode(k)+bencode(obj[k]) for k in sorted(obj))+b'e'
    raise ValueError('unsupported BEP3 test value')


class ManualReleaseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=ApiV1Tests.asyncSetUp
    rows=ApiV1Tests.rows
    request=ApiV1Tests.request
    release=ApiV1Tests.release

    async def seed(self):
        await reserve_without_metadata(self)
        aid,season,episode=self.rows(
            'SELECT anidb_id,tvdb_season,tvdb_episode FROM episode_reservations')[0]
        return aid,season,episode,f'Test Anime S{season:02d}E{episode:02d} 1080p'

    async def test_separate_candidates_are_auditable_and_do_not_take_second_reservation(self):
        aid,season,episode,title=await self.seed()
        first=submit(self.database,aid,season,episode,release_name=title,
                     magnet_uri='magnet:?xt=urn:btih:'+'a'*40)
        second=submit(self.database,aid,season,episode,release_name=title,
                      magnet_uri='magnet:?xt=urn:btih:'+'b'*40)
        self.assertNotEqual(first['job_id'],second['job_id'])
        self.assertFalse(first['reused'])
        self.assertFalse(second['reused'])
        again=submit(self.database,aid,season,episode,release_name=title,
                     magnet_uri='magnet:?xt=urn:btih:'+'a'*40)
        self.assertTrue(again['reused'])
        self.assertEqual(again['job_id'],first['job_id'])
        self.assertEqual(self.rows('SELECT count(*) FROM episode_reservations'),[(1,)])
        self.assertEqual(self.rows('SELECT count(*) FROM manual_release_submissions'),[(2,)])
        self.assertEqual(self.rows(
            "SELECT count(*) FROM jobs WHERE kind='VERIFY_RELEASE' AND event_id IN "
            '(SELECT event_id FROM manual_release_submissions)'),[(2,)])
        path=f'/api/v1/jobs?anidb_id={aid}&season={season}&episode={episode}'
        status,listing,_=await self.request('GET',path,token=ADMIN)
        self.assertEqual(status,200)
        ids={item['job_id'] for item in listing['data']['items']}
        self.assertTrue({first['job_id'],second['job_id']}.issubset(ids))
        status,details,_=await self.request('GET',
            f'/api/v1/intake/series/{aid}',token=ADMIN)
        self.assertEqual(status,200)
        target=next(item for item in details['data']['missing']
                    if item['season']==season and item['episode']==episode)
        self.assertEqual(target['manual_submission_count'],2)

    async def test_wrong_episode_or_malformed_magnet_is_rejected(self):
        aid,season,episode,title=await self.seed()
        with self.assertRaises(ManualSubmissionInvalid):
            submit(self.database,aid,season,episode,release_name=
                   f'Test Anime S{season:02d}E{episode+1:02d}',
                   magnet_uri='magnet:?xt=urn:btih:'+'c'*40)
        with self.assertRaises(ManualSubmissionInvalid):
            submit(self.database,aid,season,episode,release_name=title,
                   magnet_uri='http://not-a-magnet/')
        self.assertEqual(self.rows('SELECT count(*) FROM manual_release_submissions'),[(0,)])

    async def test_torrent_bytes_are_preserved_for_exact_existing_submission(self):
        aid,season,episode,title=await self.seed()
        filename=(f'Test Anime S{season:02d}E{episode:02d}.mkv').encode()
        raw=bencode({b'info':{b'length':100,b'name':filename,
                        b'piece length':16384,b'pieces':b'x'*20}})
        value=submit(self.database,aid,season,episode,release_name=title,
                     torrent_b64=base64.b64encode(raw).decode())
        eid=value['event_id']
        with connect(self.database) as db:
            row=db.execute('SELECT info_hash,source FROM manual_release_submissions '
                           'WHERE event_id=?',(eid,)).fetchone()
        self.assertEqual(row[1],'torrent')
        self.assertEqual(metainfo_for(self.database,eid,row[0]),raw)
        with self.assertRaises(ManualSubmissionInvalid):
            wrong=bencode({b'info':{b'length':100,b'name':b'Other Show S01E01.mkv',
                             b'piece length':16384,b'pieces':b'x'*20}})
            submit(self.database,aid,season,episode,release_name=title,
                   torrent_b64=base64.b64encode(wrong).decode())

    async def test_api_request_is_authenticated_and_validated(self):
        aid,season,episode,title=await self.seed()
        url=f'/api/v1/intake/series/{aid}/episodes/{season}/{episode}/submit'
        body={'release_name':title,'magnet_uri':'magnet:?xt=urn:btih:'+'e'*40}
        self.assertEqual((await self.request('POST',url,body,token=WEB))[0],401)
        status,result,_=await self.request('POST',url,body,token=ADMIN)
        self.assertEqual(status,202)
        self.assertIn('job_id',result['data'])
        status,reused,_=await self.request('POST',url,body,token=ADMIN)
        self.assertEqual(status,200)
        self.assertTrue(reused['data']['reused'])
