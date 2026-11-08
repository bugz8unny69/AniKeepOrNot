"""Offline declared-release/provider/worker integration; authored, not executed."""
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest

from src.nextgen.configuration import ConfigurationStore
from src.nextgen.configuration_schema import ConfigurationSnapshot
from src.nextgen.job_contracts import Outcome, Registry, Policy
from src.nextgen.jobs.verify_release_job import registration
from src.nextgen.providers.errors import ProviderInvalidResponse
from src.nextgen.providers.http import Options, Credentials
from src.nextgen.providers.release_mapping import declared_episode, forward_mappings
from src.nextgen.batch.recognition import release_scope_hint
from src.nextgen.providers.releases import make_verifier
from src.nextgen.release_contracts import VerifiedRelease, ReleaseInput, ReleaseNotReady, ReleaseConflict
from src.nextgen.storage.job_queue import LeaseLost
from src.nextgen.storage.releases import load_release
from src.nextgen.storage.sqlite import connect, initialize, save_identity, accept_event_and_job
from src.nextgen.worker import Worker

MAPPING=b'<anime-list><anime anidbid="123" tvdbid="456" defaulttvdbseason="2" episodeoffset="12"/></anime-list>'


class ReleaseMappingTests(unittest.TestCase):
    def test_reported_psyren_title_is_a_clear_single_episode(self):
        title='[ToonsHub] PSYREN S01E02 1080p CR WEB-DL DUAL AAC2.0 H.264 (PSYЯEN, Dual-Audio, Multi-Subs)'
        self.assertEqual(declared_episode(title),('tvdb',1,2))

    def test_matching_season_name_alongside_sxxeyy_is_clear(self):
        for title in ("[AnoZu] Sasaki and Peeps S02E02 1080p CR WEB-DL Dual-Audio DDP 2.0 H.264 | Sasaki to Pii-chan Season 2",
                      "[AnoZu] The Detective Is Already Dead S02E02 1080p CR WEB-DL Dual-Audio DDP 2.0 H.264 | Tantei wa Mou, Shindeiru. Season 2"):
            with self.subTest(title=title):self.assertEqual(declared_episode(title),('tvdb',2,2))
        for title in ("Show S02E02 | Show Season 3","Show S02E02 S03"):
            with self.subTest(title=title),self.assertRaises(ReleaseNotReady):declared_episode(title)

    def test_type_words_inside_a_parenthesized_alternate_title_are_part_of_the_name(self):
        self.assertEqual(declared_episode("[Judas] Kikansha no Mahou wa Tokubetsu desu (A Returner's Magic Should Be Special) "
                                          "- S02E05 [1080p][HEVC x265 10bit][Multi-Subs] (Weekly)"),('tvdb',2,5))
        for title in ("[Group] Test Anime (Batch) [1080p]","[Group] Test Anime (OVA) - 01","[Group] Test Anime OVA - 01",
                      "[Group] Test Anime - Special 01","[Group] Test Anime - 01 [Batch]","[Group] Test Anime S01E03 NCOP"):
            with self.subTest(title=title),self.assertRaises(ReleaseNotReady) as raised:declared_episode(title)
            self.assertEqual(raised.exception.reason_code,'RELEASE_TYPE_UNSUPPORTED')

    def test_special_title_and_actual_season_zero_are_distinct(self):
        for title, episode in (
            ("[ToonsHub] A Returners Magic Should Be Special S02E04 1080p CR WEB-DL DUAL AAC2.0 H.264", 4),
            ("A Returners Magic Should Be Special S02E16 1080p CR WEB-DL DUAL", 16),
        ):
            with self.subTest(title=title):
                self.assertEqual(declared_episode(title), ("tvdb", 2, episode))
                self.assertEqual(release_scope_hint(title).kind, "single_episode")
        for name in ("[Group] Show S00E01 [Special]",
                     "[Group] Show S00E01 OVA"):
            with self.subTest(name=name):
                self.assertEqual(declared_episode(name), ("tvdb", 0, 1))
                self.assertEqual(release_scope_hint(name).kind, "special")
        for title, episode in (
            ("[ToonsHub] My Happy Marriage S00E02 Special Part 1 1080p", 2),
            ("My Happy Marriage S00E03 Special Part 2", 3),
            ("My Happy Marriage S00E04 Special Part 3", 4),
        ):
            with self.subTest(title=title):
                # A "Part N" label can describe an official S00 episode.
                # Only the provider mapping can verify its identity.
                self.assertEqual(declared_episode(title), ("tvdb", 0, episode))
                self.assertEqual(release_scope_hint(title).kind, "special")
        for title in ("[Group] Show S01E04 [Special]",
                      "[Group] Show S01E04 OVA",
                      "[Group] Show - Special 01"):
            with self.subTest(title=title), self.assertRaises(ReleaseNotReady):
                declared_episode(title)

    def test_single_declarations_do_not_apply_legacy_user_evaluation_window_or_season_one_rule(self):
        self.assertEqual(declared_episode('[Group] Test - 12 [1080p]'),('anidb',None,12))
        self.assertEqual(declared_episode('Test S02E13'),('tvdb',2,13))
        self.assertEqual(declared_episode('Test S00E01'),('tvdb',0,1))
        for title in ('Test Batch E01','Test E01-E02','Test E01E02','Test S01E01E02',
                      'Test - 01 & 02','Test EP01EP02','Test EP01.5','Test E01.5','Test E00','Test S02','Test Movie - 01','Test E01 E02','Test EP01a','Test S01E01a','Test - -01'):
            with self.subTest(title=title),self.assertRaises(ReleaseNotReady): declared_episode(title)

    def test_optional_range_offset_is_zero_and_explicit_offsets_take_precedence(self):
        base=(b'<anime-list><anime anidbid="123" tvdbid="456">'
              b'<mapping-list><mapping anidbseason="1" tvdbseason="2" '
              b'start="1" end="2"/></mapping-list></anime></anime-list>')
        blank=base.replace(b'end="2"',b'end="2" offset=""')
        for payload in (base,blank):
            self.assertEqual(forward_mappings(payload,123,456,{1:987,2:988}),
                             {1:(2,1),2:(2,2)})
        shifted=base.replace(b'end="2"',b'end="2" offset="5"')
        self.assertEqual(forward_mappings(shifted,123,456,{1:987,2:988}),
                         {1:(2,6),2:(2,7)})
        invalid=base.replace(b'end="2"',b'end="2" offset="oops"')
        with self.assertRaises(ReleaseNotReady):
            forward_mappings(invalid,123,456,{1:987})
        # A missing default season is still not proof of mapping.
        no_season=b'<anime-list><anime anidbid="123" tvdbid="456"/></anime-list>'
        self.assertEqual(forward_mappings(no_season,123,456,{1:987}),{})

    def test_one_piece_1157_open_ended_tvdb_range(self):
        # Anime-Lists: AniDB 69 uses absolute numbering; TMDB rows are separate.
        mapping=(b'<anime-list><anime anidbid="69" tvdbid="81797" defaulttvdbseason="a">'
                 b'<mapping-list>'
                 b'<mapping anidbseason="1" tmdbseason="22" start="1089" end="1155" offset="-1088"/>'
                 b'<mapping anidbseason="1" tvdbseason="22" start="1086" end="1155" offset="-1085"/>'
                 b'<mapping anidbseason="1" tvdbseason="23" start="1156" offset="-1155"/>'
                 b'</mapping-list></anime></anime-list>')
        self.assertEqual(forward_mappings(mapping,69,81797,{1155:501,1156:502,1157:503}),
                         {1155:(22,70),1156:(23,1),1157:(23,2)})

    def test_soul_eater_range_is_a_season_pack_hint(self):
        hint=release_scope_hint('[figged] Soul Eater S01 E27-E51 (BluRay 720p)')
        self.assertEqual((hint.kind,hint.season_hint,hint.episode_hint),('season_pack',1,27))
        self.assertEqual(release_scope_hint('Show S01E27').kind,'single_episode')
        self.assertEqual(release_scope_hint('Show S01E51-E27').kind,'ambiguous')

    def test_explicit_offset_remap_and_season_zero_supported_without_guessing(self):
        self.assertEqual(forward_mappings(MAPPING,123,456,{1:987,2:988}),{1:(2,13),2:(2,14)})
        custom=MAPPING.replace(b'/></anime-list>',b'><mapping-list><mapping anidbseason="1" tvdbseason="0">1-5;</mapping></mapping-list></anime></anime-list>')
        self.assertEqual(forward_mappings(custom,123,456,{1:987})[1],(0,5))
        missing=MAPPING.replace(b' episodeoffset="12"',b'')
        blank=MAPPING.replace(b'episodeoffset="12"',b'episodeoffset=""')
        # Anime-Lists explicitly defines absent/empty episodeoffset as zero.
        for payload in (missing,blank):
            with self.subTest(payload=payload):
                self.assertEqual(forward_mappings(payload,123,456,{1:987,2:988}),
                                 {1:(2,1),2:(2,2)})
        with self.assertRaises(ReleaseConflict): forward_mappings(MAPPING,123,999,{1:987})
        split=custom.replace(b'1-5;',b'1-5+6;')
        with self.assertRaises(ReleaseNotReady): forward_mappings(split,123,456,{1:987})


class ReleaseVerifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_specific_episode_blockers_are_preserved(self):
        cases=(
            ('RELEASE_ANIDB_EPISODE_MISSING',b'{"release_name":"Test - 99"}',MAPPING,((100,2,13),)),
            ('RELEASE_EPISODE_MAPPING_MISSING',self.source.raw_body,MAPPING.replace(b' defaulttvdbseason="2"',b''),((100,2,13),)),
            ('RELEASE_MAPPING_MISSING',self.source.raw_body,b'<anime-list/>',((100,2,13),)),
            ('RELEASE_TVDB_EPISODE_MISSING',self.source.raw_body,MAPPING,((100,2,14),)),
            ('RELEASE_TYPE_UNSUPPORTED',b'{"release_name":"Test Batch"}',MAPPING,((100,2,13),)),
            ('RELEASE_TITLE_UNCLEAR',b'{"release_name":"Test"}',MAPPING,((100,2,13),)),
            ('RELEASE_MAPPING_UNSUPPORTED',self.source.raw_body,b'<anime-list><anime anidbid="123" tvdbid="456"><mapping-list><mapping anidbseason="1" tvdbseason="a"/></mapping-list></anime></anime-list>',((100,2,13),)),
            ('RELEASE_EPISODE_MAPPING_AMBIGUOUS',self.source.raw_body,b'<anime-list><anime anidbid="123" tvdbid="456"><mapping-list><mapping anidbseason="1" tvdbseason="2">1-13;2-13;</mapping></mapping-list></anime></anime-list>',((100,2,13),)),
        )
        for code,body,mapping,records in cases:
            with self.subTest(code=code):
                self.client.mapping_xml=lambda:mapping
                self.client.episode_page=lambda *args:(records,False)
                with self.assertRaises(ReleaseNotReady) as caught:
                    await self.verify(replace(self.source,raw_body=body),self.configuration,self.cancelled)
                self.assertEqual(caught.exception.reason_code,code)

    async def asyncSetUp(self):
        self.source=ReleaseInput(1,1,123,456,b'{"release_name":"Test S02E13"}')
        self.cancelled=threading.Event()
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database=Path(temporary.name)/'verification.sqlite'
        db=connect(database)
        initialize(db)
        db.close()
        self.configuration=ConfigurationSnapshot(1,{'storage':{'database_path':str(database)}})
        owner=self
        class Client:
            def episodes(self,aid): return {1:987,2:988}
            def mapping_xml(self): return MAPPING
            def episode_page(self,tid,page,credentials):
                owner.pages.append(page)
                return ((100,2,13),),False
        self.client=Client()
        self.pages=[]
        self.verify=make_verifier(Options('registeredclient',1),lambda:Credentials('private-key'),
                                  client_factory=lambda *args:self.client)

    async def test_s00_special_requires_anidb_regular_episode_and_tvdb_proof(self):
        release = replace(self.source,
            raw_body=b'{"release_name":"Test S00E05 Special Part 1"}')
        mapping = (
            b'<anime-list><anime anidbid="123" tvdbid="456">'
            b'<mapping-list><mapping anidbseason="1" tvdbseason="0">'
            b'1-5;</mapping></mapping-list></anime></anime-list>'
        )
        self.client.mapping_xml = lambda: mapping
        self.client.episode_page = lambda *args: (((999, 0, 5),), False)
        proven = await self.verify(release, self.configuration, self.cancelled)
        self.assertEqual((proven.anidb_episode_number, proven.anidb_episode_id,
                          proven.tvdb_season, proven.tvdb_episode, proven.tvdb_episode_id),
                         (1, 987, 0, 5, 999))
        # The same release with no explicit regular AniDB -> TVDB S00
        # rule must not guess a special episode number.
        self.client.mapping_xml = lambda: MAPPING
        with self.assertRaises(ReleaseNotReady) as caught:
            await self.verify(release, self.configuration, self.cancelled)
        self.assertEqual(caught.exception.reason_code, "RELEASE_SPECIAL_UNSUPPORTED")

    async def test_no_episodeoffset_verifies_only_with_matching_tvdb_episode(self):
        release=replace(self.source,raw_body=b'{"release_name":"Test S02E01"}')
        for mapping in (MAPPING.replace(b' episodeoffset="12"',b''),
                        MAPPING.replace(b'episodeoffset="12"',b'episodeoffset=""')):
            with self.subTest(mapping=mapping):
                self.client.mapping_xml=lambda value=mapping:value
                self.client.episode_page=lambda *args:(((100,2,1),),False)
                result=await self.verify(release,self.configuration,self.cancelled)
                self.assertEqual((result.anidb_episode_number,result.anidb_episode_id,
                                  result.tvdb_season,result.tvdb_episode,result.tvdb_episode_id),
                                 (1,987,2,1,100))
                # Zero offset never bypasses actual TVDB episode confirmation.
                self.client.episode_page=lambda *args:(((100,2,13),),False)
                with self.assertRaises(ReleaseNotReady):
                    await self.verify(release,self.configuration,self.cancelled)

    async def test_soul_eater_range_uses_batch_verifier_not_single_episode_parser(self):
        async def existing_batch(source,configuration,cancelled):
            return 'batch handler called'
        verifier=make_verifier(Options('registeredclient',1),lambda:Credentials('private-key'),
                               client_factory=lambda *args:self.client,batch_verifier=existing_batch)
        release=replace(self.source,raw_body=b'{"release_name":"[figged] Soul Eater S01 E27-E51 (BluRay 720p)"}')
        self.assertEqual(await verifier(release,self.configuration,self.cancelled),
                         'batch handler called')

    async def test_reverse_offset_uses_exact_anidb_id_and_tvdb_record(self):
        value=await self.verify(self.source,self.configuration,self.cancelled)
        self.assertEqual((value.anidb_episode_number,value.anidb_episode_id,value.tvdb_season,
                          value.tvdb_episode,value.tvdb_episode_id),(1,987,2,13,100))
        self.assertEqual(self.pages,[0])

    async def test_all_pages_must_finish_and_duplicates_or_merged_evidence_are_rejected(self):
        def pages(tid,page,credentials):
            self.pages.append(page)
            return (((100,2,13),),True) if page==0 else (((101,2,14),),False)
        self.client.episode_page=pages
        value=await self.verify(self.source,self.configuration,self.cancelled)
        self.assertEqual(value.tvdb_episode_id,100)
        self.assertEqual(self.pages,[0,1])
        self.client.episode_page=lambda *args:(((100,2,13),),True)
        with self.assertRaises(ProviderInvalidResponse): await self.verify(self.source,self.configuration,self.cancelled)
        self.client.episode_page=lambda *args:(((100,2,13),(101,2,13)),False)
        with self.assertRaises(ReleaseConflict): await self.verify(self.source,self.configuration,self.cancelled)

    async def test_pack_missing_mapping_and_cancellation_are_reviewable(self):
        for raw in (b'{"release_name":"Test Batch"}',b'{"release_name":"Test - 99"}'):
            with self.assertRaises(ReleaseNotReady):
                await self.verify(replace(self.source,raw_body=raw),self.configuration,self.cancelled)
        self.cancelled.set()
        with self.assertRaises(ReleaseNotReady): await self.verify(self.source,self.configuration,self.cancelled)

    async def test_anidb_origin_verifies_without_tvdb_credentials(self):
        # W-15: AniDB-origin release can verify with NULL TVDB placement when credentials unavailable.
        anidb_source = replace(self.source, raw_body=b'{"release_name":"Test - 01"}')
        verifier = make_verifier(Options('registeredclient', 1), lambda: None,
                                 client_factory=lambda *args: self.client)
        result = await verifier(anidb_source, self.configuration, self.cancelled)
        self.assertEqual((result.anidb_episode_number, result.anidb_episode_id), (1, 987))
        self.assertIsNone(result.tvdb_season)
        self.assertIsNone(result.tvdb_episode)
        self.assertIsNone(result.tvdb_episode_id)

    async def test_anidb_origin_verifies_when_tvdb_paging_fails(self):
        # W-15: Provider outage during TVDB paging does not fail AniDB-origin verification.
        from src.nextgen.providers.errors import ProviderUnavailable
        anidb_source = replace(self.source, raw_body=b'{"release_name":"Test - 01"}')
        self.client.episode_page = lambda *args: (_ for _ in ()).throw(ProviderUnavailable())
        result = await self.verify(anidb_source, self.configuration, self.cancelled)
        self.assertEqual((result.anidb_episode_number, result.anidb_episode_id), (1, 987))
        self.assertIsNone(result.tvdb_season)
        self.assertIsNone(result.tvdb_episode)
        self.assertIsNone(result.tvdb_episode_id)


class ReleaseJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.database=self.root/'queue.sqlite'
        self.store=ConfigurationStore(self.root)
        self.startup=self.store.initialize({'storage':{'database_path':str(self.database)}}).snapshot
        db=connect(self.database)
        initialize(db)
        self.event,self.job=accept_event_and_job(db,'test',b'{"release_name":"Test - 01"}',
                                                kind='VERIFY_RELEASE',payload={'anidb_id':123})
        save_identity(db,self.event,{'schema_version':2,'anime':{'anidb_id':123,'tvdb_series_id':456,'title':'Test'}})
        db.close()

    def rows(self,sql,args=()):
        db=connect(self.database)
        try:return db.execute(sql,args).fetchall()
        finally:db.close()

    def worker(self,verify):
        worker=Worker(self.store,self.startup,Registry((registration(verify),)),policy=Policy(lease_ms=30000))
        self.addAsyncCleanup(worker.close)
        return worker

    async def test_worker_persists_declared_scope_without_mutating_identity_or_starting_acquisition(self):
        before=self.rows('SELECT series_json FROM identity_results')
        async def verify(source,*args):return VerifiedRelease(source,1,987,2,13,100)
        worker=self.worker(verify)
        await worker.attempt()
        self.assertEqual(self.rows('SELECT verification_scope,anidb_episode_id,tvdb_season,tvdb_episode,tvdb_order FROM release_verifications'),
                         [('DECLARED_SINGLE_EPISODE',987,2,13,'official')])
        self.assertEqual(self.rows('SELECT series_json FROM identity_results'),before)
        self.assertEqual(self.rows('SELECT kind,state FROM jobs'),[('VERIFY_RELEASE','DONE')])
        self.assertEqual(self.rows('SELECT * FROM identified_episodes'),[(987,123)])
        self.assertEqual(self.rows('SELECT tvdb_series_id,tvdb_season,anidb_id FROM recognized_seasons'),[(456,2,123)])
        self.assertEqual(self.rows('SELECT anidb_episode_id,tvdb_episode_id FROM recognized_episodes'),[(987,100)])
        self.assertTrue(self.rows('SELECT alias_key FROM recognized_aliases'))
        self.assertEqual(self.rows('PRAGMA foreign_key_check'),[])

    async def test_source_change_during_provider_work_rolls_back_facts_and_reviews_job(self):
        async def verify(source,*args):
            db=connect(self.database)
            try:
                with db:db.execute('UPDATE incoming_events SET raw_body=? WHERE event_id=?',(b'changed',self.event))
            finally:db.close()
            return VerifiedRelease(source,1,987,2,13,100)
        await self.worker(verify).attempt()
        self.assertEqual(self.rows('SELECT count(*) FROM release_verifications'),[(0,)])
        self.assertEqual(self.rows('SELECT count(*) FROM identified_episodes'),[(0,)])
        self.assertEqual(self.rows('SELECT state,last_reason_code FROM jobs'),[('NEEDS_REVIEW','RELEASE_CONFLICT')])

    async def test_stale_lease_cannot_commit_release_facts(self):
        async def verify(source,*args): return VerifiedRelease(source,1,987,2,13,100)
        worker=self.worker(verify)
        now=worker.queue.now()
        worker.queue.clock=lambda:now
        claim,_=worker.queue.claim()
        spec=worker.registry.entries['VERIFY_RELEASE']
        worker.queue.start(claim,spec,'a'*32,worker.registry)
        source=load_release(self.database,self.event,123)
        worker.queue.clock=lambda:now+worker.policy.lease_ms
        with self.assertRaises(LeaseLost):
            worker.queue.finish(claim,Outcome(value=VerifiedRelease(source,1,987,2,13,100)),
                                spec=spec,invocation='a'*32,registry=worker.registry)
        self.assertEqual(self.rows('SELECT count(*) FROM release_verifications'),[(0,)])

    async def test_ambiguous_release_goes_to_review(self):
        async def verify(*args):raise ReleaseNotReady()
        await self.worker(verify).attempt()
        self.assertEqual(self.rows('SELECT state,last_reason_code FROM jobs'),[('NEEDS_REVIEW','RELEASE_UNRESOLVED')])

    async def test_specific_episode_reason_reaches_job_and_history_without_acquisition(self):
        async def verify(*args):raise ReleaseNotReady('RELEASE_EPISODE_MAPPING_MISSING')
        await self.worker(verify).attempt()
        self.assertEqual(self.rows('SELECT state,last_reason_code FROM jobs'),[('NEEDS_REVIEW','RELEASE_EPISODE_MAPPING_MISSING')])
        self.assertTrue(self.rows("SELECT 1 FROM job_history WHERE reason_code='RELEASE_EPISODE_MAPPING_MISSING'"))
        self.assertEqual(self.rows('SELECT count(*) FROM release_verifications'),[(0,)])


    async def test_provider_wait_defers_without_persisting_release_or_spending_retry_budget(self):
        from src.nextgen.providers.errors import ProviderDeferred
        async def verify(*args): raise ProviderDeferred(2000)
        await self.worker(verify).attempt()
        self.assertEqual(self.rows('SELECT state,last_reason_code FROM jobs'),[('RETRY_WAIT','JOB_WAITING')])
        self.assertEqual(self.rows("SELECT count(*) FROM job_history WHERE event_code='RETRY_SCHEDULED'"),[(0,)])
        self.assertEqual(self.rows('SELECT count(*) FROM release_verifications'),[(0,)])
