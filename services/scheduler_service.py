"""
Background scheduler (CR-005).

Runs one check round over all devices every `interval` seconds, starting right
away when the application starts. Rounds never overlap: if a round is still
running when the next one is due, the missed one is SKIPPED (not queued) and
the scheduler waits for the next due time. Other changes (log clean-up, daily
configuration export, history clean-up) can register their own jobs later;
for now there is one job: the check round.
"""
import asyncio
import contextlib
import logging
import time
from typing import Optional

log = logging.getLogger("mikrapp.scheduler")


class Scheduler:
    def __init__(self, device_service, interval: float, enabled: bool = True):
        self.device_service = device_service
        self.interval = interval
        self.enabled = enabled
        self._task: Optional[asyncio.Task] = None
        self.rounds_started = 0

    async def start(self):
        if not self.enabled:
            log.info("scheduler is switched off (MIKRAPP_SCHEDULER=off)")
            return
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="mikrapp-scheduler")
            log.info("scheduler started: check round every %s s", self.interval)

    async def stop(self):
        """Stop cleanly. Database writes are single transactions, so a round that is
        cut off in the middle leaves no half-written result."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        log.info("scheduler stopped")

    async def _run(self):
        loop = asyncio.get_running_loop()
        next_due = loop.time()                       # first round: immediately
        while True:
            delay = next_due - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            started = loop.time()
            await self._round()
            # Next due time = next multiple of the interval after `started`.
            # If the round overran, the missed slots are skipped, never queued.
            finished = loop.time()
            slots = int((finished - started) // self.interval) + 1
            next_due = started + slots * self.interval

    async def _round(self):
        self.rounds_started += 1
        t0 = time.monotonic()
        try:
            results = await self.device_service.check_all(full=False, trigger="scheduler")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("check round failed; the scheduler keeps running")
            return
        online = sum(1 for r in results if r.status == "online")
        log.info("check round done: %d devices, %d online, %d offline, %.1f s",
                 len(results), online, len(results) - online, time.monotonic() - t0)
