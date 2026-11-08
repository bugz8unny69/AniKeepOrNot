"""release_names is the single, pure home of release-name parsing (JD-1a)."""
import ast
import pathlib
import unittest

SOURCE = pathlib.Path(__file__).resolve().parents[1] / 'release_names.py'


class ReleaseNamesModuleTests(unittest.TestCase):
    def test_pure_text_parsing(self):
        # No database, network, provider or other NextGen module: only text tools.
        tree = ast.parse(SOURCE.read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split('.')[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or '').split('.')[0])
        self.assertLessEqual(imported, {'dataclasses', 're', 'unicodedata'})

    def test_the_two_release_tech_patterns_stay_distinct(self):
        # Moved as-is (no behaviour change): TITLE_END_TECH marks where a written
        # title ends and does not include Repack/Uncensored/AT-X; RELEASE_TECH,
        # used to clean titles, does.
        from src.anime.release_names import RELEASE_TECH, TITLE_END_TECH
        self.assertIsNotNone(RELEASE_TECH.search('Title Repack'))
        self.assertIsNone(TITLE_END_TECH.search('Title Repack'))
        self.assertIsNotNone(TITLE_END_TECH.search('Title 1080p'))

    def test_reads_a_release(self):
        from src.anime.release_names import read_release, release_title
        parsed, reason = read_release('[SubsPlease] Sousou no Frieren - 05 (1080p) [ABCD1234].mkv')
        self.assertIsNone(reason)
        self.assertEqual((parsed.season, parsed.episode), (None, 5))
        self.assertIn('Sousou no Frieren', parsed.titles)
        self.assertEqual(release_title('[Judas] Frieren - S02E05 [1080p]')[1], 5)


if __name__ == '__main__':
    unittest.main()
