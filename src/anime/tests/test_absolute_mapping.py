"""Work item P1 1.5: Anime-Lists defaulttvdbseason="a" resolves through TVDB absolute numbers.

The XML shapes are copied from the real anime-list.xml (One Piece is anidbid 69;
Fairy Tail (2014), anidbid 9980, has episodeoffset="175").
"""
import unittest

from src.anime.providers.parsing import tvdb_absolute_page
from src.anime.providers.release_mapping import absolute_index, forward_mappings

ONE_PIECE = b'''<anime-list>
  <anime anidbid="69" tvdbid="81797" defaulttvdbseason="a" tmdbtv="37854" tmdbseason="a">
    <name>One Piece</name>
    <mapping-list>
      <mapping anidbseason="0" tvdbseason="0">;1-27;2-3;</mapping>
      <mapping anidbseason="1" tmdbseason="1" start="1" end="61" offset="0"/>
    </mapping-list>
  </anime>
  <anime anidbid="9980" tvdbid="114801" defaulttvdbseason="a" episodeoffset="175" tmdbtv="46261" tmdbseason="a">
    <name>Fairy Tail (2014)</name>
    <mapping-list>
      <mapping anidbseason="1" tvdbseason="5" start="1" end="51" offset="0"/>
    </mapping-list>
  </anime>
  <anime anidbid="20" tvdbid="72435" defaulttvdbseason="1" tmdbtv="34834" tmdbseason="1">
    <name>Tokyo Underground</name>
  </anime>
</anime-list>'''


class AbsoluteMappingTests(unittest.TestCase):
    def test_episode_1000_is_the_tvdb_episode_with_absolute_number_1000(self):
        calls = []
        def index():
            calls.append(1)
            return {1000: (21, 108), 999: (21, 107)}
        self.assertEqual(forward_mappings(ONE_PIECE, 69, 81797, {999: 1, 1000: 2}, absolute=index),
                         {999: (21, 107), 1000: (21, 108)})
        self.assertEqual(len(calls), 1)                      # one TVDB read for the whole catalog

    def test_episodeoffset_is_added_and_explicit_rules_come_first(self):
        index = lambda: {227: (8, 1), 52 + 175: (8, 1), 1 + 175: (7, 1)}
        mapped = forward_mappings(ONE_PIECE, 9980, 114801, {1: 1, 52: 2}, absolute=index)
        self.assertEqual(mapped[1], (5, 1))                  # mapping-list rule, not absolute 176
        self.assertEqual(mapped[52], (8, 1))                 # absolute 52+175

    def test_no_match_or_a_duplicate_absolute_number_stays_unmapped(self):
        self.assertEqual(forward_mappings(ONE_PIECE, 69, 81797, {1000: 2}, absolute=lambda: {}), {})
        self.assertEqual(forward_mappings(ONE_PIECE, 69, 81797, {1000: 2}, absolute=lambda: {1000: None}), {})
        self.assertEqual(forward_mappings(ONE_PIECE, 69, 81797, {1000: 2}), {})   # no TVDB credentials

    def test_season_based_mappings_never_read_tvdb_absolute_numbers(self):
        def index():
            raise AssertionError('not an absolute series')
        self.assertEqual(forward_mappings(ONE_PIECE, 20, 72435, {1: 1, 2: 2}, absolute=index), {1: (1, 1), 2: (1, 2)})

    def test_absolute_index_marks_duplicates(self):
        class Client:
            def absolute_page(self, tid, page, secret):
                return ((1, 1, 1), (2, 1, 2), (3, 1, 2)), False
        self.assertEqual(absolute_index(Client(), 81797, object()), {1: (1, 1), 2: None})

    def test_tvdb_page_absolute_numbers(self):
        page = {'data': {'episodes': [
            {'id': 1, 'seriesId': 81797, 'seasonNumber': 21, 'number': 108, 'absoluteNumber': 1000},
            {'id': 2, 'seriesId': 81797, 'seasonNumber': 0, 'number': 3, 'absoluteNumber': 0},
            {'id': 3, 'seriesId': 81797, 'seasonNumber': 21, 'number': 109, 'absoluteNumber': None},
        ]}, 'links': {'next': None}}
        self.assertEqual(tvdb_absolute_page(page, 81797), (((21, 108, 1000),), False))


if __name__ == '__main__':
    unittest.main()
