# Backfill and continue: intended behavior

Automatic backfill and continue search Prowlarr directly. Sonarr takes no part
in finding or downloading these episodes. The separate, user-triggered **Keep**
import is unchanged and still uses Sonarr.

## Episode selection

AniDB decides which episodes are selected. Episode numbers here are AniDB
episode numbers of the verified anime; TVDB seasons never reset the window.

The searched window is the union of:

- backfill (`trial.backfill_count` = N): episodes 1 through N while the incoming
  episode is within the first N, otherwise the N episodes just before it;
- continue: the incoming episode and the following ones (`trial.following_count`
  includes the incoming episode).

Overlaps count once. The incoming episode itself is never searched: its own
delivery is already acquiring it. Examples with N=6 and following_count=3:
incoming 5 searches 1-4, 6, 7; incoming 1000 searches 994-999, 1001, 1002 even
when AniDB 1000 is TVDB S02E01.

Each selected AniDB episode is searched, matched and acquired at its own
verified TVDB placement, so one page can span TVDB seasons. An episode without
a unique verified placement is `MAPPING_MISSING` and is not acquired (Step 1:
reservations, staging and Keep still need TVDB).

Episodes with an active reservation (single or batch) are skipped and reported
as `IN_PROGRESS`. Staged and kept episodes keep their reservation, so they are
skipped too. A canceled episode releases its reservation and is searched again.

There is one frozen plan per AniDB anime; it keeps its captured counts and
anchor, and later deliveries in any TVDB season reuse it. The window is searched in
pages of four episodes; each page is one `DISCOVER_SONARR_BACKFILL` job (the
historical job and table names are kept so existing jobs and history stay valid).

For an unstaged failed anchor, exhausted `CANDIDATES`, and manual submissions
that are no longer making progress, use the episode-scoped recovery workflow:
[Episode reservation recovery](EPISODE_RESERVATION_RECOVERY.md). Search again
with **Delete cache** does not delete historical reservation rows. It cancels
owners, waits for owned cleanup to mark reservations RELEASED, archives manual
candidate registrations, then re-searches. Explicit **Retry acquisition**
re-verifies a size-rejected source against current configurable limits.

## How an episode is searched

1. The episode catalog (`providers/backfill_catalog.py`) lists the AniDB
   episodes and their AniDB air dates (`NOT_AIRED` until aired), and maps each
   aired one to its TVDB placement with the same cached AniDB record,
   Anime-Lists offsets and TVDB official episode list that VERIFY_RELEASE uses.
2. Prowlarr is queried with GET `/api/v1/search` with a bounded, deterministic
   query budget of at most six queries per episode:
   - When `acquisition.audio_check=japanese_english` and a verified English
     title is known, Western scene naming takes precedence: `English S01E01`,
     followed by `English S01E01 Dual-Audio`, `English 01`, AniDB main title
     queries (`Title 01`, `Title S01E01`), and short synonym broad fallbacks
     (`Saikyosoubi 01`);
   - In standard discovery (when audio check is not required), traditional
     AniDB episode formatting (`Title 01`) and scene formatting (`Title S01E01`)
     come first, followed by verified English aliases and short synonyms;
   - When the mapped episode has a distinct TVDB-informed absolute number,
     reserve one of the six searches for that convention (for example,
     `Base 29` for S02E05). Use a verified season base where available, or the
     yearless main title otherwise. This is only a search hint, not a season
     guess; the existing exact matcher still validates every returned release.
     Season 1 query priority stays unchanged when the numbers coincide.
   - Franchise and sequel base queries (`Base 01`, `Base SxxEyy`).
   Indexer throttling and rate limiting remain Prowlarr's responsibility;
   Anime Discovery does not add an internal rate limiter, honoring HTTP 429
   `Retry-After` headers and transient backoffs directly. A query returning
   an already-failed hash proceeds to subsequent queries. In fallback mode,
   searching stops early once the target candidate hash is located; for initial
   searches, queries execute deterministically to populate the candidate
   fallback set in `sonarr_episode_candidates`.
3. Each result title is parsed the way release groups write it
   (`acquisition/release_titles.py`): `[Group] Title - 05 (1080p)`,
   `Title 2nd Season - 05`, `Title S2 - 05`, `Title II - 05`, `Title - S02E05`,
   scene-style `The.Title.S02E05.1080p`, `Romaji | English - 05`, `v2` suffixes.
   It matches only when the title is one of the anime's AniDB titles (or its
   franchise title with the right season marker) and the number is this
   episode as an AniDB number, TVDB `SxxEyy`, season-relative number or
   absolute number (`- 29` for season 2 episode 5 after a 24-episode season).
   A bare franchise title without a season marker only matches the absolute
   number, so season 1 episode 5 is never taken for season 2 episode 5.
   Batches, ranges, `.5` recaps, OVAs, movies and specials never match.
   Unclear titles are skipped, never sent for review.
