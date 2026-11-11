# Keep through Sonarr

Series-first Intake submits the existing background Sonarr v4 Keep jobs for
all staged acquisitions explicitly selected with one Keep action. Individual
per-file and batch journals remain independently retryable. New arrivals are
not silently included. Each underlying service call retains its exact scope. It reuses the existing staged path and both
provider identities. Sonarr matching uses the verified TVDB series, season,
episode and available episode ID. AniDB IDs remain available for optional Shoko;
there is no Shoko dependency or second staging/NFO implementation.

Configure the Sonarr address and private API key in Settings, plus the local
permanent Keep folder and the Sonarr quality profile ID Keep adds new series with.
Optional staging/library roots describe the same folders as Sonarr sees them in
a container or another service. Blank uses the recorded staging root/local Keep
root. Both processes must actually have the corresponding storage mounted.

Keep means Sonarr owns the series from now on. Keep does three things, in order:

1. **Add.** If Sonarr has no series with the verified TVDB ID, Keep adds it. It
   looks the series up (`series/lookup?term=tvdb:<id>`, exactly one result with
   that TVDB ID), checks that the permanent folder is one of Sonarr's root
   folders and that the quality profile ID (`sonarr.quality_profile_id`) exists,
   then POSTs the lookup record with that profile and root folder, series type
   anime, season folders, monitored, and `searchForMissingEpisodes: false`.
   ADD_UNCERTAIN is recorded before the POST. A lost reply is reconciled by TVDB
   ID on the next attempt, never by a second POST; if the series is still missing
   the job stops for review (`SERIES_ADD_UNCONFIRMED`). A refused POST returns
   the journal to PLANNED. Only one Keep job adds a given series at a time; the
   others wait and then find it. A series that already exists keeps its
   monitoring as it is.
2. **Import** the staged video, as below.
3. **Search.** When the last Keep job of the anime has imported everything,
   Sonarr is asked once (`SeriesSearch`) for the monitored episodes that are
   still missing. The search waits until then because the staged episodes would
   otherwise be downloaded again. The time is recorded first on the series'
   ACCEPT decision (`anime_review_decisions.sonarr_search_sent_at_ms`), so it
   is never sent twice. A failed search does not fail the Keep; it is recorded in
   History as `KEEP_SEARCH_FAILED`. After that, AniKeepOrNot does nothing more
   for the series.

**Long-running series.** AniDB splits normal shows into one entry per season;
only true long runners (One Piece, Detective Conan) have hundreds of regular
episodes in one entry. When Keep adds a series whose highest regular AniDB
episode is at least `sonarr.long_running_threshold` (default 100; 0 turns the
rule off), it is added with `monitor: "future"`, so Sonarr only gets new
episodes. The number comes from the cached AniDB record, or else from the
episodes AniKeepOrNot verified for it; no request is made. The Settings
option "Let Sonarr backfill long-running series" (`sonarr.backfill_long_running`,
off) adds them with `monitor: "all"` like any other series. Not recommended:
hundreds of episodes can use a lot of disk space. Kept episodes are imported by
explicit episode ID either way; Sonarr's ManualImport does not require the
episode to be monitored.

**Absolute numbering.** Anime-Lists maps long runners with
`defaulttvdbseason="a"`: AniDB episode N is the TVDB episode whose absolute
number is N + `episodeoffset`. The absolute numbers come from the same TVDB
official episode pages used for placement. Explicit mapping-list rules come
first. No match, or two TVDB episodes with that absolute number, leaves the
episode unmapped (`MAPPING_MISSING`), as before; nothing is guessed.

Keep refuses (the button and the job) when Sonarr is configured without a
quality profile ID. A series outside the configured permanent root needs review.

The job scans the staged parent folder, selects only the exact approved file,
and reprocesses that candidate with explicit Sonarr series/episode IDs. It uses
Sonarr's returned quality/languages and requires no import rejection. The actual
ManualImport command contains only that file and uses copy mode. Existing episode
files are not overwritten automatically. Series packs and other folder files
cannot silently join this import.

Keep records series-add/import intent before each external write. Lost replies
are reconciled by TVDB identity or a matching saved command; they never authorize
an automatic repeat POST. A command that failed, was purged, or cannot be matched
may require operator attention; explicitly resubmitting Keep resumes the same job
and journal. It does not erase an uncertain send or grant a second send right.
Before any external intent, a PLANNED job can take corrected settings on explicit
retry. After external authorization, client/root/options stay bound to the saved
plan. Credentials are read afresh. A per-job lock drains stale attempts before
recovered work proceeds, and queue leases fence mutations and completion.

The job waits without holding a worker slot while Sonarr works. Kept is recorded
after the command completes, Sonarr binds the expected episode to its file, and
that regular readable file exists under the configured local permanent folder
with the expected size. There is no destination checksum or byte-for-byte
comparison. A returned command ID alone is not a successful import.

After Keep completes, a separate cleanup job removes only the selected staged
video and episode NFO after rechecking the permanent result. Shared series
metadata and the kept episode reservation remain. Torrent/download cleanup,
cancellation reservation release and optional Shoko remain separate work. Configured Jellyfin refresh is now an
independent follow-up; see JELLYFIN_REFRESH.md. This job does not
delete staged videos, torrent data or permanent media. Each worker imports the
exact selected episode; the series-level action requests all currently staged
scopes in one user action.

The dashboard enables Keep for eligible staged work and uses existing job
receipts, polling and disabled-button explanations. It displays the catalog
message "This episode is kept." only when a durable Keep result exists.
Held/review work can explicitly resume the same job after the cause is addressed.
Successful imports are not submitted again. Jobs stopped before Keep existed
(KEEP_NOT_READY) were migrated to KEEP_IMPORT_REVIEW by migration 064; they
resume only through an explicit Keep request.

Configuration v8 explicitly adds Sonarr settings; existing v0–v7 shapes remain
versioned. Secrets v4 adds the API key while v1–v3 files remain readable and retain
their format until a Sonarr credential write. API reads expose presence only.
The existing management session/Bearer, CSRF and separate webhook authentication
contracts remain unchanged. SQLite v22 adds keep_imports alongside immutable
001–021 migrations. Startup performs no implicit configuration/SQL upgrade.

Offline tests cover staged import, new/existing series, lost add/import replies,
duplicate/resumed actions, changed files, wrong destinations, pinned settings,
credential isolation and migration preservation/rollback. They remain unrun,
as does CI. Source/syntax/DDL checks do not verify Sonarr/browser runtime behavior.
No live service, import, media deletion, production migration or deployment ran.

API payloads were checked against Sonarr's official source: ManualImportController,
ManualImportResource, ManualImportFile and ManualImportCommand under
https://github.com/Sonarr/Sonarr/tree/develop/src . Sonarr's API v3 routes are
used for Sonarr v4; unreviewed server major versions stop for review.
