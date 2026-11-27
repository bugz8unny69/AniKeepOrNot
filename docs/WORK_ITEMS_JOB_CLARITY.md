# Work items: errors show a stack trace, no catch-alls

Status: open. Written 2026-11-22.

## The rule

- **A step that logs "started" and then "finished" worked.** Nothing more is needed in the log.
- **An error raises as itself and logs a full stack trace.** That means the error type, its message, and every frame (file, line, function), with credentials redacted.
- **No catch-alls.** Code catches only the errors it expects and knows how to handle. Anything else is a program bug and must be visible as one: never turned into a reason code, retried, or hidden.

## Branch and tests

- Branch from `develop`; keep `refactor`, `NextGen` and `main` untouched.
- Run the tests that target each change. Run the full suite only when asked.
- Deploy with `/usr/local/bin/akon-install --upgrade --revision <sha> --yes`, after pushing, then check the production log.

---

## 1. Errors log the full stack trace

**Done 2026-11-23** (branch `feat/api-identity-bridge`): `SafeFormatter` prints the standard traceback, paths from `src/`, redacted; `safe_traceback` and `_ERROR_TYPES` removed.

**Today:** the log formatter throws the stack away.

- `SafeFormatter.formatException` ([diagnostics.py:62](../src/anime/diagnostics.py#L62)) calls `safe_traceback`, which prints only line numbers. For example:

  ```
  Traceback (safe): exception=Exception frame_lines=12,40,7
  ```

  That line has no file, no function and no message, and any error type not in a short list is called just "Exception".
- `formatStack` prints "Stack details omitted."
- `exact_error` ([diagnostics.py:260](../src/anime/diagnostics.py#L260)) keeps the message but only the last two frames.

**Change:**

- `formatException` prints the standard Python traceback, including chained errors ("During handling of…", "The above exception was the direct cause…").
  - File paths are shortened to start at `src/`.
  - The whole text passes through `redact()` for credentials.
- `formatStack` prints the stack.
- Remove `safe_traceback`, and `_ERROR_TYPES` as a filter: the real type name is printed.

**Done when:** a test raises an error from inside a job and sees, in the log, the error type, its message, and the file:line:function of every frame. A test also checks that a password in the message is redacted.

## 2. An unexpected error in a job is a bug: logged, recorded, failed

**Done 2026-11-27** (branch `fix/job-errors-are-bugs`): the boundary logs at ERROR with the trace and returns `fail JOB_HANDLER_FAILED`; History has `failed_check` (`file:line function RAISED_<TYPE>`); the bell says "Program error in <step>: <release>". Cancellation and a lost lease pass through to the attempt, which handles them as before. Test: `test_worker_errors_and_log.py`.

**Today:** the worker's boundary ([worker.py:374](../src/anime/worker.py#L374)) handles an unexpected error like this:

1. It logs at WARNING with two frames.
2. It returns `retry JOB_HANDLER_FAILED`.
3. The job is retried up to the budget, and then the torrent is canceled.

So a bug looks like a temporary problem and keeps retrying.

**Change:** the boundary stays, so one job's bug can't stop the worker, but it doesn't disguise the bug. For an error that no code expected:

- **Log:** at ERROR with the full traceback (item 1).
- **Job:** ends FAILED with reason `JOB_HANDLER_FAILED`. Its history records:
  - the error type;
  - the file:line:function where it was raised;
  - the release name.
- **Notification:** a `JOB_FAILED` notification, "Program error in <step>: <torrent name>".
- **No retry.** Retrying the same code gives the same bug.

Expected errors keep their own handling: the service waits in `_exception_wait`, and storage errors in `storage_outcome`.

**Done when:** a test handler that raises `KeyError` ends FAILED after one attempt, with the traceback in the log, the file:line in history, and a notification.

## 3. Remove the catch-alls

There are 53 `except Exception` / `except BaseException` sites in `src/anime`, not counting tests.

**These stay:** eight of them close or roll back, then re-raise the same error. They are not catch-alls:

- `async_io.py:20`
- `install_support.py:103`
- `staging.py:163`, `:195`, `:321`
- `migrations.py:160`
- `storage/adapter.py:91`
- `storage/sqlite.py:1004`

**Every other site gets one of these, decided one at a time:**

- **(a)** Catch only the specific errors that place expects.
- **(b)** Let the error raise.
- **(c)** If the place really is a boundary that must keep going, log the full traceback at ERROR and say what was skipped.

Nothing may `pass`, return a default, or turn the error into a reason code without a traceback.

| Where | What it hides today |
|---|---|
| `worker.py:209, 236, 334, 528, 570, 618` | worker loop and service checks |
| `worker.py:459, 475, 510, 522` | "Audit logging failed" (keep running, but with a traceback) |
| `job_report.py:438, 447, 736, 745` | `except Exception: pass`: report sections go missing silently |
| `job_report.py:1280`, `job_diagnostics.py:193, 306`, `job_recovery.py:572` | report and recovery readers |
| `cancellation_service.py:136` | `pass`: a log failure during cleanup |
| `keep_service.py:36`, `providers/identity.py:256`, `providers/backfill_catalog.py:49, 53, 85` | provider and keep work |
| `acquisition/metainfo_source.py:247`, `acquisition/prowlarr_search.py:537` | torrent metadata and Prowlarr search |
| `torrent_client/qbittorrent.py:106` | every error becomes `ClientInvalid` |
| `services/health.py:148, 166`, `services/check.py:64`, `services/adapters/qbittorrent.py:41`, `services/adapters/metadata.py:58, 146` | service probes |
| `api/adm.py:34`, `api/admin_socket.py:41`, `__main__.py:145` | `adm` and startup |

**Done in the libraries (2026-11-26):** qBittorrent client (cookie parse: `CookieError`), providers (TVDB credentials and the match threshold: no catch, errors pass through), Prowlarr search (`SearchReview`/`SearchUnavailable`), torrent metadata (`AcquisitionConflict`), service checks (qBittorrent login: the four client errors; provider cache reads: database and file errors; `adm services check`: settings errors). `services/health.py:151, 176` stay as boundaries (c): a check that raises anything else logs its full trace at ERROR and counts as down.

**Done in `api/app.py` (2026-11-23):** one boundary per request: a program error answers `500 API_PROGRAM_ERROR` with "Program error at file:line in function (ErrorType)" and logs the full trace. Bad input (400) logs where it was refused. The webhook decision and candidate-evidence catch-alls are removed; the admin-socket startup logs its trace.
| `diagnostics.py:250`, `install_support.py:189`, `batch/backlog_audit.py:296` | logging, install, backlog audit |

**Done when:** `grep -rn "except Exception\|except BaseException" src/anime` (outside tests) lists only the eight re-raise sites and the boundaries marked (c), each with a comment saying why it is a boundary.

## 4. Started and finished stay in the log; repeated waits print nothing

**Done 2026-11-27** (branch `fix/job-errors-are-bugs`): "Job started", "Step started" and "Step finished" log at DEBUG. The worker holds the claim and start lines until `finish` returns; a repeated wait drops them, and an error writes them before its trace. Test: `test_worker_errors_and_log.py`.

**Today, on branch `fix/quiet-polls`, not deployed:** "Job started", "Step started" and "Step finished" were moved to TRACE, so they no longer show at DEBUG. That goes against the rule above, which needs them visible.

**Change:**

- Put "started" and "finished" back at DEBUG.
- A check that only repeats the same wait still prints nothing. Its history rows are already not stored. The worker holds the "started" lines until it knows the result, and drops them for a repeated wait. Without that, a "started" line with no "finished" after it would look like a crash.

**Done when:**

- A step that works logs "started" and then "finished".
- A step that raises logs "started", then the traceback, then "failed".
- A repeated download check logs nothing.

---

## 5. Design: the API cleans the release, then one job per series

Decided by the owner, 2026-11-22.

**Stage 1, built on branch `feat/api-identity-bridge` (not deployed): the API identifies.**

- **Autobrr asks first** through the External filter `AKON Check` (`POST /api/v1/check`), which works like SMTP greylisting:
  - `200` accepted, `422` rejected (with why), `503` "ask again later" while the identity process gets the real data from AniDB. Autobrr retries on 503 (15 s apart, 10 tries).
  - A decided series, a hand match or a recognized name is answered at once from local data.
  - One row per torrent (info hash). Its answer is kept for a day, so retries and the webhook action get the same answer and nothing is identified twice.
- **Webhook action:** the same intake; 202 while the release waits.
- **Identity process:** the API starts `src/anime/identity_process.py` when releases are due. It runs the AniDB-first resolver with the shared pacing and reports one JSON line per release on stdout. AniDB is the truth: the API checks only that a result is well-formed. The process exits when nothing is due within a minute, and the API stops it on shutdown.
- **The API applies each result** (`src/anime/identity_bridge.py`):
  - identified: the delivery, its identity and the worker's first job (`VERIFY_RELEASE`) are saved in one transaction;
  - anything the worker could not complete (not identified, a movie, unsupported, a decided series) is refused with API Reject. The notification says why and keeps the release for "Match by hand" or "Process anyway". The worker never sees it.
- **The worker no longer identifies.** "Match by hand" moved from the job to the notification.
- **Migration v77 moves the old data:**
  - unfinished identification jobs move to the bridge's queue;
  - every old identification job whose release was never identified is removed, with its delivery;
  - nothing stale is left, and no database copy is made.
- **Production copy test:** 72 dead deliveries and jobs removed; 1 release (Land of the Lustrous) queued; schema valid; 44 s.

**Stage 2, not started:** one job per series, as below.

**Today:**
- A release from Autobrr becomes an `IDENTIFY_ANIME` job.
- That job creates a chain of jobs for each torrent and each episode: verify, choose, reserve, prepare, submit, download, verify media, transcode, stage, keep, cleanup.
- Production has 28 job kinds and about 11,000 jobs for 137 series. Following why one torrent failed means reading many jobs.

**New shape:**

1. **The API cleans the release** (the `/webhook` handler in [api/app.py](../src/anime/api/app.py)):
   - **Checks:** it reads and validates the payload and parses the release name: title, season, episode or pack, group.
   - **Turns away what can't be used, at once:**
     - a duplicate release name;
     - a movie;
     - an unsupported special;
     - a series that was already rejected.

     It tells Autobrr why, and shows it in the intake failures view. Nothing is stored for a release that is turned away, so the database doesn't grow from intake.
   - **Recognized title:** if the name belongs to a series already identified (recognized names), the API adds the torrent to **that series' job**.
   - **Unknown title:** otherwise it creates one identify job.
2. **Identify:** stays a worker job, because AniDB requests must be paced and can't run inside an API request. It is the only job that can stop in Needs review, saying why it didn't identify. Once identified, the torrent joins its series' job: a new one, or the one that already exists.
3. **One job per series** (keyed by AniDB ID). The worker only works on identified series. The series job:
   - **holds its torrents in order:** each one recorded by release name, with its state and why;
   - **runs each torrent through the stages itself:** check, download, verify, transcode, stage. These are steps of the series job, not separate jobs;
   - **keeps one history:** each entry names the torrent and episode, for example "S02E03 [Group] … : downloading, 53%";
   - **drops a torrent it can't use:** the reason is recorded, its files are cleaned up, and the series moves on to the next torrent. One bad torrent never stops the series;
   - **owns the series' decisions and cleanup:** Keep and Reject are actions on the series job, not separate jobs;
   - **ends only when the series is decided.** Kept means Sonarr owns it from then on; rejected means everything is removed.

**Moving over:**
- Torrents already in progress finish on today's jobs.
- New releases go to the new path, behind a setting, so it can be turned on and off on production.
- Today's job kinds are removed once nothing uses them.

**Questions for the owner before building:**
- **Parallel work:** may a series job work on two torrents at once (a weekly episode downloading while a pack is verified), or strictly one at a time?
- **Unknown titles:** should the API decide on its own that a title is a known series, or should every release still go through identification and only then join the series?

**Done when:**
- `adm report series <anime>` and the dashboard show one history per series.
- Every entry names the torrent.
- A failed torrent says why, and the next torrent is tried.

---

## Later (from earlier today, not started)

- Name the actual episode in batch steps ("Downloading S02E01 to check the pack"), not "E01 probe".
- Show download progress in the job and the log.
- A season pack that doesn't start at episode 1 (E13–E24) is canceled. Start the probe at the earliest episode in the pack.
- The job report says "Suspected stranded" for a job that is downloading.
- qBittorrent out of disk space would cancel every download. It should wait instead.
- Clear about 400,000 repeated wait rows from production history (needs your go-ahead).
- Needs review leftovers:
  - 8341: no action works on it.
  - 8572: manual match to AniDB 13165.
- Merge `fix/review-only-identity` and `fix/quiet-polls` into develop.

