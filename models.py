"""
Plain dataclasses describing the domain. No SQL, no HTTP, no network code
lives here on purpose -- these objects are what every other layer passes
around, so keeping them dumb keeps the rest of the app testable.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Site:
    id: int
    name: str
    location: str = ""
    description: str = ""
    device_count: int = 0


@dataclass
class Device:
    id: int
    site_id: int
    ip: str
    username: str = "admin"
    password: str = ""
    protocol: str = "auto"        # auto | api | ssh
    port: int = 22                # SSH port
    api_port: int = 8728          # RouterOS API port
    poll_timeout: int = 8

    identity: str = ""
    custom_identity: str = ""
    status: str = "checking"      # checking | online | offline
    routeros_version: str = ""
    board: str = ""

    cpu_usage: float = 0.0
    ram_usage: float = 0.0
    temperature: Optional[float] = None
    voltage: Optional[float] = None
    uptime: str = ""

    last_seen: Optional[str] = None
    last_check_at: Optional[str] = None   # time of the last check, whatever its outcome
    offline_since: Optional[str] = None

    poe_ok: bool = False
    poe_power: Optional[float] = None
    poe_count: Optional[int] = None
    poe_ports: Optional[int] = None
    poe_updated: Optional[str] = None

    # Agent support: a device can be reached THROUGH another (agent) MikroTik.
    # kind "generic" = not a MikroTik, only pinged (via its agent).
    kind: str = "mikrotik"        # mikrotik | generic
    agent_id: Optional[int] = None
    latency_ms: Optional[float] = None
    last_error: str = ""

    site_name: str = ""           # denormalized for display only
    agent_name: str = ""          # denormalized for display only (immediate agent)
    agent_path: str = ""          # denormalized for display only, e.g. "A1 → A2"

    @property
    def display_name(self) -> str:
        return self.custom_identity or self.identity or self.ip


@dataclass
class CommandTemplate:
    id: int
    name: str
    category: str = "Custom"
    description: str = ""
    commands: str = ""            # newline-separated RouterOS commands


@dataclass
class BackupFile:
    id: int
    device_id: int
    filename: str
    path: str
    size: int
    created_at: str


@dataclass
class CheckResult:
    device_id: int
    status: str                   # online | offline
    data: dict = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class CheckRecord:
    """One check of one device: what was asked, how, and what came out."""
    device_id: int
    checked_at: str                      # UTC, ISO 8601, milliseconds
    device_name: str
    ip: str
    route: str                           # "direct" or the agent path, e.g. "A1 \u2192 A2"
    trigger: str                         # "manual" (a button) or "scheduler"
    status: str                          # online | offline | error
    previous_status: str
    status_changed: bool
    latency_ms: Optional[float]          # ping time; only for hosts pinged through an agent
    error: str
    duration_ms: int                     # how long the whole check took
    attempts: list = field(default_factory=list)   # [{"step","ok","ms","error"}, ...]
    id: int = 0

    def as_dict(self) -> dict:
        return {
            "ts": self.checked_at, "device_id": self.device_id, "name": self.device_name,
            "ip": self.ip, "route": self.route, "trigger": self.trigger,
            "status": self.status, "previous_status": self.previous_status,
            "status_changed": self.status_changed, "latency_ms": self.latency_ms,
            "error": self.error, "duration_ms": self.duration_ms, "attempts": self.attempts,
        }

    @property
    def attempts_text(self) -> str:
        """Short human form: 'api failed 6012 ms (timed out) \u2192 ssh ok 812 ms'."""
        parts = []
        for a in self.attempts:
            text = f"{a.get('step', '?')} {'ok' if a.get('ok') else 'failed'} {a.get('ms', 0)} ms"
            if a.get("error"):
                text += f" ({a['error']})"
            parts.append(text)
        return " \u2192 ".join(parts)


@dataclass
class LogEntry:
    """One log message a router sent to MikrApp by syslog."""
    device_id: int
    received_at: str                     # when MikrApp received it (UTC, ISO 8601, milliseconds)
    device_name: str
    sender_ip: str
    severity: str                        # emergency .. debug
    severity_num: int                    # 0 (worst) .. 7
    topics: str                          # e.g. "system,info,account"
    message: str
    id: int = 0


@dataclass
class TopologyLink:
    """A connection you drew: DeviceA.port -> DeviceB.port. Ports are optional."""
    id: int
    a_device_id: int
    a_port: str
    b_device_id: int
    b_port: str


@dataclass
class PortCounter:
    """Latest reading of one port (the port at end A of a link)."""
    device_id: int
    port: str
    rx_bytes: Optional[int]
    tx_bytes: Optional[int]
    read_at: str
    rx_bps: Optional[float]
    tx_bps: Optional[float]
    found: bool = True
