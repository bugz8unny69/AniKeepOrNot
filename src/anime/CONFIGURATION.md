# Shared configuration library and API contract

configuration_schema.py supplies pure validation, conversion and snapshots.
configuration.py supplies service-side persistence. Existing settings.py
logging validation is reused. No API server, ADM client, worker, credential store or production configuration is created.

## Operation snapshots, not reload broadcasts

Retain a ConfigurationSnapshot at process startup. At the start of each job
attempt/API operation, call store.operation(startup, component=...) to read
the current file and obtain a detached immutable view:

- Ordinary fields come from the latest saved revision.
- Restart fields come from that process's startup snapshot.
- pending_restart_fields reports saved/startup differences affecting that
  component. Reverting a desired value to its active value clears the flag.
- Running operations retain their snapshot; new attempts/requests read again.
  A saved revision older than process startup is rejected until reconciled.
- desired_revision and startup_revision identify both contributing versions.

Do not apply desired port/database/concurrency values to already-open resources.
Use startup/effective values for runtime operations. store.read() intentionally
returns desired values for editing. The API can show desired and active values
separately. No config-revision field is added to SQLite history in this PR.

## Central field definitions

| Field | Default | Apply mode | Restart scope |
| --- | --- | --- | --- |
| logging.level | INFO | Next operation | None |
| logging.components | Empty overrides | Next operation | None |
| api.host | 127.0.0.1 | Restart | API |
| api.port | 8765 | Restart | API |
| worker.concurrency | 4 | Restart | Worker |
| storage.trial_root | Unconfigured | Next operation | None |
| storage.keep_root | Unconfigured | Next operation | None |
| torrent_metadata.base_url | Unconfigured | Next operation | None |
| torrent_metadata.timeout_seconds | 30 | Next operation | None |
| storage.database_path | /var/lib/anime-discovery/nextgen.sqlite | Restart | All current components |

FIELDS defines defaults, type/range validation and apply metadata.
field_definitions() returns detached JSON-compatible API metadata. New fields
must explicitly declare next_operation or restart and affected components.
Add provider/acquisition fields with their actual consuming components; this
foundation exposes no speculative nonfunctional integrations.

logging.components patches replace the complete override map. Revision checks
protect the map the caller edited. snapshot.logging_settings() supplies the
existing logging helper's Settings type. The library does not change global
logger thresholds while other operations run: consumers need an appropriate
operation-scoped adapter or explicit process configuration.

## Independent format version and publication revision

Canonical JSON has schema_version=8, a positive revision, and a settings object
containing the sections above. Example:

    {
      "schema_version": 8,
      "revision": 1,
      "settings": {
        "torrent_metadata": {"base_url": "", "timeout_seconds": 30},
        "qbittorrent": {"base_url": "", "category": "anime-discovery", "timeout_seconds": 30},
        "acquisition": {"max_episode_mb": 600, "max_plan_mb": 3000},
        "trial": {"backfill_count": 6, "following_count": 1},
        "anidb": {"client_name": "", "client_version": 1},
        "providers": {"timeout_seconds": 30},
        "logging": {"level": "INFO", "components": {}},
        "api": {"host": "127.0.0.1", "port": 8765},
        "worker": {"concurrency": 4},
        "storage": {"database_path": "/var/lib/anime-discovery/nextgen.sqlite", "download_root": "", "trial_root": "", "keep_root": ""}
      }
    }

Configuration version, publication revision, SQLite v18 and AnimeSeries v2 are
independent. Effective no-op updates neither increment revision nor rewrite.
Unversioned historical NextGen logging-only JSON is source v0. Explicit 0→1
conversion preserves its logging values and adds the original v1 envelope/defaults.
The 1→2 step renames coordinator.concurrency to worker.concurrency and the
logging.components.coordinator override to logging.components.worker, preserving
values and incrementing the publication revision. A v0→2 chain finishes at
revision 2. A v1→2 conversion increments its existing revision exactly once;
revision overflow is refused. Migration previews/backups still use the service
store; files are never rewritten on read.
This is not a legacy production-config import. Unknown keys and unsupported
versions fail instead of being silently discarded.

CONVERSIONS is an ordered immutable registry of one-version steps. Future
renames/type conversions require a new explicit step and regression cases;
do not guess renames or default an incompatible value. The v2 rename rejects mixed historical/current names instead of choosing
one or dropping a value. Current writes accept only worker, not coordinator.
Store reads never migrate implicitly; old versioned files require an explicit
migration. load_settings() reads current versioned files or current logging-only
objects; historical coordinator overrides require the explicit converter.

## Service library methods

| Method | Behavior |
| --- | --- |
| initialize(settings=None) | Explicit first-run publication; never replace existing files |
| read() | Validate current version; no cache or implicit file creation |
| operation(startup, component=...) | Fresh operational values, pinned restart values |
| preview(changes, expected_revision=...) | Validate/classify without writing |
| update(changes, expected_revision=...) | Lock, re-read, compare revision, atomically publish |
| preview_migration() | Return fingerprint/version plan without writing lock/backup files |
| migrate(expected_fingerprint=...) | Verify reviewed source, back up exact bytes, explicitly convert |

