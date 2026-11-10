# Notifications

The bell in the dashboard header lists what needs your attention. It shows an
unread badge (`99+` above 99) on every page; the count comes with the service
status poll. Click it for the latest 20, newest first, with **Load more** and
**Mark all read**. Each item has its title, message, time, `×count`, its actions
and **Mark read**. The panel closes on an outside click or Esc.

## Three layers

1. **`notify(db, event)`** (`src/anime/notifications.py`) is what the API and the
   worker call. It runs in the caller's transaction, so a rolled-back action
   leaves no notification, and it never does network I/O. It trusts its callers:
   the event contract (in the module docstring) is checked by regression tests
   (`test_notifications.py`), not at runtime.
2. **Notifiers** implement it. **Generic** is always on and stores every event in
   the inbox. Each enabled extra channel is called after it and only pushes.
   There are none yet; ntfy is a later item (one class plus a Settings checkbox).
3. **The inbox** (`src/anime/storage/notifications.py`): `save`, `items`, `get`,
   `delete`, `delete_all`, `unread`. An event whose `group_key` matches an inbox
   item merges into it (count, latest title and message, moved to the top). Mark
   read deletes the row. At most 5000 rows are kept; the oldest go first.

## Event types

| Type | Posted when | Actions |
|---|---|---|
| `API_REJECTION` | The webhook refused a delivery (work item P4). `group_key = release:<name>` | Process anyway, Add torrent/magnet, Open series |
| `SERIES_EPISODE_TROUBLE` | An episode job of a tracked series ends in Needs review, or discovery finds no usable release for an episode (`NO_RESULTS`, all candidates failed). Not for retries or waits. One item per series (`group_key = series:<anidb_id>`) listing the episodes. | Open series, Open job |
| `KEEP_SEARCH_FAILED` | Keep's one missing-episode search did not start. | Open job |

## API

Same session and CSRF rules as the other management routes.

- `GET /api/v1/notifications?before_id=&limit=` → `items`, `unread`, `next_before_id`
- `DELETE /api/v1/notifications/{id}` (Mark read)
- `DELETE /api/v1/notifications` (Mark all read)
- `POST /api/v1/notifications/{id}/actions/{action}`; an action the type does not allow is a 400.
- `GET /api/v1/services` also returns `notifications_unread`.
