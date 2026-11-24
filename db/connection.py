"""
Thin SQLite wrapper. Replaces the old Storage class that rewrote one giant
JSON file on every single change -- here each write is a targeted SQL
statement, so cost no longer grows with the size of the whole dataset.
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    location    TEXT DEFAULT '',
    description TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS devices (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id           INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    ip                TEXT NOT NULL,
    username          TEXT DEFAULT 'admin',
    password          TEXT DEFAULT '',
    protocol          TEXT DEFAULT 'auto',
    port              INTEGER DEFAULT 22,
    api_port          INTEGER DEFAULT 8728,
    poll_timeout      INTEGER DEFAULT 8,

    identity          TEXT DEFAULT '',
    custom_identity   TEXT DEFAULT '',
    status            TEXT DEFAULT 'checking',
    routeros_version  TEXT DEFAULT '',
    board             TEXT DEFAULT '',

    cpu_usage         REAL DEFAULT 0,
    ram_usage         REAL DEFAULT 0,
    temperature       REAL,
    voltage           REAL,
    uptime            TEXT DEFAULT '',

    last_seen         TEXT,
    offline_since     TEXT,

    poe_ok            INTEGER DEFAULT 0,
    poe_power         REAL,
    poe_count         INTEGER,
    poe_ports         INTEGER,
    poe_updated       TEXT,

    kind              TEXT DEFAULT 'mikrotik',
    agent_id          INTEGER,
    latency_ms        REAL,
    last_error        TEXT DEFAULT '',
    last_check_at     TEXT
);

CREATE TABLE IF NOT EXISTS templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    category    TEXT DEFAULT 'Custom',
    description TEXT DEFAULT '',
    commands    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    filename    TEXT NOT NULL,
    path        TEXT NOT NULL,
    size        INTEGER DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topology_cache (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    links       TEXT NOT NULL DEFAULT '[]',
    unmanaged   TEXT NOT NULL DEFAULT '[]',
    scanned_at  TEXT
);

-- History of every check. No foreign key on purpose: the history of a deleted device
-- is kept, and each row carries the device's name and address as they were.
CREATE TABLE IF NOT EXISTS check_history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id        INTEGER NOT NULL,
    checked_at       TEXT NOT NULL,
    device_name      TEXT DEFAULT '',
    ip               TEXT DEFAULT '',
    route            TEXT DEFAULT 'direct',
    trigger          TEXT DEFAULT 'manual',
    status           TEXT NOT NULL,
    previous_status  TEXT DEFAULT '',
    status_changed   INTEGER DEFAULT 0,
    latency_ms       REAL,
    error            TEXT DEFAULT '',
    duration_ms      INTEGER DEFAULT 0,
    attempts         TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_check_history_device ON check_history(device_id, checked_at);
CREATE INDEX IF NOT EXISTS idx_check_history_time ON check_history(checked_at);

-- Log messages routers send by syslog. No foreign key: the logs of a deleted device are
-- kept (each row carries the device's name as it was).
CREATE TABLE IF NOT EXISTS logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id     INTEGER NOT NULL,
    received_at   TEXT NOT NULL,
    device_name   TEXT DEFAULT '',
    sender_ip     TEXT DEFAULT '',
    severity      TEXT DEFAULT 'info',
    severity_num  INTEGER DEFAULT 6,
    topics        TEXT DEFAULT '',
    message       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_device ON logs(device_id, id);
CREATE INDEX IF NOT EXISTS idx_logs_time ON logs(received_at);

CREATE INDEX IF NOT EXISTS idx_devices_site ON devices(site_id);
CREATE INDEX IF NOT EXISTS idx_backups_device ON backups(device_id);
"""

DEFAULT_TEMPLATES = [
    ("System status", "System", "Resource, health, identity",
     "/system/resource/print\n/system/health/print\n/system/identity/print"),
    ("Full export", "Backup", "Compact configuration export", "/export compact"),
    ("Interfaces overview", "Network", "All interfaces + ethernet",
     "/interface/print\n/interface/ethernet/print"),
    ("IP & routes", "Network", "Addresses, routes, DNS",
     "/ip/address/print\n/ip/route/print\n/ip/dns/print"),
    ("DHCP leases", "DHCP", "Active DHCP leases", "/ip/dhcp-server/lease/print"),
    ("Firewall snapshot", "Security", "Filter + NAT rules",
     "/ip/firewall/filter/print\n/ip/firewall/nat/print"),
    ("Neighbors", "Discovery", "LLDP/NDP neighbors", "/ip/neighbor/print"),
    ("Recent logs", "Logs", "System log without paging", "/log/print without-paging"),
]


class Database:
    def __init__(self, path: str = "mikrapp.db"):
        self.path = path
        self._init_schema()
        self._seed_defaults()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")   # readers don't block the writer
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self):
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn):
        """Add columns introduced after the first release to existing databases."""
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(devices)")}
        wanted = (
            ("kind", "TEXT DEFAULT 'mikrotik'"),
            ("agent_id", "INTEGER"),
            ("latency_ms", "REAL"),
            ("last_error", "TEXT DEFAULT ''"),
            ("last_check_at", "TEXT"),
        )
        for name, ddl in wanted:
            if name not in existing:
                conn.execute(f"ALTER TABLE devices ADD COLUMN {name} {ddl}")

    def _seed_defaults(self):
        with self.connect() as conn:
            if conn.execute("SELECT COUNT(*) FROM sites").fetchone()[0] == 0:
                conn.execute("INSERT INTO sites (name, location) VALUES ('Default', '')")
            if conn.execute("SELECT COUNT(*) FROM templates").fetchone()[0] == 0:
                conn.executemany(
                    "INSERT INTO templates (name, category, description, commands) VALUES (?,?,?,?)",
                    DEFAULT_TEMPLATES,
                )
