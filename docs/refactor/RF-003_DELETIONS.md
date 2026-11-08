# RF-003 deletion summary

This change removes confirmed dead code identified in [DEAD_CODE_AUDIT.md](DEAD_CODE_AUDIT.md). It does not change behavior. Every removal below points to its audit entry.

**Base:** `refactor` at `54889b96`, which includes NextGen `304a76f1`.
**Source size** (`src/anime`, excluding tests): 27,854 → **27,763 lines (−91)**. 115 lines were deleted and 24 lines were re-wrapped as shorter import statements, across 34 files.

## Removed definitions

| Audit | Item | Evidence |
|---|---|---|
| D1 | `sonarr.Client.add_payload()` | Its only caller was removed in `b0ca9e37`. Keep now requires the series to already exist. This was the last piece of the superseded Sonarr add-series path. |
| D2 | `acquisition/prowlarr_search._release()` | Its logic was inlined into `Client._search()` in `45e1876a`. |
| D3 | `acquisition/audio.audio_tagged()` and its `_TAG` regex | Replaced by `dual_audio_tagged()` in `c9c14c35`. |
| D5 | `acquisition/audio.MODES` | Nothing referenced it. |
| D6 | `storage/prowlarr_snapshots.MAX_INDEXER_LEN` | Nothing referenced it, and removing it changes no bound. |
| D7 | 51 unused imported names in 30 files | None of them are re-exported or used as patch targets. Where a whole import line was dropped, the imported module has no import-time side effects. |

## Removed tests

The removed tests covered only deleted code:

- `test_audio_check.AudioTagTests.test_common_group_spellings_count_as_audio_tags`
- `test_audio_check.AudioTagTests.test_subtitles_and_plain_titles_do_not_count`

Both tested only `audio_tagged()`. The live `dual_audio_tagged()` keeps its own test, which also covers the "Multi-Subs is not audio" rule. No test was weakened to make the refactor pass.

## Reverted removal

- **D4, `storage/sonarr_backfill.audio_exhausted()`**, was restored. The operator script `fixes/requeue_waiting_for_dub.py` imports it, and the audit had not scanned `fixes/`. The full suite caught this.
- The function is restored unchanged in its own commit and reclassified as required in the audit.
- Every other removal and every potentially-dead item was rechecked against `fixes/`, `bin/` and `deploy/`, and no other references were found.

## Intentionally not removed

- `prune_snapshots()` was kept by owner decision. It is unwired retention code, and wiring it in is NextGen work.
- `audio_exhausted()` stays because `fixes/` uses it.
- P2–P14, the potentially-dead items, stay pending an owner decision.
- Migrations, compatibility code, the `schema/__init__.py` re-exports and `StagingConfiguration`.
- No duplicates were consolidated. The audit found no equivalent duplicates large enough to justify a shared helper.

## Follow-ups found during RF-003

These need NextGen behavior changes or owner decisions, so none of them were changed here.

1. **`ACQUISITION_KINDS` uses the wrong batch kind names** (`acquisition/readiness.py`, added in #261).
   - It lists `BATCH_SUBMIT`, `BATCH_TRANSFER` and `BATCH_UNLOCK`. The real kinds are `SUBMIT_BATCH`, `TRANSFER_BATCH` and `UNLOCK_BATCH`.
   - As a result, those batch jobs are still claimed while qBittorrent is not ready.
2. **The Sonarr request allowlist** (`sonarr.py:98`) still permits `series/lookup`, `rootfolder` and `qualityprofile`. Only the removed `add_payload()` used them.
3. **The `sonarr.quality_profile_id` setting** is still validated and captured, but nothing uses it now. Its help text, "Required for new series", is no longer accurate. It is persisted configuration.
4. **`SONARR_SELECTION_CONFLICT`** is used as a `reason_code` (`storage/sonarr_selection.py:15`, `storage/sonarr_candidates.py:9`) but is not registered in `reasons.py`.
5. **`fixes/test_remove_staged_torrents.test_decided_episodes_are_left_alone` was already failing on NextGen `aab7db0b`.** It asks `ManagementActions.submit` for an action that is no longer accepted. This is unrelated to the refactor.

## Validation

| Check | Result |
|---|---|
| `python -m src.anime.tests.run_batches` | 31/31 batches, **1,506 tests passed**, 0 failed. That is the previous 1,508 minus the 2 removed `audio_tagged` tests. |
| `python -m compileall -q src/anime fixes` | passed |
| `node --check …/dashboard.js` and `node --test …/dashboard_browser.test.cjs` | passed, 25/25 |
| `python -m src.anime --help` and `python -m src.anime.api --help` | Both entrypoints import cleanly. |
| `fixes/` tests | 22/23. The one error is the pre-existing follow-up 5 above, which also fails on NextGen. |
