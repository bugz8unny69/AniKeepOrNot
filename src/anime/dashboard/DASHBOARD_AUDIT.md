# Dashboard control audit

Baseline: D1 commit e7840c08. Local Firefox against 127.0.0.1:12080, using a throwaway SQLite database, synthetic identities/staged media, and the real API. No background acquisition/cleanup worker or production service was run. Browser control inventories were captured for twelve views, with follow-up checks for generated settings. API side effects in this audit mean queued operations and local test-state changes; they do not claim provider/production execution.

The Works column describes control/route behavior. A disabled control with a valid reason is checked as a control, not an executed destructive action. Conditional evidence/recovery forms are additionally checked with the existing synthetic browser/API fixtures. D4 records a final verification pass below.

## Intake

| Control | Label today | When shown | What it calls (route / function) | Works? | Problem | Proposal |
|---|---|---|---|---|---|---|
| Navigation tabs | Intake / Jobs / Recognized Names / Settings | Signed in | GET /, /jobs, /recognized, /settings | yes | No numeric shortcuts; inconsistent page chrome | rename: intake, jobs, names, settings |
| Search | Search | Intake | applySearch -> GET /api/v1/search?scope=intake | yes | Shared with Jobs | keep |
| Intake filter | Show | Intake | GET /api/v1/intake/series?status=… | yes | Shares a panel with unrelated job controls | keep |
| Open series | View series | One per anime | openSeries -> GET /api/v1/intake/series/{id} | yes | Long label; no hint | rename: details |
| Refresh | Refresh | Intake and Jobs | loadActivity | yes | Duplicates live polling | remove |
| Pagination | Previous / Next | Activity list | loadActivity with before_id | yes | Disabled without explanation; shared across pages | rename: previous / next |

## Jobs

| Control | Label today | When shown | What it calls (route / function) | Works? | Problem | Proposal |
|---|---|---|---|---|---|---|
| Search | Search | Jobs | GET /api/v1/search?scope=jobs | yes | Shared Intake controls | keep |
| Status filter | All statuses…Completed jobs/Canceled | Jobs | GET /api/v1/jobs?state=… | partly | Overlaps visibility checkboxes; FAILED absent | fix |
| Completed toggle | Show completed | Jobs | GET /api/v1/jobs?show_completed=… | yes | Duplicates status filter | merge: status select |
| Canceled toggle | Show canceled | Jobs | GET /api/v1/jobs?show_canceled=… | yes | Duplicates status filter | merge: status select |
| Row selection | Select job #… | Jobs | state.selected / renderBulkControls | yes | No explanation on select-all when unavailable | keep |
| Page selection | Select jobs on this page | Jobs | select-page change handler | yes | State is not explained | keep |
| Bulk action selector | Retry eligible / Cancel / Cleanup | Selection exists | submitSelected | yes | Extra step before applying action | merge: explicit action buttons |
| Bulk submit | Apply to selected | Selection exists | POST /jobs/bulk or /jobs/{id}/{action} | yes | One button selects unrelated actions | fix |
| Clear selection | Clear | Selection exists | state.selected.clear | yes | Ambiguous label | rename: clear selection |
| Release expansion | Release details | Job has a release | details element | yes | Raw diagnostics spread across table | rename: details |
| Keep or follow-up | Keep / Retry download cleanup / Retry staging cleanup / Retry Shoko metadata | Capability exists | POST /jobs/{id}/keep | partly | Same endpoint/label represents unrelated actions | fix |
| Cancel | Cancel | Not staged | POST /jobs/{id}/cancel | yes | Shown disabled when inapplicable | rename: cancel |
| Cleanup | Cleanup | Canceled work | POST /jobs/{id}/cleanup | yes | Disabled controls and inconsistent case | rename: cleanup |
| Retry | Retry | Needs review | POST /jobs/{id}/retry | partly | Failed jobs can be retried by API but have no button | fix |
| Matching details | Matching details | Delivery jobs | GET /jobs/{id}/matching | yes | Missing evidence gives an explanatory empty state | rename: details |
| Episode coverage | Episode coverage | VERIFY_RELEASE | GET /jobs/{id}/coverage | yes | No hint | rename: coverage |
| History | History | Every job | GET /jobs/{id}/history | yes | No hint | rename: history |
| Transcode batch | Progress text | Batch queued/running | Jobs response transcode_batch | yes | Read-only, not a button | keep |

