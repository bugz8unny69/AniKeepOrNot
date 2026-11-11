# AniKeepOrNot — NextGen development handoff

> **Authoritative architecture decision: 2026-10-22.** NextGen is built fresh in `src/anime/`. There is **no migration**, compatibility bridge, legacy SQLite reader or transitional data assembler. Legacy `src/legacy/` is reference material for domain lessons and regression tests only.
>
> **Operational guardrails:** Draft work on branches/PRs; do not merge, deploy, restart services, change production databases, acquire/delete torrents, or touch user media without explicit authorization. Use `NextGen` as the PR target for new NextGen work. Keep `develop` for the existing legacy application. Current authorization (2026-10-24): merge the completed NextGen stack, run offline tests and CI across it, and fix failures. Keep deployment and live media operations separate. Test policy (2026-11-04): offline tests run locally for every work item before commit/push/PR; GitHub Actions is not relied on (see "NextGen tests" below).

## Merged stack and CI repair — 2026-10-24

User authorized merging the completed stack and running full offline CI/tests.
PR #138 merged #135–#138 into NextGen; all four closed. Add dedicated NextGen CI
on Python 3.11/3.12/3.13, without archive publishing/deployment. Initial execution
exposed missing target-attempt audit details, zero-timeout connection startup
races, stale concurrent submission/transfer observations and older test fixtures.
The repaired local complete run passed 15/15 batches (827 test executions); three
new deterministic regression cases passed separately. The final CI matrix and
complete run include those regression additions. Preserve explicit attempt validation, lease checks,
no duplicate external mutation and immutable published SQL. Later results and CI
runs supersede earlier unrun notes below. No production or live-media operations.

## Optional Shoko metadata and service setup — 2026-10-24

Finish the planned source implementation with independent AniDB-ID metadata
lookups after Keep and explicit local setup/maintenance/worker entry points.
Shoko titles are display-only; no file registration, scanning, hashing, renaming
or Sonarr mapping changes. The dashboard exposes optional status/private keys
and retry without reimport. Config v10 converts explicitly from v9; secrets v6
remains backward-readable. SQLite remains v23 with earlier SQL untouched.

`python -m src.anime` now provides fresh init, local check, worker run and
explicit config/database preview/apply commands. Fresh init never replaces old
files. Failed database apply retains maintenance; plan tokens pin schema/scripts.
API startup also checks local readiness. Add dedicated non-root systemd unit
examples, mount/path controls and a fresh-install/upgrade/lab-validation guide.
See [Shoko metadata](src/anime/SHOKO_METADATA.md) and
[Service setup](src/anime/SERVICE_SETUP.md).

The source work is ready for review, not runtime-proven completion. Tests/CI,
service installation, SQLite upgrades, client calls and media operations remain
unrun. Lab validation, including the v23 migration, is the next required milestone.
This draft is stacked on #137 and includes the earlier unmerged cleanup drafts.
No new merge or deployment is authorized or performed by this implementation.

## Keep/Reject download cleanup — 2026-10-24

New completed Reject decisions and completed post-Keep staging cleanups now
queue independent CLEANUP_DECIDED_DOWNLOAD jobs. Reuse cancellation's owned
stop/remove journal and manifest-only local cleanup. Keep/Reject completion
remains valid while cleanup waits, retries or needs review. The dashboard exposes
separate progress and Retry download cleanup; explicit retry never reimports or
repeats an uncertain client mutation. Preserve kept reservations, rejected-anime
protection, permanent media and shared series metadata.

Historical source ownership is checked without bypassing acquisition's Reject
fence. Require drained terminal acquisition work, no external consumer, original
download root identity, exact client marker/path/inventory and captured remaining
file identities. Reuse the existing cleanup table; v23/v9/v5 unchanged. No
historical automatic backfill. See [Download cleanup](src/anime/DECISION_DOWNLOAD_CLEANUP.md).

Source checks only; offline cases authored but tests/CI remain unrun. No live
clients, SQL/migration execution, media operations or deployment. This draft
builds on #136 and includes the unrun v23 migration from that stack. Optional
Shoko, service lifecycle/setup and lab validation remain. Automatic missing-
episode search is excluded by the consolidated architecture decision.

## Canceled-work reservation release — 2026-10-24

Completed drained cancellation cleanup now retires its exact reservation and
prepared acquisition claims without deleting history. Later verified deliveries
can reserve the episode and same torrent hash with fresh identities/markers;
released plans no longer consume active size budget. Shared consumers, uncertain
cleanup and staged/kept work retain protection. Existing completed cancellations
can explicitly resume Cleanup to retire retained slots.

