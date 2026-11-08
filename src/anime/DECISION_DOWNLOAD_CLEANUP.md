# Download cleanup after staging, Keep or Reject

New staging jobs COPY verified media. The original qBittorrent download
remains available until the staging transaction commits. A committed single
episode queues one CLEANUP_DECIDED_DOWNLOAD job
(`{"staging_job_id": N}`) due one minute later. The completed batch staging
flow also schedules owned-download cleanup. Pre-upgrade MOVE staging plans
remain supported.

The cleanup job uses the existing `OwnedCleanup` journal:
`PREPARED -> STOP_UNCERTAIN -> REMOVE_UNCERTAIN -> FILES -> COMPLETE`.
After verifying ownership and the stopped torrent, it requests qBittorrent
removal with `deleteFiles=true`. Only after observing that the torrent is
absent does the worker purge the pinned, owned `ad-<marker>` download
folder if qBittorrent left it behind. The staging and Keep directories
are never part of that purge. A lost response is reconciled by inspecting
the torrent; the existing stop/remove recovery rules still apply.

The exact torrent hash, owned marker, category, save path, original
manifest, exclusive operation gate, and other consumers are checked
before any deletion. Folder cleanup verifies the pinned directory
identity, rejects symlinks/replacements, and is safe if the directory is
already absent. Do not run cleanup if staging did not complete.

Keep and Reject can enqueue the same durable cleanup contract; if an
earlier staging cleanup already completed, no second qBittorrent delete is
issued. A later Keep/Reject action must not disturb staged/permanent
files or release an episode reservation.

The original setup, completed-job lineage, independent control
operation and UI status/retry logic are unchanged. This revision adds
no database/configuration migration.
