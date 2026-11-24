"""
Background scheduler (CR-005, extended by CR-011).

Main job: one check round over all devices every `interval` seconds, starting right
away when the application starts. Other parts register their own periodic jobs with
`add_job()` (for example the clean-up of old device logs).

Every job runs on its own loop and never overlaps itself: if a run is still going when the
next one is due, the missed slot is SKIPPED (not queued). A job that crashes is logged and
tried again at its next slot; it never stops the scheduler or the other jobs.
"""
import asyncio
import contextlib
import logging
import time
from typing import Awaitable, Callable, List, Optional, Tuple

log = logging.getLogger("mikrapp.scheduler")


class Scheduler:
    def __init__(self, device_service, interval: float, enabled: bool = True):
        self.device_service = device_service
        self.interval = interval
        self.enabled = enabled
        self._task: Optional[asyncio.Task] = None            # the check round
        self._job_tasks: List[asyncio.Task] = []              # extra jobs
        self._jobs: List[Tuple[str, Callable[[], Awaitable], float, bool]] = []
        self.rounds_started = 0

    def add_job(self, name: str, func: Callable[[], Awaitable], interval: float, run_now: bool = True):
        """Register a periodic job (an async function). Call before start()."""
        self._jobs.append((name, func, interval, run_now))

    async def start(self):
        if not self.enabled:
            log.info("scheduler is switched off (MIKRAPP_SCHEDULER=off)")
            return
        if self._task is None:
            self._task = asyncio.create_task(
                self._loop("check round", self._round, self.interval, True), name="mikrapp-scheduler")
            for name, func, interval, run_now in self._jobs:
                self._job_tasks.append(asyncio.create_task(
                    self._loop(name, func, interval, run_now), name=f"mikrapp-job-{name}"))
            log.info("scheduler started: check round every %s s, %d extra job(s)",
                     self.interval, len(self._jobs))

    async def stop(self):
        """Stop cleanly. Database writes are single transactions, so a run that is cut off
        in the middle leaves no half-written result."""
        tasks = ([self._task] if self._task else []) + self._job_tasks
        self._task, self._job_tasks = None, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if tasks:
            log.info("scheduler stopped")

    async def _loop(self, name: str, func, interval: float, run_now: bool):
        loop = asyncio.get_running_loop()
        next_due = loop.time() + (0 if run_now else interval)
        while True:
            delay = next_due - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            started = loop.time()
            try:
                await func()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("job '%s' failed; it will run again at its next slot", name)
            # Next due time = next multiple of the interval after `started`.
            # If the run overran, the missed slots are skipped, never queued.
            finished = loop.time()
            slots = int((finished - started) // interval) + 1
            next_due = started + slots * interval

    async def _round(self):
        self.rounds_started += 1
        t0 = time.monotonic()
        results = await self.device_service.check_all(full=False, trigger="scheduler")
        online = sum(1 for r in results if r.status == "online")
        log.info("check round done: %d devices, %d online, %d offline, %.1f s",
                 len(results), online, len(results) - online, time.monotonic() - t0)
