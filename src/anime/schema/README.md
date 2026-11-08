# AnimeSeries v2 — RFC-style series identity schema

**Status:** V2 proposed on an independent draft PR targeting `NextGen`; offline-only. **Version:** 2. This schema is independent from the production application and from the NextGen scheduler. V1 remains in Git history and permanent regression fixtures.

## Scope

`AnimeSeries` describes **the identified anime series and its episodes**, not how the application processed a webhook. The schema contains **no** notification ID, trace ID, release name, source GUID, retry state, acquisition, staging, Keep/Reject, export status, or job ID. These live in separate operational storage, connected to an identified series only after identity resolution. Multiple deliveries may link to one identified series; the identity component/cache and post-identification episode claimant own reuse and duplicate decisions.

AnimeSeries v2 represents a **verified anime-series identity**, optionally including zero or more individually verified episodes. A webhook is not expected to produce one directly. The identification component may accumulate partial evidence in its own work state and MUST call the schema parser/validator before marking **SERIES IDENTIFICATION COMPLETE**. An empty episode list means no individual episode identity is claimed, **not** that the series has no episodes, that a pack is empty, or that every episode of the release is identified.

The schema defines identity information, **not the release's actual contents**. Season-pack labeling may be treated as a provisional clue by the job worker. The AniDB episode catalog tells us which episodes may exist for the relevant AniDB anime entry; it does not prove which files the release contains. Those distinctions belong in the SQLite job and release-handling design, not as fields on AnimeSeries.

## RFC-IDENTITY-001 — identity completion gate

- Identity MUST populate `anime.anidb_id` with a positive AniDB anime ID.
- Identity MUST populate `anime.tvdb_series_id` with a verified positive TVDB **series** ID for Sonarr. A series is not pipeline-ready without this required series link.
- Identity MUST populate `anime.title` with the identified anime title, **not the webhook release name**.
- `episodes` MAY be omitted or empty and then normalizes to an empty array `[]`. When any `episodes[]` entries exist, **every entry MUST have an exact, independently verified positive AniDB episode ID**, for precise AniDB/Shoko episode identity. No placeholder or guessed episode IDs.
- Identity MUST NOT publish a **completed series identity** if the series fails the V2 requirements. The job MUST record explicit incomplete/invalid-identity outcomes and traceable history, and MAY retry or await review. A **series-only** valid identity with zero episodes MUST NOT be treated as **completed episode/release identification** or authorize episode-specific acquisition completion, Shoko linking, or Sonarr episode import. Those later stages require exact verified episode identities and any additional integration-specific data.
- Field types MUST be checked; unknown schema fields and incompatible versions MUST be rejected, not silently reinterpreted.
- Empty optional information MUST be safe: missing optional scalars normalize to `null`, missing `episodes` to `[]`, and missing `attributes` to `{}`. Missing `anime.tvdb_series_id` is an identity validation failure, not an optional field. No component MAY silently invent a season number, episode number, AniDB ID or TVDB ID.
- The schema describes verified **AniDB anime identity and only the episodes actually identified**. Numeric validation alone cannot independently verify a claimed AniDB ID: the identity component MUST establish AniDB identity using provider evidence before building the schema. Original release names are accepted as received and used as identification input; there is no provider voting or generic truth/trust scoring mechanism.
- An AniDB anime ID may represent one sequel/cour entry while the same TVDB series groups several AniDB anime entries. Never assume a TVDB series ID uniquely determines AniDB anime ID, TVDB season, or episode placement.

## Fields