4. Resolution is read from the title (480p … 2160p, 4K/UHD). Releases without a
   resolution, above `prowlarr.max_resolution`, below `prowlarr.min_seeders`,
   or whose Prowlarr-reported size is over `acquisition.max_episode_mb` are
   counted but never sent to qBittorrent. The rest are ranked by
   `prowlarr.rank_by`. A release with no reported size is not skipped; the
   size limit is still checked after its file list is read.
   When `acquisition.audio_check=japanese_english`, only titles advertising
   Dual/Multi Audio, MULTi AD, or both Japanese and English audio are eligible
   for download. Sub-only, Multi-Subs, untagged and English-dub-only releases
   are rejected by the dual-audio title gate. Release title tags are search hints
   only; ffprobe verification after download remains the sole authority requiring
   both `jpn` and `eng` audio tracks.
5. The magnet comes from `magnetUrl`, a magnet `guid`, or the `infoHash`
   (bare hash). Conflicting hashes drop the result.
6. The best release goes through the existing pipeline unchanged: an internal
   delivery named plainly (`<AniDB title> - 05`, the group's title kept
   alongside) is verified by VERIFY_RELEASE, reserved, sent to qBittorrent with
   its sanitized magnet, downloaded, verified and staged. Lower-ranked releases
   are kept as fallbacks: if any later step fails (bad metadata, size limit,
   failed verification), the attempt is cleaned up and the next eligible ranked
   release is tried automatically. When every attempted release failed and at
   least one failed audio verification, the exhausted episode waits for a dub
   instead of entering Needs Review.

Webhook intake uses the same reader: `declared_episode()`, the identity
aliases and season hints, and the batch router (`release_scope_hint`) all call
`release_titles.read_release()`, so a title readable for backfill is readable
at intake too. Season packs keep the existing batch path.

## Results are reported per episode

| Status | Meaning |
| --- | --- |
| `CANDIDATES` | Matching releases found; the best usable one was sent on |
| `NO_RESULTS` | Prowlarr returned no matching release when dual-audio gating is disabled |
| `WAITING_FOR_DUB` | English and Japanese audio required, but Prowlarr returned no Dual/Multi Audio titles to try |
| `NOT_AIRED` | AniDB has no past air date, or TVDB does not list the episode yet |
| `MAPPING_MISSING` | TVDB lists the episode but the AniDB/TVDB mapping is missing or ambiguous |
| `IN_PROGRESS` | Already reserved, staged or kept |

One episode without releases never holds back the others on the page.
When the configured audio check requires Japanese and English, a searchable
episode with no Dual/Multi Audio titles (including no results at all) is
persisted as `WAITING_FOR_DUB`. The Intake series view shows **Waiting for dub**
with **Search again**, which reuses the existing one-episode Prowlarr retry.
This search outcome does not create an ON_HOLD job or a torrent. For held
WAITING_FOR_DUB acquisitions, Search again first tries an eligible fully cleaned
cached magnet when Delete cache is unchecked. If no such magnet is available,
it queries Prowlarr for new Dual/Multi Audio releases instead of silently
returning to ON_HOLD. Checking Delete cache skips cache replay, scrubs reusable
magnet URIs without removing the original release history, and queries
Prowlarr directly. Previously tried hashes are never chosen as new Prowlarr
releases. A tagged
release disqualified for an unrelated reason (such as size) is not incorrectly
reported as a missing dub. Existing downloaded-release audio failures still
use the held-job WAITING_FOR_DUB path.

A slow or busy Prowlarr is retried, not reported straight away:

1. Each search waits up to `prowlarr.timeout_seconds` (default 60). A
   timeout, 429 or 5xx gets two more tries, 15 s and then 60 s later; a 429
   `Retry-After` replaces the wait, capped at two minutes.
2. If all three tries fail, nothing from that page is published and the job
   runs again once, 30 minutes later.
3. If that also fails, it stops as BLOCKED with Retry available:
   `PROWLARR_SEARCH_TIMEOUT` or `PROWLARR_UNAVAILABLE`. A manual Retry gets
   the same allowance again.

`PROWLARR_AUTH_FAILED`, `PROWLARR_CONFIGURATION_INVALID` and unreadable
responses are never retried automatically: fix Settings, then Retry.

Not-aired episodes are reported, not polled. Continue still admits later
episodes when they arrive through the webhook.

## Seeing why a release was rejected

Open **History** on any job of a backfill episode. "Releases tried for
episode N" lists every ranked release in order: the group's title, whether it
was staged, failed (with the reason and, for size stops, the actual sizes),
is in progress or was not tried yet, and the trace ID (`AD-<n>`) of that
attempt for the worker log:
`journalctl -u anime-discovery-worker | grep 'trace_id=AD-<n>'`.

## Settings

Settings → Prowlarr: address, API key, search timeout, maximum resolution,
minimum seeders and ranking. Configuration v11 moves the former
`sonarr.discovery_*` preferences there, keeping their values. Secrets v8 adds
the Prowlarr API key; older secret files stay readable.

## Known limits

- Titles only in non-Latin scripts, or groups that put the episode only in
  brackets (`[Title][05]`), are not matched; other groups usually cover them.
- Media that was already in the library before NextGen (no reservation) is not
  detected and may be searched again.
- No live homelab end-to-end run has been performed.
