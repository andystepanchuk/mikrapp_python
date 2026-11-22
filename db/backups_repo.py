from typing import List, Optional
from db.connection import Database
from models import BackupFile


class BackupRepository:
    def __init__(self, db: Database):
        self.db = db

    def add(self, device_id: int, filename: str, path: str, size: int, created_at: str) -> int:
        with self.db.connect() as conn:
            cur = conn.execute("""
                INSERT INTO backups (device_id, filename, path, size, created_at)
                VALUES (?,?,?,?,?)
            """, (device_id, filename, path, size, created_at))
            return cur.lastrowid

    def get(self, backup_id: int) -> Optional[BackupFile]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT id, device_id, filename, path, size, created_at FROM backups WHERE id=?",
                (backup_id,),
            ).fetchone()
        return BackupFile(**dict(row)) if row else None

    def list_for_device(self, device_id: int) -> List[BackupFile]:
        with self.db.connect() as conn:
            rows = conn.execute("""
                SELECT id, device_id, filename, path, size, created_at FROM backups
                WHERE device_id=? ORDER BY created_at DESC
            """, (device_id,)).fetchall()
        return [BackupFile(**dict(r)) for r in rows]

    def list_recent(self, limit: int = 50) -> List[BackupFile]:
        with self.db.connect() as conn:
            rows = conn.execute("""
                SELECT id, device_id, filename, path, size, created_at FROM backups
                ORDER BY created_at DESC LIMIT ?
            """, (limit,)).fetchall()
        return [BackupFile(**dict(r)) for r in rows]