SQLite v23 adds state/provenance and active-only uniqueness via an explicit
table rebuild. Historical SQL 001–022 stays unchanged. Config v9/secrets v5 stay.
Source syntax and matching fresh/migration table definitions checked; offline
migration/lifecycle cases authored but unrun. No SQL, tests/CI, live cleanup,
merge or deployment performed. See [Reservation release](src/anime/RESERVATION_RELEASE.md).

PR #135 provides staged-source cleanup after Keep. Download/torrent cleanup
after Keep/Reject, optional Shoko, service setup and lab validation remain.

## Post-Keep selected staging cleanup — 2026-10-24

Successful Keep completion now creates one independent CLEANUP_KEPT_MEDIA job
and saves its ID/state in the existing keep_imports JSON journal in the same
transaction. It rechecks Sonarr's episode binding and the readable permanent file
at the expected size, then reuses selected staging cleanup for the video and
episode NFO only. No destination video hashing; shared series NFOs stay.

The API exposes cleanup progress on the Keep receipt. A stopped cleanup can be
explicitly resumed by the Keep action; the dashboard labels this Retry staging
cleanup and never resubmits the completed Sonarr import. Successful cleanup can
queue an independent Jellyfin refresh. Existing completed Keeps are not backfilled.
No SQL/configuration/secrets upgrade is needed (v22/v9/v5 unchanged).

Kept episode reservations remain durable duplicate protection. Canceled-work
reservation release, torrent/download cleanup, optional Shoko and service setup
remain unfinished. Tests/CI and live media operations remain unrun; source checks
only. This work is published as a draft for review.

## Consolidation of completed drafts — 2026-10-24

User authorized reconciling completed NextGen work and merging it into NextGen.
Bring the #132 readable diagnostic responses and canonical no-search architecture
into the current #134 Sonarr/Jellyfin implementation. Preserve the #121 planning
proposals with historical banners; current code and user decisions supersede
older copy/hash/search proposals. #128–#130 are alternative implementations,
not additive migrations: retain the current v9/v5/v22 contracts and no destination
video hashing. No extra automatic search work is required.

Source checks only; tests, CI, browser checks and live media operations remain
unrun. Repository integration does not deploy the service or establish lab
readiness. Post-Keep cleanup/reservation release, optional Shoko and service
supervision remain unfinished.

## Jellyfin scan follow-up — 2026-10-24

Configured staging and Keep completions now enqueue independent Jellyfin scan
jobs. A failed refresh does not undo completed media work. Settings and private
API-key entry are exposed through the existing API/dashboard; blank address
keeps automatic refresh disabled. A successful job means scan accepted, not
confirmed episode visibility. Configuration v9, backward-readable secrets v5,
SQLite unchanged at v22. See [Jellyfin refresh](src/anime/JELLYFIN_REFRESH.md).
Offline cases authored/unrun; source checks only. No live requests or deployment.
Cleanup, optional Shoko and missing-episode search remain separate work.

## Keep through Sonarr — 2026-10-24

Keep now uses the existing staged path and verified TVDB placement to find/add a
Sonarr series and request one exact-file copy import. The worker records durable
intent, reconciles lost replies and checks the episode-file result without
hashing the permanent file. Shoko remains optional and separate. Source staging
files stay available; post-Keep cleanup and Jellyfin refresh remain next work.

Settings/API/dashboard now expose Sonarr address, profile, path mappings and
private credential replacement. Keep is available on staged work; readable job
receipts and a durable Kept result drive the dashboard. Configuration v8,
secrets v4 and SQLite v22 are explicit upgrades. Older SQL migrations stay intact.
See [Keep through Sonarr](src/anime/KEEP_SONARR.md). Tests/CI remain unrun;
source checks only. No live imports, migrations, media changes or deployment.


## Keep/Sonarr implementation in progress — 2026-10-23

User clarification: Shoko is optional. Keep must use verified TVDB series and
season/episode placement for Sonarr; Shoko results do not override or block it.
Reuse the stored staged path and existing NFO output. KeepSelection now reads
that existing manifest and carries both AniDB and TVDB identities, matching a
Sonarr episode by TVDB-authoritative placement rather than title or AniDB ID.
This is the first local implementation step, not completed Sonarr import.
Sonarr settings/credentials, HTTP adapter and import execution remain unfinished.
No duplicate staging/NFO pipeline or mandatory Shoko call is introduced.
Offline mapping cases are authored/unrun; Python syntax checks only.

## Finish the functional flow first — 2026-10-23

User priority: finish the code so it can be exercised in the lab, then improve it
from observed behavior. Do not expand optional safeguards/hardening as a substitute
for finishing missing functionality. Preserve the agreed staging move with no
byte-for-byte destination comparison. Tests/CI remain unrun until authorized;
no merge, deployment or live media operations are implied.

