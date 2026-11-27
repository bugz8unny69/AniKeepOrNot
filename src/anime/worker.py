"""The AKON worker: runs the series job, one due series at a time; no HTTP API.

Owner, 2026-11-26: one job per series; the old per-step jobs are gone. The
worker checks every service at startup (refusing to start when a required one
is not set up), re-checks the ones that are due, saves their status for the
API, and runs series attempts (series_job/worker.py) with the real steps
(series_job/context.py). Each attempt holds the maintenance gate, so a
migration or backup never runs while a series is being worked on.
"""
import asyncio
import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from src.anime.async_io import blocking
from src.anime.configuration import ConfigurationStore
from src.anime.diagnostics import configure_logging, event, exception
from src.anime.maintenance import MaintenanceBusy, MaintenanceGate, MaintenancePending
from src.anime.services.startup import StartupRefused
from src.anime.systemd_notify import notify, watchdog_interval

IDLE_S = 0.5          # Nothing due: look again in half a second.


class Worker:
    def __init__(self, configuration_store, startup, *, logger=None, service_health=None, secrets=None,
                 context_factory=None):
        self.config, self.startup = configuration_store, startup
        self._secrets, self._service_health = secrets, service_health
        self.gate = MaintenanceGate(startup.value("storage.database_path"))
        self.logger = configure_logging(startup.logging_settings(), "worker", logger=logger)
        self._stop = asyncio.Event()
        self._halt = threading.Event()        # The steps' cancel event: the worker is stopping.
        self._services_dirty = True
        self._services_saved_ms = None
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="akon-control")
        if context_factory is None:
            from src.anime.series_job.context import build as context_factory
        self._context = context_factory(secrets, self._halt)

    async def _io(self, callable_, *args, **kwargs):
        return await blocking(callable_, *args, executor=self._pool, **kwargs)

    def stop(self):
        """Finish the current series attempt, then stop."""
        self._halt.set()
        self._stop.set()

    def _log_service_changes(self, changes):
        """Log each service status change once: down, or available again (a first UP is not a recovery)."""
        from src.anime.services import USABLE
        for change in changes:
            if not change.state.usable:
                name, level = "SERVICE_DOWN", logging.WARNING
            elif change.old not in USABLE:
                name, level = "SERVICE_RESTORED", logging.INFO
            else:
                continue
            event(self.logger, level, name, component="worker", service=change.service,
                  service_status=change.new, service_detail=change.state.message())
        if changes:
            self._services_dirty = True

    async def _save_services(self):
        """Service status for the API (status bar, Keep check): on change, and at least once a minute."""
        health = self._service_health
        if health is None:
            return
        now = health.now()
        if not self._services_dirty and self._services_saved_ms is not None and now - self._services_saved_ms < 60000:
            return
        from src.anime.services import status_store
        try:
            await self._io(status_store.save, self.startup.value("storage.database_path"), health.snapshot(), now_ms=now)
        except (sqlite3.Error, OSError) as exc:
            exception(self.logger, "OPERATION_FAILED", exc, level=logging.WARNING, reason_code="DATABASE_ERROR",
                      component="worker")
        self._services_dirty = False
        self._services_saved_ms = now

    async def _check_services_at_startup(self, configuration):
        """Check every service once; refuse to start if a required one is not set up."""
        from src.anime.services import status_store
        from src.anime.services.startup import refusals
        previous = await self._io(status_store.load, self.startup.value("storage.database_path"))
        self._log_service_changes(await self._io(self._service_health.startup, configuration, self._secrets, previous))
        await self._save_services()
        refused = refusals(self._service_health)
        if refused:
            for state in refused:
                event(self.logger, logging.ERROR, "SERVICE_STARTUP_REFUSED", component="worker",
                      reason_code="SERVICE_SETUP_INVALID", service=state.service,
                      service_status=state.status, service_detail=state.message())
            raise StartupRefused(refused)

    async def attempt(self):
        """One series attempt under the maintenance gate. True when a series was worked on."""
        from contextlib import closing
        from src.anime.series_job.store import LeaseLost
        from src.anime.series_job.worker import run_series_attempt
        from src.anime.storage.sqlite import connect
        configuration = await self._io(self.config.operation, self.startup, component="worker")
        if self._service_health is not None:
            self._log_service_changes(await self._io(self._service_health.probe_due, configuration, self._secrets))
            await self._save_services()
        self._context.configuration = configuration
        try:
            with self.gate.operation(), closing(connect(configuration.value("storage.database_path"))) as db:
                self._context.db = db
                return await run_series_attempt(db, self._context)
        except (MaintenanceBusy, MaintenancePending):
            return False                      # A migration or backup has the database.
        except LeaseLost as exc:
            self.logger.warning("Series job: %s; this attempt is not saved, the next one redoes it", exc)
            return False
        except sqlite3.OperationalError as exc:      # Busy or locked database: try again shortly.
            self.logger.warning("Series job: the database was busy (%s); trying again", exc, exc_info=exc)
            return False

    async def run(self, *, on_ready=None):
        try:
            configuration = await self._io(self.config.operation, self.startup, component="worker")
            if self._service_health is not None and self._secrets is not None:
                await self._check_services_at_startup(configuration)
            if on_ready is not None:
                on_ready()
            while not self._stop.is_set():
                if not await self.attempt():
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=IDLE_S)
                    except asyncio.TimeoutError:
                        pass
        finally:
            self.stop()
            await blocking(self._pool.shutdown, wait=True)


async def build_worker(config_directory, *, logger=None, service_health=None, client_factory=None):
    """The installed worker: settings, secrets, service checks and the series job's real steps."""
    from src.anime.secrets import SecretStore
    store = ConfigurationStore(config_directory)
    startup = await blocking(store.read)
    if service_health is None:
        from src.anime.services import ServiceHealth, ServiceRegistry
        from src.anime.services.adapters import builtin_adapters
        from src.anime.services.adapters.qbittorrent import QbittorrentAdapter
        adapters = tuple(QbittorrentAdapter(client_factory=client_factory)
                         if client_factory is not None and isinstance(a, QbittorrentAdapter) else a
                         for a in builtin_adapters())
        service_health = ServiceHealth(ServiceRegistry(adapters))
    return Worker(store, startup, logger=logger, service_health=service_health, secrets=SecretStore(config_directory))


async def _systemd_watchdog(worker, interval):
    """Heartbeat from the worker event loop; a blocked loop cannot fake health."""
    while not worker._stop.is_set():
        try:
            await asyncio.wait_for(worker._stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            notify("WATCHDOG=1")


async def serve_worker(worker):
    """Run the worker with service signal handling."""
    import signal
    loop = asyncio.get_running_loop()
    installed = []
    watchdog_task = None
    try:
        for selected in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(selected, worker.stop)
            installed.append(selected)
        interval = watchdog_interval()
        if interval is not None:
            watchdog_task = asyncio.create_task(_systemd_watchdog(worker, interval))
        await worker.run(on_ready=lambda: notify("READY=1"))
    finally:
        worker.stop()
        if watchdog_task is not None:
            await watchdog_task
        notify("STOPPING=1")
        for selected in installed:
            loop.remove_signal_handler(selected)


async def run_worker(config_directory):
    await serve_worker(await build_worker(config_directory))
