# CR-005 - Background scheduler and periodic status checks

**Status:** implemented, automated tests pass; not yet tried on your routers.

## Desired state (as implemented)
1. A scheduler runs inside the application (`services/scheduler_service.py`), started and stopped by the app's lifespan.
2. A check round over all devices starts right after MikrApp starts, then every `MIKRAPP_CHECK_INTERVAL` seconds (default 60, minimum 5). Same concurrency limits and agent rules as the "Check all" button.
3. Rounds never overlap. A round that overruns the interval makes the missed slots skip; they are not queued.
4. The dashboard shows each device's status and the time of its last check (new column `last_check_at`, shown in your own time zone). Every `MIKRAPP_CHECK_INTERVAL` seconds it asks `/api/stamp` whether a check has finished since the page was loaded and reloads **only if so**.
5. No notifications are sent anywhere. Status history (database and log file) comes with CR-013.
6. `MIKRAPP_SCHEDULER=off` disables automatic checks; the buttons still work.
7. Other changes will register jobs on the same scheduler (CR-011, CR-012, CR-013).

Periodic rounds use the light check (no PoE query); "Check all" and "Check now" still read PoE.

## Acceptance results
Automated: `python tests/test_cr005_scheduler.py` (19 checks).

| ID | Result | How checked |
|---|---|---|
| AC-1 first round at start | met | scheduler starts within 0.1 s; the real app's lifespan starts it |
| AC-2 interval | met | round gaps 0.30 s with a 0.3 s interval |
| AC-3 overrun skipped | met | 0.5 s rounds at 0.2 s interval start at 0.0, 0.6, 1.2; never two at once |
| AC-4 one device down | met | offline with reason; others checked; an unexpected error in one check does not lose the rest |
| AC-5 recovery | met | back to online, error cleared |
| AC-6 dashboard | met | stamp stable until a check finishes; page carries interval, stamp and "Last check"; JavaScript run in Node: polls every interval, no reload when nothing changed or the request fails, reload when a check finished |
| AC-7 switched off | met | no rounds; settings parsed (default 60, minimum 5) |
| AC-8 manual "Check all" during rounds | met | works alongside the scheduler |
| AC-9 clean stop | met | stop in the middle of a round: no error, under 1 s |
| AC-10 upgrade | met | existing database gets the new column; data intact |
| AC-11 no notifications | met | no notification code in the application |

**Could not test:** a real browser (the JavaScript is exercised in Node with a fake page) and
long-running behaviour on your network.
