"""Required provider configuration and worker composition; no HTTP at startup."""
from functools import partial

from src.nextgen.configuration import ConfigurationStore
from src.nextgen.configuration_schema import ConfigurationError
from src.nextgen.job_contracts import Policy, Registry
from src.nextgen.jobs.identify_anime_job import registration
from src.nextgen.jobs.verify_release_job import KIND as RELEASE_KIND, registration as release_registration
from src.nextgen.jobs.select_user_evaluation_job import KIND as USER_EVALUATION_KIND, registration as user_evaluation_registration
from src.nextgen.jobs.discover_episode_releases_job import KIND as BACKFILL_KIND, registration as backfill_registration
from src.nextgen.jobs.evaluate_batch_job import KIND as BATCH_KIND, registration as batch_registration
from src.nextgen.jobs.reserve_batch_job import KIND as BATCH_RESERVE_KIND, registration as batch_reserve_registration
from src.nextgen.jobs.submit_batch_job import KIND as BATCH_SUBMIT_KIND, registration as batch_submit_registration
from src.nextgen.jobs.transfer_batch_job import KIND as BATCH_TRANSFER_KIND, registration as batch_transfer_registration
from src.nextgen.jobs.verify_batch_probe_job import KIND as BATCH_PROBE_KIND, registration as batch_probe_registration
from src.nextgen.jobs.unlock_batch_job import KIND as BATCH_UNLOCK_KIND, registration as batch_unlock_registration
from src.nextgen.jobs.verify_batch_episode_job import KIND as BATCH_EPISODE_KIND, registration as batch_episode_registration
from src.nextgen.jobs.stage_batch_episode_job import KIND as BATCH_STAGE_KIND, registration as batch_stage_registration
from src.nextgen.jobs.discover_batch_prowlarr_job import registration as batch_prowlarr_registration
from src.nextgen.jobs.reserve_episode_job import KIND as RESERVE_KIND, registration as reserve_registration
from src.nextgen.jobs.prepare_acquisition_job import KIND as PREPARE_KIND, registration as prepare_registration
from src.nextgen.jobs.submit_torrent_job import KIND as SUBMIT_KIND, registration as submit_registration
from src.nextgen.jobs.download_torrent_job import KIND as DOWNLOAD_KIND, registration as download_registration
from src.nextgen.jobs.verify_media_job import KIND as MEDIA_KIND, registration as media_registration
from src.nextgen.jobs.stage_user_evaluation_media_job import KIND as STAGE_KIND, registration as stage_registration
from src.nextgen.jobs.management import registrations as management_registrations
from src.nextgen.acquisition.metainfo_source import make_reader, make_batch_reader
from src.nextgen.acquisition.legacy_magnet import make_bootstrap
from src.nextgen.batch.provider_worker import make_batch_verifier
from src.nextgen.providers.backfill_catalog import make_catalog_factory
from src.nextgen.providers.errors import ProviderConfigurationError
from src.nextgen.providers.http import Options
from src.nextgen.providers.identity import ClientPool, make_resolver
from src.nextgen.providers.releases import make_verifier
from src.nextgen.secrets import SecretStore
from src.nextgen.worker import Worker, blocking, serve_worker


def options_from_configuration(configuration):
    try:
        return Options(configuration.value("anidb.client_name"),
                       configuration.value("anidb.client_version"),
                       configuration.value("providers.timeout_seconds"))
    except ConfigurationError:
        raise ProviderConfigurationError() from None


def series_credentials(secrets):
    """Retrieve required TVDB credentials for anime series identification."""
    return secrets.tvdb_credentials()


def episode_placement_credentials(secrets):
    """Retrieve credentials for optional TVDB episode placement; None if unconfigured."""
    try:
        return secrets.tvdb_credentials()
    except ProviderConfigurationError:
        return None


def validate_provider_configuration(configuration, *, secrets=None, require_series_credentials=False):
    options_from_configuration(configuration)
    if require_series_credentials and secrets is not None:
        series_credentials(secrets)


