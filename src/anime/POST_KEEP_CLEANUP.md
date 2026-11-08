# Post-Keep selected staging cleanup

A successful Keep completion atomically creates one independent
CLEANUP_KEPT_MEDIA job and stores its ID plus PENDING state in keep_imports JSON.
This uses the existing v22 journal schema; configuration stays v9 and secrets v5.
A failed cleanup cannot roll back a completed Keep or trigger another import.

Before deleting, the worker requires the original Keep job to be DONE with a
COMPLETE import journal, matching source event/control provenance and the original
published staging manifest. It reads the configured Sonarr service using the
Keep job's pinned endpoint/root settings and fresh credentials. The exact TVDB
series, canonical episode and permanent episode-file binding must still match.
The local permanent file must be readable, regular, under the recorded root and
at the expected size. There is no destination checksum or byte comparison.

Reuse remove_selected from the Reject cleanup library: remove only the selected
staged video and episode NFO when their recorded directory and file signatures
match. Leave tvshow.nfo, other episodes, permanent media and download/torrent data
alone. A missing selected file can be reconciled after interruption. Recovered
attempts serialize with the existing per-job lock; lease checks precede file
operations and journal updates. A successful cleanup persists COMPLETE and may
queue another independent Jellyfin library scan for the removed staging entries.

Keep receipts include post_keep_cleanup with job ID, state, completion and a
catalog reason. The dashboard displays this independently from Kept. For stopped
review/blocked cleanup, Retry staging cleanup uses the existing Keep action to
resume that exact follow-up job; it never resets or submits the Sonarr import.
Network/filesystem errors use bounded worker retries. Replaced staging files,
missing permanent proof and unexpected metadata need review.

Existing completed Keep jobs without a cleanup record are not backfilled or
implicitly granted deletion authority. Episode reservations remain as retained
identity/deduplication records for kept media, not free acquisition slots.
Releasing canceled acquisitions, removing download/torrent resources and optional
Shoko registration remain separate implementation work.

Offline cases for selected-only deletion, missing permanent output, changed
staging files, interrupted deletion, retry without reimport and unavailable
Sonarr are authored but unrun. Source checks do not establish runtime or lab
correctness. No live calls, deletion, tests, CI, migration or deployment occurred.

## Decision download cleanup follow-up

New completed Reject decisions and post-Keep staging cleanups enqueue a separate
owned-download cleanup job. Receipts expose `download_cleanup`; a stopped job
can resume through Keep/Reject with the dashboard label Retry download cleanup.
Decision/import completion and episode protection remain intact. Current contract:
[Download cleanup](DECISION_DOWNLOAD_CLEANUP.md). This supersedes earlier remaining-download-cleanup notes above. Tests remain unrun.
