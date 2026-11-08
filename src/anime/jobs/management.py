"""Trusted shared composition for workers serving dashboard/ADM action jobs."""
from src.anime.jobs.cleanup_canceled_operation_job import registration as cleanup
from src.anime.jobs.management_bulk_job import registration as bulk
from src.anime.jobs.keep_media_job import registration as keep
from src.anime.jobs.refresh_jellyfin_job import registration as refresh
from src.anime.post_keep_cleanup import registration as cleanup_kept


from src.anime.jobs.enrich_shoko_job import registration as shoko
from src.anime.decision_download_cleanup import registration as cleanup_download
from src.anime.jobs.reject_series_job import registration as reject_series
from src.anime.jobs.keep_batch_job import registration as keep_batch
from src.anime.jobs.cleanup_batch_download_job import registration as cleanup_batch
from src.anime.jobs.cleanup_batch_kept_media_job import registration as cleanup_batch_kept

def registrations(secrets):
    return (bulk(),cleanup(secrets),keep(secrets),reject_series(),keep_batch(secrets),
            cleanup_batch(secrets),cleanup_batch_kept(secrets),refresh(secrets),cleanup_kept(secrets),cleanup_download(secrets),shoko(secrets))
