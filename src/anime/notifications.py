"""Notifications: "something needs your attention" (work item P3).

notify(db, event) is the interface the API and the worker call. It trusts its
callers: the event contract below is checked by regression tests, not at
runtime. It runs in the caller's transaction (a rolled-back action leaves no
notification) and never does network I/O.

Notifiers implement the interface. Generic is always on: it stores every event
in the inbox shown under the dashboard bell. Each enabled extra channel (ntfy,
a later item) is called after it; channels only push, never store.

Event fields (callers' contract):
  type      required  one of EVENT_TYPES
  title     required  <=120 chars, plain text
  message   required  <=500 chars, plain text; no paths, keys or raw errors
  anidb_id, job_id    optional positive ints
  release_name        optional, <=1024
  group_key           optional, <=200; merges with the inbox item that has it
  severity            optional 'info' (default) or 'attention'
  raw_body            optional, <=64 KiB, API_REJECTION only
"""
import time

from src.anime.storage import notifications as inbox

# Each type and the inbox actions it allows. Adding a type is one entry here
# (and in the notifications table CHECK).
EVENT_TYPES = {
    'API_REJECTION': ('match_by_hand', 'process_anyway', 'add_release', 'open_series'),
    # No job numbers (owner, 2026-11-22): every other notice opens its series.
    'SERIES_EPISODE_TROUBLE': ('open_series',),
    'KEEP_SEARCH_FAILED': ('open_series',),
    'TRANSCODE_BATCH_DONE': ('open_series',),
    'MOVIE_SKIPPED': ('open_series',),
    'RELEASE_UNSUPPORTED': ('open_series',),
    'JOB_FAILED': ('open_series',),
}


class Generic:
    """Stores every event in the inbox; severity is stored, not acted on."""
    def deliver(self, db, event, now):
        inbox.save(db, event, now)


GENERIC = Generic()
# Extra push channels, each enabled by its own Settings checkbox (ntfy later).
CHANNELS = ()


def notify(db, event, *, now=None):
    now = time.time_ns() // 1000000 if now is None else now
    GENERIC.deliver(db, event, now)
    for channel in CHANNELS:
        channel.deliver(db, event, now)
