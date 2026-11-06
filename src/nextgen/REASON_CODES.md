# NextGen operational reason-code registry

The machine-readable catalog lives in `src/nextgen/reasons.py`. It is a fresh
NextGen definition informed by legacy diagnostics and identity-triage behavior,
not a port of a complete legacy catalog. No runtime dependency on legacy exists.

## Contract

- `event_code` says **what happened**; `reason_code` explains **why**, when
  needed. Neither is the incoming delivery's `event_id`.
- New emitted reasons must be registered. Codes are stable uppercase ASCII
  identifiers, at most 64 characters; never recycle a code's meaning.
- `retryable` is advisory: true means a potentially transient condition,
  false means repeating unchanged work is not the suggested remedy, and null
  means the code alone cannot classify recovery. It never grants retry
  permission, determines a state, sets a delay, or overrides attempt limits.
  The worker and handler outcome decide recovery using actual evidence.
- Severity describes a condition, not the job state or process health.
  `NEEDS_REVIEW` can represent a normal per-item outcome.
- `display_reason` supplies static plain-language messages and actions.
  Unknown historical identifiers get generic text and null retryability.
  Malformed, non-string, or oversized identifiers display as `UNKNOWN`.
  Never pass secrets as codes: syntactic validation cannot detect a secret
  that happens to resemble an identifier. Render all values as text in a UI.
- `safe_details` builds small JSON-compatible facts before persistence.
  It rejects unknown arguments and invalid values; it is not a scrubber for
  arbitrary payloads. Never provide raw requests, provider responses, names,
  URLs, paths, tokens, lease tokens, exception strings, or credentials.
- Logging filters must never suppress essential SQLite lifecycle history.
  State changes and their history belong in the same transaction. A failed
  storage write cannot reliably record its own failure in that same database;
  sanitized runtime logging is the fallback, with later reconciliation.

## Catalog and recovery classification

| Code | Dashboard message | Retry classification |
| --- | --- | --- |
| `IDENTITY_INCOMPLETE` | Anime identification is incomplete. | Context required |
| `IDENTITY_INVALID` | Anime identification could not be verified. | Changed input/evidence needed |
| `IDENTITY_CONFLICT` | Conflicting anime information needs review. | Review needed |
| `PROVIDER_UNAVAILABLE` | An information provider is temporarily unavailable. | Potentially transient |
| `JOB_LEASE_EXPIRED` | Processing was interrupted. | Potentially transient |
| `JOB_HANDLER_FAILED` | An unexpected processing error occurred. | Context required |
| `JOB_RETRY_EXHAUSTED` | Automatic retries have stopped. | Review needed |
| `SERVICE_UNAVAILABLE` | Waiting for a service that is unavailable. | Potentially transient |
| `SERVICE_SETUP_INVALID` | A required service is not set up correctly. | Changed input/evidence needed |
| `STORAGE_UNAVAILABLE` | Processing is temporarily unavailable. | Context required |
| `STORAGE_STATEMENT_INVALID` | An internal database statement error stopped processing. | Component change needed |
| `UNSUPPORTED_JOB_KIND` | This type of work cannot be processed. | Component change needed |

Lease expiry does not prove a handler had no external effects. Recovery must
fence stale owners and establish whether replay is safe. A handler exception
may be deterministic; a storage error may be locking, disk exhaustion,
corruption or an uncertain commit. Do not classify all these as automatic retries.
Unsupported work must remain visible; do not silently discard it.

## Comparison with the current foundations

SQLite v6 retains arbitrary historical reason text in `last_reason_code`, `event_history`
and `job_history`; no SQL enum or schema migration is needed for this catalog.
Job states are PENDING, RUNNING, RETRY_WAIT, NEEDS_REVIEW and DONE; event states
also include IDENTIFIED rather than DONE. States are not reason codes.
Storage emits ACCEPTED, CREATED and SERIES_IDENTIFIED through the shared
history writers. messages.py registers the remaining lifecycle codes for
worker integration, plus runtime-only OPERATION_FAILED. The worker
execution loop itself remains unimplemented.

`IDENTITY_CONFLICT` covers canonical TVDB disagreement, conflicting retry
results and an episode already owned by another AniDB anime. Two AniDB anime
sharing a TVDB series is allowed and is not, by itself, a conflict.
`IDENTITY_INVALID` covers failed candidate validation; `IDENTITY_INCOMPLETE`
covers missing verification evidence. Numeric shape validation alone is not
provider verification. A valid series-only AnimeSeries v2 with zero episodes
does not trigger either reason. Missing optional placement is not a series
identity failure; future handlers enforce their own operation prerequisites.

