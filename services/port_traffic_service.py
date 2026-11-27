"""
Port traffic (CR-010).

During a check, the router reports the byte counters of the ports that are END A of a link.
From two consecutive readings we compute the average rate between them. A wrong number is
never produced:

  * the first reading has nothing to compare with        -> no rate
  * a counter that went DOWN (router restart/reset)       -> no rate for that interval,
                                                             the new value becomes the baseline
  * two readings less than MIN_SECONDS apart (a manual
    check right after a scheduled one)                    -> the second one is ignored (too noisy)
  * a port name the router does not have                  -> stored as "not found"
"""
import asyncio
from datetime import datetime
from typing import Dict, Iterable, Optional

from db.topology_repo import TopologyRepository
from models import PortCounter
from services.check_recorder import utc_now_iso

MIN_SECONDS = 2.0


def _t(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


class PortTrafficService:
    def __init__(self, topology_repo: TopologyRepository):
        self.repo = topology_repo

    def record_sync(self, device_id: int, requested: Iterable[str], counters: Dict[str, dict],
                    now_iso: Optional[str] = None):
        now = now_iso or utc_now_iso()
        for port in requested:
            reading = counters.get(port)
            prev = self.repo.get_counter(device_id, port)
            if reading is None:                                    # the router has no such port
                self.repo.upsert_counter(PortCounter(device_id, port, None, None, now, None, None, found=False))
                continue
            rx, tx = int(reading["rx"]), int(reading["tx"])
            rx_bps = tx_bps = None
            if prev is not None and prev.found and prev.rx_bytes is not None and prev.tx_bytes is not None:
                dt = (_t(now) - _t(prev.read_at)).total_seconds()
                if dt < MIN_SECONDS:
                    continue                                       # too close to the last reading
                if rx >= prev.rx_bytes and tx >= prev.tx_bytes:    # otherwise: counter reset -> no rate
                    rx_bps = (rx - prev.rx_bytes) * 8 / dt
                    tx_bps = (tx - prev.tx_bytes) * 8 / dt
            self.repo.upsert_counter(PortCounter(device_id, port, rx, tx, now, rx_bps, tx_bps, found=True))

    async def record(self, device_id: int, requested: Iterable[str], counters: Dict[str, dict],
                     now_iso: Optional[str] = None):
        await asyncio.to_thread(self.record_sync, device_id, list(requested), counters, now_iso)
