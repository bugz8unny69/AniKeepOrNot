# Download configuration and selection-plan size budgets

These are shared local settings, private credential and atomic size-admission
libraries. They do not implement qBittorrent login/HTTP, metadata retrieval,
submission, file selection, deletion or reconciliation. PREPARED plans still
confer no external ownership or permission to transfer data.

## Public settings (configuration v6)

| Setting | Default | Meaning |
| --- | --- | --- |
| qbittorrent.base_url | Empty | HTTP/HTTPS origin, optional port, no credentials, query, fragment or subpath |
| qbittorrent.category | anime-discovery | 1–64 letters/digits/underscore/hyphen; label only, never ownership proof |
| qbittorrent.timeout_seconds | 30 | Integer 1–60 request timeout for future client calls |
| storage.download_root | Empty | Absolute working folder, separate from trial/keep/database paths |
| acquisition.max_episode_mb | 600 | Positive decimal MB cap for each prepared selected video |
| acquisition.max_plan_mb | 3000 | Positive decimal MB cap for combined prepared videos in one selection plan |

Size settings accept integers 1–1000000. One MB is 1000000 bytes. The limits are
independent of backfill/following counts: 6+1 can select seven positions, while
all their prepared video sizes together must fit the captured total. Overlap
counts once because the exact-episode reservation and preparation bindings are
unique. There is no legacy exception that lets six known videos bypass the
per-video limit. A smaller total than per-video cap is valid and more restrictive.

The legacy defaults were 600 MB per episode and 3000 MB for six episodes. The
new total applies to the actual season-scoped selection plan, regardless of its
configured count; it is not silently multiplied or disabled for larger windows.
The dashboard's eventual labels/help expose both limits explicitly.

All fields use next_operation semantics. DownloadOptions and DownloadCredentials
capture immutable values at point of use; future work reads new values, running
work retains its captured context. Persisted external request destination/settings
must eventually remain pinned until reconciled rather than following a changed
root or endpoint. This PR implements no such external request yet.

Configuration v5→v6 explicitly adds defaults and advances revision. Historical
v0→v5 outputs remain version-shaped; new sections/fields are rejected in older
versions. Both selection counts zero remains invalid during validation/conversion.
Settings never accept client passwords. Blank endpoint/root permit setup but
download_options()/download_context() raise DOWNLOAD_CONFIGURATION_INVALID until
configured. Point-of-use validation does no login, directory creation, access
probe, ownership repair or HTTP. Provider readiness is unchanged: identity work
does not require a download client merely to identify a release.

Public URL shape validation does not establish authenticated server identity,
reachability, TLS validity or endpoint permissions. Future transport must verify
TLS, contain credentials and disable redirects rather than weakening these checks.
No certificate-disable setting, automatic environment/legacy fallback or client
enable flag is introduced.

## Central private credentials (secrets v2)

SecretStore keeps tvdb_api_key and the new qbittorrent_username/password pair in
the same private secrets.json. A pair is either both nonempty or both empty
(explicit clear); partial pairs are rejected. Username/password are bounded and
control-free. Credential values are hidden from repr. Public status/mutation
results contain revision, tvdb_api_key_configured and qbittorrent_configured plus
saved/durability fields, never values. Trusted download libraries call
qbittorrent_credentials(), not a public management response serializer.

New initialize() writes secrets schema v2. Existing strict v1 TVDB-only files
remain readable without rewriting. A TVDB-only update preserves v1. An explicit
update_qbittorrent_credentials() that changes the pair upgrades v1→v2 while
preserving the key. Clearing an already empty pair is a no-op. A v2 TVDB update
preserves download credentials, and a download update preserves TVDB. Revisions
remain independent of settings revisions and conflict checks protect each write.

The existing service-owned regular single-link private-file checks, shared stable
lock, mode-0600 temp publication, fsync/atomic replace, post-publication durability
reporting and no plaintext backup policy continue to apply. Unknown secret fields
and versions remain invalid. Nothing repairs filesystem ownership or permissions.
No qBittorrent SID/session cookie is persisted. There is no authenticated API/ADM
route for these new operations yet; future service-side handlers call the library.

## Atomic budget admission (SQLite v14)

The PREPARE_ACQUISITION completion adapter checks the exact selected video's
metadata size, not an advertised release total. It creates acquisition_budgets
on the first accepted preparation for the frozen user_evaluation_plan_id, capturing the
limits and configuration revision. Each acquisition_plans row records its actual
selected_size_bytes. Subsequent preparations use the stored limits, not changed
settings. New budgets capture current settings. Existing budgets do not silently
replan; future explicit operator controls must reconcile them first.

The adapter sums existing prepared selected bytes for that plan and checks both
caps in the same short lease-fenced SQLite write transaction as insertion. Two
concurrent preparations cannot each spend the same remaining space. Same-job
idempotent completion excludes its already recorded bytes rather than charging
twice. A refused admission records the readable ACQUISITION_SIZE_LIMIT review
reason and rolls back any tentative budget/plan/marker. A later completion failure
also rolls back budget creation. Durable history uses existing completion receipts.

This is an admission ledger, not live disk usage. Prepared/held/canceled work
continues to occupy its recorded budget; there is no automatic release/transfer.
It is not a global free-space quota, network-transfer limit or sidecar/piece-overlap
budget. Future client selection must decide what auxiliary bytes/pieces can
transfer before starting work. Unreceived numeric backfill/future positions do
not consume invented sizes or bypass limits when they later arrive.

014_acquisition_budgets.sql preserves existing v13 plans and adds a nullable
selected_size_bytes column. Old rows remain NULL rather than inferred from
arbitrary stored JSON. A plan with unknown prior size blocks further admission
with ACQUISITION_SIZE_EVIDENCE_MISSING until explicit source reinspection/recovery;
no automatic rewrite/release/rebudget API is claimed. Schema v14 fresh DDL,
helpers, documentation and offline cases accompany the migration. SQL 001–013
is unchanged. AnimeSeries remains v2; configuration v6 and secrets v2 are separate
version domains.

Offline cases cover v6 conversion/historical shapes, endpoint/path/limit validation,
private pair updates/v1 compatibility/revisions/failed publication, captured
contexts, concurrent aggregate admission, frozen limits, oversized/unknown evidence,
rollback and v13→v14 preservation/constraints/fresh equivalence/rollback. Tests
and CI remain unexecuted under project policy.

Next work is the qBittorrent transport and durable submission/reconciliation
state machine. It must use these settings/credentials and stable captured intent,
not assume that a prepared inventory, successful response or category proves
ownership. See jobs/PREPARE_ACQUISITION.md for the boundary.

## Stopped client transport follow-up

The qBittorrent transport and SQLite v15 submission journal now use these captured
settings and credentials. Persisted endpoint/destination/category/timeout are
pinned until reconciliation; fresh attempts may read rotated private credentials.
A one-shot intent can add stopped only; no selection/start/delete controls are
implemented. See jobs/SUBMIT_TORRENT.md. Existing v6 settings remain unchanged.
