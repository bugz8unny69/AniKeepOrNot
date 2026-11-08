"""Offline regression: NFO title repairs preserve staging/cleanup proofs."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
import xml.etree.ElementTree as ET

from fixes.nfo_title_repair import repair, relabel
from src.nextgen.configuration_schema import ConfigurationSnapshot
from src.nextgen.acquisition.media import signature
from src.nextgen.staging import nfo, specification


class NfoTitleRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'staged'
        self.root.mkdir()
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE identified_anime(anidb_id INTEGER PRIMARY KEY,tvdb_series_id INTEGER,title TEXT);
            CREATE TABLE anime_title_metadata(anidb_id INTEGER PRIMARY KEY,english_title TEXT,romaji_title TEXT);
            CREATE TABLE staging_publications(plan_id INTEGER PRIMARY KEY,manifest_json TEXT,receipts_json TEXT,state TEXT);
            CREATE TABLE acquisition_plans(plan_id INTEGER PRIMARY KEY,reservation_id INTEGER);
            CREATE TABLE episode_reservations(reservation_id INTEGER PRIMARY KEY,anidb_id INTEGER);
            CREATE TABLE batch_staging_publications(plan_id INTEGER,file_index INTEGER,manifest_json TEXT,receipts_json TEXT,state TEXT);
            CREATE TABLE batch_episode_reservations(plan_id INTEGER,file_index INTEGER,anidb_id INTEGER);
            CREATE TABLE staging_cleanup_artifacts(artifact_id INTEGER PRIMARY KEY,
                file_device INTEGER,file_inode INTEGER,phase TEXT);
            INSERT INTO identified_anime VALUES(12,34,'Japanese Romaji');
            INSERT INTO anime_title_metadata VALUES(12,'English Name','Japanese Romaji');
            INSERT INTO acquisition_plans VALUES(22,33);
            INSERT INTO episode_reservations VALUES(33,12);
        """)
        self.folder = self.root / 'Japanese Romaji [tvdbid-34] [anidbid-12]'
        season = self.folder / 'Season 01'
        season.mkdir(parents=True)
        self.video = season / 'Japanese Romaji - S01E01.mkv'
        self.video.write_bytes(b'media')
        self.episode = self.video.with_suffix('.nfo')
        self.series = self.folder / 'tvshow.nfo'
        self.episode.write_bytes(nfo('episodedetails',
            [('title', 'Episode 1'), ('showtitle', 'Japanese Romaji'),
             ('season', 1), ('episode', 1)], [('tvdb', 567), ('anidb', 8)]))
        self.series.write_bytes(nfo('tvshow', [('title', 'Japanese Romaji')],
                                     [('tvdb', 34), ('anidb', 12)]))
        def entry(role, path, artifact):
            raw = path.read_bytes()
            sig = list(signature(path.stat()))
            rel = str(path.relative_to(self.root))
            return ({'role':role,'path':rel,'size':len(raw),
                     'sha256':hashlib.sha256(raw).hexdigest(),
                     'text':raw.decode() if role!='media' else None},
                    {'role':role,'path':rel,'file':sig,'root':
                     [path.parent.stat().st_dev,path.parent.stat().st_ino],
                     'sha256':hashlib.sha256(raw).hexdigest(),
                     'artifact':artifact,'hash_checked':True})
        pairs=[entry('media',self.video,None),entry('episode',self.episode,11),
               entry('series',self.series,12)]
        self.manifest={'root':str(self.root),'root_identity':
                       [self.root.stat().st_dev,self.root.stat().st_ino],
                       'entries':[x[0] for x in pairs]}
        self.receipts=[x[1] for x in pairs]
        for index, path in [(11,self.episode),(12,self.series)]:
            stat=path.stat()
            self.db.execute('INSERT INTO staging_cleanup_artifacts VALUES(?,?,?,\'PUBLISHED\')',
                            (index,stat.st_dev,stat.st_ino))
        self.db.execute("INSERT INTO staging_publications VALUES(?,?,?,'PUBLISHED')",
                        (22,json.dumps(self.manifest),json.dumps(self.receipts)))
        self.db.commit()
        self.config=ConfigurationSnapshot(1,{'storage':{'user_evaluation_root':str(self.root)}})

    def test_new_staging_nfo_display_title_does_not_rename_files(self):
        intent=SimpleNamespace(options=SimpleNamespace(download_root='/source'))
        evidence={'path':'video.mkv','sha256':'a'*64,'file':[1,2,500]}
        plan=specification(intent,evidence,(12,8,34,1,1,567,'Japanese Romaji'),
                           str(self.root),1,display_title='English Name')
        self.assertIn('Japanese Romaji',plan['entries'][0]['path'])
        episode=ET.fromstring(plan['entries'][1]['text'])
        show=ET.fromstring(plan['entries'][2]['text'])
        self.assertEqual(episode.findtext('showtitle'),'English Name')
        self.assertEqual(show.findtext('title'),'English Name')
        self.assertEqual(episode.findtext('uniqueid'),'567')

    def test_repair_reconciles_nfo_and_cleanup_receipts_without_touching_video(self):
        old_video=self.video.read_bytes()
        preview=repair(self.db,self.config)
        self.assertEqual(preview['changed'],2)
        self.assertEqual(self.series.read_bytes(),
                         self.manifest['entries'][2]['text'].encode())
        done=repair(self.db,self.config,apply=True)
        self.assertEqual(done['changed'],2)
        self.assertEqual(ET.fromstring(self.series.read_bytes()).findtext('title'),'English Name')
        self.assertEqual(ET.fromstring(self.episode.read_bytes()).findtext('showtitle'),'English Name')
        self.assertEqual(self.video.read_bytes(),old_video)
        row=self.db.execute('SELECT manifest_json,receipts_json FROM staging_publications').fetchone()
        plan,receipts=map(json.loads,row)
        self.assertEqual(plan['entries'][0],self.manifest['entries'][0])
        self.assertEqual(receipts[0],self.receipts[0])
        for index,path in [(1,self.episode),(2,self.series)]:
            self.assertEqual(receipts[index]['file'],list(signature(path.stat())))
            self.assertEqual(plan['entries'][index]['sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
            artifact=self.db.execute('SELECT file_device,file_inode FROM staging_cleanup_artifacts '
                                     'WHERE artifact_id=?',(11 if index==1 else 12,)).fetchone()
            self.assertEqual(artifact,(path.stat().st_dev,path.stat().st_ino))
        self.assertEqual(repair(self.db,self.config,apply=True)['changed'],0)
        # Post-Keep/Reject cleanup must still accept the repaired receipts.
        from src.nextgen.storage.review_decisions import remove_selected
        remove_selected(row[0],row[1],lambda:None,'/unrelated-permanent-library')
        self.assertFalse(self.video.exists())
        self.assertFalse(self.episode.exists())
        self.assertTrue(self.series.exists())

    def test_shared_series_nfo_survives_historical_nfs_identity_changes(self):
        # Three episode publications reuse the same tvshow.nfo, with root and
        # file inode data recorded before/after an NFS remount. This must not
        # block a title-only repair.
        old_manifest=json.loads(json.dumps(self.manifest))
        old_receipts=json.loads(json.dumps(self.receipts))
        for plan_id,root_device in ((23,12345),(24,98765)):
            manifest=json.loads(json.dumps(old_manifest))
            receipts=json.loads(json.dumps(old_receipts))
            manifest['root_identity']=[root_device,999]
            receipts[2]['root']=[root_device,999]
            receipts[2]['file'][0]=root_device
            receipts[2]['file'][1]=888
            self.db.execute('INSERT INTO acquisition_plans VALUES(?,33)',(plan_id,))
            self.db.execute("INSERT INTO staging_publications VALUES(?,?,?,'PUBLISHED')",
                            (plan_id,json.dumps(manifest),json.dumps(receipts)))
        self.db.commit()
        self.assertEqual(repair(self.db,self.config)['changed'],2)
        self.assertEqual(repair(self.db,self.config,apply=True)['changed'],2)
        self.assertEqual(repair(self.db,self.config)['changed'],0)
        rows=self.db.execute('SELECT manifest_json,receipts_json FROM staging_publications').fetchall()
        self.assertEqual(len(rows),3)
        for manifest,receipts in rows:
            self.assertEqual(json.loads(manifest)['entries'][2]['text'],
                             self.series.read_text())
            self.assertEqual(json.loads(receipts)[2]['sha256'],
                             hashlib.sha256(self.series.read_bytes()).hexdigest())

    def test_cleanup_of_staged_nfo_ignores_saved_nfs_device_inode(self):
        repair(self.db,self.config,apply=True)
        row=self.db.execute('SELECT manifest_json,receipts_json FROM staging_publications').fetchone()
        manifest,receipts=map(json.loads,row)
        manifest['root_identity']=[9999,8888]
        receipts[1]['root']=[9999,8888]
        receipts[1]['file'][:2]=[9999,8888]
        from src.nextgen.storage.review_decisions import remove_selected
        remove_selected(json.dumps(manifest),json.dumps(receipts),lambda:None,
                        '/unrelated-permanent-library')
        self.assertFalse(self.video.exists())
        self.assertFalse(self.episode.exists())
        self.assertTrue(self.series.exists())

    def test_foreign_nfo_changes_are_never_overwritten(self):
        self.series.write_text('<tvshow><title>Externally Modified</title></tvshow>')
        with self.assertRaises(ValueError):
            repair(self.db,self.config,apply=True)
        self.assertIn('Externally Modified',self.series.read_text())

    def test_missing_episode_nfo_from_completed_keep_is_skipped(self):
        self.episode.unlink()
        summary=repair(self.db,self.config,apply=True)
        self.assertEqual(summary['missing'],1)
        self.assertEqual(summary['changed'],1)

    def test_nfo_title_change_never_changes_provider_ids(self):
        raw=self.series.read_text()
        updated=relabel(raw,'series','English Name')
        fields=ET.fromstring(updated)
        self.assertEqual(fields.findtext('title'),'English Name')
        self.assertEqual([(e.attrib['type'],e.text) for e in fields.findall('uniqueid')],
                         [('tvdb','34'),('anidb','12')])


if __name__ == '__main__':
    unittest.main()