## Reject decision and selected staging cleanup — 2026-10-23

Reject is now wired through the dashboard, versioned management API and worker.
Admission records the AniDB-anime decision and queued control job together;
duplicate clicks reuse it. Future Intake selection/reservation stops for that
anime. Cleanup removes only the selected staged video and episode NFO, preserving
other episodes, shared series metadata and permanent files. A cleanup problem
does not undo the decision. See [the Reject contract](src/anime/jobs/REJECT_MEDIA.md).
SQLite v21 explicitly upgrades action vocabulary and decision storage.

Keep/Sonarr execution remains the next missing functional part. Reconsideration,
viewer refresh and optional integrations remain follow-ups. Tests and CI are
authored/unrun; source checks only. No live deletion, migration or deployment.

## Verified-media staging — 2026-10-23

The default worker now advances VERIFY_MEDIA into STAGE_TRIAL_MEDIA. Publish a
selected video by move, plus episode/series NFOs, using a pinned destination
and no-overwrite Linux rename. Same-filesystem moves rename the original inode;
cross-filesystem moves stream once then remove the exact source. Per the user
decision, staging performs no destination video hashing or byte-for-byte comparison. Durable publication intent
and inode receipts reconcile interrupted copies/renames; uncertain files remain
protected. All files must be revalidated before the same worker transaction
records the manifest, STAGED milestone and DONE. Keep remains unavailable.

SQLite v20 adds staging_publications through an explicit maintenance upgrade;
configuration stays v7. See [the staging contract](src/anime/jobs/STAGE_USER_EVALUATION_MEDIA.md)
for layout, fixed initial permission policy, non-root/shared-path requirements,
recovery, cancellation and deferred viewer integration. No Jellyfin refresh or
live media changes were performed. Offline tests and CI are authored/unrun.

## Downloaded-media verification — 2026-10-23

Completed downloads now hand off atomically to VERIFY_MEDIA in the default
worker. Read the exact pinned selected file without symlinks, require stable
size/inode/timestamps, record SHA-256, and use bounded local ffprobe metadata for
one video plus Japanese/English audio. Evidence and job completion commit under
existing lease/cancellation fencing. SQLite v19 adds media_verifications with an
explicit maintenance upgrade; configuration remains v7. See
[the verification contract](src/anime/jobs/VERIFY_MEDIA.md) for exact guarantees,
shared-path/executable requirements and limitations. No full decode or semantic
episode proof is claimed. Verification does not mark STAGED or authorize Keep.

Staging/publication recovery remains next. Tests and CI are authored/unrun; source
checks only. No live client/media operations, migrations or deployment.

## Hash-addressed metadata retrieval — 2026-10-23

The identity worker now supplies an HTTP(S) metainfo reader and installs
preparation/submission/download by default. A trusted configured base folder
serves lowercase-infohash.torrent files; no third-party default or webhook URL
is selected. Missing configuration enters review; absent metadata waits; bad
hash/inventory/access cannot authorize submission. Custom readers still work.

Configuration v7 explicitly adds metadata source/timeout settings, rendered by
the dashboard, while preserving historical converter shapes. SQLite stays v18;
secrets stay v3. Retrieval is bounded, cancellation-drained and redirect/proxy-free.
It persists no raw metainfo and creates no temporary client entries. See
[the metadata source contract](src/anime/acquisition/METAINFO_SOURCE.md).
Video verification and staging remain next. Offline tests and CI are unexecuted;
no live services, migrations, media operations or deployment.

## Reuse the dashboard through API v1 — 2026-10-23

Adapt the legacy dark shell, jobs table, badges and schema-driven forms in an
independent NextGen dashboard served by the API process. All private data and
actions use `/api/v1/`, management browser sessions and CSRF; webhook authority
remains separate. Intake lists explicit acquisition scopes; Jobs lists individual
steps and their history. Keep/Cancel/Cleanup use API button availability and
catalog explanations. Keep remains visibly unavailable for execution.

Settings and credential replacements use revision-checked API updates. No legacy
runtime/data bridge, old action endpoints, saved-credential reveal, extra server
or frontend dependency is introduced. Read [the dashboard contract](src/anime/dashboard/README.md)
for scope, source attribution and deployment paths. No media pipeline step is
implemented by this UI. Offline Python/UI cases are authored but unexecuted;
syntax/source checks only, with no CI, live listener or deployment.

## Intake cancellation and presentation correction — 2026-10-23

Follow-up to draft PR #120 and the Intake → Staging → Keep/Reject design in
draft PR #121: a completed episode reservation keeps unfinished acquisition
cancelable even when no metadata adapter is installed and all member jobs are
DONE. Capability reads and admission use the same predicate. Reservation-only
cancellation completes locally once drain is proven; active handlers still
require the existing cleanup control job. Reservation history and exclusivity
remain protected. STAGED continues to close Cancel.

