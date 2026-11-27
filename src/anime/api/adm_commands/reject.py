"""adm reject --id <anidb_id>: reject a series; the worker removes its torrents and staged files."""
NAME = 'reject'
SUMMARY = 'Reject a series and clean up (--id ANIDB_ID)'


def main(argv, prog):
    from src.anime.api.adm_commands._series import run_action
    from src.anime.series_job.actions import reject_series_sync

    def reject(db, anidb_id, reason, note):
        return reject_series_sync(db, anidb_id, reason=reason or 'REJECTED_BY_USER', note_text=note)
    return run_action(argv, prog, SUMMARY, reject, reason='Reason code (default REJECTED_BY_USER)',
                      note='Why, in your words')