## Recognized names

| Control | Label today | When shown | What it calls (route / function) | Works? | Problem | Proposal |
|---|---|---|---|---|---|---|
| Search | Search | Names page | GET /api/v1/search?scope=recognized | yes | No keyboard focus shortcut | keep |
| Refresh | Refresh recognized names | Names page | loadRecognized | yes | Duplicates live intent; page does not start polling | remove |
| Episode details | Episodes / Hide episodes | Recognized row | GET /recognized/{id}/episodes | yes | Label changes; long returned type/status codes | rename: details |

## Settings

| Control | Label today | When shown | What it calls (route / function) | Works? | Problem | Proposal |
|---|---|---|---|---|---|---|
| Section disclosure | Section names | Settings | details toggle; credentials reveal on eligible integrations | yes | Internal section names and inconsistent casing | rename: plain section titles |
| Section save | Save changes | Every section | PATCH /settings and /credentials/{integration} | yes | Disabled until edits; no consistent hint | rename: save |
| Refresh | Refresh settings | Settings | loadSettings with discard confirmation | yes | Can discard edits; duplicates live intent | remove |
| Credential reveal | Eye icon | Integration open | POST /credentials/{integration}/reveal, local visibility toggle | yes | Needs stable visibility label/hint | rename: visibility |
| Agent list | Add agent / Up / Down / Remove | Transcode section | Local list; PATCH /settings on save | yes | Separate token save is not explained | rename: add / up / down / remove |
| Agent token | Save token | One per agent | PATCH /credentials/transcode | yes | No title | rename: save token |
| Password fields | Current / New / Confirm new password | Security panel | POST /auth/password | yes | No label/hint on submission outcome beyond sentence | keep |
| Password change | Change password | Security panel | POST /auth/password; clears session | yes | Long label; no hint | rename: change password |
| transcode.mode | Mode | Its Settings section | PATCH /settings (transcode.mode) | yes | No independent control problem | keep |
| transcode.encoders | Local encoders | Its Settings section | PATCH /settings (transcode.encoders) | yes | No independent control problem | keep |
| transcode.tmp | Local temp path | Its Settings section | PATCH /settings (transcode.tmp) | yes | No independent control problem | keep |
| transcode.vaapi_device | VAAPI device | Its Settings section | PATCH /settings (transcode.vaapi_device) | yes | No independent control problem | keep |
| transcode.target_mb | Target MB per 24 minutes | Its Settings section | PATCH /settings (transcode.target_mb) | yes | No independent control problem | keep |
| transcode.retries | Retries per agent | Its Settings section | PATCH /settings (transcode.retries) | yes | No independent control problem | keep |
| transcode.retry_seconds | Retry delay (seconds) | Its Settings section | PATCH /settings (transcode.retry_seconds) | yes | No independent control problem | keep |
| transcode.agents | Remote agents | Its Settings section | PATCH /settings (transcode.agents) | yes | No independent control problem | keep |
| anime.title_language | Anime title language | Its Settings section | PATCH /settings (anime.title_language) | yes | No independent control problem | keep |
| identity.match_threshold_pct | AniDB match threshold (%) | Its Settings section | PATCH /settings (identity.match_threshold_pct) | yes | No independent control problem | keep |
| shoko.base_url | Shoko address | Its Settings section | PATCH /settings (shoko.base_url) | yes | No independent control problem | keep |
| shoko.timeout_seconds | Shoko timeout | Its Settings section | PATCH /settings (shoko.timeout_seconds) | yes | No independent control problem | keep |
| jellyfin.base_url | Jellyfin address | Its Settings section | PATCH /settings (jellyfin.base_url) | yes | No independent control problem | keep |
| jellyfin.timeout_seconds | Jellyfin timeout | Its Settings section | PATCH /settings (jellyfin.timeout_seconds) | yes | No independent control problem | keep |
| prowlarr.base_url | Prowlarr address | Its Settings section | PATCH /settings (prowlarr.base_url) | yes | No independent control problem | keep |
| prowlarr.timeout_seconds | Prowlarr search timeout | Its Settings section | PATCH /settings (prowlarr.timeout_seconds) | yes | No independent control problem | keep |
| prowlarr.max_resolution | Maximum resolution | Its Settings section | PATCH /settings (prowlarr.max_resolution) | yes | No independent control problem | keep |
| prowlarr.min_seeders | Minimum seeders | Its Settings section | PATCH /settings (prowlarr.min_seeders) | yes | No independent control problem | keep |
| prowlarr.rank_by | Ranking | Its Settings section | PATCH /settings (prowlarr.rank_by) | yes | No independent control problem | keep |
| sonarr.base_url | Sonarr address | Its Settings section | PATCH /settings (sonarr.base_url) | yes | No independent control problem | keep |
| sonarr.timeout_seconds | Sonarr timeout | Its Settings section | PATCH /settings (sonarr.timeout_seconds) | yes | No independent control problem | keep |
| sonarr.quality_profile_id | Sonarr quality profile ID | Its Settings section | PATCH /settings (sonarr.quality_profile_id) | yes | No independent control problem | keep |
| sonarr.long_running_threshold | Long-running series threshold (episodes) | Its Settings section | PATCH /settings (sonarr.long_running_threshold) | yes | No independent control problem | keep |
| sonarr.backfill_long_running | Let Sonarr backfill long-running series | Its Settings section | PATCH /settings (sonarr.backfill_long_running) | yes | No independent control problem | keep |
| sonarr.staging_root | Staging folder as seen by Sonarr | Its Settings section | PATCH /settings (sonarr.staging_root) | yes | No independent control problem | keep |
| sonarr.library_root | Permanent folder as seen by Sonarr | Its Settings section | PATCH /settings (sonarr.library_root) | yes | No independent control problem | keep |
| qbittorrent.base_url | qBittorrent address | Its Settings section | PATCH /settings (qbittorrent.base_url) | yes | No independent control problem | keep |
| qbittorrent.category | Download category | Its Settings section | PATCH /settings (qbittorrent.category) | yes | No independent control problem | keep |
| qbittorrent.timeout_seconds | Download client timeout | Its Settings section | PATCH /settings (qbittorrent.timeout_seconds) | yes | No independent control problem | keep |
| qbittorrent.metadata_poll_seconds | Metadata poll interval (seconds) | Its Settings section | PATCH /settings (qbittorrent.metadata_poll_seconds) | yes | No independent control problem | keep |
| qbittorrent.metadata_poll_attempts | Metadata checks per window | Its Settings section | PATCH /settings (qbittorrent.metadata_poll_attempts) | yes | No independent control problem | keep |
| acquisition.max_episode_mb | Maximum video size (MB) | Its Settings section | PATCH /settings (acquisition.max_episode_mb) | yes | No independent control problem | keep |
| acquisition.audio_check | Audio check | Its Settings section | PATCH /settings (acquisition.audio_check) | yes | No independent control problem | keep |
| acquisition.max_plan_mb | Maximum selection-plan size (MB) | Its Settings section | PATCH /settings (acquisition.max_plan_mb) | yes | No independent control problem | keep |
| storage.download_root | Download working folder | Its Settings section | PATCH /settings (storage.download_root) | yes | No independent control problem | keep |
| user_evaluation.backfill_count | Backfill first episodes | Its Settings section | PATCH /settings (user_evaluation.backfill_count) | yes | No independent control problem | keep |
| user_evaluation.following_count | Episodes starting with the received episode | Its Settings section | PATCH /settings (user_evaluation.following_count) | yes | No independent control problem | keep |
| anidb.client_name | AniDB client name | Its Settings section | PATCH /settings (anidb.client_name) | yes | No independent control problem | keep |
| anidb.client_version | AniDB client version | Its Settings section | PATCH /settings (anidb.client_version) | yes | No independent control problem | keep |
| providers.timeout_seconds | Provider request timeout | Its Settings section | PATCH /settings (providers.timeout_seconds) | yes | No independent control problem | keep |
| logging.level | logging.level | Its Settings section | PATCH /settings (logging.level) | yes | Fallback labels expose internal paths | rename: plain field label |
| logging.components | logging.components | Its Settings section | PATCH /settings (logging.components) | yes | Fallback labels expose internal paths | rename: plain field label |
| api.host | api.host | Its Settings section | PATCH /settings (api.host) | yes | Fallback labels expose internal paths | rename: plain field label |
| api.port | api.port | Its Settings section | PATCH /settings (api.port) | yes | Fallback labels expose internal paths | rename: plain field label |
| worker.concurrency | worker.concurrency | Its Settings section | PATCH /settings (worker.concurrency) | yes | Fallback labels expose internal paths | rename: plain field label |
| storage.user_evaluation_root | Staging folder | Its Settings section | PATCH /settings (storage.user_evaluation_root) | yes | No independent control problem | keep |
| storage.keep_root | Keep video folder | Its Settings section | PATCH /settings (storage.keep_root) | yes | No independent control problem | keep |
| storage.database_path | storage.database_path | Its Settings section | PATCH /settings (storage.database_path) | yes | Fallback labels expose internal paths | rename: plain field label |