Changes are nested section/field dictionaries. Cooperative publishers cannot
silently overwrite each other's revision. Migration applies the exact previewed
source using its SHA-256 fingerprint. Manual edits behind the running service
cannot participate in that revision protocol.

UpdateResult includes changed_fields, restart_fields and restart_components
as classifications of the edit. These are not proof that runtime needs restart:
use operation(...).pending_restart_fields against the relevant startup snapshot
to compute actual pending state. saved and durability_confirmed describe
publication, never automatic application to running resources.

## API notation: separate process and service-owned files

ADM/dashboard call the future authenticated API, which calls this shared
library under the service account. Clients must not edit files or invoke write
methods locally as an alternative management path. The worker contains
neither these management endpoints nor settings-writing responsibilities.

The API must authenticate/authorize, obtain one operation snapshot per request,
require the editor's expected revision for changes, publish via update(), and
report the saved revision plus actual pending restart status. Resolve errors
through the shared reason/message catalog. Migration uses separate preview and
apply operations tied to the reviewed source fingerprint. No-op apply reports
saved=false.

Installation must provide an existing writable configuration directory and
correctly owned files. The library does not become root, loosen access or
repair installed ownership. Secrets are not supported by this settings schema;
credential storage and bootstrap authentication remain future work.

## Publication and failure contract

One stable .settings.lock uses flock for cooperative publishers; never lock the
settings.json inode being replaced. Async consumers must offload these blocking
filesystem methods to a worker thread. Each read opens the current file.
Same-directory atomic replacement publishes a complete old or new document.

The final directory, settings and lock symlinks and nonregular files are
rejected. FIFO checks cannot block waiting for input. JSON size, duplicate keys,
nonfinite values and unknown settings are validated. Replacement preserves
existing owner/group/mode; first-run files use mode 0600. The directory must not
be writable by untrusted actors: cooperative flock is not authorization.

Write/fsync/rename failure before replacement leaves the old file published.
Migration backups are exclusive exact copies, fsynced before replacement.
Normal updates do not create a backup for each edit. No automatic backup
deletion/retention is introduced.

If rename succeeds but directory fsync fails, return saved=true,
durability_confirmed=false and CONFIGURATION_DURABILITY_UNCONFIRMED.
The new revision is already visible; read/reconcile it instead of claiming
rollback or blindly retrying the previous revision. JSON and SQLite are not
made one transaction by this library.

Runtime markers include component=api and bounded config_revision, never
configuration values or raw exception text. Logging failure cannot undo
publication. These logs are not a new durable management-audit database.

## Validation and remaining scope

Offline cases cover conversion/defaults/types/version/revision bounds,
immutability, startup pinning, concurrent revision conflict, atomic failures,
directory-fsync uncertainty, metadata, symlink/FIFO rejection and migration
preview/backup/source conflict. Tests and CI remain unexecuted. Source/AST
checks are not runtime verification.

The worker rename independently requires SQLite v7 history CHECK constraints;
configuration publication still adds no database fields or configuration rows.
The SQLite runner, maintenance/readiness protocol, authenticated API transport,
credential store and job-handler/worker implementation remain separate.


Handler invocation history subsequently extends SQLite to v8, independently of
configuration schema v2. The existing jobs table remains unchanged.


## Media destinations (configuration v3)

The shared registry borrows the legacy anime_settings_schema.py pattern of
central defaults, readable labels/help, validation and presentation metadata.
No legacy daemon import or wholesale legacy setting/secret schema is used.
`storage.trial_root` is the trial video folder; `storage.keep_root` is the
permanent destination for approved Keep videos. Both default to an empty,
unconfigured value. A publishing/import handler must refuse to act until its
required destination is configured and filesystem access is validated.

Values must be canonical absolute non-root paths when configured, without
traversal, control characters or trailing separators. The two roots cannot be
equal/nested, and neither can contain the NextGen database path. Pure validation
does not resolve symlinks or inspect/make directories; the responsible library
must recheck real containment, permissions and external service visibility at use.
When Sonarr handles Keep, its actual registered root must agree with keep_root;
there is no competing silently selected permanent destination.

Both apply to new operations without restart. Recorded artifact/root bindings
and already-approved/submitted Keep plans are retained on retry; a saved setting
never silently moves or retargets existing files. Version 2→3 explicitly adds
blank destination fields and advances the publication revision. Earlier 0→1→2
conversions keep their historical key/default shapes, then convert to v3. Unknown
new fields in an older version are rejected. SQLite remains v8; no SQL change.


Operation snapshots revalidate cross-field constraints after pinning restart-only
values. A saved future database location cannot make a live media root safe if
that root contains the database still in use. In that case new operations fail
with CONFIGURATION_INVALID before work is claimed. Choose compatible roots or
restart the affected service against the saved database configuration after its
separate storage/migration readiness checks. Saving settings does not move data,
create a database or run migrations.


