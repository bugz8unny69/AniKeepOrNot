"""Trusted display registrations reused by API views; no job execution."""
from src.anime.handlers import HandlerCatalog
from src.anime.jobs.identify_anime_job import DEFINITION as identify
from src.anime.jobs.verify_release_job import DEFINITION as verify
from src.anime.jobs.select_user_evaluation_job import DEFINITION as evaluate
from src.anime.jobs.evaluate_batch_job import DEFINITION as evaluate_batch
from src.anime.jobs.reserve_batch_job import DEFINITION as reserve_batch
from src.anime.jobs.submit_batch_job import DEFINITION as submit_batch
from src.anime.jobs.transfer_batch_job import DEFINITION as transfer_batch
from src.anime.jobs.verify_batch_probe_job import DEFINITION as verify_batch_probe
from src.anime.jobs.unlock_batch_job import DEFINITION as unlock_batch
from src.anime.jobs.verify_batch_episode_job import DEFINITION as verify_batch_episode
from src.anime.jobs.stage_batch_episode_job import DEFINITION as stage_batch_episode
from src.anime.jobs.keep_batch_job import DEFINITION as keep_batch
from src.anime.jobs.cleanup_batch_download_job import DEFINITION as cleanup_batch_download
from src.anime.jobs.cleanup_batch_kept_media_job import DEFINITION as cleanup_batch_kept_media
from src.anime.jobs.reserve_episode_job import DEFINITION as reserve
from src.anime.jobs.discover_episode_releases_job import DEFINITION as backfill
from src.anime.jobs.prepare_acquisition_job import DEFINITION as prepare
from src.anime.jobs.submit_torrent_job import DEFINITION as submit
from src.anime.jobs.download_torrent_job import DEFINITION as download
from src.anime.jobs.transcode_media_job import DEFINITION as transcode
from src.anime.jobs.verify_media_job import DEFINITION as media
from src.anime.jobs.stage_user_evaluation_media_job import DEFINITION as stage
from src.anime.jobs.reject_series_job import DEFINITION as reject_series
from src.anime.jobs.keep_media_job import DEFINITION as keep
from src.anime.jobs.cleanup_canceled_operation_job import DEFINITION as cleanup
from src.anime.jobs.management_bulk_job import DEFINITION as bulk

from src.anime.jobs.refresh_jellyfin_job import DEFINITION as refresh

from src.anime.post_keep_cleanup import DEFINITION as cleanup_kept

from src.anime.decision_download_cleanup import DEFINITION as cleanup_download

from src.anime.jobs.enrich_shoko_job import DEFINITION as shoko
from src.anime.jobs.reset_episode_search_job import DEFINITION as reset_episode

HANDLERS=HandlerCatalog((identify,verify,evaluate,evaluate_batch,reserve,reserve_batch,submit_batch,transfer_batch,verify_batch_probe,unlock_batch,verify_batch_episode,stage_batch_episode,keep_batch,cleanup_batch_download,cleanup_batch_kept_media,bulk,backfill,prepare,submit,download,media,transcode,stage,keep,reject_series,cleanup,refresh,cleanup_kept,cleanup_download,shoko,reset_episode))
