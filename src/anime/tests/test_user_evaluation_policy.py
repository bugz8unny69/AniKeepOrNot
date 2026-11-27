"""Offline trial windows, concurrency and fenced persistence; unexecuted."""
import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.anime.configuration import ConfigurationStore
from src.anime.configuration_schema import ConfigurationError, CONVERSIONS, convert_document, ConfigurationSnapshot
from src.anime.user_evaluation_policy import Window, UserEvaluationProposal, InvalidEpisodeCounts
from src.anime.worker import Worker
from src.anime.storage.sqlite import connect, initialize


class UserEvaluationWindowTests(unittest.TestCase):
    def test_following_includes_trigger_and_overlaps_count_once(self):
        self.assertEqual(Window(6,1).episodes(10),(1,2,3,4,5,6,10))
        self.assertEqual(Window(0,1).episodes(10),(10,))
        self.assertEqual(Window(0,2).episodes(10),(10,11))
        self.assertEqual(Window(0,6).episodes(10),(10,11,12,13,14,15))
        self.assertEqual(Window(6,2).episodes(3),(1,2,3,4,5,6))
        self.assertEqual(Window(6,3).episodes(5),(1,2,3,4,5,6,7))
        self.assertEqual(Window(6,0).episodes(10),(1,2,3,4,5,6))
        with self.assertRaises(InvalidEpisodeCounts):Window(0,0)
        for value in (-1,True,1.5,1001):
            with self.assertRaises(ValueError):Window(value,1)
        self.assertEqual(len(Window(100,100).episodes(200)),200)

    def test_zero_zero_rejected_for_snapshot_and_conversion(self):
        settings={'user_evaluation':{'backfill_count':0,'following_count':0}}
        # Published v5 calls the section "trial"; the upgrade must refuse 0/0 there too.
        historical={'trial':{'backfill_count':0,'following_count':0}}
        for make in (lambda:ConfigurationSnapshot(1,settings),
                     lambda:convert_document({'schema_version':5,'revision':1,'settings':historical})):
            with self.assertRaises(ConfigurationError) as error:make()
            self.assertEqual(error.exception.reason_code,'USER_EVALUATION_COUNTS_INVALID')
            self.assertIn('at least 1',str(error.exception))
        for backfill,following in ((6,0),(0,1)):
            snapshot=ConfigurationSnapshot(1,{'user_evaluation':{'backfill_count':backfill,'following_count':following}})
            self.assertEqual(snapshot.value('user_evaluation.following_count'),following)

    def test_v4_upgrade_adds_defaults_without_changing_historical_shapes(self):
        document={}
        for version in range(4):
            document=CONVERSIONS[version](document)
            self.assertNotIn('trial',document['settings'])
        current=convert_document(document)
        self.assertEqual((current.schema_version,current.revision),(17,17))
        self.assertEqual(current.value('user_evaluation.backfill_count'),6)
        self.assertEqual(current.value('user_evaluation.following_count'),1)
        with self.assertRaises(ConfigurationError):
            convert_document({'schema_version':4,'revision':1,'settings':{'user_evaluation':{'following_count':2}}})
        for value in (-1,True,1001):
            with self.assertRaises(ConfigurationError):ConfigurationSnapshot(1,{'user_evaluation':{'backfill_count':value}})







class AniDBWindowTests(unittest.TestCase):
    """Spec §0: single-episode selection in AniDB numbers (season packs unchanged)."""
    def test_spec_examples(self):
        from src.anime.user_evaluation_policy import Window
        cases = ((1, 6, 3, range(1, 7)), (5, 6, 3, range(1, 8)), (8, 6, 3, range(2, 11)),
                 (1000, 6, 3, range(994, 1003)), (1000, 0, 6, range(1000, 1006)))
        for anchor, backfill, following, expected in cases:
            with self.subTest(anchor=anchor, backfill=backfill, following=following):
                self.assertEqual(Window(backfill, following).anidb_episodes(anchor), tuple(expected))

    def test_backfill_only_and_large_numbers(self):
        from src.anime.user_evaluation_policy import Window
        self.assertEqual(Window(6, 0).anidb_episodes(1000), tuple(range(994, 1000)))
        self.assertEqual(Window(6, 0).anidb_episodes(3), tuple(range(1, 7)))
        self.assertEqual(Window(2, 2).anidb_episodes(5000), (4998, 4999, 5000, 5001))

    def test_season_pack_window_is_unchanged(self):
        from src.anime.user_evaluation_policy import Window
        self.assertEqual(Window(6, 3).episodes(10), (1, 2, 3, 4, 5, 6, 10, 11, 12))
