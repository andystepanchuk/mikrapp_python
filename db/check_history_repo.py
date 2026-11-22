import json
from typing import List

from db.connection import Database
from models import CheckRecord


def _to_record(row) -> CheckRecord:
    try:
        attempts = json.loads(row["attempts"] or "[]")
    except ValueError:
        attempts = []
    return CheckRecord(
        id=row["id"], device_id=row["device_id"], checked_at=row["checked_at"],
        device_name=row["device_name"] or "", ip=row["ip"] or "", route=row["route"] or "direct",
        trigger=row["trigger"] or "manual", status=row["status"],
        previous_status=row["previous_status"] or "", status_changed=bool(row["status_changed"]),
        latency_ms=row["latency_ms"], error=row["error"] or "",
        duration_ms=row["duration_ms"] or 0, attempts=attempts,
    )


class CheckHistoryRepository:
    """Every check ever made (until retention removes it). Written in batches."""

    def __init__(self, db: Database):
        self.db = db

    def add_many(self, records: List[CheckRecord]):
        if not records:
            return
        rows = [(r.device_id, r.checked_at, r.device_name, r.ip, r.route, r.trigger, r.status,
                 r.previous_status, 1 if r.status_changed else 0, r.latency_ms, r.error,
                 r.duration_ms, json.dumps(r.attempts, ensure_ascii=False)) for r in records]
        with self.db.connect() as conn:          # one transaction for the whole batch
            conn.executemany("""
                INSERT INTO check_history (device_id, checked_at, device_name, ip, route, trigger,
                    status, previous_status, status_changed, latency_ms, error, duration_ms, attempts)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, rows)

    def latest_for_device(self, device_id: int, limit: int = 50) -> List[CheckRecord]:
        with self.db.connect() as conn:
            rows = conn.execute("""
                SELECT * FROM check_history WHERE device_id=?
                ORDER BY checked_at DESC, id DESC LIMIT ?
            """, (device_id, limit)).fetchall()
        return [_to_record(r) for r in rows]

    def count(self) -> int:
        with self.db.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM check_history").fetchone()[0]

    def purge_older_than(self, cutoff_iso: str) -> int:
        with self.db.connect() as conn:
            return conn.execute("DELETE FROM check_history WHERE checked_at < ?", (cutoff_iso,)).rowcount