Older prose in `development.md` calls these INCOMPLETE_IDENTITY and
INVALID_IDENTITY. Those were descriptive draft names, not aliases emitted by
this registry; new code uses IDENTITY_INCOMPLETE and IDENTITY_INVALID.
Historical unknown identifiers remain readable with generic display text.

The merged diagnostics helper accepts `reason_code` and a boolean
`retryable` log field. A null catalog classification must be omitted there.
The logging field should describe a contextual decision, not blindly copy a
catalog hint. The PR #100 diagnostic infrastructure implements correlation, durable history
writers, event-code validation and shared English/translation-key rendering.
See DIAGNOSTIC_CONTRACT.md and HUMAN_READABLE_MESSAGES.md.

Legacy diagnostics contribute bounded correlation, catch-boundary exceptions
and redaction lessons. Legacy exact episode/TVDB placement gates do not become
new mandatory AnimeSeries v2 fields. No acquisition or provider-specific reason
catalog is invented before those components are designed.

## Structured details

| Field | Allowed values |
| --- | --- |
| `missing_fields`, `invalid_fields` | Up to 20 schema-location names from the explicit allowlist; sorted and deduplicated |
| `retry_at_ms` | Optional nonnegative signed-64-bit integer timestamp |
| `attempt`, `max_attempts` | Optional nonnegative signed-64-bit integer observations |
| `cause_reason_code` | Optional registered reason, e.g. the last cause when retries stop |

Booleans are not integers here. Omitted values produce no field; empty detail
is an empty object. Field values and arbitrary nested attributes are never
accepted. Lists name schema locations without exposing their values.
Only report episode fields for relevant supplied entries or an operation
requiring episode evidence, never because a valid series has no episodes.
These fields explain a condition; canonical scheduling still lives in the
job row. The helper does not enforce worker transition policy.

Example: a policy-authorized retry may record
`event_code=RETRY_SCHEDULED`, `reason_code=PROVIDER_UNAVAILABLE`,
`details_json={"attempt":2,"max_attempts":3,"retry_at_ms":1234}`.
Whether a retry is actually scheduled comes from persisted state, not the
message text. Exhaustion can retain PROVIDER_UNAVAILABLE as cause_reason_code.

## Delivery and validation status

This PR supplies the reason catalog, presentation helper and detail builder,
with offline tests authored but **not executed** under the current project
policy. The reason catalog itself required no SQL change; diagnostic PR #100 adds
SQLite v4 component attribution. No CI runs, worker execution, dashboard,
management API, journald integration or supervisor reload distribution are
included. Existing Python logging/settings and central reason copy are reused.


The v5 schema adds held/blocked/canceled lifecycle states and typed activity
without deriving status from a reason string. See JOB_STATUSES.md.

## Configuration-library reasons and markers

| Code | Human-readable meaning |
| --- | --- |
| CONFIGURATION_INVALID | Configuration could not be validated. |
| CONFIGURATION_CONFLICT | Configuration changed before this update could be applied. |
| CONFIGURATION_MIGRATION_REQUIRED | Configuration needs an upgrade. |
| CONFIGURATION_STORAGE_FAILED | Configuration storage is unavailable. |
| CONFIGURATION_DURABILITY_UNCONFIRMED | Configuration was updated, but durable storage could not be confirmed. |

The first three require corrected input, refreshed revision or explicit upgrade,
not an automatic unchanged retry. Storage outcomes require context; successful
rename plus failed directory sync does not mean nothing saved. See
CONFIGURATION.md. CONFIGURATION_SAVED, CONFIGURATION_MIGRATED and
CONFIGURATION_SAVE_UNCONFIRMED are runtime-only event markers in messages.py.
Every reason/message/action retains the same stable translation-key contract.
These additions do not alter SQLite schemas or diagnostic-history detail shapes.


### Maintenance and database upgrades

The shared libraries add MAINTENANCE_PENDING, MAINTENANCE_BUSY,
MAINTENANCE_FAILED, DATABASE_VERSION_UNSUPPORTED, DATABASE_SCHEMA_INVALID,
DATABASE_MIGRATION_CONFLICT and DATABASE_MIGRATION_FAILED. Every entry has a
static English message/action and translation keys. Retry hints do not schedule
retries or clear maintenance. The future API catches typed errors and emits
OPERATION_FAILED with their reason_code, never raw SQLite messages/SQL/paths.

Runtime success markers DATABASE_MIGRATED, MAINTENANCE_REQUESTED and
MAINTENANCE_COMPLETED resolve the same shared catalog. They do not add job states
or incoming-event history rows. Durable marker/schema state drives recovery
independently of logging; a management audit API/table remains future work.
See storage/MIGRATIONS.md for drain, backup and partial-upgrade behavior.