## Series dialog

| Control | Label today | When shown | What it calls (route / function) | Works? | Problem | Proposal |
|---|---|---|---|---|---|---|
| Close | Close | Dialog open | dialog.close | yes | No hint | rename: close |
| Related jobs | Related jobs | Placed episode | navigateEpisodeJobs -> /jobs#anidb_id=… | yes | Different words for history/navigation | rename: jobs |
| Open job | View job | Missing episode with acquisition | /jobs#job=… | yes | No hint | rename: details |
| Retry acquisition | Retry acquisition | Retryable acquisition / prior size rejection | POST /jobs/{id}/retry or /intake/series/{id}/backfill/{episode}/acquire | yes | Same label spans distinct recovery routes | rename: retry / retry acquisition |
| Try next release | Try next release | Stopped candidate can cancel | POST /jobs/{id}/cancel | yes | Actually cancels the current candidate | rename: cancel |
| Search | Search / Search again / Retry search | Retryable missing episode | Search-again dialog -> POST backfill/{episode}/retry | yes | One action has three names | rename: search again |
| Manual submission opener | Submit release | Eligible missing episode | openManualSubmission | yes | No hint | rename: submit release |
| Release fields | Release name / Source / Magnet / Torrent file | Manual form open | Validation and POST episodes/{season}/{episode}/submit | yes | Validation messages are sentences | keep |
| Submit candidate | Submit candidate | Manual form open | POST episodes/{season}/{episode}/submit | yes | No hint | rename: submit |
| Dismiss manual form | Dismiss | Manual form open | Clear manualTarget and hide form | yes | Different close label | rename: close |
| Reject reason/note | Reason / Additional note | Series undecided | Payload for series reject | yes | No problem | keep |
| Reject | Reject series | Series undecided | POST /intake/series/{id}/reject after confirm | yes | Confirmation must explain irreversible deletion | rename: reject |
| Keep staged | Keep all staged (N acquisitions) | Staged acquisitions available | POST /intake/series/{id}/keep | yes | Label changes with count; hint can be blank | rename: keep |
| Rejection history | Rejection history | Series rejected | openHistory | yes | Different history label | rename: history |

