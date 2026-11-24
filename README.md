# MikrApp (Python rewrite)

Async rewrite of the original PHP Mikrotik monitoring tool. The goal of
this pass was to fix the actual cause of the slowness (fully sequential,
blocking device polling + whole-file JSON rewrites on every save) with a
clean, layered architecture -- not just translate PHP to Python 1:1.

## Why this is faster

The old tool checked devices one at a time (`fsockopen` + shell SSH calls
in a loop, with an 80ms sleep between each) and rewrote one big JSON file
on every change. Here:

- **Devices are checked concurrently** (`services/device_service.py`),
  bounded by a semaphore (`MIKRAPP_CONCURRENCY`, default 20) so N devices
  take roughly as long as the *slowest* device, not the sum of all of them.
- **The RouterOS binary API is used directly** over asyncio streams
  (`mikrotik/routeros_api.py`) instead of spawning an SSH process per
  command. SSH (via `asyncssh`, also async) is only a fallback for
  devices where the API is disabled.
- **SQLite replaces the single JSON blob** -- every write is a targeted
  `UPDATE`/`INSERT`, so cost doesn't scale with total dataset size.

## Architecture

```
config.py            - all settings, env-overridable
models.py             - plain dataclasses (Device, Site, CommandTemplate, ...)
db/
  connection.py        - SQLite connection + schema
  sites_repo.py         - SiteRepository
  devices_repo.py        - DeviceRepository
  templates_repo.py       - TemplateRepository (command templates)
  backups_repo.py          - BackupRepository (metadata; files live on disk)
mikrotik/
  routeros_api.py       - async RouterOS binary API client (port 8728)
  ssh_client.py           - async SSH fallback (asyncssh)
  client.py                - MikrotikClient facade: picks API/SSH, parses metrics
services/
  auth_service.py        - login check
  device_service.py       - check_device / check_all / run_commands (the concurrency fix)
  backup_service.py        - backup_device / backup_many
routers/                - FastAPI route handlers, one file per area
  auth.py, dashboard.py, sites.py, devices.py, commands.py, backups.py
templates/              - Jinja2 HTML, no logic beyond loops/ifs
static/                 - style.css, app.js
app.py                  - FastAPI app factory, wires everything together
```

Each layer only knows about the one below it: routers call services,
services call repositories and the Mikrotik client, repositories talk to
SQLite. Nothing reaches "up" a layer, which is what makes it possible to
add a feature (say, a new page) without touching the polling code, or
change the storage engine without touching a single router.

## Setup

