# Manual episode release submissions

## Behavior

In View Series, each missing episode exposes **Submit release** without
opening a second dialog. The form accepts a human-readable release title
that includes the desired episode identifier and either a v1 magnet or an
uploaded `.torrent` file (up to 4 MiB). The title is verified against the
episode selected in the series view, not a release number guessed in Jobs.

Manual submissions create **independent candidate jobs**, one per info hash
per episode. They are not additional ACTIVE episode reservations. Each
submission is persisted in `manual_release_submissions` with its event,
source, exact TVDB placement and immutable hash, and creates an ordinary
`VERIFY_RELEASE` job in its own operation. The existing pipeline handles
identity, episode verification, evaluation, reservation, acquisition, audio,
size and staging.

Only `RESERVE_EPISODE` may acquire an ACTIVE episode reservation. If one
candidate already owns the episode, others wait with the existing
`EPISODE_RESERVED` lifecycle. Once cancellation cleanup releases the owner,
existing waiters can resume. Successful staging still wins exclusively;
other candidates cannot stage duplicate episodes. Manual entries are shown
in the ordinary Jobs table when filtered by series + episode. Cancel,
Retry, Try next release, Matching Details and History remain existing actions.

The DB enforces unique (AniDB series, TVDB season, TVDB episode, info hash)
manual candidates; resubmitting the same hash returns the original job.
It does **not** disallow other hashes for that episode. Staged and
series-rejected episodes cannot be newly submitted.

## Magnet path

A v1 `urn:btih` 40-hex magnet is normalized through
`acquisition.metadata.hash_magnet`: only safe public trackers and a
bounded display name survive. The worker uses the existing
qBittorrent legacy-magnet or metadata fetch workflow, with the original
one-shot send journal and ownership checks. A magnet needs peers/trackers
for metadata discovery. Its actual downloaded file list, video size and
audio are still checked downstream.

## Uploaded torrent path

The API accepts JSON with bounded base64-encoded `.torrent` bytes, solely
on the authenticated, CSRF-guarded manual endpoint. It verifies canonical
BEP3, v1 info hash, file inventory, exactly one video, safe paths, and
that the filename's episode declaration agrees with the requested placement
(or with an already verified AniDB-to-TVDB mapping).

The original bounded metainfo bytes are kept in SQLite, never in the job
payload or response. `PREPARE_ACQUISITION` re-reads the original bytes,
verifies hash and manifest, and `SUBMIT_TORRENT` uses its existing
lease-fenced, idempotent stopped `.torrent` upload. This preserves tracker
information in the original file, including private tracker configuration,
instead of converting it to an unreliable magnet. Protect the database and
backups accordingly. The raw torrent is not included in API history.

The existing metadata limits apply (4 MiB, v1/BEP3, one video and allowed
sidecars). v2-only, hybrid and multi-episode packs do not enter this
single-episode flow; they require a separate reviewed batch contract.

## API

`POST /api/v1/intake/series/{anidb_id}/episodes/{season}/{episode}/submit`

Exactly one of:

```json
{"release_name":"Show S01E05 1080p","magnet_uri":"magnet:?xt=urn:btih:..."}
```

```json
{"release_name":"Show S01E05 1080p","torrent_b64":"<base64 of v1 .torrent file>"}
```

Successful submissions return `event_id`, `job_id`, `reused`, a readable
notification, and the existing job status URL. The endpoint requires a
management session and CSRF token, and never creates a Sonarr series or
requests Sonarr to download anything.

## Release lifecycle caveat

A manual candidate is a queued acquisition **attempt**, not permission
to have multiple simultaneous download owners for one episode. The user
can submit multiple alternatives before canceling a stuck owner. They
must still use the existing cancellation/cleanup or Try Next Release
control to free that owner. Releasing an ACTIVE reservation, rather than
simply changing job state, is the necessary handoff boundary.

## Tests and CI

Regression tests have been authored for two distinct candidate hashes,
duplicate submissions, wrong episode strings, file metadata round trips,
authentication, and the no-stacked-modal submit form. No CI was invoked,
and these cases have not been executed in this environment.