## Matching details

| Control | Label today | When shown | What it calls (route / function) | Works? | Problem | Proposal |
|---|---|---|---|---|---|---|
| Close | Close | Dialog open | dialog.close | yes | No hint | rename: close |
| Evidence expansions | Advanced / recorded evidence | Evidence exists | details elements | yes | Appropriate location for technical codes | keep |
| Manual candidate selection | Candidate selector | Ambiguous identity evidence | manualMatchForm | yes | Labels show verified identity choices | keep |
| Save match | Save match and retry | Eligible reviewed identity | POST /jobs/{id}/manual-match | yes | No hint | rename: save match |

## Search again

| Control | Label today | When shown | What it calls (route / function) | Works? | Problem | Proposal |
|---|---|---|---|---|---|---|
| Delete cache | Delete cache | Dialog open | delete_cache field in POST backfill retry | yes | Destructive cache effect needs a hint | keep |
| Submit search | Search again | Dialog open | Close with search result, then POST retry | yes | No hint | rename: search again |
| Cancel dialog | Cancel | Dialog open | Close without POST | yes | Uses job-cancel word for dialog closing | rename: close |

## Job history

| Control | Label today | When shown | What it calls (route / function) | Works? | Problem | Proposal |
|---|---|---|---|---|---|---|
| Close | Close | Dialog open | dialog.close | yes | No hint | rename: close |
| More history | Load more | More history available | GET /jobs/{id}/history?after_id=… | yes | No hint | rename: more |
| Target job | Job #… | Management history has target | openHistory(target_job_id) | yes | No hint | rename: history |
| Technical details | Recorded failure/check details | Recorded evidence | details / pre display | yes | Some technical fields also leak into Jobs summary | keep |