Dashboard-facing settings now label storage.trial_root as Staging folder, and
episode-selection catalog/handler wording uses Intake. Internal trial names,
configuration/schema versions, codes, translation keys and stored records are
unchanged. No staging/Keep execution or dashboard HTML is introduced. Regression
cases are authored but unexecuted; source checks only, no tests or CI.

## Initial async worker core — 2026-10-22

The worker follow-up builds on draft destination-settings PR #106. It implements
trusted async/thread handler registration, bounded maintenance-aware polling,
transactional claims, token/expiry/attempt fencing, heartbeat renewal, expired
lease recovery, typed outcomes and atomic result/successor/history completion.
Normal deferred waits are separate from failure retries. Pause/stop drain active
work; forced cancellation waits for blocking threads before releasing admission.
See [WORKER.md](src/anime/WORKER.md). SQLite remains v8; jobs is unchanged.

No business/provider/trial/Keep handlers are installed: an empty registry fails
readiness. run_worker is the service entry point for trusted registrations; no
worker has been run/deployed. The management API and supervisor remain separate.
Offline lifecycle/concurrency/cancellation cases are included but unexecuted.

## Merged foundations and media destination settings — 2026-10-22

PRs #103, #104 and #105 were confirmed merged into NextGen with explicit user
authorization. Worker naming, handler history and both architecture documents
are now integration-branch foundations. The media settings follow-up adds
storage.trial_root and storage.keep_root with centralized labels/help and
validation, inspired by the legacy settings registry without runtime coupling.
Configuration v3 explicitly upgrades v2 with blank/unconfigured destinations;
new operations see updates while recorded artifact destinations stay bound.
SQLite remains v8. Tests are included but unexecuted; no CI or live writes.

Next implementation: async worker core, explicit handler registry, lease-fenced
job lifecycle, atomic successors and maintenance-aware polling/draining.
Identity/acquisition/staging/Keep handlers remain focused feature PRs.

## Pipeline architecture drafts — 2026-10-22

Before implementing the worker/business handlers, review the two separate
[architecture documents](src/anime/architecture/README.md):
[intake through the configured trial folder](src/anime/architecture/INTAKE_TO_USER_EVALUATION.md)
and [Keep through the permanent library](src/anime/architecture/KEEP_TO_LIBRARY.md).
They contain ownership/pipeline graphs, proposed handler boundaries, safety and
recovery contracts, legacy references and explicit open decisions. They do not
claim the illustrated components or proposed settings/storage already exist.

The worker owns pipeline advancement: a handler returns a typed outcome, and
registered feature routing/persistence adapters run under the worker-owned
lease-checked completion transaction. Persist result facts, completion, allowed
successor jobs and audit atomically. No independent DONE-then-enqueue sequence,
startup sweep of all DONE jobs, work_items table or parent/child workflow engine.
Keep is a separate authorized management request; staging does not authorize
permanent import. Sonarr-managed copy is the draft final-library assumption,
with scope, monitoring, retention and exact path/policy choices still to review.
This documentation-only follow-up builds on PR #104 (and #103); it does not
merge them or implement any worker, handlers, SQL/configuration or live changes.

## Handler invocation history follow-up — 2026-10-22

Keep the existing jobs table and independent queued-job model. No work_items or
to-do table is introduced. A job's kind selects its registered entry handler;
additional handler calls appear as separate job_history entries, not an array
on jobs. The history follow-up builds on draft naming PR #103; it is not assumed
merged. Its implementation is a separate focused PR targeting NextGen.

SQLite v8 adds nullable handler_code, handler_label and invocation_id to
job_history with paired metadata and invocation indexes. HANDLER_STARTED,
HANDLER_COMPLETED and HANDLER_FAILED have static readable catalog messages;
trusted handler registrations supply static labels and translation keys.
All attempts/calls remain tied to the existing job_id. Historical rows retain
NULL metadata. Published 001–007 are unchanged. Details and worker transaction/
lease-fencing requirements: [JOB_HANDLERS.md](src/anime/JOB_HANDLERS.md).
No jobs columns, state policies, API or worker execution loop change here.
Tests are included but unexecuted; source/AST/diff checks only.

## SQLite migration and maintenance follow-up — 2026-10-22

PR #101 (configuration library) and PR #102 (SQLite migrations/maintenance)
were confirmed merged into NextGen on 2026-10-22 with explicit authorization.
The canonical job process name is now **worker**, replacing coordinator.
The separate management API remains responsible for management requests.
Job handler modules use the `_job.py` suffix; see
[JOB_HANDLERS.md](src/anime/JOB_HANDLERS.md) for the naming convention.

