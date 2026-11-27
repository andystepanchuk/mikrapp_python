"""
What the Topology page draws (CR-010). Pure functions: devices + links + saved positions +
latest port readings in, plain dicts out. No database, no network - so it is easy to test.
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from models import Device, PortCounter, TopologyLink

GRID_X, GRID_Y, GRID_COLS, GRID_ORIGIN = 210, 130, 5, (40, 40)
NODE_W, NODE_H = 156, 84
STALE_AFTER_INTERVALS = 3
DASH = "\u2013"          # shown where a number is expected but not available


def format_rate(bps: float) -> str:
    if bps < 1000:
        return f"{bps:.0f} bps"
    if bps < 1_000_000:
        return f"{bps / 1000:.1f} kbps"
    if bps < 1_000_000_000:
        return f"{bps / 1_000_000:.1f} Mbps"
    return f"{bps / 1_000_000_000:.2f} Gbps"


def node_info(d: Device) -> str:
    if d.status == "offline":
        return "offline"
    if d.status != "online":
        return "not checked yet"
    if d.kind == "generic":
        return f"ping {d.latency_ms} ms" if d.latency_ms is not None else "ping only"
    return f"CPU {round(d.cpu_usage)}% \u00b7 RAM {round(d.ram_usage)}%"


def auto_positions(devices: List[Device], saved: Dict[int, Tuple[int, int]]) -> Dict[int, Tuple[int, int]]:
    """A free spot for every device that has none. Devices that already have a position never move."""
    taken = list(saved.values())
    fresh: Dict[int, Tuple[int, int]] = {}
    for d in sorted(devices, key=lambda x: x.id):
        if d.id in saved:
            continue
        i = 0
        while True:
            x = GRID_ORIGIN[0] + (i % GRID_COLS) * GRID_X
            y = GRID_ORIGIN[1] + (i // GRID_COLS) * GRID_Y
            if all(abs(x - tx) >= NODE_W + 10 or abs(y - ty) >= NODE_H + 10 for tx, ty in taken):
                break
            i += 1
        fresh[d.id] = (x, y)
        taken.append((x, y))
    return fresh


def traffic_for(link: TopologyLink, dev_a: Optional[Device], counters: Dict[Tuple[int, str], PortCounter],
                interval: float, now: datetime) -> dict:
    """The traffic of end A's port. `available` False = this link can never show numbers."""
    if dev_a is None or dev_a.kind == "generic" or not link.a_port:
        return {"available": False, "text": "", "rx_bps": None, "tx_bps": None}
    c = counters.get((link.a_device_id, link.a_port))
    nothing = {"available": True, "text": f"\u2193 {DASH} \u2191 {DASH}", "rx_bps": None, "tx_bps": None}
    if c is None:
        return nothing
    if not c.found:
        return {"available": True, "text": "port not found", "rx_bps": None, "tx_bps": None}
    if dev_a.status == "offline":
        return nothing
    age = (now - datetime.fromisoformat(c.read_at)).total_seconds()
    if age > STALE_AFTER_INTERVALS * interval or c.rx_bps is None or c.tx_bps is None:
        return nothing
    return {"available": True, "rx_bps": c.rx_bps, "tx_bps": c.tx_bps,
            "text": f"\u2193 {format_rate(c.rx_bps)} \u2191 {format_rate(c.tx_bps)}"}


def build_view(devices: List[Device], links: List[TopologyLink], saved: Dict[int, Tuple[int, int]],
               counters: Dict[Tuple[int, str], PortCounter], interval: float,
               now: Optional[datetime] = None) -> dict:
    """`devices` = the ones to show. Links and agent lines are drawn only when both ends are shown."""
    now = now or datetime.now(timezone.utc)
    shown = {d.id: d for d in devices}
    fresh = auto_positions(devices, saved)
    pos = {**{k: v for k, v in saved.items() if k in shown}, **fresh}

    nodes = [{"id": d.id, "name": d.display_name, "ip": d.ip, "status": d.status, "kind": d.kind,
              "info": node_info(d), "x": pos[d.id][0], "y": pos[d.id][1]} for d in devices]

    by_pair: Dict[Tuple[int, int], List[TopologyLink]] = {}
    for l in links:
        if l.a_device_id in shown and l.b_device_id in shown:
            by_pair.setdefault(tuple(sorted((l.a_device_id, l.b_device_id))), []).append(l)

    out_links = []
    for pair, group in by_pair.items():
        for idx, l in enumerate(group):
            a, b = shown[l.a_device_id], shown[l.b_device_id]
            out_links.append({
                "id": l.id,
                "a": {"id": a.id, "name": a.display_name, "port": l.a_port},
                "b": {"id": b.id, "name": b.display_name, "port": l.b_port},
                "state": "down" if "offline" in (a.status, b.status) else "ok",
                "traffic": traffic_for(l, a, counters, interval, now),
                "slot": idx, "slots": len(group),            # several links between the same pair
            })
    out_links.sort(key=lambda x: x["id"])

    agent_lines = [{"from": d.agent_id, "to": d.id} for d in devices
                   if d.agent_id and d.agent_id in shown]
    return {"nodes": nodes, "links": out_links, "agent_lines": agent_lines,
            "new_positions": fresh, "config": {"interval": interval}}
