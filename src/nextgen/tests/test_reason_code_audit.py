"""The reason/event code audit covers today's catalog, and its proposals follow its rules.

docs/refactor/reason_code_proposals.py is what RF-006 applies. When a NextGen
merge adds, removes or rewords a code, this fails until the audit is updated:
add a proposal (REASONS/EVENTS), or map a code that is no longer emitted in
REMOVED, then regenerate REASON_CODE_AUDIT.md with
docs/refactor/render_reason_code_audit.py.
"""
from pathlib import Path
import re
import runpy
import unittest

from src.nextgen.messages import EVENTS
from src.nextgen.reasons import REGISTRY

ROOT = Path(__file__).resolve().parents[3]
PROPOSALS = runpy.run_path(str(ROOT / 'docs' / 'refactor' / 'reason_code_proposals.py'))
REASONS, NEW_EVENTS = PROPOSALS['REASONS'], PROPOSALS['EVENTS']
REMOVED, UNREGISTERED = PROPOSALS['REMOVED'], PROPOSALS['UNREGISTERED']
RETIRED, NEW = PROPOSALS['RETIRED'], PROPOSALS['NEW']
# Codes name the situation or the role, never the product, so a service can be
# replaced without renaming codes (owner decision, 2026-11-06).
PRODUCTS = re.compile(r'PROWLARR|SONARR|SHOKO|JELLYFIN|QBITTORRENT|AUTOBRR|TRIAL')
# AniDB and TVDB name the episode numbering the whole data model is keyed on
# (anidb_id, tvdb_season), not a service that could be swapped.
NUMBERING = {'RELEASE_ANIDB_EPISODE_MISSING', 'RELEASE_TVDB_EPISODE_MISSING'}
SERVICES = {'qbittorrent', 'prowlarr', 'sonarr', 'jellyfin', 'shoko', 'anidb', 'tvdb', 'anime-lists'}
CODE = re.compile(r'^[A-Z][A-Z0-9_]{0,63}$')
JARGON = re.compile(r'\b(durable|durably|fenced|provenance|immutable|bounded|authoritative|'
                    r'journal|permit|admission|intent)\b', re.I)
UPDATE = ('Update docs/refactor/reason_code_proposals.py, then regenerate REASON_CODE_AUDIT.md '
          '(python docs/refactor/render_reason_code_audit.py > docs/refactor/REASON_CODE_AUDIT.md).')


class CoverageTests(unittest.TestCase):
    def test_every_registered_reason_has_a_proposal_or_is_removed(self):
        missing = sorted(set(REGISTRY) - set(REASONS) - set(REMOVED) - set(RETIRED))
        self.assertEqual(missing, [], f'Reason codes the audit does not cover: {missing}. {UPDATE}')

    def test_every_registered_event_has_a_proposal_or_is_removed(self):
        missing = sorted(set(EVENTS) - set(NEW_EVENTS) - set(REMOVED))
        self.assertEqual(missing, [], f'Event codes the audit does not cover: {missing}. {UPDATE}')

    def test_proposals_only_cover_codes_that_exist(self):
        stale = sorted((set(REASONS) - set(REGISTRY)) | (set(NEW_EVENTS) - set(EVENTS))
                       | (set(REMOVED) - set(REGISTRY) - set(EVENTS)) | (set(RETIRED) - set(REGISTRY)))
        self.assertEqual(stale, [], f'Proposals for codes that are no longer registered: {stale}. {UPDATE}')

    def test_unregistered_labels_are_really_unregistered(self):
        registered = sorted(set(UNREGISTERED) & (set(REGISTRY) | set(EVENTS)))
        self.assertEqual(registered, [], f'Now registered; move them to REASONS or EVENTS: {registered}')


class ProposalRuleTests(unittest.TestCase):
    def test_each_code_is_handled_once(self):
        tables = {'REASONS': set(REASONS), 'REMOVED': set(REMOVED) & set(REGISTRY), 'RETIRED': set(RETIRED)}
        for first in tables:
            for second in tables:
                if first < second:
                    self.assertEqual(sorted(tables[first] & tables[second]), [], (first, second))

    def test_added_codes_are_new_and_fully_described(self):
        for code, entry in NEW.items():
            with self.subTest(code=code):
                self.assertRegex(code, CODE)
                self.assertNotIn(code, REGISTRY)
                severity, retryable, *text = entry
                self.assertIn(severity, {'INFO', 'WARNING', 'ERROR', 'CRITICAL'})
                self.assertIn(retryable, {True, False, None})
                self.assertEqual(len(text), 3)
                for part in text:
                    self.assertTrue(part.strip())
                    self.assertIsNone(JARGON.search(part), part)

    def test_retired_codes_name_a_known_service(self):
        for code, (target, service) in RETIRED.items():
            with self.subTest(code=code):
                self.assertIn(service, SERVICES | {None})

    def test_new_names_are_valid_and_unique(self):
        for table in (REASONS, NEW_EVENTS):
            new = [entry[0] for entry in table.values()]
            for name in new:
                with self.subTest(name=name):
                    self.assertRegex(name, CODE)
            self.assertEqual(sorted(n for n in set(new) if new.count(n) > 1), [])

    def test_no_new_name_reuses_a_removed_or_retired_name(self):
        new = ({entry[0] for entry in REASONS.values()} | {entry[0] for entry in NEW_EVENTS.values()}
               | set(NEW))
        retired = set(REMOVED) | set(RETIRED) | {old for table in (REASONS, NEW_EVENTS)
                                                for old, entry in table.items() if entry[0] != old}
        self.assertEqual(sorted(new & retired), [])

    def test_removed_and_retired_codes_migrate_to_a_code_that_is_kept(self):
        kept = ({entry[0] for entry in REASONS.values()} | {entry[0] for entry in NEW_EVENTS.values()}
                | set(NEW))
        for old, target in [*REMOVED.items(), *((old, entry[0]) for old, entry in RETIRED.items())]:
            with self.subTest(code=old):
                self.assertIn(target, kept)

    def test_no_product_names_remain(self):
        names = [entry[0] for entry in REASONS.values()] + [entry[0] for entry in NEW_EVENTS.values()]
        names += list(UNREGISTERED.values()) + list(NEW)
        self.assertEqual([n for n in names if PRODUCTS.search(n) and n not in NUMBERING], [])

    def test_wording_is_complete_and_free_of_jargon(self):
        # Some codes are both a reason and an event, so each table is checked on its own.
        for table, parts in ((REASONS, 3), (NEW_EVENTS, 1)):
            for old, (new, *text) in table.items():
                with self.subTest(code=old, parts=parts):
                    self.assertEqual(len(text), parts)
                    for part in text:
                        self.assertTrue(part.strip(), 'empty wording')
                        self.assertIsNone(JARGON.search(part), part)


if __name__ == '__main__':
    unittest.main()
