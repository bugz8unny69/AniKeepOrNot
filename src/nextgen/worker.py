"""Separate async jobs worker, with explicit trusted registration; no HTTP API.

No provider/acquisition/Keep handlers are bundled. Installed application code
passes registrations to Worker; an empty registry fails readiness before claims.
"""
import asyncio
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
import logging
import inspect
import threading
import uuid

from src.nextgen.async_io import blocking
from src.nextgen.systemd_notify import notify, watchdog_interval
from src.nextgen.cancellation import OperationGate, CancellationRequested
from src.nextgen.storage.cancellation import CancellationStore
from src.nextgen.configuration import ConfigurationStore
from src.nextgen.configuration_schema import ConfigurationError
from src.nextgen.diagnostics import configure_logging, event, exception, log_history
from src.nextgen.job_contracts import CompletionRejected, Outcome, Policy, Registry, payload_json, failure_facts
from src.nextgen.reasons import get_reason
from src.nextgen.maintenance import MaintenanceGate, MaintenanceBusy, MaintenancePending
from src.nextgen.storage.job_queue import JobQueue, LeaseLost, QueueUnavailable


class WorkerNotReady(RuntimeError):
    reason_code = "WORKER_NOT_READY"
    def __init__(self):
        super().__init__("The worker is not ready to process jobs.")




async def settle(tasks):
    """Drain children without cancellation escaping into their cleanup paths."""
    future = asyncio.gather(*tasks,return_exceptions=True)
    cancelled = False
    while True:
        try:
            await asyncio.shield(future)
            break
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError()


@dataclass(frozen=True)
class Context:
    claim: object = field(repr=False)
    configuration: object = field(repr=False)
    logger: object = field(repr=False)
    cancelled: object = field(repr=False)

    def data(self):
        return self.claim.data()


