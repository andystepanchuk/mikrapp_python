"""
Central configuration. Everything is overridable via environment variables
so the same code runs in dev (laptop) and on a real server without edits.
"""
import os

AUTH_USER = os.environ.get("MIKRAPP_USER", "admin")
AUTH_PASS = os.environ.get("MIKRAPP_PASS", "admin")          # CHANGE THIS in production
SESSION_SECRET = os.environ.get("MIKRAPP_SECRET", "change-this-secret-key")

DB_PATH = os.environ.get("MIKRAPP_DB", "mikrapp.db")
BACKUP_DIR = os.environ.get("MIKRAPP_BACKUP_DIR", "backups")

# How many devices are polled/commanded in parallel. This is the main lever
# that replaces the old PHP one-device-at-a-time loop.
MAX_CONCURRENT_CHECKS = int(os.environ.get("MIKRAPP_CONCURRENCY", "20"))

# Per-device network timeouts (seconds)
PORT_PROBE_TIMEOUT = float(os.environ.get("MIKRAPP_PROBE_TIMEOUT", "0.6"))
API_TIMEOUT = float(os.environ.get("MIKRAPP_API_TIMEOUT", "6"))
SSH_TIMEOUT = float(os.environ.get("MIKRAPP_SSH_TIMEOUT", "8"))

CRON_KEY = os.environ.get("MIKRAPP_CRON_KEY", "change-me-cron")

# Max simultaneous operations sent through ONE agent router (ping / tunnel).
# Keeps a small MikroTik from being overwhelmed when it serves many hosts.
AGENT_CONCURRENCY = int(os.environ.get("MIKRAPP_AGENT_CONCURRENCY", "4"))

# How many agents a connection may pass through (server -> A1 -> A2 -> target = 2).
# This is a hard stop, not loop detection: a mis-configured loop simply hits it.
MAX_AGENT_DEPTH = int(os.environ.get("MIKRAPP_MAX_AGENT_DEPTH", "2"))

# Background scheduler (CR-005). Interval is in SECONDS and is also how often the
# dashboard asks whether a new check has finished. Lower bound protects the routers.
CHECK_INTERVAL = max(5, int(os.environ.get("MIKRAPP_CHECK_INTERVAL", "60")))
SCHEDULER_ENABLED = os.environ.get("MIKRAPP_SCHEDULER", "on").strip().lower() not in ("off", "0", "false", "no")

# Check history (CR-013). Every check writes one record to a log file (JSON lines, one file
# per UTC day) and to the database. Set MIKRAPP_CHECK_LOG=off to disable the file,
# MIKRAPP_CHECK_HISTORY=off to disable the database table.
_off = ("off", "0", "false", "no", "")
CHECK_LOG_DIR = os.environ.get("MIKRAPP_CHECK_LOG", "logs").strip()
CHECK_LOG_ENABLED = CHECK_LOG_DIR.lower() not in _off
CHECK_LOG_DAYS = int(os.environ.get("MIKRAPP_CHECK_LOG_DAYS", "30"))          # 0 = keep files forever
CHECK_HISTORY_ENABLED = os.environ.get("MIKRAPP_CHECK_HISTORY", "on").strip().lower() not in _off
CHECK_HISTORY_DAYS = int(os.environ.get("MIKRAPP_CHECK_HISTORY_DAYS", "90"))  # 0 = keep rows forever

# Device logs by syslog (CR-011). Routers send their log to this UDP port; see README.
SYSLOG_ENABLED = os.environ.get("MIKRAPP_SYSLOG", "on").strip().lower() not in _off
SYSLOG_HOST = os.environ.get("MIKRAPP_SYSLOG_HOST", "0.0.0.0")
SYSLOG_PORT = int(os.environ.get("MIKRAPP_SYSLOG_PORT", "5514"))     # 514 needs admin rights on most systems
SYSLOG_DAYS = int(os.environ.get("MIKRAPP_SYSLOG_DAYS", "90"))       # 0 = keep forever
