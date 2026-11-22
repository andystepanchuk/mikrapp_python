# CR-013 - Check history: log file first, then database

**Status:** implemented, automated tests pass; not yet tried on your network.

## Desired state (as implemented)
**Step 1, the log file**
1. Every check of every device writes one line: time, device, address, route, trigger, result, previous result, status-changed flag, ping latency, error, duration, attempts.
2. Attempts are listed inside the line (`api`, `ssh`, `probe`, `ping-api`, `ping-ssh`), each with success, milliseconds and error. One line per check, with its attempts inside.
3. A change of status is marked (`status_changed`). The first check of a device and a failure of our own are not changes.
4. JSON lines, one file per UTC day: `logs/checks-YYYY-MM-DD.jsonl`. Folder `MIKRAPP_CHECK_LOG` (`off` disables). Files older than `MIKRAPP_CHECK_LOG_DAYS` (30) are deleted.
5. A writing problem never stops or slows a check.

**Step 2, the database**
6. Same records in table `check_history` (new table; additional columns for name, address, trigger and previous status, so a row stands on its own).
7. Rows older than `MIKRAPP_CHECK_HISTORY_DAYS` (90) are deleted.
8. The device page shows the latest 50 checks. Charts, uptime figures and a network-wide view are left for a later change request.
9. Existing databases get the table automatically.

## How it is built
`DeviceService` collects the attempts while a check runs and hands one `CheckRecord` to `CheckRecorder`.
`record()` only puts it on a queue. A background writer saves batches to the file and to the database,
independently of each other. The recorder starts and stops with the application (shutdown writes what is still queued).
Clean-up runs at start and once per UTC day.

## Example line
```json
{"ts":"2026-11-22T12:45:10.016+00:00","device_id":1,"name":"HQ-RB","ip":"10.110.110.1","route":"direct","trigger":"scheduler","status":"online","previous_status":"checking","status_changed":false,"latency_ms":null,"error":"","duration_ms":3,"attempts":[{"step":"api","ok":false,"ms":0,"error":"timed out"},{"step":"ssh","ok":true,"ms":0,"error":null}]}
```

## Acceptance results
Automated: `python tests/test_cr013_check_history.py` (20 checks).

| ID | Result | How checked |
|---|---|---|
| AC-1 one line per check | met | 2 rounds x 4 devices = exactly 8 lines; database has the same number |
| AC-2 line contents | met | all fields present and typed |
| AC-3 attempts inside | met | "API failed (timed out), then SSH ok"; offline shows api, ssh, probe; no ping reply is explained |
| AC-4 status change | met | first check not a change; online to offline and back marked; repeats not |
| AC-5 file format and rotation | met | JSON lines, daily files, old files deleted, other files left alone |
| AC-6 write failures | met | broken log folder: checks fine, database written, one warning; broken database: checks fine, file written |
| AC-7 not in the way | met | a 1-second-slow disk: 5 checks returned in 25 ms; lines arrived later |
| AC-8 database rows | met | same fields as the file, attempts included |
| AC-9 retention | met | rows older than 90 days deleted; 0 keeps everything |
| AC-10 device page | met | latest 50, newest first, changes and attempts shown |
| AC-11 upgrade | met | an old-format database gets the table automatically |
| AC-12 shutdown and deleted devices | met | 20 of 20 queued records written at stop; history of a deleted device stays |

**Could not test:** behaviour on your network and disk; very large volumes. Estimate: 50 devices checked every
minute is about 72,000 records a day, roughly 90 days = 6.5 million rows in SQLite; plan to review retention
if you add many more devices.

## Notes
- Latency is the ping time and exists only for hosts pinged through an agent; direct checks have `null`. The duration of each attempt is in `attempts`.
- Topology scans read device data but are not checks, so they are not recorded.
