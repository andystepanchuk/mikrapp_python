from typing import List, Optional
from db.connection import Database
from models import Site


class SiteRepository:
    def __init__(self, db: Database):
        self.db = db

    def list_all(self) -> List[Site]:
        with self.db.connect() as conn:
            rows = conn.execute("""
                SELECT s.*, (SELECT COUNT(*) FROM devices d WHERE d.site_id = s.id) AS device_count
                FROM sites s ORDER BY s.name
            """).fetchall()
        return [Site(**dict(r)) for r in rows]

    def get(self, site_id: int) -> Optional[Site]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
        return Site(**dict(row)) if row else None

    def create(self, name: str, location: str = "", description: str = "") -> int:
        with self.db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO sites (name, location, description) VALUES (?,?,?)",
                (name, location, description),
            )
            return cur.lastrowid

    def update(self, site_id: int, name: str, location: str, description: str):
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE sites SET name=?, location=?, description=? WHERE id=?",
                (name, location, description, site_id),
            )

    def delete(self, site_id: int):
        with self.db.connect() as conn:
            conn.execute("DELETE FROM sites WHERE id=?", (site_id,))
