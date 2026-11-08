# Verify a declared release episode

VERIFY_RELEASE / verify_release_job.py is the next installed job after
IDENTIFY_ANIME in the concrete worker composition. Its static readable handler
label is "Verify release episode". The queue payload contains only anidb_id;
jobs.event_id supplies the incoming delivery. The handler loads that event's
immutable AnimeSeries result, requires IDENTIFIED state and binds the source
bytes plus identity result ID to every returned fact. Supplied episode IDs,
provider URLs or executable names are never accepted from the queue payload.

## Current supported verification

The initial normalized input remains JSON release_name. The parser recognizes
one supported declared episode: a regular AniDB-number hint such as " - 12" or
"EP12", or explicit TVDB season/episode such as "S02E13". These are declarations
to verify against providers, not facts taken on faith or proof of media contents.
Batch/ranges, multiple identifiers, fractional episodes and missing or
ambiguous declarations go to review. A word such as “Special” inside the
anime's actual title is not by itself a special-release declaration. An
explicit S00Eyy release may verify when an AniDB **regular** episode has an
explicit Anime-Lists mapping to that TVDB Season 0 episode and TVDB independently
confirms the episode record. Separate AniDB special-numbering types are not
interchangeable with regular episode numbers, and have no automated support.
The phrase `S00E02 Special Part 1` may describe a complete TVDB S00
episode, so a "Part" label alone does not prove a split torrent. The declared
S00 episode still requires exact AniDB → Anime-Lists → TVDB corroboration.
Without that explicit mapping it receives `RELEASE_SPECIAL_UNSUPPORTED`;
the verifier does not invent episode IDs or offsets. There is no trial-window
or season-one limit.

The verifier checks the full-anime AniDB regular-episode catalog (exact episode
number→AniDB episode ID), then supported explicit/default Anime-Lists mapping.
Missing offset is never inferred to be zero. Explicit pairs outrank ranges;
conflicting/split/absolute/unsupported rules fail closed. TVDB-style declarations
are reversed over actual AniDB catalog entries and must resolve uniquely. Merged
mappings cannot make a single declared episode look uniquely verified.

The mapped episode must exist uniquely in TVDB's explicit official season order.
Its seriesId, episode record ID, season and number are validated. Pagination is
bounded and completes before acceptance; wrong-series/duplicate records fail.
The same shared provider clients, authentication scopes, caches and SQL pacing
are reused across identity and release stages. Provider waits release worker
capacity through normal deferral; failures use the existing readable provider
reason paths.

## Persistence and authorization boundary

The worker's fenced transaction rechecks the identity result, canonical series,
source bytes/hash, event correlation and episode ownership. It inserts canonical
episode ownership and one release_verifications row tied to the job. Completion,
handler ending and facts commit atomically. An expected conflict rolls back the
whole tentative completion and records review. A stale lease cannot commit.
Original AnimeSeries JSON and optional episode coverage remain untouched.

Every row is constrained to verification_scope=DECLARED_SINGLE_EPISODE and
TVDB order official. It records a declaration's provider-verified identity, not
an inspected file list, downloaded media proof, trial eligibility, user approval
or acquisition authorization. The composed worker advances to SELECT_USER_EVALUATION; there is no acquisition
successor. Its DONE
means only this explicitly scoped step completed.

The next work is trial eligibility policy and exact-resource claim design,
followed by acquisition with trusted torrent/file inventory and ownership intent.
Batch/pack coverage requires exact inventory evidence and a broader versioned
manifest design. No existing title or pack label supplies that proof. Trial
window count/default, parts/ongoing/special policy and acquisition limits remain
architecture decisions to settle before download behavior is implemented.

## Storage and review status

Schema v9→v10 adds release_verifications with source hash, constrained scope,
provider episode IDs/numbers/order, timestamp and job/identity/episode ownership
references. The fresh schema, helpers, runner/version guards and offline cases
are updated together; numbered scripts 001–009 remain unchanged. Configuration
stays v4 and AnimeSeries v2. This draft depends on provider-config PR #110.

Legacy references reviewed: release_identity.py, anime_episode_identity.py,
anime_identity_mapping_v4.py, anime_identity_episode_v6.py and
anime_episode_verification_v16.py. Reuse conservative mapping/conflict behavior;
do not import the legacy six-episode/season-one policy, score-based approval or
runtime worker/controller code. The TVDB endpoint/episode fields were checked
against the provider's official v4-api Swagger source.

Offline tests are authored but unexecuted under project policy. Source review,
AST syntax parsing and diff/schema-source checks are the only validation so far.
No application imports/execution, SQL, CI/tests, live provider requests,
acquisition, filesystem media work or deployment occurred.
