"""The catalog is what the reason/event code audit says, and the audit follows its rules.

docs/refactor/reason_code_proposals.py was applied by RF-006b (migration 064).
When a NextGen merge adds or rewords a code, this fails until the audit data and
the catalog are updated together: add the code to REASONS or EVENTS (or NEW), and
if NextGen still uses an old name, map it in the next migration. Old names must
never come back (see also the ledger rule RF-006b).
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
UPDATE = 'Update docs/refactor/reason_code_proposals.py and the catalog (reasons.py, messages.py) together.'


class CatalogMatchesAuditTests(unittest.TestCase):
    """RF-006b applied the audit: the catalog is exactly what the proposals say."""

    def test_registered_reasons_are_the_audits_new_catalog(self):
        retired_registered = set(RETIRED) & set(REGISTRY)
        # The retired per-service codes stay until RF-006c retires them all at once.
        self.assertIn(retired_registered, (set(), set(RETIRED)))
        expected = {entry[0] for entry in REASONS.values()} | (set(NEW) & set(REGISTRY)) | retired_registered
        self.assertEqual(sorted(set(REGISTRY) ^ expected), [], f'Catalog and audit differ. {UPDATE}')
        missing_new = sorted(set(NEW) - set(REGISTRY))
        targets_pending = {entry[0] for code, entry in RETIRED.items() if code in REGISTRY}
        self.assertLessEqual(set(missing_new), targets_pending,
                             f'Added codes that should be registered: {missing_new}')

    def test_registered_events_are_the_audits_new_catalog(self):
        self.assertEqual(sorted(set(EVENTS) ^ {entry[0] for entry in NEW_EVENTS.values()}), [])

    def test_old_names_are_gone(self):
        renamed = {old for table in (REASONS, NEW_EVENTS) for old, entry in table.items() if entry[0] != old}
        self.assertEqual(sorted((renamed | set(REMOVED)) & (set(REGISTRY) | set(EVENTS))), [])

    def test_wording_is_the_audits(self):
        for old, (new, description, message, action) in REASONS.items():
            with self.subTest(code=new):
                reason = REGISTRY[new]
                self.assertEqual((reason.description, reason.message, reason.action), (description, message, action))
        for old, (new, message) in NEW_EVENTS.items():
            with self.subTest(event=new):
                self.assertEqual(EVENTS[new].message, message)
        for code, (severity, retryable, description, message, action) in NEW.items():
            if code in REGISTRY:
                with self.subTest(added=code):
                    reason = REGISTRY[code]
                    self.assertEqual((reason.severity, reason.retryable, reason.description, reason.message,
                                      reason.action), (severity, retryable, description, message, action))

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
                severity, retryable, *text = entry
                self.assertIn(severity, {'INFO', 'WARNING', 'ERROR', 'CRITICAL'})
                self.assertIn(retryable, {True, False, None})
                self.assertEqual(len(text), 3)
                for part in text:
                    self.assertTrue(part.strip())
                    self.assertIsNone(JARGON.search(part), part)
                    # messages._validate_text refuses these when the catalog loads.
                    self.assertLessEqual(len(part), 512, part)
                    self.assertNotRegex(part, r'[{}<>]')

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
                        # messages._validate_text refuses these when the catalog loads.
                        self.assertLessEqual(len(part), 512, part)
                        self.assertNotRegex(part, r'[{}<>]')


if __name__ == '__main__':
    unittest.main()
