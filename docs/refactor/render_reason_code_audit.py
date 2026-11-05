"""Regenerate REASON_CODE_AUDIT.md from reason_code_proposals.py.

Run from the repository root:
    python docs/refactor/render_reason_code_audit.py > docs/refactor/REASON_CODE_AUDIT.md
Reads the current catalog from src/nextgen (reasons.py, messages.py) for the
old wording and usage counts. Data only; nothing at runtime imports this.
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
    'SONARR_DISCOVERY_EPISODE_NOT_FOUND': ('Before `db41b589`; still listed in `review_retry`', '**Decision point (§5.1).** Sonarr\'s catalog is no longer consulted. The target keeps these BLOCKED jobs retry-eligible, which they are today.'),
    'SONARR_EPISODES_NOT_LISTED': ('Before `db41b589`; still listed in `review_retry`', 'Same as above.'),
    'KEEP_NOT_READY': ('Only on 2026-10-23 (`bf89c34b` to `e1a8bd3b`), before Keep existed', 'The person reviews the job and presses Keep again.'),
    'ACTIVITY_CHANGED': ('Registered in `df9655aa` but never emitted', 'No rows are expected. The target is only a safe fallback.'),
}

out = []
P = out.append
P('# Reason and event code audit')
P('')
P('**Base:** `refactor` at `9fdb64eb` (NextGen `9071be2e`). This is a review document: it changes no code.')
P('**Data:** [`reason_code_proposals.py`](reason_code_proposals.py) is the machine-readable source. Rebuild this page with `python docs/refactor/render_reason_code_audit.py > docs/refactor/REASON_CODE_AUDIT.md`. RF-006 applies the data.')
P('')
P('## Decisions this audit implements')
P('')
P('- Persisted identifiers are **renamed**. One versioned SQL migration rewrites the stored values, including `job_history`. The code recognizes **only the new values**, and old names are removed.')
P('- **"Trial" becomes "user evaluation"** (`USER_EVALUATION_*`) everywhere it can. The other places the term is used are mapped in [NAMING_MAP.md](NAMING_MAP.md) §8.')
P('- Every code\'s wording is rewritten to be human-readable: plain words, the service named, and a concrete next step.')
P('- **Severity and retryable flags are unchanged.** This audit changes wording and names only, not behavior.')
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
P(f'| Unregistered label fixed | {len(U)} | 0 |')
P(f'| New catalog | {len(R)} | {len(E)} |')
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
P('For every renamed or removed code, migration `059` rewrites:')
P('')
P('- **History:** `job_history.event_code` and `.reason_code`; `event_history.event_code` and `.reason_code`.')
P('- **Current state:** `jobs.last_reason_code`, `incoming_events.last_reason_code` and `job_operations.reason_code`.')
P('- **Per-step records:**')
P('  - `trial_evaluations.reason_code`, renamed in RF-006 (NAMING_MAP §8);')
P('  - `sonarr_episode_candidates.reason_code`, renamed in RF-006;')
P('  - `torrent_submissions.reason_code` and `torrent_transfers.reason_code`;')
P('  - `batch_torrent_submissions.reason_code`, `batch_torrent_transfers.reason_code` and `batch_torrent_unlocks.reason_code`.')
P('- **Codes inside JSON:** `job_history.details_json` and `event_history.details_json` (`cause_reason_code`). Also any stored JSON that embeds codes, such as `prowlarr_search_snapshots.snapshot_json`. RF-006 checks each JSON column with `json_extract`/`json_tree` before writing the update.')
P('')
P('These columns were inventoried from the current schema (every column named `*reason*`, `event_code` or `*_code`). `activity_code` and `handler_code` hold activity and handler codes, not reason codes. `handler_code` changes only for the job-kind renames (`DISCOVER_SONARR_BACKFILL`, `EVALUATE_TRIAL`, `STAGE_TRIAL_MEDIA`).')
P('')
P('The same release removes the old names from the code:')
P('')
P('- `reasons.py` and `messages.py`;')
P('- `review_retry.py` (its historical `SONARR_DISCOVERY_*` list goes away, because migrated rows carry `PROWLARR_*`);')
P('- `job_recovery.py`, `job_report.py`, `dashboard.js`, `fixes/` and the tests.')
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
P('### 4.3 Removed reason codes (old wording, for reference)')
P('')
P('| Code | Old description |')
P('|---|---|')
for k in X:
    if k in REGISTRY:
        P(f'| `{k}` | {esc(REGISTRY[k].description)} |')
P('')
P('## 5. Open decisions')
P('')
P('1. **`SONARR_DISCOVERY_EPISODE_NOT_FOUND` and `SONARR_EPISODES_NOT_LISTED`.** These meant "Sonarr has no record of this series or its episodes", but Sonarr is no longer part of discovery. The proposal migrates them to `PROWLARR_UNAVAILABLE`, which keeps the affected jobs retry-eligible with no new code. The alternative is a dedicated historical code such as `DISCOVERY_STOPPED_BEFORE_PROWLARR` ("This search stopped under the old Sonarr-based discovery; press Search again to search Prowlarr"). That is clearer in History, but it adds a code that nothing emits.')
P('2. **Wording review.** Please read §4 and mark anything that is unclear or wrong. The wording is the point of this audit.')
P('')
P('Decided: "trial" is replaced by "user evaluation" (`USER_EVALUATION`), owner decision on 2026-11-04.')
P('')
P('## 6. Notes for the service-health foundation (later)')
P('')
P('`DOWNLOAD_CLIENT_UNAVAILABLE`, `KEEP_SERVICE_UNAVAILABLE`, `SHOKO_UNAVAILABLE`, `JELLYFIN_UNAVAILABLE`, `PROWLARR_UNAVAILABLE` and `PROVIDER_UNAVAILABLE` describe the same situation (a service cannot be reached) with different retry behavior. Their wording is aligned here. Their behavior is left for the planned cross-service health foundation.')
print('\n'.join(out))
