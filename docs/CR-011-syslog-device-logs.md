# CR-011 - Syslog receiver and device log accumulation

**Status:** implemented and complete, automated tests pass; not yet tried on your routers.

## Desired state (as implemented)
1. Routers send their log to MikrApp by syslog (UDP). A listener inside the application receives it
   (`MIKRAPP_SYSLOG_HOST` 0.0.0.0, `MIKRAPP_SYSLOG_PORT` 5514; `MIKRAPP_SYSLOG=off` disables).
2. The sender's IP address is matched to a device. Messages from other addresses are dropped and counted.
3. Each message is stored in table `logs`: device, the time MikrApp received it (UTC), severity, topics, message, sender address.
4. The **Logs** page filters by device, severity (and worse), topic, text and time; newest first, with paging.
   Each device page shows its latest 50 messages and a link to all of its logs.
5. Messages older than `MIKRAPP_SYSLOG_DAYS` (90) are deleted: at start and every hour by the scheduler (CR-005). 0 keeps everything.
6. The scheduler can now run extra periodic jobs (`Scheduler.add_job`); this is the first one.

## How it is built
`LogService` owns a UDP endpoint. Each datagram is matched, parsed (`services/syslog_parser.py`: plain RouterOS text,
BSD syslog, RFC 5424) and put on a queue - instantly. One writer saves the queue in batches. A burst or a slow
database cannot block the listener; when the queue is full, messages are dropped and counted (shown on the Logs page).
If the port cannot be opened (already in use, no permission) MikrApp still starts and the Logs page says why.
Log text is escaped on the pages, so a router cannot inject script into them.

## Router setup
See the README, "Sending router logs to MikrApp" (two commands per router, plus check and remove).

## Acceptance results
Automated: `python tests/test_cr011_syslog.py` (23 checks; datagrams go through a real local UDP socket).

| ID | Result | How checked |
|---|---|---|
| AC-1 listens at start | met | real app starts the listener on a UDP port |
| AC-2 known device stored | met | all fields, server time within seconds |
| AC-3 unknown senders | met | dropped, counted, address listed |
| AC-4 formats | met | BSD, plain, RFC 5424; words not mistaken for topics; broken/empty/huge input never raises |
| AC-5 filters and paging | met | device, severity, topic (whole word), text (wildcards literal), time; newest first; paging |
| AC-6 device page | met | latest messages with link; "Logs" in the menu |
| AC-7 retention | met | 90 days, 0 = forever; also at start |
| AC-8 listener problem | met | port in use: app keeps running, page says why |
| AC-9 burst / slow database | met | 50 datagrams handled in 1 ms with a 1-second-slow database; overflow dropped and counted |
| AC-10 shutdown | met | 40 of 40 queued messages written; socket closed |
| AC-11 upgrade | met | existing database gets the table automatically |
| AC-12 switch off | met | `MIKRAPP_SYSLOG=off`; defaults 5514 and 90 days |
| AC-13 scheduler jobs | met | runs every interval, skips overrun slots, a crashing job never stops the scheduler |

**Could not test:** real RouterOS message formats and routers' ability to reach your computer; the router commands
are not tried on real devices. If a format is read wrongly, send me a sample line from the Logs page.

## Decided: not needed
Considered during review and dropped, so CR-011 is complete as delivered:
- A "Set up syslog" button that runs the router commands for you (the two commands in the README are entered by hand).
- A polling fallback for routers that cannot reach the server (for example behind an agent).
