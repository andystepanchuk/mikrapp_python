"""
Device logs by syslog (CR-011).

MikrApp listens on a UDP port. Routers send their log messages there. For every datagram:
  1. the sender's IP address is matched to a device (unknown senders are dropped and counted),
  2. the message is parsed (severity, topics, text),
  3. it is put on a queue - instantly. A background writer saves the queue in batches.

So a burst of messages or a slow database can never block the listener; when the queue is
full, messages are dropped and counted (shown on the log page). The time stored is the time
MikrApp RECEIVED the message, so the missing year in RouterOS log times does not matter.
"""
import asyncio
import contextlib
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from db.devices_repo import DeviceRepository
from db.logs_repo import LogRepository
from models import LogEntry
from services.check_recorder import utc_now_iso
from services.syslog_parser import parse_syslog

log = logging.getLogger("mikrapp.syslog")
MAX_UNKNOWN_SENDERS = 50


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, service: "LogService"):
        self.service = service

    def datagram_received(self, data, addr):
        self.service.handle_datagram(data, addr[0])

    def error_received(self, exc):
        log.warning("syslog socket error: %s", exc)


class LogService:
    def __init__(self, devices_repo: DeviceRepository, logs_repo: LogRepository, host: str,
                 port: int, days: int, enabled: bool = True, queue_size: int = 20000):
        self.devices_repo, self.logs_repo = devices_repo, logs_repo
        self.host, self.port, self.days, self.enabled = host, port, days, enabled
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self._devices: Dict[str, Tuple[int, str]] = {}      # sender IP -> (device id, name)
        self._refreshed = 0.0
        self._refreshing = False
        self._transport = None
        self._writer_task: Optional[asyncio.Task] = None
        self._write_failing = False
        # what the log page shows
        self.listening = False
        self.listen_error = ""
        self.bound_port: Optional[int] = None
        self.received = self.stored = self.dropped_unknown = self.dropped_full = 0
        self.unknown_senders: Dict[str, int] = {}

    # ---- lifecycle -------------------------------------------------------------------

    async def start(self):
        if not self.enabled:
            self.listen_error = "switched off (MIKRAPP_SYSLOG=off)"
            return
        await asyncio.to_thread(self.purge)
        await self._refresh()
        self._writer_task = asyncio.create_task(self._writer(), name="mikrapp-syslog-writer")
        loop = asyncio.get_running_loop()
        try:
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: _Protocol(self), local_addr=(self.host, self.port))
        except OSError as exc:
            # e.g. port already in use, or no permission: MikrApp must still start
            self.listen_error = f"cannot listen on UDP {self.host}:{self.port}: {exc}"
            log.error(self.listen_error)
            return
        self.bound_port = self._transport.get_extra_info("sockname")[1]
        self.listening = True
        log.info("syslog listener on UDP %s:%s", self.host, self.bound_port)

    async def stop(self):
        """Close the socket, write what is still queued (up to 5 s), stop the writer."""
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        self.listening = False
        task, self._writer_task = self._writer_task, None
        if task is None:
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._queue.join(), 5)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def flush(self):
        if self._writer_task is not None:
            await self._queue.join()

    # ---- the listener (runs in the event loop; must be instant) ------------------------

    def handle_datagram(self, data: bytes, ip: str):
        self.received += 1
        now = time.monotonic()
        known = self._devices.get(ip)
        if known is None:
            self.dropped_unknown += 1
            if ip in self.unknown_senders or len(self.unknown_senders) < MAX_UNKNOWN_SENDERS:
                self.unknown_senders[ip] = self.unknown_senders.get(ip, 0) + 1
            self._maybe_refresh(min_age=5)                 # a device may have been added just now
            return
        self._maybe_refresh(min_age=30)
        parsed = parse_syslog(data)
        entry = LogEntry(device_id=known[0], received_at=utc_now_iso(), device_name=known[1],
                         sender_ip=ip, severity=parsed.severity, severity_num=parsed.severity_num,
                         topics=parsed.topics, message=parsed.message)
        try:
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            self.dropped_full += 1
            if self.dropped_full == 1:
                log.warning("syslog queue is full; messages are being dropped")

    def _maybe_refresh(self, min_age: float):
        if not self._refreshing and time.monotonic() - self._refreshed >= min_age:
            self._refreshing = True
            asyncio.get_running_loop().create_task(self._refresh())

    async def _refresh(self):
        try:
            devices = await asyncio.to_thread(self.devices_repo.list_all)
            mapping: Dict[str, Tuple[int, str]] = {}
            for d in sorted(devices, key=lambda x: x.id):
                mapping.setdefault(d.ip, (d.id, d.display_name))   # same IP twice: lowest id wins
            self._devices = mapping
            self._refreshed = time.monotonic()
        except Exception:
            log.exception("could not refresh the device list for syslog matching")
        finally:
            self._refreshing = False

    # ---- the writer -------------------------------------------------------------------

    async def _writer(self):
        while True:
            batch: List[LogEntry] = [await self._queue.get()]
            while len(batch) < 500 and not self._queue.empty():
                batch.append(self._queue.get_nowait())
            try:
                await asyncio.to_thread(self.logs_repo.add_many, batch)
                self.stored += len(batch)
                if self._write_failing:
                    log.info("syslog: the database works again")
                self._write_failing = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._write_failing:                 # warn once per streak, not per batch
                    log.warning("syslog: saving messages failed (%s); they are lost", exc)
                self._write_failing = True
            finally:
                for _ in batch:
                    self._queue.task_done()

    # ---- retention ---------------------------------------------------------------------

    def purge(self) -> int:
        if self.days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.days)).isoformat(timespec="milliseconds")
        return self.logs_repo.purge_older_than(cutoff)

    async def purge_job(self):
        removed = await asyncio.to_thread(self.purge)
        if removed:
            log.info("syslog: deleted %d messages older than %d days", removed, self.days)

    # ---- for the log page ---------------------------------------------------------------

    def status(self) -> dict:
        return {
            "enabled": self.enabled, "listening": self.listening, "error": self.listen_error,
            "host": self.host, "port": self.bound_port or self.port,
            "received": self.received, "stored": self.stored,
            "dropped_unknown": self.dropped_unknown, "dropped_full": self.dropped_full,
            "unknown_senders": sorted(self.unknown_senders.items(), key=lambda kv: -kv[1])[:10],
        }
