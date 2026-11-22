"""Only a torrent that cannot be identified stops in Needs review.

Everything else is automatic (decided with the user, 2026-11-22):

- Identification (IDENTIFY_ANIME): Needs review, with why it did not identify.
  A person can match the anime by hand; nothing else can.
- Housekeeping (cleanup after a decision or cancel, Keep, Reject, media server
  and metadata refresh, episode search and reset, bulk management): retried
  within the normal retry budget. If it still cannot finish, that is a
  program bug: it ends FAILED with a notification naming the job, the torrent
  and the failed check. Nothing retries forever.
- Everything that works on a torrent (checking, choosing, downloading,
  verifying, transcoding, staging): canceled with a notification naming the
  torrent and why. Its download and staged files go through the normal
  cancellation cleanup, and the next torrent for the episode is tried.

Enforced where the job queue picks a job's next state, so no stop point,
present or future, can land in Needs review outside identification.
"""

IDENTIFY = frozenset(("IDENTIFY_ANIME",))

HOUSEKEEPING = frozenset((
    "BULK_MANAGE_JOBS", "CLEANUP_BATCH_DOWNLOAD", "CLEANUP_BATCH_KEPT_MEDIA", "CLEANUP_CANCELED_OPERATION",
    "CLEANUP_DECIDED_DOWNLOAD", "CLEANUP_KEPT_MEDIA", "DISCOVER_BATCH_PROWLARR", "DISCOVER_EPISODE_RELEASES",
    "ENRICH_SHOKO_METADATA", "KEEP_BATCH", "KEEP_MEDIA", "REFRESH_JELLYFIN", "REJECT_SERIES",
    "RESET_EPISODE_SEARCH",
))

def route(kind):
    """'review', 'retry' or 'cancel' for a job that would stop in Needs review."""
    if kind in IDENTIFY:
        return "review"
    if kind in HOUSEKEEPING:
        return "retry"
    return "cancel"