Handler invocation markers HANDLER_STARTED, HANDLER_COMPLETED and HANDLER_FAILED
are job-scope observations, not job states. Every marker resolves static readable
copy; failure requires an existing reason (including JOB_HANDLER_FAILED for an
unexpected handler exception). Trusted HandlerDefinition labels provide the
readable handler name and handler.CODE.label translation key. Labels are bounded
static application metadata, never payload/exception text. See JOB_HANDLERS.md.


Worker core additions: JOB_WAITING describes a normal deferred prerequisite,
JOB_PAYLOAD_INVALID requests review of malformed/oversized queued input, and
WORKER_NOT_READY reports missing installed handlers. All have readable actions.
DEFERRED is a registered job event with readable copy, not a new state; it uses
RETRY_WAIT without spending the failure retry budget. Existing retry/failure/
lease reasons and lifecycle markers are reused. No raw exceptions are persisted.

JOB_STATE_INVALID reports inconsistent job progress with static readable copy and
an action to review history. Expired RUNNING jobs with zero recorded attempts are
quarantined for review without fabricating a historical claim or handler call.


Provider library reasons: PROVIDER_RESPONSE_INVALID has the static message
"An information provider returned an unusable response." and requests a later
retry or provider review. PROVIDER_AUTH_FAILED says "An information provider
could not authorize access." and asks for provider access settings to be reviewed.
Both use existing translation keys/catalog resolution. Normal cache misses under
shared pacing use JOB_WAITING/DEFERRED; provider outages reuse PROVIDER_UNAVAILABLE.
Raw HTTP errors, URLs, bodies, credentials and tokens are never diagnostic details.


PROVIDER_CONFIGURATION_INVALID reports missing/invalid required AniDB client or
TVDB API-key configuration with "Provider configuration is incomplete or invalid."
Its action names the required setup without echoing supplied values.
SECRET_STORAGE_UNAVAILABLE says "Provider credentials could not be accessed
safely." and requests storage/permissions/format review. Both resolve through the
shared translation-ready catalog. Credential publication results expose only
presence/revision and durability; secret values never become diagnostic details.


RELEASE_UNRESOLVED is retained for older history and unclassified failures. Its
plain-language message explains that episode verification stopped and the saved
reason does not identify the failed check. Rerunning verification records the
specific current blocker; old history cannot reconstruct a discarded cause.

New episode checks distinguish RELEASE_TITLE_UNCLEAR, RELEASE_TYPE_UNSUPPORTED,
RELEASE_MAPPING_MISSING, RELEASE_MAPPING_UNSUPPORTED,
RELEASE_ANIDB_EPISODE_MISSING, RELEASE_EPISODE_MAPPING_MISSING,
RELEASE_EPISODE_MAPPING_AMBIGUOUS and RELEASE_TVDB_EPISODE_MISSING. Their messages
identify the failing source and their actions explain what to check before
retrying. These remain review outcomes: anime identity alone does not authorize
an episode download. Exact cached episode proofs and successful mappings retain
their existing behavior.
RELEASE_CONFLICT says "Release episode information conflicts." and asks for the
identifiers/mapping evidence to be checked. Both have static messages/actions and
translation keys through the shared catalog. Neither a provider title nor raw
release name/payload is placed in diagnostic history.


Episode selection retains stable historical identifiers `TRIAL_SELECTED`,
`TRIAL_OUTSIDE_WINDOW`, `TRIAL_DISABLED` and `TRIAL_PLAN_CONFLICT`. Their
English messages use "Intake selection" or "episode selection", not the old
product name. `TRIAL_EVALUATED` remains the durable job event with a registered
reason; its audit and selection facts commit together and log only after commit.
A selected episode does not mean download, staging or Keep completed.

`TRIAL_COUNTS_INVALID` rejects two zero episode-selection counts during
configuration validation. `TRIAL_DISABLED` is retained for historical
compatibility only; current evaluations do not emit it. Do not rename any of
these stored codes without a separate compatibility migration.

EPISODE_RESERVATION_GRANTED explains a durable exact-episode reservation;
EPISODE_RESERVED holds competing work; EPISODE_RESERVATION_CONFLICT requires
identity/selection review. All have static readable messages/actions and
translation keys. EPISODE_RESERVATION_CREATED and EPISODE_RESERVATION_SKIPPED
are durable job markers; a skipped gate carries TRIAL_OUTSIDE_WINDOW.

ACQUISITION_SOURCE_MISSING requires a normalized supported source identity;
ACQUISITION_METADATA_INVALID requires file-list/declaration review;
ACQUISITION_PLAN_CONFLICT protects immutable reservation/source/plan facts;
ACQUISITION_PLAN_READY describes preparation only. Each has a readable message,
action and translation key. ACQUISITION_PREPARED is a durable after-commit
logged job marker; neither file names nor tracker data are diagnostic details.

