from typing import List, Optional

from db.connection import Database
from models import LogEntry


def _like(value: str) -> str:
    """Escape LIKE wildcards so a search for '50%' means exactly that."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _to_entry(row) -> LogEntry:
    return LogEntry(id=row["id"], device_id=row["device_id"], received_at=row["received_at"],
                    device_name=row["device_name"] or "", sender_ip=row["sender_ip"] or "",
                    severity=row["severity"] or "info", severity_num=row["severity_num"],
                    topics=row["topics"] or "", message=row["message"])


class LogRepository:
    def __init__(self, db: Database):
        self.db = db

    def add_many(self, entries: List[LogEntry]):
        if not entries:
            return
        rows = [(e.device_id, e.received_at, e.device_name, e.sender_ip, e.severity,
                 e.severity_num, e.topics, e.message) for e in entries]
        with self.db.connect() as conn:          # one transaction per batch
            conn.executemany("""
                INSERT INTO logs (device_id, received_at, device_name, sender_ip, severity,
                                  severity_num, topics, message)
                VALUES (?,?,?,?,?,?,?,?)
            """, rows)

    def search(self, device_id: Optional[int] = None, max_severity: Optional[int] = None,
               topic: str = "", text: str = "", since_iso: Optional[str] = None,
               before_id: Optional[int] = None, limit: int = 200) -> List[LogEntry]:
        """Newest first. `max_severity` 4 means 'warning or worse'. `before_id` pages backwards."""
        where, params = [], []
        if device_id:
            where.append("device_id = ?"); params.append(device_id)
        if max_severity is not None:
            where.append("severity_num <= ?"); params.append(max_severity)
        if topic:
            # whole-word match on the comma separated list: 'account' must not match 'accounting'
            where.append("(',' || topics || ',') LIKE ? ESCAPE '\\'")
            params.append(f"%,{_like(topic.lower())},%")
        if text:
            where.append("message LIKE ? ESCAPE '\\'"); params.append(f"%{_like(text)}%")
        if since_iso:
            where.append("received_at >= ?"); params.append(since_iso)
        if before_id:
            where.append("id < ?"); params.append(before_id)
        sql = "SELECT * FROM logs" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 2000)))
        with self.db.connect() as conn:
            return [_to_entry(r) for r in conn.execute(sql, params).fetchall()]

    def latest_for_device(self, device_id: int, limit: int = 50) -> List[LogEntry]:
        return self.search(device_id=device_id, limit=limit)

    def count(self) -> int:
        with self.db.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]

    def purge_older_than(self, cutoff_iso: str) -> int:
        with self.db.connect() as conn:
            return conn.execute("DELETE FROM logs WHERE received_at < ?", (cutoff_iso,)).rowcount
