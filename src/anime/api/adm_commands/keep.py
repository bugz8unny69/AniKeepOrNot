"""adm keep --id <anidb_id>: Keep a series: Sonarr owns it."""
NAME = 'keep'
SUMMARY = 'Keep a series: Sonarr owns it (--id ANIDB_ID)'


def main(argv, prog):
    from src.anime.api.adm_commands._series import run_action
    from src.anime.series_job.actions import keep_series_sync
    return run_action(argv, prog, SUMMARY, keep_series_sync)