## Bell

| Control | Label today | When shown | What it calls (route / function) | Works? | Problem | Proposal |
|---|---|---|---|---|---|---|
| Open/close bell | Bell icon + unread badge | Signed in | GET /notifications | yes | No title; Esc is handled separately | keep |
| Mark all read | Mark all read | Bell open | DELETE /notifications | yes | Needs stable hint | rename: mark all read |
| More notifications | Load more | More inbox entries | GET /notifications?before_id=… | yes | No hint | rename: more |
| Notification links | Open series / Open job / Open jobs page | Event action supports it | POST /notifications/{id}/actions/{action}; local navigation | partly | Open jobs page changes state but not visible page panels/URL | fix |
| Process anyway | Process anyway | Rejected webhook event | POST notification action | yes | Bypasses a decision; needs explicit confirm | fix |
| Add release | Add torrent/magnet | Rejection supports manual source | Open manual form for selected series | yes | Different words for same submission | rename: submit release |
| Mark read | Mark read | Every notification | DELETE /notifications/{id} | yes | No hint | rename: mark read |

## Service bar

| Control | Label today | When shown | What it calls (route / function) | Works? | Problem | Proposal |
|---|---|---|---|---|---|---|
| Health chip | Service name + Available/Unknown/etc. | Signed in | GET /services, saved health only | partly | No health tick starts on Settings/Names; too much chip text | fix |
| Agent count | Agents alive N/M | Remote mode only | Saved transcode aggregate | yes | Correct green/yellow/red logic; verbose display | rename: agents N/M |

## Sign-in

| Control | Label today | When shown | What it calls (route / function) | Works? | Problem | Proposal |
|---|---|---|---|---|---|---|
| Password | Administrator password | Signed out | POST /auth/session | yes | Cleared after use | keep |
| Sign in | Sign in | Signed out | POST /auth/session | yes | No hint | rename: sign in |
| Sign out | Sign out | Signed in | DELETE /auth/session | yes | No hint | rename: sign out |

## Dead code and redundant behavior

- `renderJobs` returns immediately for Intake, so its later `state.page === intake` status branch is unreachable.
- The `held = true` branch in the search-again handler is unconditional. Remove the dead alternative and keep the dialog.
- `dashboard.page_filter` is unused. The separate refresh/reload controls and visibility checkbox handlers become redundant under D4.
- CSS `.forms`, `.actions`, `.hide-mobile`, and `input.long` have no current dashboard markup users. The duplicated `.job-release` and `.job-details` rules should be consolidated.
- Every existing request-producing function has a caller; no unrelated API endpoint is proposed for deletion.

## Errors and leaked internal details

- Valid local navigation, settings reads, recognized-episode reads, history, matching, and canceled filtering returned usable responses. Missing matching evidence is an explanatory state, not a route failure.
- There is no direct Retry capability for `CLEANUP_KEPT_MEDIA` / `ENRICH_SHOKO_METADATA`; follow-ups are hidden behind the Keep endpoint. D4 should allow these existing guarded retry requests instead of adding new API routes.
- Jobs summaries display raw cleanup phase fallbacks, failure paths/check identifiers, and an ADM command. Put these in a details disclosure and keep the row summary a sentence.
- Settings schema fallback labels such as `logging.components` are implementation paths; show readable field labels.
- Settings and Names return before scheduling the live tick; visibility changes can request Jobs data while on Names. D4 must dispatch polling by the current page and avoid overwriting unsaved settings.

## D4 final verification

Pending: update every retained control to yes after the final local browser verification; record removed controls explicitly.
