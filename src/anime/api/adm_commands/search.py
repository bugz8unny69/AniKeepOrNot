"""adm search --id <anidb_id>: Search now for a series' trial episodes no torrent covers."""
NAME = 'search'
SUMMARY = "Search now for a series' trial episodes no torrent covers (--id ANIDB_ID)"


def main(argv, prog):
    from src.anime.api.adm_commands._series import run_action
    from src.anime.series_job.actions import search_series_sync
    return run_action(argv, prog, SUMMARY, search_series_sync)