DOWNLOAD_CONFIGURATION_INVALID requires local download settings/credentials
before client work. ACQUISITION_SIZE_LIMIT reviews an oversized selected video
or aggregate plan; ACQUISITION_SIZE_EVIDENCE_MISSING reviews unknown historical
selected-byte evidence. All have bounded static readable messages/actions and
translation keys, without values or paths in exceptions/history.

DOWNLOAD_CLIENT_UNAVAILABLE/AUTH_FAILED/RESPONSE_INVALID/UNSUPPORTED describe
bounded transport failures without response bodies or credential values.
TORRENT_SUBMISSION_UNCERTAIN defers read-only reconciliation; TORRENT_ALREADY_PRESENT
protects externally present torrents; TORRENT_PROVENANCE_CONFLICT reviews changed
provenance/state/files; TORRENT_STOPPED_CONFIRMED describes a stopped snapshot
only. TORRENT_INTENT_RECORDED and TORRENT_RECONCILED are durable job markers with
readable messages/actions/translation keys and after-commit logging.

## Selected-video transfers

Every new transfer event and reason has a static English message in the shared
catalog; translations use stable codes, never exception text. Retry classification
is descriptive: worker policy and the durable journal decide behavior.

| Reason | Message |
| --- | --- |
| TORRENT_SELECTION_UNCERTAIN | Video file selection is waiting for confirmation. |
| TORRENT_SELECTION_CONFIRMED | The video file is selected and ready to download. |
| TORRENT_START_UNCERTAIN | Download startup is waiting for confirmation. |
| TORRENT_DOWNLOAD_IN_PROGRESS | The selected video is downloading. |
| TORRENT_DOWNLOAD_COMPLETE | The download client reports that the selected video is complete. |
| TORRENT_DOWNLOAD_WAITING | Waiting for the existing download to appear. |
| TORRENT_TRANSFER_CONFLICT | Download details changed and need review. |
| TORRENT_PIECE_OVERLAP | This video shares download pieces with files that are not selected. |
| TORRENT_SNAPSHOT_CHANGED | The download is changing or being checked. |

Selection/start uncertainty never authorizes replay. Client completion is not
local media verification or permission to stage a video.

## Cancellation

All new cancellation events/reasons have static readable English catalog entries,
with stable translation keys. Cancellation is an explicit operator decision, not
a reason-code retry flag.

| Code | Message |
| --- | --- |
| JOB_CANCELED | This work was canceled. |
| CANCELLATION_DRAINING | Waiting for active work to stop before cleanup. |
| CANCELLATION_PENDING | Cancellation cleanup is waiting for confirmation. |
| CANCELLATION_REVIEW | Cancellation cleanup needs attention. |
| CANCELLATION_COMPLETE | Cancellation cleanup is complete. |

CANCELLATION_REQUESTED, CANCELLATION_CONTROL_RECORDED and
CANCELLATION_CLEANUP_CHANGED identify events; reasons explain their outcomes.
Details never include arbitrary client response bodies or exception strings.

## Versioned HTTP API

API v1 responses carry registered reason codes, readable messages/actions and
translation keys. API_REQUEST_REJECTED and API_TRANSPORT_NOTICE are runtime events;
API_SESSION_CREATED, API_SESSION_CLOSED and API_CREDENTIALS_UPDATED are readable
operational notices. No request URL, query, header, cookie, body or raw exception
is used as a message. The catalog includes API_AUTH_REQUIRED, API_AUTH_UNCONFIGURED,
API_CSRF_REQUIRED, API_ORIGIN_INVALID, API_REQUEST_INVALID, API_WEBHOOK_INVALID,
API_BODY_TOO_LARGE, API_MEDIA_TYPE_INVALID, API_ROUTE_NOT_FOUND, API_METHOD_INVALID,
API_RATE_LIMITED and API_UNAVAILABLE. See api/README.md for HTTP mappings and
credential separation. These reasons do not dictate Worker retry policy.

### Durable management actions

| Code | Human-readable message | Meaning |
| --- | --- | --- |
| MANAGEMENT_ACTION_UNAVAILABLE | This action is not available for this work. | Current target cannot accept the action |
| MANAGEMENT_JOB_SUBMITTED | Job submitted. | Durable acknowledgement, not execution success |
| MANAGEMENT_JOB_EXISTS | This action already has a job. | Follow the existing action receipt |
| KEEP_NOT_READY | Keeping videos is not available yet. | Intent recorded; verified media promotion remains unimplemented |

All include static suggested actions and translation keys through MessageCatalog.
`MANAGEMENT_ACTION_RESUMED` is a registered readable job event for explicit
cleanup resubmission after review. No arbitrary exception text is presented.