class Worker:
    def __init__(self, configuration_store, startup, registry, *, policy=Policy(), logger=None, prepare=None):
        if type(registry) is not Registry or not registry.entries:
            raise WorkerNotReady()
        if prepare is not None and (not callable(prepare) or inspect.iscoroutinefunction(prepare)):
            raise ValueError("worker readiness callback must be synchronous")
        self._prepare = prepare
        self.config, self.startup, self.registry, self.policy = configuration_store, startup, registry, policy
        self.gate = MaintenanceGate(startup.value("storage.database_path"))
        self.queue = JobQueue(self.gate.database, policy=policy)
        self.concurrency = startup.value("worker.concurrency")
        self.logger = configure_logging(startup.logging_settings(), "worker", logger=logger)
        self._stop = asyncio.Event()
        self._halt_claims = threading.Event()
        self._paused = False
        self._tasks = set()
        self._attempts = set()
        self._slots = asyncio.Semaphore(self.concurrency)
        self._close_task = None
        self._running = False
        self._closed = False
        self._control_pool = ThreadPoolExecutor(max_workers=4,thread_name_prefix="nextgen-control")

    async def _io(self, callable_, *args, **kwargs):
        return await blocking(callable_, *args, executor=self._control_pool, **kwargs)

    async def close(self):
        """Stop and drain embedded attempts before releasing the control pool."""
        if self._running or asyncio.current_task() in self._attempts:
            raise RuntimeError("stop and await the worker before closing it")
        if self._close_task is None:
            self._closed = True
            self.stop()
            self._close_task = asyncio.create_task(self._shutdown())
        await settle((self._close_task,))
        self._close_task.result()

    async def _shutdown(self):
        await self.drain()
        await blocking(self._control_pool.shutdown,wait=True)

    def pause(self):
        self._paused = True
        self._halt_claims.set()

    def resume(self):
        self._paused = False
        if not self._stop.is_set():
            self._halt_claims.clear()

    def stop(self):
        """Stop claiming and drain current tasks; do not cancel them."""
        self._halt_claims.set()
        self._stop.set()

    async def drain(self):
        self.pause()
        if asyncio.current_task() in self._attempts:
            raise RuntimeError("an attempt cannot drain itself")
        while self._attempts:
            await settle(tuple(self._attempts))

    def _log_error(self, exc, claim=None):
        reason = getattr(exc, "reason_code", "JOB_HANDLER_FAILED")
        if get_reason(reason) is None:
            reason = "JOB_HANDLER_FAILED"
        try:database=self.startup.value("storage.database_path")
        except Exception:database=None
        exception(self.logger, "OPERATION_FAILED", exc, reason_code=reason, component="worker",
                  database=database,
                  job_id=None if claim is None else claim.job_id,
                  event_id=None if claim is None else claim.event_id,
                  attempt=None if claim is None else claim.attempt)

    async def _heartbeat(self, claim, finished):
        while True:
            try:
                await asyncio.wait_for(finished.wait(), timeout=self.policy.lease_ms / 3000)
                return
            except asyncio.TimeoutError:
                await self._io(self.queue.renew,claim)

    async def _invoke(self, claim, spec, context):
        finished = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(claim,finished))
        call = asyncio.create_task(spec.execute(context) if spec.mode == "async"
                                   else blocking(spec.execute,context))
        try:
            done, _ = await asyncio.wait((call,heartbeat),return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                # A renewal failure means ownership cannot be assumed, even if
                # a handler happened to finish at the same time.
                await heartbeat
                raise LeaseLost()
            try:
                outcome = await call
                if type(outcome) is not Outcome:
                    raise ValueError("handler did not return a typed outcome")
                return outcome
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                return Outcome("retry", "JOB_HANDLER_FAILED")
            except (QueueUnavailable,ConfigurationError):
                # Storage/configuration failure is not a provider retry decision.
                raise
            except Exception as exc:
                # Catch-boundary diagnostic with exact redacted error
                exception(self.logger, "OPERATION_FAILED", exc, level=logging.WARNING,
                          reason_code="JOB_HANDLER_FAILED", component="worker",
                          job_id=claim.job_id, event_id=claim.event_id, attempt=claim.attempt)
                facts = failure_facts(exc) or {"failed_check": "src/nextgen/worker.py:171 _invoke"}
                return Outcome("retry", "JOB_HANDLER_FAILED", facts=facts)
        finally:
            context.cancelled.set()
            finished.set()
            if not call.done():
                call.cancel()
            # Blocking handlers must finish before the attempt releases its gate.
            await settle((call,heartbeat))

    async def attempt(self):
        """One bounded claim/attempt, also tracked when embedded directly."""
        if self._closed:
            raise WorkerNotReady()
        task = asyncio.current_task()
        if task in self._attempts:
            raise RuntimeError("an attempt cannot recursively claim work")
        self._attempts.add(task)
        try:
            async with self._slots:
                return await self._attempt()
        finally:
            self._attempts.discard(task)

    async def _attempt(self):
        if self._stop.is_set() or self._paused:
            return False
        try:
            admission = self.gate.operation()
            admission.__enter__()
        except (MaintenanceBusy, MaintenancePending):
            return False
        claim = None
        operation_admission = None
        try:
            configuration = await self._io(self.config.operation,self.startup,component="worker")
            if self._stop.is_set() or self._paused:
                return False
            if self._prepare is not None:
                result = await self._io(self._prepare,configuration)
                if result is not None:
                    raise ValueError("readiness callback must not return values")
            claim, audits = await self._io(self.queue.claim,halted=self._halt_claims.is_set)
            if claim is None:
                return False
            operation_id=await self._io(self.queue.operation_id,claim)
            candidate_admission=OperationGate(self.queue.database).hold(operation_id)
            candidate_admission.__enter__()
            operation_admission=candidate_admission
            # Separate logger snapshot: one attempt changing verbosity cannot
            # mutate another running attempt's logger threshold/formatter.
            logger = configure_logging(configuration.logging_settings(), "worker",
                                       logger=logging.Logger("nextgen.worker.job." + str(claim.job_id)))
            for audit in audits:
                try:
                    log_history(logger, audit)
                except Exception as log_exc:
                    self.logger.warning("Audit logging failed for job %s: %s", claim.job_id, log_exc)
            spec = self.registry.entries.get(claim.kind)
            invocation = uuid.uuid4().hex
            if spec is None:
                outcome = Outcome("review", "UNSUPPORTED_JOB_KIND")
            else:
                try:
                    payload_json(claim.data())
                except (ValueError, TypeError, RecursionError):
                    spec = None
                    outcome = Outcome("review", "JOB_PAYLOAD_INVALID")
                else:
                    started = await self._io(self.queue.start,claim,spec,invocation,self.registry)
                    try:
                        log_history(logger, started)
                    except Exception as log_exc:
                        self.logger.warning("Audit logging failed for job %s: %s", claim.job_id, log_exc)
                    context = Context(claim,configuration,logger,threading.Event())
                    outcome = await self._invoke(claim,spec,context)
            try:
                records = await self._io(self.queue.finish,claim,outcome,spec=spec,
                                         invocation=invocation,registry=self.registry)
            except CompletionRejected as rejection:
                # Log the exact check that refused the save (its original error).
                # A skip (duplicate) carries no reason and is not a failure.
                if rejection.outcome.reason_code is not None:
                    exception(logger, "OPERATION_FAILED", rejection, level=logging.WARNING,
                              reason_code=rejection.outcome.reason_code, component="worker",
                              job_id=claim.job_id, event_id=claim.event_id, attempt=claim.attempt)
                # The first transaction rolled back all facts/ending/successors.
                # Record the expected domain decision under the same valid lease.
                rej_outcome = rejection.outcome
                if getattr(rej_outcome, 'search_snapshots', None) is None and getattr(outcome, 'search_snapshots', None) is not None:
                    from dataclasses import replace
                    rej_outcome = replace(rej_outcome, search_snapshots=outcome.search_snapshots)
                records = await self._io(self.queue.finish,claim,rej_outcome,spec=spec,
                                         invocation=invocation,registry=self.registry)
            for record in records:
                try:
                    log_history(logger, record)
                except Exception as log_exc:
                    self.logger.warning("Audit logging failed after commit for job %s: %s", claim.job_id, log_exc)
            return True
        except asyncio.CancelledError:
            # No invented completion. The recorded lease/start is recoverable.
            raise
        except CancellationRequested:
            if claim is not None:
                records=await self._io(CancellationStore(self.queue.database).acknowledge,claim)
                for record in records:
                    try:
                        log_history(self.logger, record)
                    except Exception as log_exc:
                        self.logger.warning("Audit logging failed for canceled job %s: %s", claim.job_id, log_exc)
            return True
        except LeaseLost as exc:
            self._log_error(exc,claim)
            return True
        except Exception as exc:
            self._log_error(exc,claim)
            raise
        finally:
            if operation_admission is not None:operation_admission.__exit__(None,None,None)
            admission.__exit__(None,None,None)

    async def _recover(self):
        try:
            with self.gate.operation():
                records = await self._io(self.queue.recover_expired)
                for record in records:
                    log_history(self.logger,record)
                from src.nextgen.storage.episode_reservations import (
                    reconcile_staged_waiters, reconcile_unreserved_waiters,
                )
                for record in await self._io(reconcile_staged_waiters,self.queue.database):
                    log_history(self.logger,record)
                for record in await self._io(reconcile_unreserved_waiters,self.queue.database):
                    log_history(self.logger,record)
        except (MaintenanceBusy, MaintenancePending):
            return

    async def run(self, *, on_ready=None):
        if self._running or self._closed:
            raise RuntimeError("worker cannot be started in its current state")
        self._running = True
        try:
            ready = False
            while not self._stop.is_set():
                worked = False
                if not self._paused:
                    await self._recover()
                    if not ready:
                        ready = True
                        if on_ready is not None:
                            on_ready()
                    while len(self._tasks) < self.concurrency and not self._stop.is_set():
                        task = asyncio.create_task(self.attempt())
                        self._tasks.add(task)
                if self._tasks:
                    done, _ = await asyncio.wait(self._tasks,timeout=self.policy.poll_ms/1000,
                                                 return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        self._tasks.remove(task)
                        worked = bool(task.result()) or worked
                if not worked:
                    try:
                        await asyncio.wait_for(self._stop.wait(),timeout=self.policy.poll_ms/1000)
                    except asyncio.TimeoutError:
                        pass
        except Exception as exc:
            self._log_error(exc)
            self.stop()
            raise
        finally:
            self.stop()
            # Drain even on failure/cancellation; retain admission until threads
            # are finished. No forced lease release or external replay here.
            tasks = tuple(self._tasks)
            try:
                if tasks:
                    await settle(tasks)
            finally:
                self._tasks.clear()
                self._running = False
                await self.close()


async def run_worker(config_directory, registrations, *, policy=Policy()):
    """Application/service entry point; registrations are trusted installed code."""
    store = ConfigurationStore(config_directory)
    startup = await blocking(store.read)
    worker = Worker(store,startup,Registry(registrations),policy=policy)
    await serve_worker(worker)


async def _systemd_watchdog(worker, interval):
    """Heartbeat from the worker event loop; a blocked loop cannot fake health."""
    while not worker._stop.is_set():
        try:
            await asyncio.wait_for(worker._stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            notify("WATCHDOG=1")


async def serve_worker(worker):
    """Run a composed worker with service signal handling and guaranteed close."""
    import signal
    loop = asyncio.get_running_loop()
    installed = []
    watchdog_task = None
    try:
        for selected in (signal.SIGTERM,signal.SIGINT):
            loop.add_signal_handler(selected,worker.stop)
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
        await worker.close()
