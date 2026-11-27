from typing import Dict, List, Optional, Tuple

from db.connection import Database
from models import PortCounter, TopologyLink


class TopologyRepository:
    """Links between devices, saved device positions, and the latest port readings."""

    def __init__(self, db: Database):
        self.db = db

    # ---- links -------------------------------------------------------------------

    def list_links(self) -> List[TopologyLink]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM topology_links ORDER BY id").fetchall()
        return [TopologyLink(r["id"], r["a_device_id"], r["a_port"] or "", r["b_device_id"], r["b_port"] or "")
                for r in rows]

    def get_link(self, link_id: int) -> Optional[TopologyLink]:
        with self.db.connect() as conn:
            r = conn.execute("SELECT * FROM topology_links WHERE id=?", (link_id,)).fetchone()
        return TopologyLink(r["id"], r["a_device_id"], r["a_port"] or "", r["b_device_id"], r["b_port"] or "") if r else None

    def add_link(self, a_device_id: int, a_port: str, b_device_id: int, b_port: str) -> int:
        with self.db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO topology_links (a_device_id, a_port, b_device_id, b_port) VALUES (?,?,?,?)",
                (a_device_id, a_port, b_device_id, b_port))
            return cur.lastrowid

    def update_link(self, link_id: int, a_device_id: int, a_port: str, b_device_id: int, b_port: str):
        with self.db.connect() as conn:
            conn.execute("UPDATE topology_links SET a_device_id=?, a_port=?, b_device_id=?, b_port=? WHERE id=?",
                         (a_device_id, a_port, b_device_id, b_port, link_id))

    def delete_link(self, link_id: int):
        with self.db.connect() as conn:
            conn.execute("DELETE FROM topology_links WHERE id=?", (link_id,))

    def end_a_ports(self, device_id: int) -> List[str]:
        """Ports of this device that are end A of some link: the only ports whose counters we read."""
        with self.db.connect() as conn:
            rows = conn.execute("""
                SELECT DISTINCT a_port FROM topology_links
                WHERE a_device_id=? AND a_port != '' ORDER BY a_port
            """, (device_id,)).fetchall()
        return [r["a_port"] for r in rows]

    # ---- positions -----------------------------------------------------------------

    def positions(self) -> Dict[int, Tuple[int, int]]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT device_id, x, y FROM topology_positions").fetchall()
        return {r["device_id"]: (r["x"], r["y"]) for r in rows}

    def save_positions(self, positions: Dict[int, Tuple[int, int]]):
        if not positions:
            return
        with self.db.connect() as conn:
            conn.executemany("""
                INSERT INTO topology_positions (device_id, x, y) VALUES (?,?,?)
                ON CONFLICT(device_id) DO UPDATE SET x=excluded.x, y=excluded.y
            """, [(d, int(x), int(y)) for d, (x, y) in positions.items()])

    def clear_positions(self):
        with self.db.connect() as conn:
            conn.execute("DELETE FROM topology_positions")

    # ---- port readings ----------------------------------------------------------------

    def get_counter(self, device_id: int, port: str) -> Optional[PortCounter]:
        with self.db.connect() as conn:
            r = conn.execute("SELECT * FROM port_counters WHERE device_id=? AND port=?", (device_id, port)).fetchone()
        return _counter(r) if r else None

    def counters(self) -> Dict[Tuple[int, str], PortCounter]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM port_counters").fetchall()
        return {(r["device_id"], r["port"]): _counter(r) for r in rows}

    def upsert_counter(self, c: PortCounter):
        with self.db.connect() as conn:
            conn.execute("""
                INSERT INTO port_counters (device_id, port, rx_bytes, tx_bytes, read_at, rx_bps, tx_bps, found)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(device_id, port) DO UPDATE SET
                    rx_bytes=excluded.rx_bytes, tx_bytes=excluded.tx_bytes, read_at=excluded.read_at,
                    rx_bps=excluded.rx_bps, tx_bps=excluded.tx_bps, found=excluded.found
            """, (c.device_id, c.port, c.rx_bytes, c.tx_bytes, c.read_at, c.rx_bps, c.tx_bps, 1 if c.found else 0))


def _counter(r) -> PortCounter:
    return PortCounter(device_id=r["device_id"], port=r["port"], rx_bytes=r["rx_bytes"], tx_bytes=r["tx_bytes"],
                       read_at=r["read_at"], rx_bps=r["rx_bps"], tx_bps=r["tx_bps"], found=bool(r["found"]))
