import json
from typing import Optional
from db.connection import Database


class TopologyRepository:
    """
    Stores the most recent topology scan as one cached row -- mirrors the
    old tool's single `topo_cache` JSON blob, just as a proper table
    instead of a key inside the giant file.
    """

    def __init__(self, db: Database):
        self.db = db

    def get_cache(self) -> Optional[dict]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT links, unmanaged, scanned_at FROM topology_cache WHERE id = 1"
            ).fetchone()
        if not row:
            return None
        return {
            "links": json.loads(row["links"]),
            "unmanaged": json.loads(row["unmanaged"]),
            "scanned_at": row["scanned_at"],
        }

    def save_cache(self, links: list, unmanaged: list, scanned_at: str):
        with self.db.connect() as conn:
            conn.execute("""
                INSERT INTO topology_cache (id, links, unmanaged, scanned_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    links = excluded.links,
                    unmanaged = excluded.unmanaged,
                    scanned_at = excluded.scanned_at
            """, (json.dumps(links), json.dumps(unmanaged), scanned_at))
