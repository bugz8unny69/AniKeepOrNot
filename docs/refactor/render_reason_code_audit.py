"""Regenerate REASON_CODE_AUDIT.md from reason_code_proposals.py.

Run from the repository root:
    python docs/refactor/render_reason_code_audit.py > docs/refactor/REASON_CODE_AUDIT.md
Reads the catalog from src/nextgen (reasons.py, messages.py) for the old
wording and usage counts, so it renders correctly only on a tree from before
RF-006b applied the audit (refactor 8da06cd2 or earlier). Data only; nothing at
runtime imports this.
"""
import pathlib
import re
import runpy
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.nextgen.reasons import REGISTRY  # noqa: E402
import src.nextgen.messages as messages  # noqa: E402

data = runpy.run_path(str(ROOT / 'docs/refactor/reason_code_proposals.py'))
R, E, X, U = data['REASONS'], data['EVENTS'], data['REMOVED'], data['UNREGISTERED']
T, N = data['RETIRED'], data['NEW']

src = ROOT / 'src/nextgen'
pool = [p for p in src.rglob('*.py') if 'tests' not in p.parts and p.name not in ('reasons.py', 'messages.py')]
pool += list((ROOT / 'fixes').glob('*.py')) + list(src.rglob('*.js')) + list(src.rglob('*.sql'))
text = '\n'.join(p.read_text() for p in pool)


def uses(code):
    return len(re.findall(r'\b' + re.escape(code) + r'\b', text))


def esc(value):
    return str(value).replace('|', '\\|')


WHY = {
    'SONARR_BACKFILL_PLAN_CONFLICT': 'Episode discovery searches Prowlarr, not Sonarr.',
    'SONARR_CANDIDATES_EXHAUSTED': 'Release candidates come from Prowlarr, not Sonarr.',
    'TORRENT_PROVENANCE_CONFLICT': '"Provenance" is jargon. The check is whether the torrent still matches what this work added.',
    'TORRENT_INTENT_RECORDED': '"Intent" is jargon. The event records that the torrent is about to be added.',
    'SONARR_BACKFILL_OBSERVED': 'Episode discovery searches Prowlarr. The old text said "Read-only Sonarr backfill results recorded."',
    'SONARR_SELECTION_CONFLICT': 'Releases come from Prowlarr, not Sonarr. Registered by NextGen #283; it appears only in the worker log, because the job itself ends with `JOB_HANDLER_FAILED`.',
    'PROWLARR_SEARCH_TIMEOUT': 'No product names in codes (owner decision, 2026-11-06). The search service is named in the details.',
    'PROWLARR_RELEASE_WITHDRAWN': 'No product names in codes. It describes the release, not the search service.',
    'SHOKO_METADATA_REVIEW': 'No product names in codes. Shoko is the anime library service that adds episode details.',
    'JELLYFIN_REFRESH_REVIEW': 'No product names in codes. Jellyfin is the media server.',
    'BATCH_SELECTION_CONFLICT': 'Matches `RELEASE_SELECTION_CONFLICT`, and says "season pack", the word the dashboard uses, instead of the internal "batch". Same log-only use.',
}
# Codes registered after the first version of this audit (base 9fdb64eb), and where they came from.
ADDED = {
    'SONARR_SELECTION_CONFLICT': ('reason', 'NextGen #283'),
    'BATCH_SELECTION_CONFLICT': ('reason', 'NextGen #283'),
    'MANUAL_RELEASE_EPISODE_MISMATCH': ('reason', 'NextGen #286'),
    'MANUAL_RELEASE_TITLE_UNREADABLE': ('reason', 'NextGen #286'),
    'MANUAL_RELEASE_SOURCE_INVALID': ('reason', 'NextGen #286'),
    'SERVICE_SETUP_INVALID': ('reason', '`services` (#279, merged in #284)'),
    'SERVICE_UNAVAILABLE': ('reason', '`services` (#279, merged in #284)'),
    'SERVICE_DOWN': ('event', '`services` (#279, merged in #284)'),
    'SERVICE_RESTORED': ('event', '`services` (#279, merged in #284)'),
    'SERVICE_STARTUP_REFUSED': ('event', '`services` (#279, merged in #284)'),
}
TRIAL_WHY = '"Trial" is replaced by "user evaluation" everywhere (owner decision).'
REMOVED_NOTE = {
    'SONARR_DISCOVERY_AUTH_FAILED': ('Before `db41b589` (Sonarr-based discovery)', 'A retry now searches Prowlarr.'),
    'SONARR_DISCOVERY_CONFIGURATION_INVALID': ('Before `db41b589`', 'Same as above.'),
    'SONARR_DISCOVERY_UNSUPPORTED': ('Before `db41b589`', 'The wrong server version was a setup problem.'),
    'SONARR_DISCOVERY_REVIEW': ('Before `db41b589`', ''),
    'SONARR_DISCOVERY_TIMEOUT': ('Before `db41b589`; still listed in `review_retry`', ''),
    'SONARR_DISCOVERY_UNAVAILABLE': ('Before `db41b589`; still listed in `review_retry`', ''),
    'SONARR_DISCOVERY_NO_RESULTS': ('Before `db41b589`', 'Same meaning: the ranked fallback is no longer offered.'),
    'SONARR_DISCOVERY_EPISODE_NOT_FOUND': ('Before `db41b589`; still listed in `review_retry`', 'Sonarr\'s catalog is no longer consulted. `review_retry` keeps these BLOCKED jobs retry-eligible under the new code, as they are today.'),
    'SONARR_EPISODES_NOT_LISTED': ('Before `db41b589`; still listed in `review_retry`', 'Same as above.'),
    'KEEP_NOT_READY': ('Only on 2026-10-23 (`bf89c34b` to `e1a8bd3b`), before Keep existed', 'The person reviews the job and presses Keep again.'),
    'ACTIVITY_CHANGED': ('Registered in `df9655aa` but never emitted', 'No rows are expected. The target is only a safe fallback.'),
}