| Path | Requirement | Meaning |
| --- | --- | --- |
| `schema_version` | MUST equal 2 | Schema version |
| `anime.anidb_id` | MUST | Exact AniDB anime identity |
| `anime.tvdb_series_id` | MUST | Verified TVDB series-level link for Sonarr |
| `anime.title` | MUST | Real identified anime title |
| `anime.english_title`, `anime.original_title`, `anime.type`, `anime.year` | MAY be null | Additional anime series descriptors |
| `episodes` | MAY be absent or empty | List of *individually verified* episodes; omitted normalizes to `[]` |
| `episodes[].anidb_episode_id` | MUST for every populated episode | Exact verified AniDB episode identity for Shoko |
| `episodes[].anidb_episode_number` | MAY be null | Descriptive AniDB numbering; an integer or e.g. `"S1"`, never used to infer season |
| `episodes[].title`, `episodes[].type` | MAY be null | Episode descriptors |
| `episodes[].tvdb_placement` | MAY be null | Optional `{season, episode}` pair when actually identified |
| `attributes` | MAY be `{}` | JSON-compatible, documented additional series metadata; MUST NOT be used for processing state or override canonical fields |

When `tvdb_placement` is present, its `season` MUST be a nonnegative integer (0 is allowed for specials), and `episode` MUST be a positive integer. It is **not** required for identity completion. **Downstream operations** such as particular Jellyfin NFO formats or exact Sonarr episode imports MAY require more evidence and MUST stop safely when they lack that evidence. That is a downstream integration prerequisite, **not** a new mandatory episode-level TVDB requirement in this RFC.

Season is **not** a new top-level field in V2. A known per-episode TVDB season exists only inside optional `tvdb_placement`. Season hints from pack names belong to operational job data. This avoids requiring TVDB season numbering for AniDB anime entries that use continuous or sequel numbering.

Every serialized episode has the same field names after parsing/normalization; omitted optional scalars are represented as `null`. Field-level machine-readable documentation includes meaning, reason, owner, availability and origin. V2 intentionally rejects V1 inputs unless explicitly upgraded by a separate, authorized conversion process; no legacy migration is included.

## Examples and offline tests

- `src/anime/tests/fixtures/anime_series_v2_verified.json`: identified series and episode with optional exact TVDB placement.
- `src/anime/tests/fixtures/anime_series_v2_continuous.json`: continuous AniDB episode numbering without TVDB placement.
- `src/anime/tests/fixtures/anime_series_v2_series_only.json`: series identity with required TVDB series link and no identified episodes (such as a season-pack whose contents are not yet known).
- `src/anime/tests/fixtures/anime_series_v1_*.json`: preserved V1 regression examples, now rejected by V2 validation.
- `src/anime/tests/test_anime_series_schema.py`: checks mandatory AniDB and TVDB series IDs, empty/missing episodes, mandatory exact IDs on any populated episode, and unconventional numbering.

## Next PR — reliability infrastructure, *not* identity provider implementation

Treat the user’s TCP analogy as **reliable delivery and recovery properties at the application boundary**, not a claim that TCP alone guarantees processing.

1. Build **fresh NextGen** authenticated, bounded webhook intake and SQLite-backed durable inbox in `src/anime/`, with no dependency on the legacy database or runtime.
2. Every accepted webhook delivery MUST be recorded individually; no GUID/name/content-based deduplication at intake. HTTP success MUST occur **only after** durable acceptance commits. Transient write/backpressure failure MUST return an explicit retryable HTTP error, not false success.
3. Use one atomic durable handoff to record an accepted delivery and its **pending identity work** (e.g. inbox status), avoiding an acknowledged-but-unprocessable gap. Whether a separate job row is created immediately is an implementation detail, not a series schema field.
4. Reclaim orphaned work after restart, bounded workers, leases and retry/backoff on transient failures; log persistent events and clear failure reasons, provide terminal/manual-review outcomes for poison requests rather than infinite hot loops.
5. Test burst submission (including 10,000 distinct or identical release names), concurrent writers, failed writes, process crashes, restart recovery, acknowledgments, retries, and end-to-end visibility. Keep only one acquisition claim after exact verified identity—this is **later**, separate from intake.
6. Scope is infrastructural: no live deployment, no actual provider calls, no acquisition, no Sonarr/Jellyfin/Shoko writes and no merger into the legacy runtime.

**Reliability goal:** no *acknowledged* event silently lost and no single bad identity record taking the pipeline down. Perfect availability, exactly-once processing and immunity to physical data loss cannot be guaranteed; make failures observable and recoverable instead.

