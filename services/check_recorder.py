"""
Check history (CR-013): where the record of every check goes.

A finished check hands its CheckRecord to `record()`, which only puts it on a
queue (instant, never blocks, never fails). One background writer takes records
off the queue in batches and writes them to two independent sinks:

  1. the log file   - one JSON object per line, one file per UTC day
                      (logs/checks-YYYY-MM-DD.jsonl), old files deleted after N days
  2. the database   - table check_history, old rows deleted after N days

So a slow or broken disk or database can never delay or stop a check, and a failure
of one sink does not stop the other. A failure is logged once per streak, not per line.
"""
import asyncio
import contextlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from db.check_history_repo import CheckHistoryRepository
from models import CheckRecord

log = logging.getLogger("mikrapp.history")
_FILE_RE = re.compile(r"^checks-(\d{4}-\d{2}-\d{2})\.jsonl$")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class CheckRecorder:
    def __init__(self, log_dir: Optional[str], log_days: int,
                 history_repo: Optional[CheckHistoryRepository], history_days: int,
                 queue_size: int = 50000):
        self.log_dir = log_dir or None
        self.log_days = log_days
        self.history_repo = history_repo
        self.history_days = history_days
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self._task: Optional[asyncio.Task] = None
        self._failing: Dict[str, bool] = {}        # sink name -> currently failing?
        self._purged_on: Optional[str] = None
        self.dropped = 0

    # ---- called by the checks (never blocks, never raises) ----------------------

    def record(self, rec: CheckRecord):
        try:
            self._queue.put_nowait(rec)
        except asyncio.QueueFull:
            self.dropped += 1
            if self.dropped == 1:
                log.warning("check history queue is full; records are being dropped")

    # ---- lifecycle ---------------------------------------------------------------

    async def start(self):
        if self._task is None:
            await asyncio.to_thread(self.purge)
            self._purged_on = datetime.now(timezone.utc).date().isoformat()
            self._task = asyncio.create_task(self._writer(), name="mikrapp-check-recorder")

    async def flush(self):
        """Wait until everything queued so far has been written."""
        if self._task is not None:
            await self._queue.join()

    async def stop(self):
        """Write what is still queued (up to 5 s), then stop the writer."""
        task, self._task = self._task, None
        if task is None:
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._queue.join(), 5)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # ---- the writer ----------------------------------------------------------------

    async def _writer(self):
        while True:
            batch: List[CheckRecord] = [await self._queue.get()]
            while len(batch) < 500 and not self._queue.empty():
                batch.append(self._queue.get_nowait())
            try:
                await asyncio.to_thread(self._write_batch, batch)
                today = datetime.now(timezone.utc).date().isoformat()
                if today != self._purged_on:               # a new UTC day: clean up old data
                    self._purged_on = today
                    await asyncio.to_thread(self.purge)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("check history writer error")
            finally:
                for _ in batch:
                    self._queue.task_done()

    def _write_batch(self, batch: List[CheckRecord]):
        if self.log_dir:
            self._guard("log file", lambda: self._write_file(batch))
        if self.history_repo:
            self._guard("database", lambda: self.history_repo.add_many(batch))

    def _guard(self, sink: str, action):
        try:
            action()
        except Exception as exc:
            if not self._failing.get(sink):                # warn once per streak, not per line
                log.warning("check history: writing to the %s failed (%s); checks go on", sink, exc)
            self._failing[sink] = True
        else:
            if self._failing.get(sink):
                log.info("check history: the %s works again", sink)
            self._failing[sink] = False

    def _write_file(self, batch: List[CheckRecord]):
        os.makedirs(self.log_dir, exist_ok=True)
        by_day: Dict[str, List[str]] = {}
        for rec in batch:
            day = rec.checked_at[:10]                      # UTC date -> daily rotation
            by_day.setdefault(day, []).append(
                json.dumps(rec.as_dict(), ensure_ascii=False, separators=(",", ":")))
        for day, lines in by_day.items():
            with open(os.path.join(self.log_dir, f"checks-{day}.jsonl"), "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")

    # ---- retention -------------------------------------------------------------------

    def purge(self):
        """Delete log files and database rows older than their retention. Safe to call any time."""
        now = datetime.now(timezone.utc)
        if self.log_dir and self.log_days > 0 and os.path.isdir(self.log_dir):
            limit = (now - timedelta(days=self.log_days)).date().isoformat()
            for name in os.listdir(self.log_dir):
                m = _FILE_RE.match(name)
                if m and m.group(1) < limit:
                    self._guard("log file clean-up", lambda n=name: os.remove(os.path.join(self.log_dir, n)))
        if self.history_repo and self.history_days > 0:
            cutoff = (now - timedelta(days=self.history_days)).isoformat(timespec="milliseconds")
            self._guard("database clean-up", lambda: self.history_repo.purge_older_than(cutoff))