```bash
cd mikrapp
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

export MIKRAPP_USER=admin
export MIKRAPP_PASS=change-me
export MIKRAPP_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")

uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

Open http://localhost:8000, log in, add a site, then add or mass-add
devices. Default template's Storage username/password are `admin`/`admin`
(via env vars above) -- change `MIKRAPP_PASS` before exposing this beyond
your laptop.

## What's included in this pass

- Auth (session cookie), Sites CRUD, Devices CRUD + mass-add
- Concurrent "Check all" / "Check one" against real devices (API or SSH)
- Command templates: save reusable RouterOS command sets, run them against
  any selection of devices concurrently, see combined output
- Config backups: export + store `.rsc` files per device, download them
- **Topology view**: concurrent MNDP/LLDP/CDP neighbor discovery (+ bridge
  host tables) across all devices, resolves neighbors to known devices by
  IP or identity, dedupes links seen from both ends, and renders an
  SVG graph with pan/zoom. Neighbors that can't be matched to a managed
  device show up as dashed "unmanaged" nodes (APs, switches you haven't
  added, etc.), same as the original. See `services/topology_service.py`.
- **Single device view** (`/devices/{id}`): live interfaces, IP addresses,
  DHCP leases, PoE, watchdog status, and per-port neighbor identity --
  ported from the original's port-level neighbor resolution (dedupe by
  MAC, prefer an actual MNDP/LLDP identity over a bare IP/MAC once one
  exists on that port). Also lists that device's backups and lets you
  edit its connection settings inline. See `services/device_detail_service.py`.

- **Agents** (reach hosts inside remote networks): any MikroTik you manage
  can act as an *agent* for its own network. Other devices are assigned
  "Via agent" when you add or edit them:
  - **Other device (ping only)** - printer, NAS, camera, PC... The agent
    runs `/ping` against it; the dashboard shows online/offline + latency.
  - **MikroTik behind an agent** - the agent pings it first, then the server
    opens the RouterOS API (or SSH) *through an SSH port-forward on the
    agent* and reads metrics, runs commands and takes backups as usual.
  - Hosts per agent are throttled (`MIKRAPP_AGENT_CONCURRENCY`, default 4)
    so a small router isn't overwhelmed. The topology graph draws an
    "agent" link from each agent to the hosts it serves.
  - **One-time setup on every agent router** (RouterOS disables this by
    default; without it you'll see "agent refused to forward the connection"):

        /ip ssh set forwarding-enabled=local

  - The agent itself must be reachable from the server over SSH (the SSH
    port you set for it, e.g. 3316) and its user needs the ssh + api/read
    policies.
  - **Two levels (CR-001):** an agent can itself be reached through another
    agent (server -> A1 -> A2 -> host). Set A2's "Via agent" to A1, then set
    hosts in A2's network to "Via agent" A2. The innermost agent (A2) does the
    pinging; MikroTik targets are read through nested SSH tunnels. Forwarding
    (`/ip ssh set forwarding-enabled=local`) must be enabled on **every agent
    that has to forward**, i.e. on A1 and on A2. Lists show the path as
    "A1 -> A2". At most `MIKRAPP_MAX_AGENT_DEPTH` agents (default 2) are
    followed; a longer chain, or a loop, is marked offline with
    "agent chain too deep (max 2)" (a hard stop, not loop detection).
  - Existing databases are upgraded automatically on start (new columns
    are added; nothing is lost).
- Each device remembers its **last error** (shown on the device page and as
  a tooltip on the status badge), so "offline" now tells you why.

- **Automatic checks (CR-005):** a background scheduler checks every device
  right after MikrApp starts and then every `MIKRAPP_CHECK_INTERVAL` seconds
  (default 60, minimum 5). Rounds never overlap; if one runs longer than the
  interval the missed slot is skipped, not queued. The dashboard asks every
  interval whether a new check has finished and reloads **only if one has**.
  Each device shows the time of its last check. Statuses are shown on the
  dashboard only; no notifications are sent. Switch the scheduler off with
  `MIKRAPP_SCHEDULER=off` (the buttons keep working). Periodic rounds skip the
  slower PoE query; "Check all" / "Check now" still read it.
  Status history in the database and a log file is planned (CR-013).

- **Check history (CR-013):** every check of every device (button or scheduler)
  is recorded twice: as one line in a log file and as a row in the database.
  - **Log file:** `logs/checks-YYYY-MM-DD.jsonl`, one JSON object per line, one
    file per UTC day (folder: `MIKRAPP_CHECK_LOG`, `off` disables; files older
    than `MIKRAPP_CHECK_LOG_DAYS` = 30 are deleted, 0 keeps them forever).
  - **Database:** table `check_history` (retention `MIKRAPP_CHECK_HISTORY_DAYS`
    = 90, 0 keeps forever; `MIKRAPP_CHECK_HISTORY=off` disables). The history of
    a deleted device is kept, with its name and address.
  - **Each record:** time, device, address, route (`direct` or `A1 -> A2`),
    trigger (`manual` / `scheduler`), result, previous result, whether the
    status changed, ping latency (hosts pinged through an agent), error text,
    duration, and the list of attempts inside the check (for example
    `api failed 6012 ms (timed out) -> ssh ok 812 ms`).
  - **Never in the way:** records go to a queue and a background writer saves
    them, so a slow or broken disk/database cannot delay or stop a check; one
    failing sink does not stop the other; a failure is logged once, not per line.
  - **Where to see it:** the device page shows the latest 50 checks. Charts and
    network-wide views are not built yet. To read the file on Windows:
    `Get-Content logs\checks-*.jsonl -Tail 20`.

- **Device logs by syslog (CR-011):** routers send their log to MikrApp, which
  stores it (table `logs`) and shows it on the **Logs** page (filters: device,
  severity, topic, text, time; newest first, "older messages" paging) and in
  the latest-50 card on each device page. The time stored is the time MikrApp
  received the message. Settings: `MIKRAPP_SYSLOG` (`off` disables),
  `MIKRAPP_SYSLOG_HOST` (0.0.0.0), `MIKRAPP_SYSLOG_PORT` (5514),
  `MIKRAPP_SYSLOG_DAYS` (90, 0 keeps forever). Old messages are deleted at
  start and every hour by the scheduler. Messages from addresses that are not
  a device in MikrApp are dropped and counted; the Logs page lists those
  addresses. If the port cannot be opened MikrApp still starts and the Logs page
  says why. A button that sets syslog up on the routers, and a polling fallback
  for routers that cannot reach the server, were considered and are not needed.

## What's intentionally left out (to add the same way, following the same layers)

The original PHP file has ~11,800 lines including per-port PoE toggling,
a firewall rule editor, an upgrade/CVE advisory checker, a device reboot/
"test connection" action bar, scheduled jobs, and multi-language UI. None
of that is architecturally hard to add -- each would be:

1. A method on `MikrotikClient` (or a new file in `mikrotik/`) to fetch/
   change the relevant data.
2. A method on the matching repository if it needs to persist anything.
3. A router + template for the page.

Happy to build any of those out next -- just say which one matters most.

## Notes

- `asyncssh` needs to be installed for the SSH fallback path; if a device
  is API-only, you don't strictly need it, but install it anyway since
  `mikrotik/client.py` imports it unconditionally.
- SQLite here uses WAL mode, which is fine for a single-process app like
  this; if you ever run multiple uvicorn workers behind a proxy, switch
  `MAX_CONCURRENT_CHECKS` down accordingly since it's per-process.

## Sending router logs to MikrApp

Run these two commands in the terminal of every MikroTik. Replace `192.168.10.5`
with the IP address of the computer that runs MikrApp, as the router sees it, and
`10.110.110.1` with the router's own IP address, the same one entered for it in MikrApp:

    /system logging action add name=mikrapp target=remote remote=192.168.10.5 remote-port=5514 src-address=10.110.110.1 bsd-syslog=yes syslog-facility=local0
    /system logging add action=mikrapp topics=info,warning,error,critical

- **Check:** `/system logging action print` and `/system logging print` show the new
  action and rule. `:log info "mikrapp syslog test"` sends a test message that should
  appear on the Logs page within seconds.
- **Remove:** `/system logging remove [find action=mikrapp]`, then
  `/system logging action remove [find name=mikrapp]`.
- **On the MikrApp computer:** allow incoming UDP on port 5514 in its firewall (Windows:
  an inbound rule in Windows Defender Firewall). Port 514 needs administrator rights on
  most systems, so the default is 5514; to change it set `MIKRAPP_SYSLOG_PORT` and use the
  same number in `remote-port`.
- `src-address` makes the router send from the address MikrApp knows. Without it the router
  may use another address and MikrApp will ignore the messages (they show up under
  "unknown senders" on the Logs page).
- The router needs a route to the MikrApp computer. A router behind an agent often has none.
- Parameter names can differ between RouterOS versions; type `/system logging action add `
  and press Tab to see what yours accepts. Not yet tried on real routers.
- Syslog over UDP is not encrypted or authenticated. Use it only on a trusted network.

## Tests

    python tests/test_cr001_agent_chain.py
    python tests/test_cr005_scheduler.py
    python tests/test_cr013_check_history.py
    python tests/test_cr011_syslog.py

Check the acceptance criteria of CR-001 (chain resolution, nested tunnels,
status logic, concurrency limits, topology, backups) CR-005 (scheduler
timing, skipped rounds, clean stop, dashboard JavaScript) and CR-013 (check
records, log file, database, retention) and CR-011 (syslog parser, real UDP listener,
queue, retention, log pages) with a fake SSH
layer and a fake RouterOS API server. No router or network is needed
(`node` is used for the dashboard JavaScript check when installed). See `docs/`.