async def build_identity_worker(config_directory, *, additional_registrations=(), release_kind=None,
                                policy=Policy(), logger=None, read_metainfo=None, read_batch_metainfo=None,
                                backfill=False, backfill_select=False, backfill_acquire=False,
                                require_series_credentials=False, client_factory=None, service_health=None):
    """Validate required local settings, then return an unstarted worker.

    Call close() if not serving it. Series identification requires TVDB
    credentials; AniDB-first release verification does not. Generic Worker
    still supports unrelated explicitly installed jobs without requiring
    identity-provider credentials.
    """
    if (type(backfill_select) is not bool or type(backfill_acquire) is not bool
            or backfill_select and not backfill
            or backfill_acquire and not backfill_select):
        raise ValueError('Backfill source selection requires opt-in backfill discovery')
    store = ConfigurationStore(config_directory)
    secrets = SecretStore(config_directory)
    startup = await blocking(store.read)
    prepare = partial(validate_provider_configuration, secrets=secrets,
                      require_series_credentials=require_series_credentials)
    await blocking(prepare,startup)  # Missing configuration fails before claims.
    pool=ClientPool()
    resolve = make_resolver(options_from_configuration, partial(series_credentials, secrets), client_pool=pool)
    read_batch_metainfo=make_batch_reader(secrets=secrets) if read_batch_metainfo is None else read_batch_metainfo
    from src.nextgen.acquisition.existing_metadata import make_existing_observer
    # Read-only qB file lists rank batch candidates; observed outside any
    # write transaction and never used to adopt a torrent.
    verify_batch=make_batch_verifier(options_from_configuration, partial(episode_placement_credentials, secrets),
                                     read_batch_metainfo,client_pool=pool,
                                     observe_existing=make_existing_observer(secrets,client_factory=client_factory))
    verify = make_verifier(options_from_configuration, partial(episode_placement_credentials, secrets), client_pool=pool,
                           batch_verifier=verify_batch)
    # The hash-bound magnet path is the normal webhook acquisition route, not
    # a backfill opt-in: it is installed with the default metadata reader.
    default_reader=read_metainfo is None
    read_metainfo=make_reader(secrets=secrets) if default_reader else read_metainfo
    preparation=(prepare_registration(read_metainfo,submission_kind=SUBMIT_KIND,
                    legacy_bootstrap=make_bootstrap(secrets) if default_reader or backfill_acquire else None),
                                                submit_registration(read_metainfo,secrets,transfer_kind=DOWNLOAD_KIND,
                                                    discovery_kind=BACKFILL_KIND if backfill else None),
                                                download_registration(secrets,verification_kind=MEDIA_KIND),media_registration(staging_kind=STAGE_KIND),stage_registration())
    # Batch jobs share trusted hash-only metadata and the same ownership safeguards.
    batch_handlers = (
        batch_reserve_registration(submission_kind=BATCH_SUBMIT_KIND),
        batch_submit_registration(read_batch_metainfo,secrets,
                                  transfer_kind=BATCH_TRANSFER_KIND),
        batch_transfer_registration(read_batch_metainfo,secrets,
                                    verification_kind=BATCH_PROBE_KIND),
        batch_probe_registration(read_batch_metainfo,unlock_kind=BATCH_UNLOCK_KIND),
        batch_unlock_registration(read_batch_metainfo,secrets,next_kind=BATCH_EPISODE_KIND),
        batch_episode_registration(read_batch_metainfo,staging_kind=BATCH_STAGE_KIND),
        batch_stage_registration(),
        batch_prowlarr_registration(secrets),
    )
    registry = Registry((registration(resolve,release_kind=RELEASE_KIND if release_kind is None else release_kind),
                         release_registration(verify,user_evaluation_kind=USER_EVALUATION_KIND,batch_kind=BATCH_KIND),
                         batch_registration(reservation_kind=BATCH_RESERVE_KIND),
                         user_evaluation_registration(reservation_kind=RESERVE_KIND),
                         reserve_registration(preparation_kind=PREPARE_KIND),
                         *((backfill_registration(secrets,
                              catalog_factory=make_catalog_factory(options_from_configuration,
                                  partial(episode_placement_credentials, secrets),client_pool=pool),
                              select_candidates=backfill_select,
                              acquire_candidates=backfill_acquire),) if backfill else ()),
                         *batch_handlers,*preparation,
                         *management_registrations(secrets),*additional_registrations))
    if service_health is None:
        from src.nextgen.services import ServiceHealth, ServiceRegistry
        from src.nextgen.services.adapters import builtin_adapters
        from src.nextgen.services.adapters.qbittorrent import QbittorrentAdapter
        adapters = tuple(QbittorrentAdapter(client_factory=client_factory)
                         if isinstance(a, QbittorrentAdapter) else a for a in builtin_adapters())
        service_health = ServiceHealth(ServiceRegistry(adapters))
    return Worker(store,startup,registry,policy=policy,logger=logger,prepare=prepare,
                  service_health=service_health,secrets=secrets)


async def run_identity_worker(config_directory, *, additional_registrations=(), release_kind=None,
                              policy=Policy(), read_metainfo=None, read_batch_metainfo=None,
                              backfill=False, backfill_select=False, backfill_acquire=False,
                              require_series_credentials=False, client_factory=None, service_health=None):
    worker = await build_identity_worker(config_directory,additional_registrations=additional_registrations,
                                         release_kind=release_kind,policy=policy,read_metainfo=read_metainfo,
                                         read_batch_metainfo=read_batch_metainfo,
                                         backfill=backfill,backfill_select=backfill_select,
                                         backfill_acquire=backfill_acquire,
                                         require_series_credentials=require_series_credentials,
                                         client_factory=client_factory, service_health=service_health)
    await serve_worker(worker)