out = []
P = out.append
P('# Reason and event code audit')
P('')
P('**Base:** `refactor` at `8c1d25a0` (NextGen `29fce109`, with `services` merged). First written against `9fdb64eb` (NextGen `9071be2e`); §0 lists the codes added since. This is a review document: it changes no code.')
P('**Kept current by** `src/nextgen/tests/test_reason_code_audit.py`: it fails when a registered code has no proposal here, so a NextGen sync that adds a code cannot slip past the audit.')
P('**Data:** [`reason_code_proposals.py`](reason_code_proposals.py) is the machine-readable source. Rebuild this page with `python docs/refactor/render_reason_code_audit.py > docs/refactor/REASON_CODE_AUDIT.md`. RF-006 applies the data.')
P('')
P('## Decisions this audit implements')
P('')
P('- Persisted identifiers are **renamed**. One versioned SQL migration rewrites the stored values, including `job_history`. The code recognizes **only the new values**, and old names are removed.')
P('- **"Trial" becomes "user evaluation"** (`USER_EVALUATION_*`) everywhere it can. The other places the term is used are mapped in [NAMING_MAP.md](NAMING_MAP.md) §8.')
P('- Every code\'s wording is rewritten to be human-readable: plain words, the service named, and a concrete next step.')
P('- **Codes name the situation or the role, never the product** (owner decision, 2026-11-06), so a service can be replaced without renaming codes. Service-health conditions use four generic codes, `SERVICE_SETUP_INVALID`, `SERVICE_UNAVAILABLE`, `SERVICE_RESPONSE_INVALID` and the role code `RELEASE_SEARCH_TIMEOUT`, and the service goes in the details. Exception: `RELEASE_ANIDB_EPISODE_MISSING` and `RELEASE_TVDB_EPISODE_MISSING` name the episode numbering the whole data model is keyed on (`anidb_id`, `tvdb_season`), not a replaceable service.')
P('- **Jobs that stopped under the old Sonarr-based discovery** migrate to one historical code, `EPISODE_DISCOVERY_OUTDATED`, so History stays true (owner decision, 2026-11-06).')
P('- **Behavior changes are allowed** where they improve things (owner decision, 2026-11-06). They are made on `refactor` only, never on NextGen, and listed in §6. Elsewhere, severity and retryable flags are unchanged.')
P('')
P('## Summary')
P('')
nr = sum(1 for k, v in R.items() if k != v[0])
ne = sum(1 for k, v in E.items() if k != v[0])
xr = sum(1 for k in X if k in REGISTRY)
xe = sum(1 for k in X if k in messages.EVENTS)
P('| | Reason codes | Event codes |')
P('|---|---|---|')
P(f'| Current catalog | {len(REGISTRY)} | {len(messages.EVENTS)} |')
P(f'| Kept, wording rewritten | {len(R) - nr} | {len(E) - ne} |')
P(f'| Kept, **renamed** | {nr} | {ne} |')
P(f'| **Removed** (no longer emitted; stored rows migrate) | {xr} | {xe} |')
P(f'| **Retired** into a generic service code (still emitted today; see §6) | {len(T)} | 0 |')
P(f'| **Added** by this refactor | {len(N)} | 0 |')
P(f'| New catalog | {len(R) + len(N)} | {len(E)} |')
P('')
P('## 0. Codes added since the first audit')
P('')
P('| Code | Kind | From | Proposal |')
P('|---|---|---|---|')
for k, (kind, origin) in ADDED.items():
    new = (R if kind == 'reason' else E)[k][0]
    P(f'| `{k}` | {kind} | {origin} | ' + ('kept' if new == k else f'renamed to **`{new}`**') + ' |')
