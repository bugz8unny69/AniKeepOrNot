"""adm retry --id <anidb_id>: Retry a series: stopped torrents resume, dropped ones start over, a stopped Keep resumes."""
NAME = 'retry'
SUMMARY = 'Retry a series: stopped torrents resume, dropped ones start over, a stopped Keep resumes (--id ANIDB_ID)'


def main(argv, prog):
    from src.anime.api.adm_commands._series import run_action
    from src.anime.series_job.actions import retry_series_sync
    return run_action(argv, prog, SUMMARY, retry_series_sync)