Implemented libraries: version-aware SQLite upgrade preview/apply, verified
backup, per-step rollback, durable maintenance admission and current-database
connections. The naming follow-up adds SQLite v7 (007_worker_component.sql)
and configuration schema v2. Published SQL 001–006 remain unchanged. See
[the migration/maintenance contract](src/anime/storage/MIGRATIONS.md).

All future runtime DB users and entire job attempts must participate in the gate.
A request blocks new work while admitted work finishes; queued job states are
preserved. No automatic resume after success/failure/crash: compatible process
readiness and explicit completion are required. No supervisor, management API,
ADM command or worker runtime is implemented by these libraries.

Remaining worker preparation: async handler/outcome contract, bounded
claim/dispatch and cancellation semantics, lease/retry recovery policy and
management-to-runtime readiness integration. Keep the API in its own process.
Tests are included but unexecuted; source/syntax/diff review only, no CI or SQL.

## Current diagnostic implementation — PR #100, 2026-10-22

The PR #100 branch implements shared messages.py event/state catalogs and a
translation-ready renderer, history.py transaction-bound audit writers, and
SQLite schema v6 (004_diagnostic_components.sql, 005_job_statuses.sql,
006_api_diagnostics.sql and fresh-schema changes).
Existing settings/logging and reason registry are reused. Logging now resolves
static English copy, derives correlation from IDs, and emits bounded safe
exception summaries. Runtime-only OPERATION_FAILED does not add a job state.

Mandatory policy: [every diagnostic marker/error needs human-readable copy](src/anime/HUMAN_READABLE_MESSAGES.md).
Job states/activity: [JOB_STATUSES.md](src/anime/JOB_STATUSES.md).
Contract/API: [DIAGNOSTIC_CONTRACT.md](src/anime/DIAGNOSTIC_CONTRACT.md).
Schema/version authority: [SCHEMA_POLICY.md](src/anime/storage/SCHEMA_POLICY.md).
These documents supersede older planning snapshots below where status differs.
Use GitHub as the authority for PR #100 publication/merge status. Source review and AST
syntax checks only; offline tests are included but unexecuted, with no CI runs.

Next component: worker claims/routing, leases and stale-owner fencing,
retry/backoff/exhaustion decisions, atomic completion/successor creation, and
offline lifecycle/concurrency/recovery cases. Diagnostic writers do not enforce
those transitions. No worker loop, supervisor reload distribution, journal
service integration, dashboard API, deployment or legacy modifications are
implemented by this diagnostics PR.

## Management process boundary and configuration readiness review

Agreed: the management API runs in a separate process, using diagnostic
component api. ADM and dashboard are clients of that API. The API owns
management authentication/authorization and invokes shared services; the
service account owns its writable files/directories. The worker only
manages independent jobs and a small async pause/drain/resume/shutdown control
interface. It does not host the public management API, persist settings or run
schema migrations as ordinary jobs. Supervisor owns process management later.

Configuration follow-up: configuration_schema.py and configuration.py implement
an explicit v0→v1→v2 NextGen configuration conversion, validated versioned snapshots,
revision-checked atomic service-owned persistence and migration preview/backups.
See [CONFIGURATION.md](src/anime/CONFIGURATION.md) for the API/library contract.
Fields explicitly declare next_operation or restart behavior. Each new job
attempt/API request reads current settings once; running operations keep their
snapshot. Restart fields stay pinned to process startup values. Process-level
ports/concurrency/database changes require explicit restart, not a live reload
broadcast. This library does not implement a credential store,
management authentication/transport or automatic runtime application.

Next work: define the async job-handler input/outcome/recovery interface and
implement the worker against its job and maintenance/control contracts. Build
the separate authenticated management API against the shared services. Process
readiness/restart integration remains future supervisor work. These contracts can be defined together without
embedding the API in the worker. No hook/plugin system is introduced.
Concurrent jobs for the same anime are allowed: no blanket per-anime
serialization. Libraries own shared provider pacing and exact-resource
acquisition gates, returning structured defer/hold/duplicate outcomes. The
async worker applies bounded overall concurrency and registered handler
execution modes; blocking operations can use threads. No process per job is
required without a concrete isolation/CPU need.
The full dashboard, supervisor, provider and acquisition implementations remain
later work. Configuration/management feature changes belong in focused PRs,
not extensions that silently enlarge the diagnostics implementation.

## NextGen settings and diagnostics foundation — 2026-10-22

