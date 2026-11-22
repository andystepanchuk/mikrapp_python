# CR-001 - Second-level agent chain

**Status:** implemented, automated tests pass; not yet tried on real routers.

## Problem
An agent had to be a direct device. A router that is only reachable through
another agent could not itself act as an agent, so hosts behind it could not
be monitored.

## Desired state (as implemented)
1. Any managed MikroTik with credentials can be chosen as "Via agent",
   including one that already has an agent. A device is never offered as its own agent.
2. The chain is derived by following "Via agent" links upward (`ClientFactory.resolve_chain`).
3. The **innermost** agent does the pinging; the server reaches it through the other agents.
4. A MikroTik target is read through nested SSH tunnels (`mikrotik/tunnel.py: open_agent_chain`).
5. At most `MIKRAPP_MAX_AGENT_DEPTH` agents (default 2) are followed. Longer chains and loops
   end with "agent chain too deep (max N)". This is a hard stop, not loop detection.
6. Failures name the exact hop (agent down, forwarding refused, could not connect on).
7. The per-agent concurrency limit is taken on every agent of the chain, outermost first.
8. Backups, command templates, device detail and topology use the same factory, so they work through chains.
9. No schema change (`db/connection.py` is identical to the previous version).
10. Lists and pages show the path, e.g. `A1 -> A2`.
11. Level-1 behaviour is unchanged.

## How a check works
```
server --SSH--> A1 --(forwarded)--> SSH to A2 --(forwarded)--> target:8728
```
Ping: run by A2 (reached through A1). Metrics for a MikroTik target: API over all three nested sessions.

## Router setup (every agent that has to forward)
```
/ip ssh set forwarding-enabled=local
```
Needed on A1 (to reach A2) and on A2 (to reach the target's API/SSH port).

## Acceptance results
Automated: `python tests/test_cr001_agent_chain.py` (31 checks).

| ID | Result | How checked |
|---|---|---|
| AC-1 ping run by A2 | met | fake clients record who pings, and through whom |
| AC-2 A1 unreachable | met | host and A2 offline; reason names A1; A2 never attempted |
| AC-3 metrics through two tunnels | met | fake RouterOS API server; innermost session asked for `target:8728` |
| AC-4 forwarding off on A2 only | met | error names A2 and the fix; host stays online (it answered ping) |
| AC-5 too deep / loop | met | offline "agent chain too deep (max 2)"; no hang |
| AC-6 A2 checked through A1 | met | same path as a level-1 device |
| AC-7 level-1 unchanged | met | A1 pings directly; old database opens unchanged |
| AC-8 concurrency | met | 12 hosts behind A2: peak 4 on A1 and on A2 (limit 4) |
| AC-9 forms and lists | met | candidates, "never its own agent", path text in lists and pages |
| AC-10 topology | met | links A1-A2 and A2-target labelled "agent" |
| AC-11 backup / commands | met | both go through the chain; generic hosts skipped |

**Could not test:** the real SSH forwarding between two RouterOS devices (the `tunnel=`
option of asyncssh and the "forwarding refused" error code). The sandbox has no network
or `asyncssh`, so that layer is faked. The first real two-level setup is the real test.

## Also changed
- When both the API and SSH attempts fail, the error now names both
  (`API: ...; SSH: ...`); a specific agent error (forwarding refused, agent down) is never hidden.
- A closed port behind an agent is no longer reported as "forwarding disabled".
