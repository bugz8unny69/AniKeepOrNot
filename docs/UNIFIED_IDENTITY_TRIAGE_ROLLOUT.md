# Unified identity triage — lab rollout

This integration performs a bounded first-pass lookup for unmatched Autobrr releases in the **unified AniKeepOrNot database**, not the older standalone inbox.

## Sequence

1. A valid Autobrr JSON webhook is received. Configured exact-name aliases still authorize episodes as they did previously.
2. Unknown releases are stored as `IDENTIFYING`. A background worker (one item per cycle) parses the candidate episode number and uses the public AniDB title catalog plus authenticated TVDB v4 searches.
3. The worker checks TVDB's explicit AniDB remote-ID cross-references. **Title overlap alone is never sufficient.**
4. Automatic dispatch additionally requires a unique TVDB/AniDB series pair, a previously verified `identity.episode_map` entry for the exact TVDB season and episode, and one matching TVDB aired-order episode record. The existing approval gate then atomically queues acquisition. No Shoko API or Shoko scan is involved in this phase.
5. If evidence is absent, contradictory, or the episode map is not verified, it becomes `NEEDS_REVIEW`. Transient lookup errors are retried with bounded pacing before review. The existing NTFY monitor sends the review alert and deduplicates subsequent reminders.
6. Historical unmatched `RECEIVED` and `UNTRUSTED` records enter the same identity worker before review rather than being immediately alerted.

## AniDB credentials

Public AniDB title data needs **no API key**. The dashboard exposes a functional `anidb.enabled` control, enabled by default. Turning it off prevents the unified identity worker from contacting the public AniDB catalog. There is no AniDB API secret to configure. TVDB must be enabled with a valid API key to resolve cross-references.

## Operational notes

The worker persists safe evidence summaries and retry metadata in `unified_identity_checks` inside the existing SQLite database. Unmatched releases are never silently downloaded. API errors do not block qBittorrent for already verified releases. The NTFY monitor is always launched and reloads enablement from settings every cycle.

The public catalog cache is refreshed at most daily. Test live TVDB credentials, public AniDB catalog download, real naming variations, rate limits, status transitions and private NTFY delivery on the lab VM before promoting to `main`. A successful CI run is not a guarantee that external provider APIs are available.