P('')
P('## 1. Renamed codes')
P('')
P('| Old | New | Kind | Why |')
P('|---|---|---|---|')
for kind, table in (('reason', R), ('event', E)):
    for k, v in table.items():
        if k != v[0]:
            P(f'| `{k}` | `{v[0]}` | {kind} | {WHY.get(k, TRIAL_WHY if k.startswith("TRIAL_") else "")} |')
for k, v in U.items():
    P(f'| `{k}` | `{v}` | internal label | It is set on two exception classes but was never registered and is never stored, because those exceptions are converted to other codes before anything is saved. The new name is registered so the contract holds. |')
P('')
P('## 1a. Retired product-named codes')
P('')
P('Each is replaced by a generic code. The migration rewrites stored rows to it and, where the old code named the service, adds `\"service\"` to the row\'s details, so History still says which service it was.')
P('')
P('| Retired | Replaced by | `service` in details | Old severity / retryable | New |')
P('|---|---|---|---|---|')
def flags(code):
    if code in REGISTRY:
        r = REGISTRY[code]; return f'{r.severity} / {r.retryable}'
    return f'{N[code][0]} / {N[code][1]}'
for k, (target, service) in T.items():
    old, new = flags(k), flags(target)
    P(f'| `{k}` | `{target}` | {service or "from the details, if recorded"} | {old} | ' + (new if new == old else f'**{new}**') + ' |')
P('')
P('## 1b. Codes added by this refactor')
P('')
P('| Code | Severity / retryable | Description | Message | Action |')
P('|---|---|---|---|---|')
for k, (severity, retryable, d, m, a) in N.items():
    P(f'| `{k}` | {severity} / {retryable} | {esc(d)} | {esc(m)} | {esc(a)} |')
P('')
P('## 2. Removed codes and where their stored rows migrate')
P('')
P('| Removed | Migrates to | Last emitted / evidence | Note |')
P('|---|---|---|---|')
for k, target in X.items():
    when, note = REMOVED_NOTE[k]
    P(f'| `{k}` | `{target}` | {when} | {note} |')
P('')
P('## 3. What the migration must rewrite (RF-006)')
P('')
P('For every renamed or removed code, the RF-006 migration rewrites the columns below. It will be `063`: `060` and `061` are the batch candidate tables and `062` is `service_status`.')
P('')
P('- **History:** `job_history.event_code` and `.reason_code`; `event_history.event_code` and `.reason_code`.')
P('- **Current state:** `jobs.last_reason_code`, `incoming_events.last_reason_code` and `job_operations.reason_code`.')
P('- **Per-step records:**')
P('  - `trial_evaluations.reason_code`, renamed in RF-006 (NAMING_MAP §8);')
P('  - `sonarr_episode_candidates.reason_code`, renamed in RF-006;')
P('  - `torrent_submissions.reason_code` and `torrent_transfers.reason_code`;')
P('  - `batch_torrent_submissions.reason_code`, `batch_torrent_transfers.reason_code` and `batch_torrent_unlocks.reason_code`;')
P('  - `batch_candidates.reason_code` and `batch_candidate_stops.reason_code`, why a season pack candidate stopped (added by the batch candidate controller, migrations `060` and `061`).')
P('- **Codes inside JSON:** `job_history.details_json` and `event_history.details_json` (`cause_reason_code`). Also any stored JSON that embeds codes, such as `prowlarr_search_snapshots.snapshot_json`, `identity_candidate_evidence.evidence_json` and `batch_candidates.metadata_json`. RF-006 scans **every** `*_json` column with `json_tree` for old code values before writing the update, rather than trusting this list.')
P('')
P('These columns were inventoried from the current schema (every column named `*reason*`, `event_code` or `*_code`). `activity_code` and `handler_code` hold activity and handler codes, not reason codes. `handler_code` changes only for the job-kind renames (`DISCOVER_SONARR_BACKFILL`, `EVALUATE_TRIAL`, `STAGE_TRIAL_MEDIA`).')
P('')
P('The same release removes the old names from the code:')
P('')
P('- `reasons.py` and `messages.py`;')
P('- `review_retry.py` (its historical `SONARR_DISCOVERY_*` list goes away, because migrated rows carry `PROWLARR_*`);')
P('- `job_recovery.py`, `job_report.py`, `dashboard.js`, `fixes/` and the tests.')
P('')
P('For a **retired** code (§1a) the migration also sets `\"service\"` in `details_json` from the old code when the old code named one, and keeps any service already recorded.')
P('')
P('Log files already on disk keep the old names. A migration cannot change them.')
P('')
P('## 4. Full catalog: old and new wording')
P('')
P('Each entry shows the **old** text first and the **new** text after it. D = description (what happened), M = message (status line), A = action (what to do). Uses = references in non-test code, `fixes/`, the dashboard and SQL.')
P('')
P('### 4.1 Reason codes')
P('')
for k, v in R.items():
    r = REGISTRY[k]
    head = f'`{k}`' if k == v[0] else f'`{k}` → **`{v[0]}`**'
    P(f'#### {head}')
    P(f'*{r.severity}, retryable: {r.retryable}, uses: {uses(k)}*')
    P('')
    P('| | Old | New |')
    P('|---|---|---|')
    P(f'| D | {esc(r.description)} | {esc(v[1])} |')
    P(f'| M | {esc(r.message)} | {esc(v[2])} |')
    P(f'| A | {esc(r.action)} | {esc(v[3])} |')
    P('')