Work in branch `nextgen/config-diagnostics-20261022`. Before developing `worker`, use the independent `src/anime/settings.py` and `src/anime/diagnostics.py` foundation. Defaults: logging INFO; supported levels TRACE, DEBUG, INFO, WARNING, ERROR and CRITICAL. Optional validated component overrides for supervisor, intake, worker, identity and acquisition. TRACE is custom level 5; higher verbosity NEVER relaxes redaction. The standard library logger writes sanitized, bounded correlation records to stderr/journald, with event/job IDs, attempt, elapsed time and reason code when available. SQLite `event_history` and `job_history` remain separate durable state histories, and worker code must persist transitions atomically when implemented.

Configuration currently loads strict JSON into immutable settings wrappers; hot reload, supervisor distribution, dashboard UI, external services, live workers and persistence of configuration are NOT implemented. Unknown keys and unsupported levels fail validation. Never log raw webhook bodies, provider responses, torrent secrets or credentials. Legacy diagnostics (merged PR #67) and legacy settings schema inform this fresh NextGen module only; no dependency on `src/legacy`. Offline tests are committed but deliberately not run yet, and CI remains disabled. Do not merge/deploy without explicit instruction.

## What is canonical

**Series schema, not processing state.** NextGen uses one versioned `AnimeSeries v2` schema (`src/anime/schema/anime_series.py`; detailed RFC at `src/anime/schema/README.md`). The series describes **the anime and its episodes**. It MUST NOT include webhook notification IDs, traces, GUIDs, source release names, job IDs, retries, acquisition, staging, evaluation, or export status. That information lives in independent durable operational records connected to the series after identification.

The identified series's required fields are:
- `anime.anidb_id`: positive AniDB anime ID (anime and sequel/cour identity).
- `anime.tvdb_series_id`: positive verified TVDB **series** ID for Sonarr; mandatory for functional downstream integration.
- `anime.title`: nonblank **identified anime title**, not the original release name.
- `schema_version`: exactly `2` in the V2 validator.

The `anime.tvdb_series_id` link is **mandatory**, because the full pipeline needs Sonarr's TVDB series identity. AniDB remains authoritative for anime and episode identities; this requirement does not make TVDB episode/season placement mandatory. `episodes` may be omitted or empty; missing normalizes to `[]`. **Every populated episode** MUST have an exact verified positive `anidb_episode_id` (for Shoko and episode-specific operations). An empty list is a series-only identity, **not** proof a batch contains zero episodes or that episode-level identification is finished.

Other series descriptors (alternative/original titles, year, anime type, AniDB episode display numbers, episode title/type, optional `episodes[].tvdb_placement` and `attributes`) are nullable or optional. TVDB *episode-level* IDs and per-episode TVDB season/episode mapping **are not hard AniDB series-identification requirements**. Known placement is represented as `{season, episode}` inside the episode, with season 0 permitted for specials. AniDB episode numbering MAY be continuous or nonstandard. Never infer TVDB season from AniDB ordinal, default unknown to S01/E01, or conflate separately cataloged AniDB sequel entries under one TVDB series.

Every published series MUST satisfy a uniform, machine-checkable schema; optional fields normalize to null/empty object to spare future consumers missing-key crashes. Field docs track meaning, reason, owner, availability, and origin. Breaking changes require explicit schema versioning and permanent offline fixture tests. Structured optional attributes never override the core named fields.

### RFC-IDENTITY-001: mandatory completion gate

**This gate is at the END of anime-series identification, not at the webhook.** Series identification and episode/release identification are different: the former MAY succeed without any identified episodes, but that alone MUST NOT authorize episode-specific downstream actions.

- The identification component gathers information in its own *incomplete* work state. An unrecognized AniDB anime is **not** a completed series. An absent TVDB series link is **not** a valid completed AnimeSeries identity. An empty episode array **may** be a valid identified anime series, but MUST NOT be interpreted as completed episode identification or release coverage.
- Before publishing an identified anime series, the identity component MUST validate the candidate via `AnimeSeries.from_dict` (or equivalent V2 validation). Only a passing result may be recorded as the series identity. A populated episode additionally requires independently verified AniDB episode evidence. Numeric shape validation cannot prove an ID came from AniDB.
- Failed series schema validation MUST prevent completed anime-series identification; missing/unverified episode identities MUST separately block episode-specific acquisition completion, Shoko linking and Sonarr episode import. Persist a reason-coded, traceable **INCOMPLETE_IDENTITY** or **INVALID_IDENTITY** outcome in operational job history. The identity work MAY retry when information improves, or enter review. A failed candidate is a normal per-item outcome; it MUST NOT crash workers or lose the original webhook notification.
- A series identity, even with no identified episodes, can be read without repeated anime-series identification by downstream components. Components still enforce their own **separate**, integration-specific prerequisites (e.g. exact TVDB episode placement before the NFO/episode import operation that needs it, file ownership/path verification at use time). The RFC is not authorization to acquire or export.

## Job architecture handoff — agreed 2026-10-22 (follow-up to merged PR #94)

**Current work:** dedicated jobs-table SQLite follow-up on branch `nextgen/dedicated-jobs-20261022`, targeting `NextGen`. PR #94 already merged the six-table SQLite foundation and AnimeSeries v2 remains unchanged.

**Agreed design:** Webhook intake and the worker are different Linux processes (different PIDs). Intake durably writes the original webhook observation and an initial `jobs` row in a single SQLite transaction, then acknowledges. The worker polls `jobs`, atomically leases eligible work, dispatches by `kind`, records outcomes and recovers expired leases. Components can request new independent jobs.

**No parent/child relationship:** `created_by_job_id` is nullable provenance only, not a dependency, hierarchy, sibling relation, execution ordering, or cascading-lifecycle rule. `event_id` is also nullable on jobs for work without a webhook. Every job has an independent lifecycle. Completing a job and creating a follow-up job must be an atomic SQLite transaction when both operations are required.

**Job history:** `job_history` records independent job transitions, attempts, handoffs and reason-coded failures; it is separate from webhook `event_history`. A future worker must insert the history record together with the associated job state change.\n\n**SQLite v1 jobs columns:** job_id, nullable event_id, nullable created_by_job_id, kind, payload_json, state (PENDING/RUNNING/RETRY_WAIT/NEEDS_REVIEW/DONE), available_at_ms, attempt_count, nullable lease_token, nullable lease_expires_at_ms, nullable last_reason_code, created_at_ms, updated_at_ms. Multiple jobs may originate from one event. No global UNIQUE(kind, subject) rule should discard independent deliveries. The worker must use lease-token and expiry checks to prevent stale owners from committing. A job result does not, by itself, prove acquisition or episode coverage.

**Incremental compatibility:** PR #94's `webhook_events` includes historical work-state fields and `save_identity` operates on them. This follow-up adds dedicated `jobs` storage; removing old event-state coupling and implementing real workers is distinct later work. Do not mistake the existence of a jobs table for a functioning worker.

**Scope:** SQLite schema, basic persistence helpers, documentation, isolated tests in `src/anime/tests/`. No intake HTTP service, no daemon, no acquisition, no migration from legacy, no production deployment. Leave automatic paid CI disabled; tests remain tracked, with offline comprehensive testing planned later per user decision. Do not claim unrun tests have passed. No merge/deployment without fresh authorization.

## Versioned SQLite SQL files — PR #95

The future **NextGen-only** migration component will read ordered scripts from `src/anime/storage/sql/`:

- `001_foundation.sql` creates exactly the initial six-table NextGen foundation and sets `PRAGMA user_version=1`. Use this only on an empty NextGen database.
- `002_independent_jobs.sql` adds the independent `jobs` and append-only `job_history` tables and their indexes and sets `PRAGMA user_version=2`. Apply this to a version-1 NextGen database. It does not recreate the six existing tables or alter their rows.
- On a fresh database, run 001 then 002. On an existing verified v1 database, run only 002. At v2, do not reapply either script.

The migration component is **not implemented** in this PR. When built, it must check `PRAGMA user_version`, reject missing/unknown/unexpected versions, apply each SQL script atomically (including its version bump), and verify integrity. A failed migration must roll back without claiming the new version. Never run these scripts against legacy production databases. The Python `initialize()` helper creates v2 on a genuinely empty database and rejects implicit upgrades of v1 rather than silently stamping a later version. Existing `webhook_events` work-state columns remain until a separate compatibility change.

## NextGen processing components

Explicit static components, each with narrow responsibility:
1. **Webhook intake:** accept each authenticated, reasonably bounded delivery, retain the supplied release name as-is and write the delivery durably. No anime/episode/GUID/name-based deduplication here. Deliveries are observations, not series.
2. **Identity and cache:** resolve identified AniDB anime + exact AniDB episode(s), real title and TVDB series link. Reuse previously established cache evidence when applicable, while checking it applies to each input. No provider scoring/voting system and no assumption that altered title means new anime. The **identity gate** validates before marking done.
3. **Canonical episode claim:** once an exact episode identity exists, atomically determine if work for that episode has already been claimed/handled, preventing duplicate acquisition under parallel submissions. Preserve every source delivery and trace history.
4. **Acquisition:** own torrent operations, torrent identity/ownership, configured size limits, and Japanese + English audio checks. Do not infer identity from an acquisition job.
5. **Staging/Jellyfin:** own staging files and NFO output; require necessary TVDB placement for the particular operation and validate path/filesystem ownership at the moment of use.
6. **Human review and Sonarr export:** explicit Keep or Reject; exact export is never authorized merely by staging or by an identified series. Preserve existing hard-won mapping and safe import semantics, without linking runtime legacy code.

The worker owns bounded durable scheduling, not business-logic decisions. Logging/history uses stable per-delivery Postfix-style `AD-N` trace IDs and independent job IDs, *outside* AnimeSeries. Store structured durable history with safe reason codes, retries, cache activity and component boundaries; protect credentials and other sensitive data in logs.

## NextGen tests: fresh, local batches, no automatic GitHub checks

The clean-slate code uses its **own** tests in `src/anime/tests/`. The legacy tests in `src/legacy/` are preserved for history and regression behavior, but MUST NOT gate or be imported by NextGen implementation. The legacy tests need not inherently fail; they simply validate a different runtime.

The automatic GitHub Actions **Python test workflow has been removed** on this draft PR branch. Do not automatically run legacy or NextGen tests on pushes or pull requests. Keep the separate manually invoked archive publishing workflow unchanged. If branch protection requires a deleted check, resolve that repository rule explicitly before merging; do not silently bypass it.

Use `python -m src.anime.tests.run_batches --match schema` for focused schema work, or `python -m src.anime.tests.run_batches` for all NextGen tests, executed in separate groups of up to five test files. Use `--batch-size N`, `--match <filename-part>`, and `--list` as needed. This needs Python 3.12+ and runs **locally**, never against live services. See `src/anime/tests/README.md`. Each new NextGen component gets its own isolated offline tests and sample data in the existing `fixtures/` folder.

**Current test policy (2026-11-04):** tests are no longer deferred. GitHub Actions is avoided for routine verification; instead, every completed work item runs the full local batch suite (`python -m src.anime.tests.run_batches`), fixes failures as they appear, and only then commits, pushes and opens a PR. PR descriptions report the local result. A failure that cannot be fixed confidently is documented in the PR rather than guessed at. Earlier "authored/unrun" notes in this file describe the previous deferral policy and are historical.

## Next PR: delivery and worker reliability (TCP-like, application-level)

**Primary goal:** a webhook receipt that got HTTP success must not disappear or be silently unprocessable after a crash. TCP is an analogy for acknowledge/retry/recovery; do not promise literally failure-free or exactly-once execution.

Scope:
- Fresh standalone NextGen webhook handler, authenticated/validated/size-bounded, plus **fresh** SQLite durable inbox. Acknowledge successful submissions only **after** durable commit.
- Commit both the delivery record and its pending identity work marker atomically. This may be one inbox row with `PENDING` status; do not assume a separate executable job row per webhook is required.
- Bounded processing, leases or recoverable claims, retry/backoff on temporary failure, explicit permanent-error/manual-review state, graceful backpressure/retryable HTTP responses under overload.
- Every accepted delivery remains independently traceable; no content-based deduplication at intake. Identity caching and *post-completion* episode claims are separate later work. Nothing in the series schema tracks retries, attempts or delivery receipt.
- Offline fault-injection and concurrency tests: 10,000 requests with duplicate or changed release names, process death between commit and acknowledgment, crash during work, SQLite failure, restart recovery, provider unavailable, poison payload, duplicate acquisition claim protection (when built), log correlation, and bounded resource usage.
- No production changes, no real provider integrations, no live acquisition, no Jellyfin/Shoko/Sonarr side effects and no migration of legacy data.

Failure semantics: **accepted does not mean identified; identified does not mean acquired.** A validation failure fails *that identification attempt* cleanly. System-wide crashlessness is a design target, not an absolute guarantee; persistence, recovery and visibility are testable service invariants.

## Regression expectations and PR boundaries

Preserve domain behaviors as test cases, not code bridges: AniDB versus TVDB sequel/cour disagreement, Re:ZERO's verified mapping and exact episode references, season zero/specials, multi-episode releases, ambiguous or duplicate mapping, missing provider data, safe file ownership, dual audio, and explicit human approval.

- **PR #89:** closed without merge, superseded.
- **PR #90:** independent optional backfill design, **not** automatic backfill for NextGen.
- **PR #91:** draft exploratory traces/docs from legacy; outdated migration suggestions are superseded. Do not merge a stale handoff.
- **PR #92:** merged into `NextGen`, offline-only AnimeSeries v1 schema and RFC identity-completion tests. Superseded by a proposed minimal V2 schema revision; no NextGen deployment or background worker included.

The separate SQLite/webhook-design effort implements **durable intake/reliability infrastructure**, not the identity provider algorithm or media integrations. V2 schema work stays in its own draft feature PR.