## Required identity providers (configuration v4)

AniDB and TVDB are always part of identity verification. There are no provider
enable flags or TVDB PIN settings. v3→v4 explicitly adds blank anidb.client_name,
anidb.client_version=1 and providers.timeout_seconds=30, advancing revision once.
Earlier conversion steps retain their published shapes. Blank client name is
allowed while preparing settings; identity-worker readiness refuses it.

The three new fields apply to new operations without restart. The concrete
identity-worker composition validates them and the protected TVDB API key at
startup and before claims. Each attempt captures fresh settings/credentials;
existing work retains its snapshot. API host/port, worker concurrency and database
location remain restart-required. Startup validation makes no provider requests.
See SECRETS.md for private credential publication, status and service entry points.


## Trial windows (configuration v5)

trial.backfill_count defaults to 6; trial.following_count defaults to 1 and
includes the triggering episode. Both accept 0–1000, with zero disabling that
window provided the other count is positive. Both zero raises
USER_EVALUATION_COUNTS_INVALID during validation/conversion and is never saved. Their union is deduplicated; there is no combined maximum. Labels/help
make the inclusive following count explicit for the future dashboard.

v4→v5 explicitly adds these fields and advances revision. Published older steps
keep their historical outputs. Fields use next_operation metadata: each new
plan uses fresh settings, while an established plan retains its original anchor,
counts and revision. See USER_EVALUATION_POLICY.md for examples and immutable-plan behavior.

The exact-episode reservation foundation uses SQLite v12; these settings remain
configuration v5. No new setting or secret is needed for the local episode gate.

Source inventory preparation uses SQLite v13 and the existing configuration v5
snapshot. It introduces no client endpoint, credential or download-size setting;
those remain explicit prerequisites before external submission is implemented.

## Download settings and budgets (v6)

The current configuration version is 6. Explicit v5→v6 adds qbittorrent endpoint,
category, timeout, storage.download_root and acquisition.max_episode_mb (600) /
max_plan_mb (3000). They are next-operation fields; existing size budgets retain
their captured limits. Download/trial/keep roots must be separate and exclude
the database. See DOWNLOAD_CONFIGURATION.md. Current SQLite version is 15.

Stopped torrent submission now uses SQLite v15 journal state. Configuration stays
v6; persisted external requests pin their original target/options across live
setting changes. See jobs/SUBMIT_TORRENT.md.

## API v1 implementation

A separate ASGI API now reads one operation snapshot for settings/management work
and writes through ConfigurationStore with expected revisions. GET settings
returns desired/effective values, restart fields and shared field metadata.
Host/port/database path remain pinned until restart. Public browser origin is an
explicit server startup option, not a live settings override. See
[api/README.md](api/README.md). No implicit settings/SQL migration or service restart
is exposed; those management routes remain future work.

## Torrent metadata source — configuration v7

Version 7 adds torrent_metadata.base_url (blank until configured) and
torrent_metadata.timeout_seconds (30, bounded 1–60), both applied to new operation
snapshots. The address is a trusted base folder serving lowercase-infohash.torrent
files, not a delivery-supplied URL/template or secret-bearing query.
The explicit v6→v7 conversion preserves existing settings and historical output
shapes. Use the existing preview/backup/apply contract; no automatic startup
conversion. The dashboard renders these new fields from metadata.
See [the retrieval contract](acquisition/METAINFO_SOURCE.md).

## Sonarr configuration v8

sonarr.base_url, timeout_seconds, quality_profile_id, staging_root and library_root
are applied to new operations. Defaults are unconfigured addresses/mappings,
30-second timeout and profile 0 (unconfigured for adding a series). The existing
storage.keep_root is the local permanent folder. Explicit v7→v8 conversion adds
these defaults without changing earlier values or historical converter output.
See [Keep through Sonarr](KEEP_SONARR.md) for mapping and saved-import behavior.

## Current Shoko and service setup contract

The latest draft adds optional Shoko AniDB metadata after Keep (configuration v10,
backward-readable secrets v6; SQLite stays v23). It also provides explicit local
setup/maintenance commands and composed worker/systemd examples. See
[Shoko metadata](SHOKO_METADATA.md) and [Service setup](SERVICE_SETUP.md).
These supersede earlier remaining-Shoko/supervision/version notes above.
Tests, migrations and services have not been executed.

## Version 15: "trial" settings renamed

Version 15 renames the settings for the first episodes picked for you to evaluate:

| Version 14 and earlier | Version 15 |
|---|---|
| `trial.backfill_count` | `user_evaluation.backfill_count` |
| `trial.following_count` | `user_evaluation.following_count` |
| `storage.trial_root` | `storage.user_evaluation_root` |

The upgrade moves each value to its new name unchanged and advances the revision once. Each version accepts only its own names: a version 14 file can't use the new names, and current settings refuse the old ones. The upgrade code for older versions keeps reading the old names. The dashboard shows the section as "Evaluation episodes".