P('### 4.2 Event codes')
P('')
P('| Code | Old message | New message |')
P('|---|---|---|')
for k, v in E.items():
    head = f'`{k}`' if k == v[0] else f'`{k}` → **`{v[0]}`**'
    P(f'| {head} | {esc(messages.EVENTS[k].message)} | {esc(v[1])} |')
P('')
P('### 4.3 Retired reason codes (old wording, for reference)')
P('')
P('| Code | Old description | Replaced by |')
P('|---|---|---|')
for k, (target, _) in T.items():
    P(f'| `{k}` | {esc(REGISTRY[k].description)} | `{target}` |')
P('')
P('### 4.4 Removed reason codes (old wording, for reference)')
P('')
P('| Code | Old description |')
P('|---|---|')
for k in X:
    if k in REGISTRY:
        P(f'| `{k}` | {esc(REGISTRY[k].description)} |')
P('')
P('## 5. Open decisions')
P('')
P('1. **Wording review.** Please read §4, §1a and §1b and mark anything that is unclear or wrong, including the names `SEASON_PACK_SELECTION_CONFLICT`, `METADATA_ENRICHMENT_REVIEW` and `MEDIA_SERVER_REFRESH_REVIEW`.')
P('2. **How fine-grained the generic codes are.** A wrong API key and a missing address are both `SERVICE_SETUP_INVALID`; the details say which. A separate `SERVICE_AUTH_FAILED` is possible if you want to filter on it.')
P('3. **Severity of `SERVICE_SETUP_INVALID`.** It is ERROR, because it was created for the worker refusing to start. The optional-service setup codes it replaces were WARNING. Proposed: keep ERROR, since a service that is set up wrong always needs you to act.')
P('')
P('Decided (owner):')
P('')
P('- 2026-11-04: "trial" is replaced by "user evaluation" (`USER_EVALUATION`).')
P('- 2026-11-06: no product names in codes; the per-service codes are retired into generic service codes.')
P('- 2026-11-06: jobs stopped under the old Sonarr-based discovery migrate to `EPISODE_DISCOVERY_OUTDATED`.')
P('- 2026-11-06: behavior changes are allowed on `refactor` where they improve things; NextGen is not changed.')
P('')
P('## 6. Behavior changes RF-006 must make')
P('')
P('These go beyond renaming, and are allowed on `refactor` (owner decision, 2026-11-06):')
P('')
P('1. **Handlers report the generic code.** Every place that raises a retired code reports its replacement instead, with `service` (the adapter name) and a readable detail, such as `qBittorrent 4.3 is not supported; 4.5 or newer is required`. Facts that were in the old wording move into that detail. The service adapters (`services/adapters/`) already classify errors per service; their code-to-status maps change to the generic codes.')
P('2. **Severity and retryable** change where §1a shows them in bold.')
P('3. **Rules that match on codes** use the new ones:')
P('   - `review_retry` (its discovery BLOCKED list becomes `RELEASE_SEARCH_TIMEOUT`, `SERVICE_UNAVAILABLE` and `EPISODE_DISCOVERY_OUTDATED`);')
P('   - `job_recovery`, `job_report`, the dashboard filters and `fixes/`.')
P('4. **Dashboard and History show the service** for `SERVICE_*` codes, from the details, because the message no longer names it.')
P('5. **The service-wait path** (`worker._service_wait`) no longer needs the per-service code as a `cause_reason_code`, because the handler reports `SERVICE_UNAVAILABLE` with the service directly. Waiting, the 72-hour give-up and `adm recover` stay as they are.')
P('')
P('Proposed follow-up, not part of this audit: **services behind role adapters**. Each role has a small interface the rest of the code calls, and the adapter for the current service implements it:')
P('')
P('- download client (qBittorrent);')
P('- release search (Prowlarr);')
P('- library manager for Keep (Sonarr);')
P('- media server (Jellyfin);')
P('- anime library service (Shoko);')
P('- metadata (AniDB, TVDB, Anime-Lists).')
P('')
P('Replacing a service then means writing an adapter, with no code renames. The adapters stay in the repository (no third-party plugin loading), and each interface starts with what the code calls today.')
print('\n'.join(out))
