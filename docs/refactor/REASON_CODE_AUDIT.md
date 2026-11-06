# Reason and event code audit

**Base:** `refactor` at `8c1d25a0` (NextGen `29fce109`, with `services` merged). First written against `9fdb64eb` (NextGen `9071be2e`); §0 lists the codes added since. This is a review document: it changes no code.
**Kept current by** `src/nextgen/tests/test_reason_code_audit.py`: it fails when a registered code has no proposal here, so a NextGen sync that adds a code cannot slip past the audit.
**Data:** [`reason_code_proposals.py`](reason_code_proposals.py) is the machine-readable source. Rebuild this page with `python docs/refactor/render_reason_code_audit.py > docs/refactor/REASON_CODE_AUDIT.md`. RF-006 applies the data.

## Decisions this audit implements

- Persisted identifiers are **renamed**. One versioned SQL migration rewrites the stored values, including `job_history`. The code recognizes **only the new values**, and old names are removed.
- **"Trial" becomes "user evaluation"** (`USER_EVALUATION_*`) everywhere it can. The other places the term is used are mapped in [NAMING_MAP.md](NAMING_MAP.md) §8.
- Every code's wording is rewritten to be human-readable: plain words, the service named, and a concrete next step.
- **Severity and retryable flags are unchanged.** This audit changes wording and names only, not behavior.

## Summary

| | Reason codes | Event codes |
|---|---|---|
| Current catalog | 166 | 52 |
| Kept, wording rewritten | 146 | 47 |
| Kept, **renamed** | 10 | 4 |
| **Removed** (no longer emitted; stored rows migrate) | 10 | 1 |
| Unregistered label fixed | 0 | 0 |
| New catalog | 156 | 51 |

## 0. Codes added since the first audit

