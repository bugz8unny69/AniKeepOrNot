# Episode selection policy

**Purpose:** determine whether an incoming **verified autobrr release** is eligible for acquisition and staging. Episode selection does **not** initiate a search, contact an indexer, or automatically fetch absent episodes. It is one gate within [the intake flow](architecture/INTAKE_TO_STAGING.md).

## Selection windows

| Setting | Default | Semantics |
| --- | --- | --- |
| Backfill first episodes | 6 | Select the first X positions of the verified official TVDB season. |
| Episodes starting with the received episode | 1 | Select X positions beginning at the triggering episode, including the trigger. |

Both values are integers from 0 to 1000, and at least one must be positive. Overlapping positions count once. Positions cannot extend beyond the verified official season. Counts are **eligibility**, not search targets, and never manufacture AniDB/TVDB episode identities.

| Verified incoming episode | Backfill | Following | Eligible positions in the frozen plan |
| --- | ---: | ---: | --- |
| 10 | 6 | 1 | 1–6 and 10 |
| 10 | 0 | 2 | 10–11 |
| 10 | 0 | 6 | 10–15, bounded by the actual season |
| 3 | 6 | 2 | 1–6 (overlap removed) |
| 10 | 6 | 0 | 1–6; incoming episode 10 is not selected |
| 10 | 0 | 0 | Invalid settings |

**Example:** if autobrr submits episode 10 with defaults 6/1, only episode 10 from *that delivery* can proceed now. Positions 1–6 are allowed should qualifying deliveries later arrive; NextGen does not actively look for them.

## Stable identity and decisions

The first successful evaluation commits one immutable plan per AniDB anime + TVDB series + verified official season/order. It freezes the trigger, window counts, source evidence and configuration revision. Later arrivals cannot move the anchor; setting changes apply to new plans only. Multiple AniDB anime sharing a TVDB series remain separate scopes. Disputed episode/season ownership is held for review, not inferred.

A received episode outside the selected window does not get a reservation or automatic download. A selected episode must still pass release, exact episode ownership, duplicate reservation, metadata, budget and media checks; selection is not proof of a good file or successful staging.

## Durable compatibility

Earlier versions already persisted versioned configuration keys, job-kind identifiers, event/reason markers and database rows using previous internal names. **Keep those stored identifiers unchanged** until an explicit compatible migration has been designed and validated; never do a raw global string replacement in storage/code. New operator-facing text and documentation use **episode selection** and **staging**. In particular, the historical event identifier `USER_EVALUATION_CHECKED`, reasons beginning with `TRIAL_` and settings under the `trial` namespace remain stable machine contracts; their English descriptions must not use the old product name.

The implementation in the current draft stack is not yet end-to-end lab validated.
