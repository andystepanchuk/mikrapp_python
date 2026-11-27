# CR-010 - Manual topology links with port traffic

**Status:** implemented, automated tests pass; not yet tried on your routers.

## Desired state (as implemented)
1. The Topology page shows the devices you added and the links you define. Neighbor discovery, the "Refresh scan" button, guessed links and the per-site chain lines are removed (`services/topology_service.py` and the `topology_cache` table are gone; the neighbor column on the device page stays).
2. A link is an object of its own (table `topology_links`): end A and end B, each a device and an optional port. Several links between the same pair are allowed (drawn side by side). A device cannot be linked to itself.
3. Links are added, edited and deleted under the diagram. Deleting a device deletes its links, saved position and port readings (foreign keys).
4. Traffic is the traffic of end A's port: received (down arrow) and sent (up arrow), read once per check round. During the check the router reports the byte counters (API `/interface/print`, SSH `/interface print stats terse`) over the connection the check already uses - through the agent chain if there is one. MikrApp stores the latest reading (`port_counters`) and shows the average rate between two consecutive readings.
5. A wrong number is never shown: not for the first reading, after a counter reset, while end A is offline, or when the reading is older than three intervals; two readings less than 2 s apart (a manual check right after a scheduled one) are not used; an unknown port shows "port not found".
6. Red dashed line when an end is offline.
7. Devices are draggable; positions are saved (`topology_positions`) at first display and never move when other devices are added or removed. "Reset layout" returns to the automatic grid.
8. Agent relationships are thin dotted lines without labels.
9. The page updates numbers and colours in place after every check round (it asks `/topology/api/state` every `MIKRAPP_CHECK_INTERVAL` seconds). A reload only happens when a device or link was added, removed or edited.
10. Only devices that are end A of a link have counters read.
11. Three new tables; existing databases are upgraded automatically.

## How it is built
`services/topology_view.py` turns devices, links, positions and readings into what the page draws (pure functions, tested without a database).
`services/port_traffic_service.py` turns two readings into a rate. `static/topology.js` draws the diagram from one JSON state and updates it in place.

## Acceptance results
Automated: `python tests/test_cr010_topology.py` (33 checks). The page's real JavaScript is run in Node against a fake page.

| ID | Result | How checked |
|---|---|---|
| AC-1 only your devices and links | met | no scan route or button; with no links only devices and dotted agent lines |
| AC-2 add a link | met | saved, port names trimmed |
| AC-3 edit / delete / device deleted | met | links, readings and position of a deleted device are removed, others keep theirs |
| AC-4 several links per pair | met | three links get separate slots |
| AC-5 link to itself | met | refused with a message; unknown device also refused |
| AC-6 rates | met | 7.5 MB in 60 s = 1.0 Mbps; two real checks 60 s apart in the service test |
| AC-7 first reading / counter reset | met | no number, no spike, new baseline |
| AC-8 offline / stale | met | no number when end A is offline or the reading is older than 3 x 60 s |
| AC-9 ping-only end A or no port | met | line without numbers |
| AC-10 offline end | met | link state "down" (red dashed) |
| AC-11 live, no reload | met | real JavaScript: numbers and colours change in place; dragged position and zoom stay; only a new/removed device or link reloads |
| AC-12 positions | met | saved, clamped, never move when devices are added/removed; reset layout |
| AC-13 behind agents | met | counters read through the chain [A1, A2]; dotted agent lines remain |
| AC-14 unknown port | met | "port not found"; the check still succeeds |
| AC-15 not end A | met | counters are not read from such devices |
| AC-16 upgrade | met | old-format database gets the tables automatically |

**Could not test:** real RouterOS counters. `/interface/print` over the API is expected to carry `rx-byte` and `tx-byte`;
the SSH form `/interface print stats terse` is a best effort. If numbers stay at a dash on your routers, tell me which
RouterOS version you run and I will look at the exact output. Also not tested in a real browser (dragging is exercised with a fake page).

## Not in this change
Traffic history and charts (the latest reading only); live traffic between check rounds.
