"""Trusted display registrations reused by API views; no job execution."""
from src.nextgen.handlers import HandlerCatalog
from src.nextgen.jobs.identify_anime_job import DEFINITION as identify
from src.nextgen.jobs.verify_release_job import DEFINITION as verify
from src.nextgen.jobs.evaluate_trial_job import DEFINITION as evaluate
from src.nextgen.jobs.evaluate_batch_job import DEFINITION as evaluate_batch
from src.nextgen.jobs.reserve_batch_job import DEFINITION as reserve_batch
from src.nextgen.jobs.submit_batch_job import DEFINITION as submit_batch
from src.nextgen.jobs.transfer_batch_job import DEFINITION as transfer_batch
from src.nextgen.jobs.verify_batch_probe_job import DEFINITION as verify_batch_probe
from src.nextgen.jobs.unlock_batch_job import DEFINITION as unlock_batch
from src.nextgen.jobs.verify_batch_episode_job import DEFINITION as verify_batch_episode
from src.nextgen.jobs.stage_batch_episode_job import DEFINITION as stage_batch_episode
from src.nextgen.jobs.keep_batch_job import DEFINITION as keep_batch
from src.nextgen.jobs.cleanup_batch_download_job import DEFINITION as cleanup_batch_download
from src.nextgen.jobs.cleanup_batch_kept_media_job import DEFINITION as cleanup_batch_kept_media
from src.nextgen.jobs.reserve_episode_job import DEFINITION as reserve
from src.nextgen.jobs.discover_sonarr_backfill_job import DEFINITION as backfill
from src.nextgen.jobs.prepare_acquisition_job import DEFINITION as prepare
from src.nextgen.jobs.submit_torrent_job import DEFINITION as submit
from src.nextgen.jobs.download_torrent_job import DEFINITION as download
from src.nextgen.jobs.verify_media_job import DEFINITION as media
from src.nextgen.jobs.stage_trial_media_job import DEFINITION as stage
from src.nextgen.jobs.reject_series_job import DEFINITION as reject_series
from src.nextgen.jobs.keep_media_job import DEFINITION as keep
from src.nextgen.jobs.cleanup_canceled_operation_job import DEFINITION as cleanup
from src.nextgen.jobs.management_bulk_job import DEFINITION as bulk

from src.nextgen.jobs.refresh_jellyfin_job import DEFINITION as refresh

from src.nextgen.post_keep_cleanup import DEFINITION as cleanup_kept

from src.nextgen.decision_download_cleanup import DEFINITION as cleanup_download

from src.nextgen.jobs.enrich_shoko_job import DEFINITION as shoko
from src.nextgen.jobs.reset_episode_search_job import DEFINITION as reset_episode

HANDLERS=HandlerCatalog((identify,verify,evaluate,evaluate_batch,reserve,reserve_batch,submit_batch,transfer_batch,verify_batch_probe,unlock_batch,verify_batch_episode,stage_batch_episode,keep_batch,cleanup_batch_download,cleanup_batch_kept_media,bulk,backfill,prepare,submit,download,media,stage,keep,reject_series,cleanup,refresh,cleanup_kept,cleanup_download,shoko,reset_episode))
