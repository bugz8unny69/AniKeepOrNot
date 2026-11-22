"""A start that never reached qBittorrent goes back to SELECTED.

journal.start records START_UNCERTAIN before the start request, so a crash
mid-send is never replayed. When the attempt gives up before the client takes
its start permit (cancel, a changed snapshot, qBittorrent unreachable), nothing
was sent: leaving START_UNCERTAIN would wait forever on a stopped torrent.
Only the attempt that recorded the start can undo it, and only from
START_UNCERTAIN, so a start that may have been sent is never repeated.
"""
from src.anime.history import append_job_history
from src.anime.storage.sqlite import transaction

TABLES = frozenset(('torrent_transfers', 'batch_torrent_transfers', 'batch_torrent_unlocks'))


def start_not_sent(queue, claim, table, plan_id):
    """Return True when this attempt's unsent start went back to SELECTED."""
    if table not in TABLES:
        raise ValueError('unknown transfer table')
    with queue.connection() as db, transaction(db):
        queue._owns(db, claim, queue.now())
        now = queue.now()
        changed = db.execute(
            f"UPDATE {table} SET phase='SELECTED',reason_code='TORRENT_SELECTION_CONFIRMED',"
            'start_job_id=NULL,start_attempt=NULL,updated_at_ms=max(updated_at_ms,?) '
            "WHERE plan_id=? AND phase='START_UNCERTAIN' AND start_job_id=? AND start_attempt=?",
            (now, plan_id, claim.job_id, claim.attempt)).rowcount
        if changed:
            append_job_history(db, job_id=claim.job_id, event_code='TORRENT_SELECTION_CONFIRMED',
                               reason_code='TORRENT_SELECTION_CONFIRMED', component='worker',
                               occurred_at_ms=now, details={'attempt': claim.attempt})
        return bool(changed)