| Code | Kind | From | Proposal |
|---|---|---|---|
| `SONARR_SELECTION_CONFLICT` | reason | NextGen #283 | renamed to **`RELEASE_SELECTION_CONFLICT`** |
| `BATCH_SELECTION_CONFLICT` | reason | NextGen #283 | renamed to **`SEASON_PACK_SELECTION_CONFLICT`** |
| `MANUAL_RELEASE_EPISODE_MISMATCH` | reason | NextGen #286 | kept |
| `MANUAL_RELEASE_TITLE_UNREADABLE` | reason | NextGen #286 | kept |
| `MANUAL_RELEASE_SOURCE_INVALID` | reason | NextGen #286 | kept |
| `SERVICE_SETUP_INVALID` | reason | `services` (#279, merged in #284) | kept |
| `SERVICE_UNAVAILABLE` | reason | `services` (#279, merged in #284) | kept |
| `SERVICE_DOWN` | event | `services` (#279, merged in #284) | kept |
| `SERVICE_RESTORED` | event | `services` (#279, merged in #284) | kept |
| `SERVICE_STARTUP_REFUSED` | event | `services` (#279, merged in #284) | kept |

## 1. Renamed codes

| Old | New | Kind | Why |
|---|---|---|---|
| `SONARR_BACKFILL_PLAN_CONFLICT` | `EPISODE_DISCOVERY_PLAN_CONFLICT` | reason | Episode discovery searches Prowlarr, not Sonarr. |
| `SONARR_CANDIDATES_EXHAUSTED` | `RELEASE_CANDIDATES_EXHAUSTED` | reason | Release candidates come from Prowlarr, not Sonarr. |
| `TORRENT_PROVENANCE_CONFLICT` | `TORRENT_OWNERSHIP_CONFLICT` | reason | "Provenance" is jargon. The check is whether the torrent still matches what this work added. |
| `TRIAL_SELECTED` | `USER_EVALUATION_SELECTED` | reason | "Trial" is replaced by "user evaluation" everywhere (owner decision). |
| `TRIAL_OUTSIDE_WINDOW` | `USER_EVALUATION_OUTSIDE_WINDOW` | reason | "Trial" is replaced by "user evaluation" everywhere (owner decision). |
| `TRIAL_DISABLED` | `USER_EVALUATION_DISABLED` | reason | "Trial" is replaced by "user evaluation" everywhere (owner decision). |
| `TRIAL_PLAN_CONFLICT` | `USER_EVALUATION_PLAN_CONFLICT` | reason | "Trial" is replaced by "user evaluation" everywhere (owner decision). |
| `TRIAL_COUNTS_INVALID` | `USER_EVALUATION_COUNTS_INVALID` | reason | "Trial" is replaced by "user evaluation" everywhere (owner decision). |
| `SONARR_SELECTION_CONFLICT` | `RELEASE_SELECTION_CONFLICT` | reason | Releases come from Prowlarr, not Sonarr. Registered by NextGen #283; it appears only in the worker log, because the job itself ends with `JOB_HANDLER_FAILED`. |
| `BATCH_SELECTION_CONFLICT` | `SEASON_PACK_SELECTION_CONFLICT` | reason | Matches `RELEASE_SELECTION_CONFLICT`, and says "season pack", the word the dashboard uses, instead of the internal "batch". Same log-only use. |
| `TORRENT_INTENT_RECORDED` | `TORRENT_SUBMISSION_RECORDED` | event | "Intent" is jargon. The event records that the torrent is about to be added. |
| `TRIAL_EVALUATED` | `USER_EVALUATION_CHECKED` | event | "Trial" is replaced by "user evaluation" everywhere (owner decision). |
| `TRIAL_AUDIO_SEARCH` | `USER_EVALUATION_AUDIO_SEARCH` | event | "Trial" is replaced by "user evaluation" everywhere (owner decision). |
| `SONARR_BACKFILL_OBSERVED` | `EPISODE_DISCOVERY_RECORDED` | event | Episode discovery searches Prowlarr. The old text said "Read-only Sonarr backfill results recorded." |

## 2. Removed codes and where their stored rows migrate

| Removed | Migrates to | Last emitted / evidence | Note |
|---|---|---|---|
| `SONARR_DISCOVERY_AUTH_FAILED` | `PROWLARR_AUTH_FAILED` | Before `db41b589` (Sonarr-based discovery) | A retry now searches Prowlarr. |
| `SONARR_DISCOVERY_CONFIGURATION_INVALID` | `PROWLARR_CONFIGURATION_INVALID` | Before `db41b589` | Same as above. |
| `SONARR_DISCOVERY_UNSUPPORTED` | `PROWLARR_CONFIGURATION_INVALID` | Before `db41b589` | The wrong server version was a setup problem. |
| `SONARR_DISCOVERY_REVIEW` | `PROWLARR_SEARCH_REVIEW` | Before `db41b589` |  |
| `SONARR_DISCOVERY_TIMEOUT` | `PROWLARR_SEARCH_TIMEOUT` | Before `db41b589`; still listed in `review_retry` |  |
| `SONARR_DISCOVERY_UNAVAILABLE` | `PROWLARR_UNAVAILABLE` | Before `db41b589`; still listed in `review_retry` |  |
| `SONARR_DISCOVERY_NO_RESULTS` | `PROWLARR_RELEASE_WITHDRAWN` | Before `db41b589` | Same meaning: the ranked fallback is no longer offered. |
| `SONARR_DISCOVERY_EPISODE_NOT_FOUND` | `PROWLARR_UNAVAILABLE` | Before `db41b589`; still listed in `review_retry` | **Decision point (§5.1).** Sonarr's catalog is no longer consulted. The target keeps these BLOCKED jobs retry-eligible, which they are today. |
| `SONARR_EPISODES_NOT_LISTED` | `PROWLARR_UNAVAILABLE` | Before `db41b589`; still listed in `review_retry` | Same as above. |
| `KEEP_NOT_READY` | `KEEP_IMPORT_REVIEW` | Only on 2026-10-23 (`bf89c34b` to `e1a8bd3b`), before Keep existed | The person reviews the job and presses Keep again. |
| `ACTIVITY_CHANGED` | `DEFERRED` | Registered in `df9655aa` but never emitted | No rows are expected. The target is only a safe fallback. |

## 3. What the migration must rewrite (RF-006)

For every renamed or removed code, the RF-006 migration rewrites the columns below. It will be `063`: `060` and `061` are the batch candidate tables and `062` is `service_status`.

- **History:** `job_history.event_code` and `.reason_code`; `event_history.event_code` and `.reason_code`.
- **Current state:** `jobs.last_reason_code`, `incoming_events.last_reason_code` and `job_operations.reason_code`.
- **Per-step records:**
  - `trial_evaluations.reason_code`, renamed in RF-006 (NAMING_MAP §8);
  - `sonarr_episode_candidates.reason_code`, renamed in RF-006;
  - `torrent_submissions.reason_code` and `torrent_transfers.reason_code`;
  - `batch_torrent_submissions.reason_code`, `batch_torrent_transfers.reason_code` and `batch_torrent_unlocks.reason_code`;
  - `batch_candidates.reason_code` and `batch_candidate_stops.reason_code`, why a season pack candidate stopped (added by the batch candidate controller, migrations `060` and `061`).
- **Codes inside JSON:** `job_history.details_json` and `event_history.details_json` (`cause_reason_code`). Also any stored JSON that embeds codes, such as `prowlarr_search_snapshots.snapshot_json`, `identity_candidate_evidence.evidence_json` and `batch_candidates.metadata_json`. RF-006 scans **every** `*_json` column with `json_tree` for old code values before writing the update, rather than trusting this list.

These columns were inventoried from the current schema (every column named `*reason*`, `event_code` or `*_code`). `activity_code` and `handler_code` hold activity and handler codes, not reason codes. `handler_code` changes only for the job-kind renames (`DISCOVER_SONARR_BACKFILL`, `EVALUATE_TRIAL`, `STAGE_TRIAL_MEDIA`).

The same release removes the old names from the code:

- `reasons.py` and `messages.py`;
- `review_retry.py` (its historical `SONARR_DISCOVERY_*` list goes away, because migrated rows carry `PROWLARR_*`);
- `job_recovery.py`, `job_report.py`, `dashboard.js`, `fixes/` and the tests.

Log files already on disk keep the old names. A migration cannot change them.

## 4. Full catalog: old and new wording

Each entry shows the **old** text first and the **new** text after it. D = description (what happened), M = message (status line), A = action (what to do). Uses = references in non-test code, `fixes/`, the dashboard and SQL.

### 4.1 Reason codes

#### `PROWLARR_CONFIGURATION_INVALID`
*WARNING, retryable: False, uses: 4*

| | Old | New |
|---|---|---|
| D | The Prowlarr address or API key is missing or invalid. | The Prowlarr address or API key is missing or not valid. |
| M | Configure Prowlarr before backfill search. | Prowlarr is not set up. |
| A | Set the Prowlarr address and API key in Settings, then retry; no download occurred. | Enter the Prowlarr address and API key in Settings, then press Retry. Nothing was downloaded. |

#### `PROWLARR_AUTH_FAILED`
*WARNING, retryable: False, uses: 4*

| | Old | New |
|---|---|---|
| D | Prowlarr rejected the configured API key. | Prowlarr rejected the API key. |
| M | Prowlarr search authentication failed. | Prowlarr did not accept the API key. |
| A | Check the Prowlarr API key in Settings, then retry. | Check the Prowlarr API key in Settings, then press Retry. |

#### `PROWLARR_SEARCH_REVIEW`
*WARNING, retryable: False, uses: 4*

| | Old | New |
|---|---|---|
| D | Prowlarr returned a response that could not be read safely. | Prowlarr sent back a search response that could not be read. |
| M | Prowlarr search needs review. | The Prowlarr search result could not be read. |
| A | Check Prowlarr and its indexers, then retry. | Check that Prowlarr and its indexers are working, then press Retry. |

#### `PROWLARR_SEARCH_TIMEOUT`
*ERROR, retryable: True, uses: 4*

| | Old | New |
|---|---|---|
| D | A Prowlarr search exceeded the configured timeout three times in a row. | The Prowlarr search took longer than the allowed time, three times in a row. |
| M | Prowlarr search timed out. | The Prowlarr search timed out. |
| A | The search runs again once after 30 minutes. If it still times out, check Prowlarr and indexer response times or raise the timeout, then retry. | Nothing to do yet: the search runs once more in 30 minutes. If it times out again, check Prowlarr and its indexers or raise the search timeout, then press Retry. |

#### `PROWLARR_UNAVAILABLE`
*ERROR, retryable: True, uses: 4*

| | Old | New |
|---|---|---|
| D | Prowlarr could not be reached, was busy, or failed the search three times in a row. | Prowlarr could not be reached, was busy, or failed the search three times in a row. |
| M | Prowlarr search failed. | Prowlarr could not be searched. |
| A | The search runs again once after 30 minutes. If it still fails, check that Prowlarr and its indexers are running, then retry. | Nothing to do yet: the search runs once more in 30 minutes. If it fails again, make sure Prowlarr and its indexers are running, then press Retry. |

#### `PROWLARR_RELEASE_WITHDRAWN`
*WARNING, retryable: True, uses: 1*

| | Old | New |
|---|---|---|
| D | The next ranked torrent is no longer returned by Prowlarr. | The next release in the ranked list is no longer offered by Prowlarr. |
| M | Trying the next ranked release. | Moving on to the next release. |
| A | No action needed; the next ranked release is tried without a new torrent being sent. | Nothing to do. The next release in the list is tried. |

#### `SONARR_BACKFILL_PLAN_CONFLICT` → **`EPISODE_DISCOVERY_PLAN_CONFLICT`**
*WARNING, retryable: False, uses: 4*

| | Old | New |
|---|---|---|
| D | Frozen backfill scope or stored search receipt did not match the worker plan. | The saved list of episodes to search no longer matches what this search job expected. |
| M | Backfill discovery needs review. | The episode search plan changed and needs review. |
| A | Check the immutable episode window and persisted discovery receipts; no download occurred. | Open the job report (adm report job --id <job>) to compare the saved episode list, then press Retry. Nothing was downloaded. |

#### `SONARR_CANDIDATES_EXHAUSTED` → **`RELEASE_CANDIDATES_EXHAUSTED`**
*WARNING, retryable: False, uses: 4*

| | Old | New |
|---|---|---|
| D | Every ranked release found for this episode failed or could not be acquired. | Every release found for this episode was tried, and each one failed or could not be downloaded. |
| M | All available releases have failed; review this episode. | All releases for this episode failed. |
| A | Check the episode on your indexers, then retry to search again. | Check whether a better release exists on your indexers, then press Search again. |

#### `WAITING_FOR_DUB`
*INFO, retryable: True, uses: 34*

| | Old | New |
|---|---|---|
| D | English audio is required and no usable Dual/Multi Audio release was advertised, or every attempted release failed and at least one lacked a required audio track. | English audio is required, but no release advertises both Japanese and English audio, or every release tried was missing one of them. |
| M | Waiting for a dub; no verified Japanese and English audio release is available. | Waiting for a release with Japanese and English audio. |
| A | New Dual/Multi Audio autobrr releases can still start Intake. Use Search again to check Prowlarr for an eligible release. | Nothing to do. A new Dual Audio release from autobrr starts automatically, or press Search again to check Prowlarr now. |

#### `SHOKO_CONFIGURATION_INVALID`
*WARNING, retryable: False, uses: 2*

| | Old | New |
|---|---|---|
| D | Optional Shoko address or API key is missing or rejected. | The Shoko address or API key is missing or was rejected. |
| M | Configure optional Shoko metadata. | Shoko is not set up. |
| A | Update Shoko settings/credentials, then retry Shoko metadata. The episode remains kept. | Fix the Shoko settings, then retry the Shoko step. The episode is still kept. |

#### `SHOKO_UNAVAILABLE`
*WARNING, retryable: True, uses: 2*

| | Old | New |
|---|---|---|
| D | Optional Shoko metadata could not be read. | Shoko could not be reached to read episode details. |
| M | Optional Shoko metadata will retry. | Shoko is unavailable; it will be tried again. |
| A | Check Shoko availability. Keep and cleanup remain independent. | Nothing to do unless this continues; then check that Shoko is running. Keeping and cleanup are not affected. |

#### `SHOKO_METADATA_REVIEW`
*WARNING, retryable: False, uses: 2*

| | Old | New |
|---|---|---|
| D | Shoko did not return the expected AniDB anime and episode records. | Shoko did not return the expected anime and episode for the saved AniDB IDs. |
| M | Optional Shoko metadata needs review. | Shoko episode details need review. |
| A | Check the Shoko catalog and saved AniDB IDs, then retry Shoko metadata. The Sonarr mapping is unchanged. | Check that Shoko has this anime and episode, then retry the Shoko step. |

#### `DECISION_DOWNLOAD_CLEANUP_COMPLETE`
*INFO, retryable: False, uses: 4*

| | Old | New |
|---|---|---|
| D | The owned torrent is absent and its recorded download files have been removed. | The torrent is gone from qBittorrent and its downloaded files were deleted. |
| M | Download cleanup completed. | Download cleanup finished. |
| A | Staged/shared metadata, permanent media and episode history are retained. | Nothing to do. Staged files, the library copy and the episode history are kept. |

#### `DECISION_DOWNLOAD_CLEANUP_WAITING`
*INFO, retryable: None, uses: 7*

| | Old | New |
|---|---|---|
| D | Waiting for drained work or reconciliation of a saved torrent control request. | Cleanup is waiting for other work on this download to finish, or for qBittorrent to confirm an earlier stop or remove request. |
| M | Download cleanup is waiting. | Download cleanup is waiting. |
| A | The worker rechecks exact ownership and may repeat safe stop or torrent-only remove commands. Local files are cleaned only after torrent absence is confirmed. | Nothing to do. Files are deleted only after qBittorrent confirms the torrent is gone. |

#### `DECISION_DOWNLOAD_CLEANUP_UNAVAILABLE`
*WARNING, retryable: True, uses: 5*

| | Old | New |
|---|---|---|
| D | The download client or filesystem is temporarily unavailable. | qBittorrent or the download folder could not be reached. |
| M | Download cleanup will retry. | Download cleanup will try again. |
| A | Check the download client and storage mounts. The staged video and any Keep or Reject result remain valid. | Check that qBittorrent is running and the download folder is mounted. The staged video and the Keep or Reject decision are not affected. |

#### `DECISION_DOWNLOAD_CLEANUP_REVIEW`
*WARNING, retryable: False, uses: 10*

| | Old | New |
|---|---|---|
| D | Download ownership, source identity, staging or decision completion could not be confirmed. | Cleanup could not confirm that this download belongs to this episode, or that its Keep or Reject step finished. |
| M | Download cleanup needs review. | Download cleanup needs review. |
| A | Review the download client and recorded files, then retry download cleanup. | Check the torrent in qBittorrent and the files listed in the job report, then press Retry. |

#### `CANCELED_RESERVATIONS_RELEASED`
*INFO, retryable: False, uses: 3*

| | Old | New |
|---|---|---|
| D | Cancellation cleanup completed and its acquisition slots were retired without deleting history. | Cleanup after a cancellation finished, so the episodes this work had reserved are free again. History is kept. |
| M | Canceled episode slots are available for a later intake. | Canceled episodes can be downloaded again. |
| A | A later verified release may reserve the episode again. Kept episodes remain protected. | Nothing to do. A later release can pick these episodes up. Kept episodes stay protected. |

#### `POST_KEEP_CLEANUP_COMPLETE`
*INFO, retryable: False, uses: 2*

| | Old | New |
|---|---|---|
| D | Selected staged artifacts were removed after confirming the permanent result. | The staged copies were deleted after the library copy was confirmed. |
| M | Selected staging files removed. | Staged files removed. |
| A | The episode remains in the permanent library. | Nothing to do. The episode is in the library. |

#### `POST_KEEP_CLEANUP_WAITING`
*INFO, retryable: None, uses: 3*

| | Old | New |
|---|---|---|
| D | Another attempt is still cleaning this kept episode. | Another attempt is still cleaning up the staged files for this kept episode. |
| M | Waiting to finish staged-file cleanup. | Waiting to finish cleaning up staged files. |
| A | The worker will check again. The episode remains kept. | Nothing to do. The episode is already kept. |

#### `POST_KEEP_CLEANUP_UNAVAILABLE`
*WARNING, retryable: True, uses: 3*

| | Old | New |
|---|---|---|
| D | Sonarr or the staging/permanent filesystem is temporarily unavailable. | Sonarr, the staging folder or the library folder could not be reached. |
| M | The episode is kept, but staged-file cleanup will retry. | The episode is kept; staged-file cleanup will try again. |
| A | Check service and mount availability. | Check that Sonarr is running and the folders are mounted. |

#### `POST_KEEP_CLEANUP_REVIEW`
*WARNING, retryable: False, uses: 4*

| | Old | New |
|---|---|---|
| D | The saved Keep result or selected staging artifacts could not be confirmed. | The saved Keep result or the staged files to delete could not be confirmed. |
| M | The episode is kept, but staged-file cleanup needs review. | The episode is kept, but staged-file cleanup needs review. |
| A | Check the permanent file and selected staged artifacts, then submit Keep again to resume only cleanup. | Check the library file and the staged files, then press Keep again. Only the cleanup runs again. |

#### `JELLYFIN_CONFIGURATION_INVALID`
*WARNING, retryable: False, uses: 3*

| | Old | New |
|---|---|---|
| D | The Jellyfin endpoint or API key is missing or rejected. | The Jellyfin address or API key is missing or was rejected. |
| M | Configure Jellyfin refresh settings and credentials. | Jellyfin is not set up. |
| A | Review the address and private API key. Completed staging and Keep remain valid. | Fix the Jellyfin address and API key in Settings. Staging and Keep are not affected. |

#### `JELLYFIN_UNAVAILABLE`
*WARNING, retryable: True, uses: 3*

| | Old | New |
|---|---|---|
| D | Jellyfin did not accept the library scan request. | Jellyfin did not accept the library scan request. |
| M | Jellyfin refresh is temporarily unavailable. | Jellyfin is unavailable; the scan will be requested again. |
| A | The refresh job will retry; completed media operations remain valid. | Nothing to do unless this continues; then check that Jellyfin is running. |

#### `JELLYFIN_REFRESH_REVIEW`
*WARNING, retryable: False, uses: 2*

| | Old | New |
|---|---|---|
| D | The server returned an unexpected scan response. | Jellyfin answered the library scan request in an unexpected way. |
| M | Jellyfin refresh needs review. | The Jellyfin library scan needs review. |
| A | Check the address and server API compatibility. | Check the Jellyfin address and that your Jellyfin version is supported. |

#### `KEEP_CONFIGURATION_INVALID`
*WARNING, retryable: False, uses: 3*

| | Old | New |
|---|---|---|
| D | Sonarr endpoint, credentials, profile or library root is unconfigured. | The Sonarr address, API key or library folder is not set. |
| M | Configure Sonarr before keeping this episode. | Sonarr is not set up for Keep. |
| A | Review Sonarr settings and credentials, then submit Keep again. | Fix the Sonarr settings, then press Keep again. |

#### `KEEP_IMPORT_WAITING`
*INFO, retryable: None, uses: 2*

| | Old | New |
|---|---|---|
| D | The saved series or import request is being reconciled. | The import into Sonarr is in progress and its result is being checked. |
| M | The Keep job is in progress. | Keeping this episode is in progress. |
| A | The job will check Sonarr again shortly. | Nothing to do. Sonarr is checked again shortly. |

#### `KEEP_SERVICE_UNAVAILABLE`
*WARNING, retryable: True, uses: 4*

| | Old | New |
|---|---|---|
| D | The configured Sonarr service could not be reached. | Sonarr could not be reached. |
| M | Sonarr is temporarily unavailable. | Sonarr is unavailable; Keep will try again. |
| A | The job will retry; check the Sonarr connection if this continues. | Nothing to do unless this continues; then check that Sonarr is running. |

#### `KEEP_IMPORT_REVIEW`
*WARNING, retryable: False, uses: 9*

| | Old | New |
|---|---|---|
| D | The selected file, TVDB mapping or saved import result could not be confirmed. | The staged file, its TVDB episode, or the Sonarr import result could not be confirmed. |
| M | The Keep job needs review. | Keeping this episode needs review. |
| A | Check the staged file and Sonarr import result, then submit Keep again. | Check the staged file and the episode in Sonarr, then press Keep again. |

#### `KEEP_COMPLETE`
*INFO, retryable: False, uses: 3*

| | Old | New |
|---|---|---|
| D | Sonarr episode-file binding and the permanent file are confirmed. | Sonarr imported the file and the library copy was confirmed. |
| M | This episode has been kept. | This episode is kept. |
| A | The episode is available in the permanent library. | Nothing to do. The episode is in the library. |

#### `ANIME_REJECTED`
*INFO, retryable: False, uses: 3*

| | Old | New |
|---|---|---|
| D | An explicit Reject decision stops acquisition for this AniDB anime. | Someone rejected this anime, so no more episodes are downloaded for it. |
| M | This anime was rejected; automatic acquisition is stopped. | This anime was rejected; downloads are stopped. |
| A | Review the recorded Reject decision before acquiring more episodes. | Nothing to do. To download it again, review the Reject decision first. |

#### `REJECT_CLEANUP_REVIEW`
*WARNING, retryable: False, uses: 3*

| | Old | New |
|---|---|---|
| D | Selected staged files could not be removed according to their recorded paths. | Some staged files of the rejected anime could not be deleted at their recorded paths. |
| M | The anime is rejected, but staged-file cleanup needs attention. | The anime is rejected, but some staged files were not removed. |
| A | Review the selected staged files and the existing Reject job. | Check the staged files listed in the Reject job, then press Retry. |

#### `STAGING_REVIEW`
*WARNING, retryable: False, uses: 4*

| | Old | New |
|---|---|---|
| D | Destination, copy identity, source digest or publication evidence could not be reconciled. | The staged copy could not be confirmed: the destination, the copied file or its checksum did not match. |
| M | Staging needs review; existing files have been preserved. | Staging needs review; no files were overwritten. |
| A | Check the worker logs and recorded staging paths, then retry this staging job. Recorded partial files remain available for recovery and cleanup. | Check the worker log and the staging paths in the job report, then press Retry. Partial files are kept for recovery. |

#### `STAGING_UNAVAILABLE`
*ERROR, retryable: True, uses: 4*

| | Old | New |
|---|---|---|
| D | A staging filesystem operation failed transiently. | A file operation in the staging folder failed, probably temporarily. |
| M | The staging filesystem is temporarily unavailable. | The staging folder is unavailable. |
| A | Check mounts, free space and worker permissions. | Check that the staging folder is mounted, has free space, and is writable by the worker. |

#### `STAGING_CONFIGURATION_INVALID`
*WARNING, retryable: None, uses: 1*

| | Old | New |
|---|---|---|
| D | An accessible disjoint staging root, non-root worker or Linux no-replace publication is unavailable. | The staging folder is missing, not writable, overlaps another folder, or the worker is running as root. |
| M | Configure an accessible separate staging folder and a non-root worker. | The staging folder is not set up correctly. |
| A | Configure the staging folder and worker service permissions. | Set a separate staging folder the worker account can write to, and run the worker as its service account. |

#### `STAGING_BUSY`
*INFO, retryable: None, uses: 1*

| | Old | New |
|---|---|---|
| D | An exact publication attempt or shared series-metadata operation is still active. | Another staging step for this episode or series is still running. |
| M | Waiting for another staging operation to finish. | Waiting for another staging step to finish. |
| A | The worker will check again shortly. | Nothing to do. It is checked again shortly. |

#### `MEDIA_VERIFICATION_INVALID`
*WARNING, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | The selected file, container or pinned content changed or failed validation. | The downloaded video changed, or it failed the file and container checks. |
| M | The downloaded video needs verification review. | The downloaded video failed verification. |
| A | Review the selected file and its recorded acquisition evidence. | Check the video file and the release it came from. |

#### `MEDIA_VERIFICATION_UNAVAILABLE`
*ERROR, retryable: None, uses: 3*

| | Old | New |
|---|---|---|
| D | Local selected-file reads or the bounded probe timed out. | The video could not be read, or checking it took too long. |
| M | The downloaded video could not be checked yet. | The downloaded video could not be checked yet. |
| A | Check the shared download mount and file readability. | Check that the download folder is mounted and the file is readable. |

#### `MEDIA_PROBE_UNAVAILABLE`
*WARNING, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | The fixed local ffprobe executable could not be started. | ffprobe could not be started to inspect the video. |
| M | The local media probe is unavailable. | The video checker (ffprobe) is not available. |
| A | Install ffprobe at /usr/bin/ffprobe and check worker permissions. | Install ffprobe at /usr/bin/ffprobe and make sure the worker can run it. |

#### `MEDIA_AUDIO_REQUIRED`
*WARNING, retryable: False, uses: 9*

| | Old | New |
|---|---|---|
| D | The non-commentary audio language tags required by the audio check setting were not established. | The video does not have the audio languages required by the Audio check setting (commentary tracks do not count). |
| M | The video is missing a required audio track. | The video is missing a required audio language. |
| A | Review the audio streams or the Audio check setting; filenames and subtitles do not satisfy this requirement. | Check the audio tracks, or change the Audio check setting. File names and subtitles do not count. |

#### `TORRENT_METADATA_CONFIGURATION_INVALID`
*WARNING, retryable: False, uses: 3*

| | Old | New |
|---|---|---|
| D | qBittorrent metadata inspection needs valid client configuration and credentials. | qBittorrent is not set up, so the torrent file list cannot be read. |
| M | Configure qBittorrent before inspecting torrent metadata. | qBittorrent is not set up. |
| A | Check the qBittorrent server, download directory and stored credentials. | Check the qBittorrent address, download folder and login in Settings, then press Retry. |

#### `TORRENT_METADATA_CLIENT_UNSUPPORTED`
*WARNING, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | The connected Web API cannot retrieve portable hash-verified torrent metadata. | This qBittorrent version cannot export a batch torrent file list. |
| M | This qBittorrent version cannot export the batch torrent metadata. | This qBittorrent version cannot read batch torrent files. |
| A | The older-client batch path needs an owned, stopped magnet admission and verified inventory; do not submit or adopt an unrelated torrent. | Upgrade qBittorrent. Do not add the torrent by hand. |

#### `TORRENT_METADATA_UNAVAILABLE`
*WARNING, retryable: True, uses: 2*

| | Old | New |
|---|---|---|
| D | A qBittorrent metadata-only request failed or timed out. | Asking qBittorrent for the torrent file list failed or took too long. |
| M | qBittorrent metadata retrieval is temporarily unavailable. | qBittorrent could not provide the torrent file list yet. |
| A | Check the qBittorrent connection if retries continue failing. | Nothing to do unless this continues; then check that qBittorrent is running. |

#### `TORRENT_METADATA_WAITING`
*INFO, retryable: None, uses: 3*

| | Old | New |
|---|---|---|
| D | qBittorrent has not yet supplied an acceptable torrent file inventory. | qBittorrent is still fetching the torrent file list from peers. |
| M | Waiting for torrent metadata. | Waiting for the torrent file list. |
| A | The worker will retry metadata discovery for the same hash. Older qBittorrent uses the existing journal-owned magnet; newer versions can fetch metadata without adding a torrent. | Nothing to do. It is checked again without adding the torrent twice. |

#### `TORRENT_METADATA_TIMEOUT`
*WARNING, retryable: False, uses: 8*

| | Old | New |
|---|---|---|
| D | An admitted magnet did not provide metadata within ten minutes. | qBittorrent could not get the torrent file list within ten minutes, usually because the torrent has no peers. |
| M | The torrent metadata timed out. | The torrent file list timed out. |
| A | The worker will safely retire this candidate before trying the next release. | Nothing to do. This release is cleaned up and the next one is tried. |

#### `TORRENT_METADATA_DENIED`
*WARNING, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | qBittorrent rejected the metadata-only request. | qBittorrent refused the request for the torrent file list. |
| M | qBittorrent refused the torrent metadata request. | qBittorrent refused to provide the torrent file list. |
| A | Check client authentication, supported metadata operations and the torrent hash. | Check the qBittorrent login and version, and the torrent hash. |

#### `MANAGEMENT_JOB_QUEUED`
*INFO, retryable: None, uses: 3*

| | Old | New |
|---|---|---|
| D | An existing action job is queued, so another action cannot start. | An action for this work is already queued, so another one cannot start. |
| M | A job is already queued. | An action is already queued. |
| A | Wait for the job to start. | Wait for it to start. |

#### `MANAGEMENT_JOB_RUNNING`
*INFO, retryable: None, uses: 4*

| | Old | New |
|---|---|---|
| D | An existing action job is running, so another action cannot start. | An action for this work is already running, so another one cannot start. |
| M | A job is already in progress. | An action is already running. |
| A | Wait for the job to finish. | Wait for it to finish. |

#### `MANAGEMENT_JOB_WAITING`
*INFO, retryable: None, uses: 3*

| | Old | New |
|---|---|---|
| D | An existing action job is waiting for its next check or attempt. | An action for this work is waiting for its next check. |
| M | A job is waiting to continue. | An action is waiting to continue. |
| A | Check the job status for details. | Open the job for details. |

#### `MANAGEMENT_JOB_ATTENTION`
*WARNING, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | An existing action job stopped and needs operator attention. | An action for this work stopped and needs someone to look at it. |
| M | The job needs attention before it can continue. | An action needs attention before it can continue. |
| A | Review the job status for details. | Open the job for details. |

#### `MANAGEMENT_JOB_COMPLETE`
*INFO, retryable: False, uses: 3*

| | Old | New |
|---|---|---|
| D | The existing action job has finished. | The action for this work already finished. |
| M | This job is already complete. | This action is already done. |
| A | Refresh the status to see available actions. | Refresh the page to see what you can do next. |

#### `MANAGEMENT_JOB_CANCELED`
*INFO, retryable: False, uses: 2*

| | Old | New |
|---|---|---|
| D | The existing action job has been canceled. | The action for this work was canceled. |
| M | This job was canceled. | This action was canceled. |
| A | Review the job status for details. | Open the job for details. |

#### `MANAGEMENT_ACTION_UNAVAILABLE`
*WARNING, retryable: False, uses: 13*

| | Old | New |
|---|---|---|
| D | The target is not eligible for the explicit management action. | This action is not allowed for the work in its current state. |
| M | This action is not available for this work. | This action is not available here. |
| A | Refresh the work status and review its available actions. | Refresh the page to see the actions you can use. |

#### `BULK_MANAGEMENT_PARTIAL`
*WARNING, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | Some selected jobs did not allow the requested action; successful changes remain recorded. | Some of the selected jobs did not allow the action. The ones that succeeded stay changed. |
| M | Some bulk actions failed and the job was canceled. | Some of the selected actions failed. |
| A | Check the selected jobs and retry only eligible items. Successful actions were not rolled back. | Check which jobs failed and retry only those. Nothing was undone. |

#### `MANAGEMENT_JOB_SUBMITTED`
*INFO, retryable: None, uses: 3*

| | Old | New |
|---|---|---|
| D | An authorized action and its control job were committed atomically. | The action was accepted and a job was created for it. |
| M | Job submitted. | Action submitted. |
| A | Follow the job status for progress. | Follow the job for progress. |

#### `MANAGEMENT_JOB_EXISTS`
*INFO, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | A durable action already exists for this operation and action kind. | This action was already submitted for this work. |
| M | This action already has a job. | This action is already submitted. |
| A | Follow the existing job instead of submitting another. | Follow the existing job instead of submitting it again. |

#### `API_AUTH_REQUIRED`
*WARNING, retryable: False, uses: 10*

| | Old | New |
|---|---|---|
| D | Request credentials are missing, invalid or conflicting. | The request had no valid login or key, or had more than one. |
| M | Authentication is required. | Please sign in. |
| A | Use the credential required for this API route. | Sign in, or send the key that this API address requires. |

#### `API_AUTH_UNCONFIGURED`
*WARNING, retryable: False, uses: 3*

| | Old | New |
|---|---|---|
| D | Required route-specific access credentials are not configured. | The access key for this API address has not been set up. |
| M | API access has not been configured. | API access is not set up. |
| A | Initialize the separate management and webhook access keys locally. | Set up the webhook and management access keys on the server. |

#### `API_CSRF_REQUIRED`
*WARNING, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | A browser mutation lacks the session-bound anti-forgery token. | The browser request was missing its security token. |
| M | This browser request could not be verified. | This request could not be verified. |
| A | Refresh your session and try again. | Refresh the page and try again. |

#### `API_ORIGIN_INVALID`
*WARNING, retryable: False, uses: 6*

| | Old | New |
|---|---|---|
| D | Request origin or host differs from the configured application origin. | The request came from an address other than the configured dashboard address. |
| M | This request came from an unexpected address. | This request came from an unexpected address. |
| A | Use the configured dashboard address. | Open the dashboard at its configured address. |

#### `API_REQUEST_INVALID`
*WARNING, retryable: False, uses: 68*

| | Old | New |
|---|---|---|
| D | Request framing, JSON structure, fields or pagination failed validation. | The request was malformed or had missing or invalid fields. |
| M | The API request is invalid. | The request is not valid. |
| A | Review the required request fields. | Check the request fields. |

#### `API_WEBHOOK_INVALID`
*WARNING, retryable: False, uses: 4*

| | Old | New |
|---|---|---|
| D | Release name or supported v1 hash is missing, ambiguous or invalid. | The incoming release is missing its name or torrent hash, or the hash is not valid. |
| M | The incoming release details are incomplete or invalid. | The incoming release details are incomplete. |
| A | Send a release name and matching 40-character torrent hash. | Send the release name and its 40-character torrent hash. |

#### `API_BODY_TOO_LARGE`
*WARNING, retryable: False, uses: 2*

| | Old | New |
|---|---|---|
| D | The request exceeds the bounded body size. | The request is larger than allowed. |
| M | The request is too large. | The request is too large. |
| A | Send a smaller request. | Send a smaller request. |

#### `API_MEDIA_TYPE_INVALID`
*WARNING, retryable: False, uses: 2*

| | Old | New |
|---|---|---|
| D | Unsupported request content type or encoding. | The request was not UTF-8 JSON. |
| M | This request format is not supported. | This request format is not supported. |
| A | Send UTF-8 JSON. | Send UTF-8 JSON. |

#### `API_ROUTE_NOT_FOUND`
*INFO, retryable: False, uses: 13*

| | Old | New |
|---|---|---|
| D | No versioned route or requested resource exists. | The API address or the requested item does not exist. |
| M | The requested API resource was not found. | Not found. |
| A | Check the API address and resource identifier. | Check the address and the item ID. |

#### `API_METHOD_INVALID`
*WARNING, retryable: False, uses: 25*

| | Old | New |
|---|---|---|
| D | The HTTP method is not supported by this route. | This API address does not support that kind of request. |
| M | This action is not supported at this address. | This action is not supported here. |
| A | Use the documented API method. | Use the request method this address documents. |

#### `API_RATE_LIMITED`
*WARNING, retryable: None, uses: 3*

| | Old | New |
|---|---|---|
| D | A bounded authentication or session limit was reached. | Too many sign-in attempts or sessions in a short time. |
| M | Too many requests; try again shortly. | Too many requests; try again shortly. |
| A | Wait before trying again. | Wait a moment, then try again. |

#### `API_UNAVAILABLE`
*ERROR, retryable: None, uses: 5*

| | Old | New |
|---|---|---|
| D | An API operation failed without safely exposing internal error text. | The request failed on the server. Details are in the server log, not shown here. |
| M | The API is temporarily unavailable. | The server could not complete the request. |
| A | Check service status and try again later. | Check the service status and the API log, then try again. |

#### `JOB_CANCELED`
*INFO, retryable: False, uses: 7*

| | Old | New |
|---|---|---|
| D | An operator explicitly canceled unfinished work. | Someone canceled this work before it finished. |
| M | This work was canceled. | This work was canceled. |
| A | Check cleanup status for any remaining downloads. | Nothing to do. Check the cleanup status if a download was in progress. |

#### `CANCELLATION_DRAINING`
*INFO, retryable: None, uses: 6*

| | Old | New |
|---|---|---|
| D | Cancellation is durable; active operation handlers must drain before cleanup. | The cancellation is saved; cleanup starts once the work that is running now has stopped. |
| M | Waiting for active work to stop before cleanup. | Waiting for running work to stop before cleanup. |
| A | Wait for the current work to finish stopping. | Nothing to do. |

#### `CANCELLATION_PENDING`
*INFO, retryable: None, uses: 5*

| | Old | New |
|---|---|---|
| D | Cleanup is incomplete; verified torrent stop and torrent-only removal can be retried after checking current ownership. | Cleanup is not finished: stopping or removing the torrent has not been confirmed yet. It is retried after checking the torrent still belongs to this work. |
| M | Cancellation cleanup is waiting for confirmation. | Cancellation cleanup is waiting for confirmation. |
| A | Check the existing torrent and cleanup status. | Nothing to do unless this continues; then check the torrent in qBittorrent. |

#### `CANCELLATION_REVIEW`
*WARNING, retryable: False, uses: 9*

| | Old | New |
|---|---|---|
| D | Cleanup ownership, file identity, sharing or external outcome cannot be established safely. | Cleanup could not safely confirm that a file or torrent belongs to this work, so nothing was deleted. |
| M | Cancellation cleanup needs attention. | Cancellation cleanup needs attention. |
| A | The failed check and the file it looked at are shown below and in History. For the full picture (job history, ad- download folder, staging paths) run sudo adm report job --id with this job number. Fix or remove only that file, then press Retry. Never delete the whole staging or torrent root. | The failed check and the file involved are shown in History. Run sudo adm report job --id <job> for the full picture, fix or remove only that file, then press Retry. Never delete the whole staging or download folder. |

#### `CANCELLATION_COMPLETE`
*INFO, retryable: None, uses: 8*

| | Old | New |
|---|---|---|
| D | Owned-resource and registered temporary-file cleanup completed or no resources existed. | The torrent and temporary files owned by this work were cleaned up, or there were none. |
| M | Cancellation cleanup is complete. | Cancellation cleanup finished. |
| A | No further cleanup is required for the recorded resources. | Nothing to do. |

#### `TORRENT_SELECTION_UNCERTAIN`
*INFO, retryable: None, uses: 24*

| | Old | New |
|---|---|---|
| D | Fixed file selection was authorized once; confirmation is pending. | qBittorrent was asked to download only the chosen file; the result has not been confirmed yet. |
| M | Video file selection is waiting for confirmation. | Waiting to confirm which file is downloaded. |
| A | Inspect the existing selection; do not repeat uncertain commands automatically. | Nothing to do. The request is not repeated automatically. |

#### `TORRENT_SELECTION_CONFIRMED`
*INFO, retryable: None, uses: 15*

| | Old | New |
|---|---|---|
| D | Exact selected-video and skipped-sidecar priorities were observed while stopped. | qBittorrent confirmed that only the chosen video is selected and other files are skipped. |
| M | The video file is selected and ready to download. | The video file is selected. |
| A | Continue with the recorded download start. | Nothing to do. The download starts next. |

#### `TORRENT_PROBE_STOP_UNCERTAIN`
*INFO, retryable: None, uses: 5*

| | Old | New |
|---|---|---|
| D | Completed E01 requires a single stopped-torrent request whose outcome is not yet confirmed. | The first episode finished, and qBittorrent was asked to pause the torrent; the pause has not been confirmed yet. |
| M | Stopping the E01 probe is awaiting confirmation. | Waiting to confirm the torrent is paused. |
| A | Recheck qBittorrent ownership and stopped state; do not reissue an uncertain stop. | Nothing to do. The pause request is not repeated automatically. |

#### `TORRENT_START_UNCERTAIN`
*INFO, retryable: None, uses: 23*

| | Old | New |
|---|---|---|
| D | A single durable start authorization exists; stopped or checking observations cannot rearm it. | qBittorrent was asked to start the download; the start has not been confirmed yet. |
| M | Download startup is waiting for confirmation. | Waiting to confirm the download started. |
| A | Inspect the existing download; do not restart it automatically. | Nothing to do. The start request is not repeated automatically. |

#### `TORRENT_DOWNLOAD_IN_PROGRESS`
*INFO, retryable: None, uses: 12*

| | Old | New |
|---|---|---|
| D | The client reports active transfer with exact selection and ownership. | qBittorrent is downloading the chosen video. |
| M | The selected video is downloading. | Downloading. |
| A | Wait for the download to finish. | Nothing to do. |

#### `TORRENT_DOWNLOAD_COMPLETE`
*INFO, retryable: None, uses: 12*

| | Old | New |
|---|---|---|
| D | The client reports selected-file progress of one with matching provenance. | qBittorrent reports the chosen video is fully downloaded. |
| M | The download client reports that the selected video is complete. | Download finished. |
| A | Verify the local video before making it available. | Nothing to do. The video is verified next. |

#### `TORRENT_DOWNLOAD_WAITING`
*INFO, retryable: None, uses: 6*

| | Old | New |
|---|---|---|
| D | The owned torrent is absent from a current authoritative inventory observation. | The torrent added for this work does not appear in qBittorrent. |
| M | Waiting for the existing download to appear. | Waiting for the torrent to appear in qBittorrent. |
| A | Check the download client; do not add another torrent automatically. | Check qBittorrent. Do not add the torrent by hand; it is not added again automatically. |

#### `TORRENT_TRANSFER_CONFLICT`
*WARNING, retryable: False, uses: 12*

| | Old | New |
|---|---|---|
| D | Transfer ownership, priorities, piece geometry, state or immutable evidence contradicts the journal. | The torrent in qBittorrent no longer matches what was recorded: its owner tag, selected files, pieces or state changed. |
| M | Download details changed and need review. | The download changed and needs review. |
| A | Review ownership, selected files and the existing download before continuing. | Check the torrent and its selected files in qBittorrent before pressing Retry. |

#### `TORRENT_PIECE_OVERLAP`
*WARNING, retryable: False, uses: 10*

| | Old | New |
|---|---|---|
| D | Selected video shares boundary pieces with unwanted file bytes; zero-overlap policy blocks start. | The chosen video shares torrent pieces with files that are not wanted, so it cannot be downloaded alone. |
| M | This video shares download pieces with files that are not selected. | This video cannot be downloaded without other files. |
| A | Review the torrent layout or choose a source containing only the video. | Use a different release, ideally one with only the video. |

#### `TORRENT_SNAPSHOT_CHANGED`
*INFO, retryable: None, uses: 12*

| | Old | New |
|---|---|---|
| D | Client state changed during inspection or is temporarily checking or moving. | The torrent changed while it was being checked, or qBittorrent is checking or moving it. |
| M | The download is changing or being checked. | The download is being checked or moved. |
| A | Wait for a stable status check. | Nothing to do. It is checked again shortly. |

#### `IDENTITY_INCOMPLETE`
*WARNING, retryable: None, uses: 3*

| | Old | New |
|---|---|---|
| D | Required provider-verified identity facts are missing. | Some details needed to identify the anime are missing. |
| M | Anime identification is incomplete. | Anime identification is incomplete. |
| A | Review the missing identification details. | Check the missing details in the job report, or match the anime by hand. |

#### `IDENTITY_INVALID`
*WARNING, retryable: False, uses: 3*

| | Old | New |
|---|---|---|
| D | The resolved identity failed AnimeSeries validation. | The anime that was found failed validation. |
| M | Anime identification could not be verified. | The anime could not be verified. |
| A | Review the identification results. | Check the identification results, or match the anime by hand. |

#### `IDENTITY_CONFLICT`
*WARNING, retryable: False, uses: 9*

| | Old | New |
|---|---|---|
| D | Verified identifiers conflict with stored identity. | The anime IDs that were found disagree with the ones already saved. |
| M | Conflicting anime information needs review. | Conflicting anime information needs review. |
| A | Review the conflicting identifiers. | Compare the AniDB and TVDB IDs in the job report, then match the anime by hand. |

#### `DOWNLOAD_CLIENT_UNAVAILABLE`
*WARNING, retryable: None, uses: 9*

| | Old | New |
|---|---|---|
| D | A bounded client request failed; submission outcome may require reconciliation. | qBittorrent could not be reached. If a torrent was being added, it is checked later instead of being added again. |
| M | The download client could not be reached safely. | qBittorrent could not be reached. |
| A | Check client availability; preserve any pending submission intent. | Check that qBittorrent is running and reachable from the worker. |

#### `DOWNLOAD_CLIENT_RESPONSE_INVALID`
*WARNING, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | Client response shape or provenance observation failed validation. | qBittorrent sent back information that could not be read or did not match. |
| M | The download client returned information that needs review. | qBittorrent returned unexpected information. |
| A | Review client compatibility and recorded source evidence. | Check your qBittorrent version and the torrent, then press Retry. |

#### `DOWNLOAD_CLIENT_AUTH_FAILED`
*WARNING, retryable: False, uses: 2*

| | Old | New |
|---|---|---|
| D | The configured client did not accept session authentication. | qBittorrent rejected the username or password. |
| M | The download client rejected authentication. | qBittorrent did not accept the login. |
| A | Review private client credentials and access settings. | Check the qBittorrent username and password in Settings. |

#### `DOWNLOAD_CLIENT_UNSUPPORTED`
*WARNING, retryable: False, uses: 2*

| | Old | New |
|---|---|---|
| D | qBittorrent must be 4.5+ with Web API 2.8.14+. | qBittorrent must be version 4.5 or newer (Web API 2.8.14 or newer). |
| M | This qBittorrent version is not supported. | This qBittorrent version is not supported. |
| A | Upgrade to qBittorrent 4.5 or newer (Web API 2.8.14+) before retrying. | Upgrade qBittorrent to 4.5 or newer, then press Retry. |

#### `TORRENT_SUBMISSION_UNCERTAIN`
*INFO, retryable: None, uses: 17*

| | Old | New |
|---|---|---|
| D | A durable one-shot intent cannot yet be confirmed; never replay its add. | The torrent was sent to qBittorrent, but it has not been confirmed yet. It is never sent twice. |
| M | The torrent submission is waiting for confirmation. | Waiting to confirm the torrent was added. |
| A | Check the existing submission; do not add it again automatically. | Nothing to do. Check qBittorrent if this continues; do not add the torrent by hand. |

#### `TORRENT_ALREADY_PRESENT`
*WARNING, retryable: False, uses: 16*

| | Old | New |
|---|---|---|
| D | Exact-hash inventory existed before this application's send authorization. | qBittorrent already had this torrent before this work tried to add it. |
| M | This torrent already exists in qBittorrent. No new torrent was submitted. | This torrent is already in qBittorrent; nothing new was added. |
| A | Check the existing torrent and its originating job before retrying. | Check who added the existing torrent before pressing Retry. |

#### `TORRENT_PROVENANCE_CONFLICT` → **`TORRENT_OWNERSHIP_CONFLICT`**
*WARNING, retryable: False, uses: 20*

| | Old | New |
|---|---|---|
| D | Observed marker, destination, category, management, state or files contradict the saved intent. | The torrent in qBittorrent does not match what this work added: its owner tag, folder, category, state or files differ. |
| M | Torrent ownership or file details need review. | The torrent no longer matches what was added and needs review. |
| A | Compare the existing torrent with the saved submission intent. | Compare the torrent in qBittorrent with the job report, then press Retry. |

#### `TORRENT_STOPPED_CONFIRMED`
*INFO, retryable: None, uses: 14*

| | Old | New |
|---|---|---|
| D | Saved intent matches authoritative stopped torrent provenance and file inventory. | qBittorrent confirmed the torrent was added, paused, with the expected files. |
| M | The submitted torrent is confirmed and stopped. | The torrent is added and paused. |
| A | Apply approved file selection before starting any download. | Nothing to do. The video file is selected next. |

#### `DOWNLOAD_CONFIGURATION_INVALID`
*WARNING, retryable: False, uses: 2*

| | Old | New |
|---|---|---|
| D | Download endpoint, local folder or credentials are incomplete or invalid. | The qBittorrent address, login or download folder is missing or not valid. |
| M | Configure the download client and download folder before acquisition. | qBittorrent is not set up. |
| A | Review download settings and private credentials. | Check the qBittorrent settings and the download folder. |

#### `ACQUISITION_SIZE_LIMIT`
*WARNING, retryable: False, uses: 4*

| | Old | New |
|---|---|---|
| D | The selected video is larger than the per-episode size limit. | The video is larger than the maximum size allowed per episode. |
| M | The video is larger than the episode size limit. | The video is larger than the episode size limit. |
| A | Raise 'Maximum video size' in Settings, or wait for a smaller release of this episode. | Raise "Maximum video size" in Settings, or wait for a smaller release. |

#### `ACQUISITION_PLAN_SIZE_LIMIT`
*WARNING, retryable: False, uses: 3*

| | Old | New |
|---|---|---|
| D | Adding this video would exceed the total size limit for this anime's trial episodes (selection plan). | Adding this video would go over the total size allowed for the episodes picked for your evaluation of this anime. |
| M | This anime's trial episodes would exceed their total size limit. | The evaluation episodes for this anime would go over their total size limit. |
| A | The selection plan is every episode picked for this anime's trial. Raise 'Maximum selection-plan size' in Settings. | Raise "Maximum selection-plan size" in Settings. |

#### `ACQUISITION_SIZE_EVIDENCE_MISSING`
*WARNING, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | A prior prepared plan lacks independently recorded selected-byte evidence. | An earlier download plan for this anime has no recorded file size. |
| M | An existing acquisition plan needs its file size reviewed. | An earlier download plan is missing its file size. |
| A | Reinspect the existing source before adding more work to this plan. | Re-check that earlier release before adding more episodes. |

#### `ACQUISITION_SOURCE_MISSING`
*WARNING, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | A normalized v1 torrent identity is absent or unsupported. | There is no supported torrent hash for this release. |
| M | A supported torrent identity is required before acquisition. | This release has no usable torrent. |
| A | Check the incoming release source. | Check the incoming release. |

#### `ACQUISITION_METADATA_INVALID`
*WARNING, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | Hash-bound metadata or its single-file declaration could not be validated. | The torrent file list could not be verified for the expected episode. |
| M | The torrent file list could not be verified. | The torrent file list could not be verified. |
| A | Review the source and declared episode before proceeding. | Check the release and its episode number. |

#### `ACQUISITION_PLAN_CONFLICT`
*WARNING, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | Reservation, source or immutable preparation facts changed. | The episode reservation or the release changed after the download was planned. |
| M | Acquisition details changed and need review. | Download details changed and need review. |
| A | Review the recorded episode reservation and source. | Check the episode reservation and the release in the job report. |

#### `ACQUISITION_PLAN_READY`
*INFO, retryable: None, uses: 1*

| | Old | New |
|---|---|---|
| D | Hash-bound single-video metadata and a stable submission marker were recorded. | The torrent file list was checked and the download is planned. |
| M | Source files have been inspected and the acquisition plan is ready. | The download is planned. |
| A | Complete download policy and client setup before submission. | Nothing to do. The torrent is added next. |

#### `EPISODE_RESERVATION_GRANTED`
*INFO, retryable: None, uses: 2*

| | Old | New |
|---|---|---|
| D | A selected verified episode has an exclusive durable reservation. | This episode is reserved for this work, so no other release downloads it at the same time. |
| M | This episode is reserved for acquisition. | This episode is reserved. |
| A | Continue with source verification before starting a download. | Nothing to do. The release is verified next. |

#### `EPISODE_ALREADY_STAGED`
*INFO, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | Another operation has staged this verified episode, so competing held work was canceled. | Another release of this episode is already staged, so this one was canceled. |
| M | Canceled: episode already staged. | Canceled: this episode is already staged. |
| A | No action needed. The competing release was skipped and only its owned resources are cleaned up; the staged episode is retained. | Nothing to do. Only this release's own files are cleaned up; the staged episode stays. |

#### `EPISODE_RESERVED`
*INFO, retryable: None, uses: 12*

| | Old | New |
|---|---|---|
| D | Another job owns the exact verified episode reservation. | Another job already reserved this episode. |
| M | Another job has reserved this episode. | Another job is handling this episode. |
| A | Review the existing reservation before resuming this job. | Check the other job before resuming this one. |

#### `EPISODE_RESERVATION_CONFLICT`
*WARNING, retryable: False, uses: 3*

| | Old | New |
|---|---|---|
| D | Episode evidence, selection or reservation identity is inconsistent. | The episode details or its reservation do not match each other. |
| M | Episode reservation details need review. | The episode reservation needs review. |
| A | Check the episode identity and recorded reservation. | Check the episode and its reservation in the job report. |

#### `TRIAL_SELECTED` → **`USER_EVALUATION_SELECTED`**
*INFO, retryable: None, uses: 7*

| | Old | New |
|---|---|---|
| D | The verified received episode falls within its immutable episode-selection window. | This episode is one of the first episodes picked so you can evaluate this anime. |
| M | This episode is selected for Intake. | This episode is picked for your evaluation. |
| A | Continue only after acquisition prerequisites are verified. | Nothing to do. It downloads once its checks pass. |

#### `TRIAL_OUTSIDE_WINDOW` → **`USER_EVALUATION_OUTSIDE_WINDOW`**
*INFO, retryable: False, uses: 7*

| | Old | New |
|---|---|---|
| D | The received episode is outside the recorded episode-selection windows. | This episode is outside the episodes picked for you to evaluate this anime. |
| M | This episode is outside the Intake selection. | This episode is not picked for your evaluation. |
| A | Review the recorded episode window if a different selection is needed. | Nothing to do. To pick more episodes, raise the evaluation episode counts in Settings. |

#### `TRIAL_DISABLED` → **`USER_EVALUATION_DISABLED`**
*INFO, retryable: False, uses: 4*

| | Old | New |
|---|---|---|
| D | Both counts are zero and no prior episode-selection plan exists. | Both evaluation episode counts are zero, so no episodes are picked for new anime. |
| M | Intake episode selection is disabled. | Picking episodes for evaluation is turned off. |
| A | Set a backfill or following count to create new selection plans. | Set the "backfill" or "following" evaluation episodes above zero in Settings. |

#### `RELEASE_AUDIO_UNTAGGED`
*INFO, retryable: None, uses: 1*

| | Old | New |
|---|---|---|
| D | The audio check requires English and the autobrr release title has no Dual/Multi Audio or English dub tag. | English audio is required, and this autobrr release title does not mention Dual or Multi Audio. |
| M | This release is not being downloaded; its episode is searched in Prowlarr instead. | This release is skipped; its episode is searched in Prowlarr instead. |
| A | Nothing to do. Set Audio check to japanese or off to download such releases directly. | Nothing to do. To download such releases directly, set Audio check to "japanese" or "off". |

#### `TRIAL_PLAN_CONFLICT` → **`USER_EVALUATION_PLAN_CONFLICT`**
*WARNING, retryable: False, uses: 2*

| | Old | New |
|---|---|---|
| D | The source verification or episode-selection plan is inconsistent. | The verified episode does not match the saved list of episodes picked for evaluation. |
| M | The Intake selection plan needs review. | The evaluation episode list needs review. |
| A | Review the verified episode and recorded selection plan. | Check the verified episode and the saved evaluation list in the job report. |

#### `RELEASE_UNRESOLVED`
*WARNING, retryable: False, uses: 3*

| | Old | New |
|---|---|---|
| D | A release has missing, ambiguous or unsupported declared episode evidence. | The release does not clearly say which episode it is. |
| M | This episode could not be confirmed, so the job has stopped. | The episode could not be confirmed, so the job stopped. |
| A | The saved reason does not identify the failed check. Retry episode verification to record more detail. | Press Retry to check again; this records which check failed. |

#### `RELEASE_TITLE_UNCLEAR`
*WARNING, retryable: False, uses: 5*

| | Old | New |
|---|---|---|
| D | The release title does not declare one supported regular episode. | The release title does not name exactly one regular episode. |
| M | The release title does not clearly name one episode. | The release title does not clearly name one episode. |
| A | Check the episode number in the release title. This job cannot download until one episode is identified. | Check the episode number in the release title. This release cannot be downloaded until one episode is clear. |

#### `RELEASE_TYPE_UNSUPPORTED`
*WARNING, retryable: False, uses: 4*

| | Old | New |
|---|---|---|
| D | The title declares a pack, range or unsupported release type. | The release is a batch, an episode range, a movie or another unsupported type. |
| M | This release is a batch, range, movie, or unsupported media type. | This type of release is not supported. |
| A | Check the release scope. A show's title containing 'Special' is not itself evidence of a special release. | Check what the release contains. A show whose name includes "Special" is not itself a special. |

#### `RELEASE_SPECIAL_UNSUPPORTED`
*WARNING, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | No explicit Anime-Lists mapping verifies this TVDB Season 0 special from a regular AniDB episode. | This is a TVDB Season 0 special, and Anime-Lists has no mapping that links it to an AniDB episode. |
| M | This Season 0 special has no verified AniDB-to-TVDB episode mapping. | This special has no AniDB-to-TVDB episode mapping. |
| A | Check Anime-Lists for an explicit regular AniDB episode mapping to the TVDB Season 0 entry. Separate AniDB special numbers cannot be assumed equivalent. | Check Anime-Lists for a mapping for this special. AniDB special numbers are not assumed to match TVDB. |

#### `RELEASE_MAPPING_MISSING`
*WARNING, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | No unique Anime-Lists record is available for the selected AniDB work. | Anime-Lists has no single entry for this anime, so its episodes cannot be matched to TVDB. |
| M | Anime-Lists has no single matching episode-mapping record for this anime. | Anime-Lists has no entry for this anime. |
| A | The series was identified, but its episodes cannot be matched to TVDB yet. Check the Anime-Lists entry before retrying verification. | Check the Anime-Lists entry for this anime, then press Retry. |

#### `RELEASE_MAPPING_UNSUPPORTED`
*WARNING, retryable: False, uses: 3*

| | Old | New |
|---|---|---|
| D | An Anime-Lists episode mapping rule cannot be interpreted safely. | The Anime-Lists episode-numbering rule for this anime cannot be read safely. |
| M | The episode-numbering rule in Anime-Lists is not supported. | This anime's episode numbering in Anime-Lists is not supported. |
| A | Check the Anime-Lists episode mapping. This job has stopped rather than guess which episode to download. | Check the Anime-Lists mapping. The job stopped rather than guess the episode. |

#### `RELEASE_ANIDB_EPISODE_MISSING`
*WARNING, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | AniDB does not list the declared regular episode number. | AniDB does not list this episode number for the anime. |
| M | AniDB does not list this episode number for the identified anime. | AniDB does not have this episode. |
| A | Check the release episode number and AniDB episode list. If AniDB is missing the episode, retry verification after its list is updated. | Check the episode number in the release and the AniDB episode list. Press Retry once AniDB lists it. |

#### `RELEASE_EPISODE_MAPPING_MISSING`
*WARNING, retryable: False, uses: 3*

| | Old | New |
|---|---|---|
| D | No mapping connects the declared episode to the other provider. | Anime-Lists does not link this episode between AniDB and TVDB. |
| M | The anime was found, but Anime-Lists does not connect this episode between AniDB and TVDB. | This episode is not linked between AniDB and TVDB. |
| A | Check the episode mapping in Anime-Lists. If it is missing, retry verification after the mapping is added. This job will not continue automatically. | Check the episode mapping in Anime-Lists, then press Retry once it is added. |

#### `RELEASE_EPISODE_MAPPING_AMBIGUOUS`
*WARNING, retryable: False, uses: 2*

| | Old | New |
|---|---|---|
| D | Multiple AniDB episodes match the declared TVDB season and episode. | More than one AniDB episode matches this TVDB season and episode. |
| M | More than one AniDB episode matches this season and episode number. | More than one episode matches. |
| A | Check the Anime-Lists episode mapping. This job has stopped because it cannot choose one episode safely. | Check the episode mapping in Anime-Lists. The job stopped rather than guess. |

#### `RELEASE_TVDB_EPISODE_MISSING`
*WARNING, retryable: False, uses: 2*

| | Old | New |
|---|---|---|
| D | TVDB does not list the mapped season and episode. | TVDB does not list the season and episode that Anime-Lists points to. |
| M | TVDB does not list the season and episode supplied by the mapping. | TVDB does not have this season and episode. |
| A | Compare the Anime-Lists mapping with the TVDB episode list. Retry verification after the missing entry or incorrect mapping is fixed. | Compare the Anime-Lists mapping with TVDB, then press Retry once either is fixed. |

#### `RELEASE_CONFLICT`
*WARNING, retryable: False, uses: 3*

| | Old | New |
|---|---|---|
| D | Release mapping, episode ownership or persisted source evidence conflicts. | The episode mapping or the saved release details disagree. |
| M | Release episode information conflicts. | The release episode details disagree. |
| A | Review the episode identifiers and mapping evidence. | Check the episode IDs and mapping in the job report. |

#### `PROVIDER_UNAVAILABLE`
*WARNING, retryable: True, uses: 1*

| | Old | New |
|---|---|---|
| D | A provider request could not complete due to availability. | AniDB, TVDB or Anime-Lists could not be reached. |
| M | An information provider is temporarily unavailable. | An anime information service is unavailable. |
| A | The operation can be retried. | Nothing to do; it is tried again. If it continues, check the network and the service status. |

#### `PROVIDER_CONFIGURATION_INVALID`
*ERROR, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | Required AniDB client or TVDB API-key configuration is missing or invalid. | The AniDB client or the TVDB API key is missing or not valid. |
| M | Provider configuration is incomplete or invalid. | AniDB or TVDB is not set up. |
| A | Configure the registered AniDB client and TVDB API key before starting identity work. | Set the registered AniDB client and the TVDB API key in Settings. |

#### `SECRET_STORAGE_UNAVAILABLE`
*ERROR, retryable: None, uses: 1*

| | Old | New |
|---|---|---|
| D | The private credential file could not be read or published safely. | The private credentials file could not be read or saved safely. |
| M | Credentials could not be accessed safely. | Saved credentials could not be read. |
| A | Review credential storage, permissions and file format. | Check secrets.json: its owner, its permissions (0600) and its format. |

#### `PROVIDER_RESPONSE_INVALID`
*WARNING, retryable: None, uses: 1*

| | Old | New |
|---|---|---|
| D | A provider response failed bounded decoding or structural checks. | AniDB, TVDB or Anime-Lists sent back data that could not be read. |
| M | An information provider returned an unusable response. | An anime information service returned unusable data. |
| A | Retry later or review provider availability. | Press Retry later. If it continues, check the service status. |

#### `PROVIDER_AUTH_FAILED`
*WARNING, retryable: False, uses: 5*

| | Old | New |
|---|---|---|
| D | Provider authentication or authorization failed. | AniDB or TVDB rejected the configured access details. |
| M | An information provider could not authorize access. | An anime information service rejected access. |
| A | Review the provider access settings. | Check the AniDB client and TVDB API key in Settings. |

#### `JOB_LEASE_EXPIRED`
*WARNING, retryable: True, uses: 3*

| | Old | New |
|---|---|---|
| D | A running job lease expired before completion. | The worker stopped or lost contact while this job was running. |
| M | Processing was interrupted. | Processing was interrupted. |
| A | Check the interrupted work and its recovery status. | Nothing to do; the job is tried again. If it keeps happening, check the worker log. |

#### `JOB_HANDLER_FAILED`
*ERROR, retryable: None, uses: 8*

| | Old | New |
|---|---|---|
| D | A job handler failed unexpectedly. | The job failed with an unexpected error. |
| M | An unexpected processing error occurred. | An unexpected error occurred. |
| A | Review the job details. | Check the failed check and the worker log, then press Retry. |

#### `JOB_RETRY_EXHAUSTED`
*ERROR, retryable: False, uses: 4*

| | Old | New |
|---|---|---|
| D | A job reached its allowed attempt limit. | The job failed as many times as allowed, so automatic retries stopped. |
| M | Automatic retries have stopped. | Automatic retries stopped. |
| A | Review the job before retrying manually. | Check the earlier failure reason in History, fix it, then press Retry. |

#### `STORAGE_UNAVAILABLE`
*ERROR, retryable: None, uses: 3*

| | Old | New |
|---|---|---|
| D | Storage access failed; the cause and commit outcome require investigation. | The database could not be read or written. Whether the last change was saved is unknown. |
| M | Processing is temporarily unavailable. | The database is unavailable. |
| A | Check database health and storage. | Check the database file, its disk and its permissions. |

#### `STORAGE_STATEMENT_INVALID`
*ERROR, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | A database statement was rejected as invalid, such as a wrong number of bound values; this is a programming defect, not a storage outage. The statement did not run. | A database query in the program is wrong, for example it has the wrong number of values. This is a bug, not a database problem. Nothing was saved. |
| M | An internal database statement error stopped processing. | A program error stopped processing. |
| A | Report the failed check from the log; retrying unchanged code will fail the same way. | Report the failed check from the log. Retrying will fail the same way until the program is fixed. |

#### `UNSUPPORTED_JOB_KIND`
*ERROR, retryable: False, uses: 2*

| | Old | New |
|---|---|---|
| D | No handler is registered for this job type. | No part of the program handles this type of job. |
| M | This type of work cannot be processed. | This type of job cannot be processed. |
| A | Check the installed components. | Check that the installed version matches the database. |

#### `TRIAL_COUNTS_INVALID` → **`USER_EVALUATION_COUNTS_INVALID`**
*WARNING, retryable: False, uses: 2*

| | Old | New |
|---|---|---|
| D | Both episode selection counts are zero. | Both evaluation episode counts are zero. |
| M | Set backfill or episodes starting with the received episode to at least 1. | Set at least one evaluation episode count above zero. |
| A | Increase at least one of the two episode counts. | Raise the "backfill" or "following" evaluation episodes in Settings. |

#### `CONFIGURATION_INVALID`
*WARNING, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | Configuration shape, field or value failed validation. | A setting is missing, unknown, or has a value that is not allowed. |
| M | Configuration could not be validated. | The settings are not valid. |
| A | Review the configuration and supported settings. | Check the settings shown in the error. |

#### `CONFIGURATION_CONFLICT`
*WARNING, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | The saved revision or migration source changed. | The settings were changed somewhere else while this change was being made. |
| M | Configuration changed before this update could be applied. | The settings changed before this update was saved. |
| A | Refresh the configuration and review the changes again. | Reload the settings, check the changes, and save again. |

#### `CONFIGURATION_MIGRATION_REQUIRED`
*WARNING, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | An older supported configuration needs explicit conversion. | The settings file is from an older version and must be upgraded first. |
| M | Configuration needs an upgrade. | The settings need an upgrade. |
| A | Preview and apply the configuration migration. | Preview and apply the settings upgrade. |

#### `CONFIGURATION_STORAGE_FAILED`
*ERROR, retryable: None, uses: 1*

| | Old | New |
|---|---|---|
| D | Safe configuration access or publication failed. | The settings file could not be read or saved. |
| M | Configuration storage is unavailable. | Settings could not be read or saved. |
| A | Check the service account permissions and storage. | Check the settings folder permissions and disk. |

#### `CONFIGURATION_DURABILITY_UNCONFIRMED`
*ERROR, retryable: None, uses: 3*

| | Old | New |
|---|---|---|
| D | Rename succeeded but directory synchronization failed. | The new settings were written, but the disk did not confirm they are safely stored. |
| M | Configuration was updated, but durable storage could not be confirmed. | Settings were saved, but the disk did not confirm it. |
| A | Read the current configuration and check storage before retrying. | Reload the settings to check them, and check the disk, before saving again. |

#### `MAINTENANCE_PENDING`
*INFO, retryable: None, uses: 1*

| | Old | New |
|---|---|---|
| D | A durable maintenance request blocks new admissions. | Maintenance was requested, so no new work starts. |
| M | New work is paused for maintenance. | New work is paused for maintenance. |
| A | Wait for maintenance to finish. | Wait for maintenance to finish. |

#### `MAINTENANCE_BUSY`
*INFO, retryable: None, uses: 1*

| | Old | New |
|---|---|---|
| D | Existing admissions or another maintenance controller still hold a lock. | Work that is still running, or another maintenance task, is holding the database. |
| M | Existing work is still finishing. | Waiting for running work to finish. |
| A | Check maintenance progress before trying again. | Wait, then check the maintenance status. |

#### `MAINTENANCE_FAILED`
*ERROR, retryable: None, uses: 1*

| | Old | New |
|---|---|---|
| D | Maintenance state, ownership or storage needs review. | The maintenance state is unclear or could not be saved. |
| M | Maintenance state needs review. | Maintenance needs review. |
| A | Check maintenance status before resuming work. | Check the maintenance status before resuming work. |

#### `DATABASE_VERSION_UNSUPPORTED`
*ERROR, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | The database version is unversioned, newer or outside the supported upgrade path. | The database is from an unknown, newer, or unsupported version. |
| M | This database version cannot be upgraded by this application. | This database version cannot be upgraded by this version. |
| A | Verify the application version and selected database. | Check that you installed the right version and opened the right database. |

#### `DATABASE_SCHEMA_INVALID`
*ERROR, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | Schema, integrity or foreign-key validation failed. | The database structure or its integrity checks failed. |
| M | The database does not match the expected application structure. | The database structure does not match this version. |
| A | Keep maintenance active and review database health. | Leave maintenance on and check the database, or restore a backup. |

#### `DATABASE_MIGRATION_CONFLICT`
*WARNING, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | Database structure/version or packaged migration inputs changed after preview. | The database or the upgrade files changed after the upgrade was previewed. |
| M | The database upgrade plan changed. | The database upgrade plan changed. |
| A | Preview the upgrade again before applying it. | Preview the upgrade again before applying it. |

#### `DATABASE_MIGRATION_FAILED`
*ERROR, retryable: None, uses: 1*

| | Old | New |
|---|---|---|
| D | Upgrade or backup failed; earlier version steps may already be committed. | The upgrade or its backup failed. Some upgrade steps may already be applied. |
| M | Database upgrade stopped. Maintenance remains active. | The database upgrade stopped; maintenance is still on. |
| A | Review the current database version and backup before continuing. | Check the current database version and the backup before continuing. |

#### `JOB_WAITING`
*INFO, retryable: None, uses: 7*

| | Old | New |
|---|---|---|
| D | A handler deferred a normal prerequisite or external-work wait. | The job is waiting for something it needs, such as another job or an outside service. |
| M | Work is waiting for a prerequisite. | Waiting for something this job needs. |
| A | Check the recorded waiting condition. | Nothing to do. Open the job to see what it is waiting for. |

#### `JOB_STATE_INVALID`
*ERROR, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | A queued job has inconsistent lifecycle bookkeeping. | The job's saved progress does not add up, so it was stopped. |
| M | Job progress needs review. | This job's progress needs review. |
| A | Review the job history before resuming work. | Check the job history, then press Retry. |

#### `JOB_PAYLOAD_INVALID`
*ERROR, retryable: False, uses: 32*

| | Old | New |
|---|---|---|
| D | A queued payload is invalid or exceeds the worker bound. | The job's saved input is not valid or is too large. |
| M | Job information could not be validated. | This job's input is not valid. |
| A | Review the queued job before trying again. | Check the job in the report. It usually needs to be recreated rather than retried. |

#### `WORKER_NOT_READY`
*ERROR, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | No trusted job handlers are registered for execution. | The worker started without any job handlers installed. |
| M | The worker is not ready to process jobs. | The worker is not ready. |
| A | Install and register the required job handlers. | Reinstall or upgrade the application, then restart the worker. |

#### `SONARR_SELECTION_CONFLICT` → **`RELEASE_SELECTION_CONFLICT`**
*WARNING, retryable: False, uses: 2*

| | Old | New |
|---|---|---|
| D | An episode's candidate releases or selection state are inconsistent. | The saved list of releases for this episode no longer matches what this job expected, for example because another job changed it. |
| M | The release choice for this episode needs review. | The release choice for this episode needs review. |
| A | Review the episode's candidate releases. | Open the job report (adm report job --id <job>) to compare the saved releases, then press Retry. |

#### `BATCH_SELECTION_CONFLICT` → **`SEASON_PACK_SELECTION_CONFLICT`**
*WARNING, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | A season's candidate releases or selection state are inconsistent. | The saved list of season packs for this season no longer matches what this job expected, for example because another job changed it. |
| M | The release choice for this season needs review. | The season pack choice for this season needs review. |
| A | Review the season's candidate releases. | Open the job report (adm report job --id <job>) to compare the saved season packs, then press Retry. |

#### `MANUAL_RELEASE_EPISODE_MISMATCH`
*WARNING, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | A manually submitted release title or torrent file names a different episode than the one it was submitted for. | A release submitted by hand names a different episode, in its name or in its video file, than the episode it was submitted for. |
| M | This release is for a different episode than the one selected. | This release is for a different episode. |
| A | Check the episode in the release title, or submit it from that episode. | Check the episode in the release name. A release can only be submitted from its own episode. Nothing was downloaded. |

#### `MANUAL_RELEASE_TITLE_UNREADABLE`
*WARNING, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | No episode could be read from a manually submitted release title. | No season and episode could be read from the name of a release submitted by hand. |
| M | The release title does not say which episode it is. | The release name does not say which episode it is. |
| A | Use the full release name, including its season and episode (for example S01E05). | Paste the full release name, including its season and episode (for example S01E05). Nothing was downloaded. |

#### `MANUAL_RELEASE_SOURCE_INVALID`
*WARNING, retryable: False, uses: 1*

| | Old | New |
|---|---|---|
| D | A manually submitted magnet link or torrent file failed validation. | The magnet link or .torrent file submitted by hand is not valid. |
| M | The magnet link or torrent file is not valid. | The magnet link or torrent file is not valid. |
| A | Paste a magnet link that contains its info hash, or choose a valid .torrent file. | Paste a magnet link that contains its info hash (xt=urn:btih:...), or choose a valid .torrent file. Nothing was downloaded. |

#### `SERVICE_SETUP_INVALID`
*ERROR, retryable: False, uses: 2*

| | Old | New |
|---|---|---|
| D | A required service (qBittorrent, AniDB or TVDB) is not set up, rejected the login, client name or API key, or is an unsupported version. The worker did not start; waiting would not fix this. | qBittorrent, AniDB or TVDB is not set up, rejected the login, client name or API key, or runs a version that is not supported. The worker did not start, because waiting would not fix this. |
| M | A required service is not set up correctly. | A required service is not set up correctly. |
| A | Fix the service named in the details in Settings, or run adm services check, then start the worker again. | Fix the service named in the log in Settings, or run adm services check, then start the worker again. |

#### `SERVICE_UNAVAILABLE`
*WARNING, retryable: True, uses: 8*

| | Old | New |
|---|---|---|
| D | A service this job needs could not be reached. The job waits and tries again by itself; it does not use up its retries. | A service this job needs could not be reached. The job waits and tries again by itself, without using up its retries. |
| M | Waiting for a service that is unavailable. | Waiting for a service to come back. |
| A | Check the service named in the details. Waiting jobs continue on their own once it is back; after 72 hours of failed attempts the job stops for review. | Nothing to do for a short outage: waiting jobs continue on their own. If it lasts, check the service named in the details. After 72 hours the job stops for review. |

### 4.2 Event codes

| Code | Old message | New message |
|---|---|---|
| `EPISODE_RESERVATIONS_RELEASED` | Canceled episode reservations released after cleanup. | Canceled episodes were released after cleanup. |
| `KEEP_COMPLETED` | Episode kept in the permanent library. | Episode kept in the library. |
| `MANAGEMENT_ACTION_RESUMED` | The action was submitted again after review. | The action was submitted again after review. |
| `SERIES_REJECTED` | Series rejected; future Intake is blocked. | Anime rejected; no more episodes are downloaded. |
| `API_SESSION_CREATED` | A management browser session was created. | Signed in to the dashboard. |
| `API_SESSION_CLOSED` | A management browser session was closed. | Signed out of the dashboard. |
| `API_CREDENTIALS_UPDATED` | Private service credentials were updated. | Service credentials were updated. |
| `API_REQUEST_REJECTED` | An API request could not be completed. | An API request was refused. |
| `API_TRANSPORT_NOTICE` | The API transport needs attention. | The API connection needs attention. |
| `ACCEPTED` | Incoming event accepted. | Release received. |
| `SERIES_IDENTIFIED` | Anime series identification completed. | Anime identified. |
| `CREATED` | Job created. | Job created. |
| `CLAIMED` | Job processing started. | Job started. |
| `LEASE_EXPIRED` | The processing lease expired before the job completed. | The job was interrupted before it finished. |
| `DEFERRED` | Work is waiting for a later check. | Waiting; the job is checked again later. |
| `RETRY_SCHEDULED` | Another processing attempt has been scheduled. | Another attempt is scheduled. |
| `RETRY_REQUESTED` | An administrator requested another attempt at this step. | Retry requested. |
| `NEEDS_REVIEW` | Processing has stopped for review. | Stopped; needs review. |
| `TORRENT_SELECTION_RECORDED` | Video file selection recorded. | Asked qBittorrent to download only the chosen file. |
| `TORRENT_SELECTION_CONFIRMED` | Video file selection confirmed. | qBittorrent confirmed the chosen file. |
| `TORRENT_TRANSFER_CHANGED` | Download status updated. | Download status changed. |
| `TORRENT_INTENT_RECORDED` → **`TORRENT_SUBMISSION_RECORDED`** | Torrent submission intent recorded. | About to add the torrent to qBittorrent. |
| `TORRENT_RECONCILED` | Torrent submission checked against the download client. | Checked the torrent in qBittorrent. |
| `ACQUISITION_PREPARED` | Source files inspected and acquisition plan recorded. | Torrent files checked; download planned. |
| `EPISODE_RESERVATION_CREATED` | Episode reserved for acquisition. | Episode reserved. |
| `EPISODE_RESERVATION_SKIPPED` | Episode reservation was not needed. | No episode reservation was needed. |
| `TRIAL_EVALUATED` → **`USER_EVALUATION_CHECKED`** | Intake episode selection evaluated. | Checked whether this episode is picked for your evaluation. |
| `TRIAL_AUDIO_SEARCH` → **`USER_EVALUATION_AUDIO_SEARCH`** | Release title has no Dual/Multi Audio tag; searching Prowlarr instead of downloading it. | Release has no Dual Audio tag; searching Prowlarr instead. |
| `SONARR_BACKFILL_OBSERVED` → **`EPISODE_DISCOVERY_RECORDED`** | Read-only Sonarr backfill results recorded. | Episode search results saved. |
| `DONE` | Job completed. | Job finished. |
| `FAILED` | Job processing failed. | Job failed. |
| `ON_HOLD` | Job placed on hold. | Job on hold. |
| `BLOCKED` | Job blocked from proceeding. | Job blocked. |
| `DECISION_DOWNLOAD_CONTROL_RECORDED` | Download cleanup step recorded. | Download cleanup step saved. |
| `CANCELLATION_CONTROL_RECORDED` | Cancellation cleanup step recorded. | Cancellation cleanup step saved. |
| `CANCELLATION_REQUESTED` | Cancellation requested for unfinished work. | Cancellation requested. |
| `CANCELLATION_CLEANUP_CHANGED` | Cancellation cleanup status updated. | Cancellation cleanup status changed. |
| `CANCELED` | Job canceled. | Job canceled. |
| `HANDLER_STARTED` | Job handler started. | Step started. |
| `HANDLER_COMPLETED` | Job handler completed. | Step finished. |
| `HANDLER_FAILED` | Job handler failed. | Step failed. |
| `CONFIGURATION_SAVED` | Configuration updated. | Settings saved. |
| `CONFIGURATION_MIGRATED` | Configuration upgrade completed. | Settings upgraded. |
| `CONFIGURATION_SAVE_UNCONFIRMED` | Configuration was updated, but durable storage could not be confirmed. | Settings saved, but the disk did not confirm it. |
| `DATABASE_MIGRATED` | Database upgrade completed. | Database upgraded. |
| `MAINTENANCE_REQUESTED` | New work paused for maintenance. | New work paused for maintenance. |
| `MAINTENANCE_COMPLETED` | Maintenance completed. New work can start. | Maintenance finished; new work can start. |
| `OPERATION_FAILED` | A processing operation failed. | An operation failed. |
| `SERVICE_DOWN` | An external service is not usable; jobs that need it are waiting. | A service stopped working; jobs that need it wait. |
| `SERVICE_RESTORED` | An external service is available again; waiting jobs continue. | A service is working again; waiting jobs continue. |
| `SERVICE_STARTUP_REFUSED` | The worker did not start: a required service is not set up correctly. | The worker did not start: a required service is not set up correctly. |

### 4.3 Removed reason codes (old wording, for reference)

| Code | Old description |
|---|---|
| `SONARR_DISCOVERY_AUTH_FAILED` | Sonarr rejected the configured API key. |
| `SONARR_DISCOVERY_CONFIGURATION_INVALID` | The Sonarr address, API key, quality profile or library root folder is missing or not accepted by Sonarr. |
| `SONARR_DISCOVERY_UNSUPPORTED` | The connected Sonarr server is not a supported v4 instance. |
| `SONARR_DISCOVERY_REVIEW` | Sonarr interactive search returned ambiguous or invalid mapping or release evidence. |
| `SONARR_DISCOVERY_TIMEOUT` | The Sonarr request exceeded its transport timeout; release searches allow 180 seconds. |
| `SONARR_DISCOVERY_UNAVAILABLE` | Sonarr or its interactive search could not complete the request. |
| `SONARR_DISCOVERY_NO_RESULTS` | The previously ranked fallback torrent is not currently offered by Sonarr. |
| `SONARR_DISCOVERY_EPISODE_NOT_FOUND` | The requested TVDB series is absent from Sonarr; its release API requires existing catalog records. |
| `SONARR_EPISODES_NOT_LISTED` | Sonarr returned no matching episode record for one or more requested backfill episodes. |
| `KEEP_NOT_READY` | Media promotion requires the future verified staging and permanent-publication library. |

## 5. Open decisions

1. **`SONARR_DISCOVERY_EPISODE_NOT_FOUND` and `SONARR_EPISODES_NOT_LISTED`.** These meant "Sonarr has no record of this series or its episodes", but Sonarr is no longer part of discovery. The proposal migrates them to `PROWLARR_UNAVAILABLE`, which keeps the affected jobs retry-eligible with no new code. The alternative is a dedicated historical code such as `DISCOVERY_STOPPED_BEFORE_PROWLARR` ("This search stopped under the old Sonarr-based discovery; press Search again to search Prowlarr"). That is clearer in History, but it adds a code that nothing emits.
2. **Wording review.** Please read §4 and mark anything that is unclear or wrong. The wording is the point of this audit. The new codes in §0 need the same review, in particular the proposed name `SEASON_PACK_SELECTION_CONFLICT`.
3. **The per-service "unavailable" codes** (see §6). `services` now makes a job wait with `SERVICE_UNAVAILABLE` when a service is down, but the handlers still report their own code, kept as the `cause_reason_code`. `services/README.md` leaves retiring them to RF-006. Choose one:
   - **Keep them as causes** (proposed). `SERVICE_UNAVAILABLE` is the state and the per-service code says which kind of failure it was. Only their wording changes.
   - **Retire them.** The handlers report `SERVICE_UNAVAILABLE` naming the service, and stored rows migrate. This changes what `adm recover`, `review_retry` and the reports match on, so it is a behaviour change for NextGen first, not a refactor rename.

Decided: "trial" is replaced by "user evaluation" (`USER_EVALUATION`), owner decision on 2026-11-04.

## 6. Per-service "unavailable" codes and the service-health foundation

`DOWNLOAD_CLIENT_UNAVAILABLE`, `KEEP_SERVICE_UNAVAILABLE`, `SHOKO_UNAVAILABLE`, `JELLYFIN_UNAVAILABLE`, `PROWLARR_UNAVAILABLE` and `PROVIDER_UNAVAILABLE` describe the same situation (a service cannot be reached). Their wording is aligned here.

The service-health foundation (`services`, merged into `refactor` in #284) added `SERVICE_UNAVAILABLE`: a job that hits an outage now waits with that reason, and the handler's own code above is kept as its `cause_reason_code`. Whether to retire the per-service codes is open decision 3 in §5. This audit keeps them.
