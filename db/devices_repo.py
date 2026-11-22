from datetime import datetime, timezone
from typing import List, Optional
import config
from db.connection import Database
from models import Device

# One shared SELECT: device + its site name + (optional) agent's display name.
_SELECT = """
    SELECT devices.*, sites.name AS site_name,
           COALESCE(NULLIF(a.custom_identity, ''), NULLIF(a.identity, ''), a.ip, '') AS agent_name
    FROM devices
    JOIN sites ON sites.id = devices.site_id
    LEFT JOIN devices a ON a.id = devices.agent_id
"""


def _row_to_device(row) -> Device:
    d = dict(row)
    d["poe_ok"] = bool(d.get("poe_ok", 0))
    d["kind"] = d.get("kind") or "mikrotik"
    d["last_error"] = d.get("last_error") or ""
    site_name = d.pop("site_name", "")
    agent_name = d.pop("agent_name", "")
    dev = Device(**{k: v for k, v in d.items() if k in Device.__dataclass_fields__})
    dev.site_name = site_name or ""
    dev.agent_name = agent_name or ""
    return dev


class DeviceRepository:
    def __init__(self, db: Database):
        self.db = db

    def _fill_paths(self, devices: List[Device]) -> List[Device]:
        """Set `agent_path` ("A1 → A2", outermost agent first) for display."""
        if not any(d.agent_id for d in devices):
            return devices
        with self.db.connect() as conn:
            rows = conn.execute("""
                SELECT id, agent_id,
                       COALESCE(NULLIF(custom_identity, ''), NULLIF(identity, ''), ip) AS name
                FROM devices
            """).fetchall()
        info = {r["id"]: (r["name"], r["agent_id"]) for r in rows}
        limit = config.MAX_AGENT_DEPTH + 1       # one extra, so a too-deep chain stays visible
        for d in devices:
            parts, current = [], d.agent_id
            while current and len(parts) < limit and current in info:
                name, nxt = info[current]
                parts.append(name)
                current = nxt
            d.agent_path = " → ".join(reversed(parts))
        return devices

    def list_all(self, site_id: Optional[int] = None) -> List[Device]:
        query = _SELECT
        params = ()
        if site_id is not None:
            query += " WHERE devices.site_id = ?"
            params = (site_id,)
        query += " ORDER BY sites.name, devices.ip"
        with self.db.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return self._fill_paths([_row_to_device(r) for r in rows])

    def list_agent_candidates(self) -> List[Device]:
        """Devices that may act as an agent: any MikroTik with credentials (it may itself
        sit behind another agent). A device is excluded from its own list by the caller."""
        return [d for d in self.list_all() if d.kind != "generic" and d.password]

    def get(self, device_id: int) -> Optional[Device]:
        with self.db.connect() as conn:
            row = conn.execute(_SELECT + " WHERE devices.id = ?", (device_id,)).fetchone()
        if not row:
            return None
        return self._fill_paths([_row_to_device(row)])[0]

    def get_many(self, device_ids: List[int]) -> List[Device]:
        if not device_ids:
            return []
        placeholders = ",".join("?" * len(device_ids))
        with self.db.connect() as conn:
            rows = conn.execute(
                _SELECT + f" WHERE devices.id IN ({placeholders})", device_ids
            ).fetchall()
        return self._fill_paths([_row_to_device(r) for r in rows])

    def create(self, site_id: int, ip: str, username: str, password: str,
               protocol: str = "auto", port: int = 22, api_port: int = 8728,
               kind: str = "mikrotik", agent_id: Optional[int] = None,
               custom_identity: str = "") -> int:
        with self.db.connect() as conn:
            cur = conn.execute("""
                INSERT INTO devices (site_id, ip, username, password, protocol, port, api_port,
                                     kind, agent_id, custom_identity)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (site_id, ip, username, password, protocol, port, api_port,
                  kind, agent_id, custom_identity))
            return cur.lastrowid

    def update_connection(self, device_id: int, site_id: int, ip: str, username: str,
                           password: str, protocol: str, port: int, api_port: int,
                           custom_identity: str, kind: str = "mikrotik",
                           agent_id: Optional[int] = None):
        with self.db.connect() as conn:
            conn.execute("""
                UPDATE devices SET site_id=?, ip=?, username=?, password=?, protocol=?,
                       port=?, api_port=?, custom_identity=?, kind=?, agent_id=?
                WHERE id=?
            """, (site_id, ip, username, password, protocol, port, api_port,
                  custom_identity, kind, agent_id, device_id))

    def delete(self, device_id: int):
        with self.db.connect() as conn:
            # devices that used this one as their agent become direct again
            conn.execute("UPDATE devices SET agent_id=NULL WHERE agent_id=?", (device_id,))
            conn.execute("DELETE FROM devices WHERE id=?", (device_id,))

    def set_status(self, device_id: int, status: str, error: str = ""):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.db.connect() as conn:
            if status == "online":
                conn.execute(
                    "UPDATE devices SET status=?, last_seen=?, last_check_at=?, offline_since=NULL, "
                    "last_error=? WHERE id=?",
                    (status, now, now, error, device_id),
                )
            else:
                conn.execute("""
                    UPDATE devices SET status=?, last_error=?, last_check_at=?,
                           offline_since = COALESCE(offline_since, ?)
                    WHERE id=?
                """, (status, error, now, now, device_id))

    def update_metrics(self, device_id: int, data: dict):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        fields, values = [], []
        allowed = ("identity", "routeros_version", "board", "cpu_usage", "ram_usage",
                   "temperature", "voltage", "uptime", "poe_ok", "poe_power",
                   "poe_count", "poe_ports", "latency_ms")
        for key in allowed:
            if key in data:
                fields.append(f"{key}=?")
                values.append(data[key])
        if "poe_ok" in data or "poe_power" in data:
            fields.append("poe_updated=?")
            values.append(now)
        fields += ["status=?", "last_seen=?", "last_check_at=?", "offline_since=NULL", "last_error=?"]
        values += ["online", now, now, data.get("last_error", "")]
        values.append(device_id)
        with self.db.connect() as conn:
            conn.execute(f"UPDATE devices SET {', '.join(fields)} WHERE id=?", values)

    def get_status(self, device_id: int) -> Optional[str]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT status FROM devices WHERE id=?", (device_id,)).fetchone()
        return row["status"] if row else None

    def last_check_stamp(self) -> str:
        """Time of the most recent check of any device ('' if none yet).
        The dashboard compares it to decide whether a reload is needed."""
        with self.db.connect() as conn:
            row = conn.execute("SELECT MAX(last_check_at) AS stamp FROM devices").fetchone()
        return row["stamp"] or ""

    def stats(self) -> dict:
        with self.db.connect() as conn:
            row = conn.execute("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status='online' THEN 1 ELSE 0 END) AS online,
                    SUM(CASE WHEN status='offline' THEN 1 ELSE 0 END) AS offline,
                    AVG(CASE WHEN status='online' AND COALESCE(kind,'mikrotik') != 'generic'
                             THEN cpu_usage END) AS avg_cpu
                FROM devices
            """).fetchone()
        d = dict(row)
        d["total"] = d["total"] or 0
        d["online"] = d["online"] or 0
        d["offline"] = d["offline"] or 0
        d["avg_cpu"] = round(d["avg_cpu"] or 0, 1)
        return d
