"""
Rebuilds the network graph from live neighbor discovery (MNDP/LLDP/CDP)
plus bridge host tables, same approach as the original tool -- but every
device is polled concurrently here instead of one-at-a-time with a sleep
between each.
"""
import asyncio
from datetime import datetime, timezone
from typing import List, Optional

import config
from db.devices_repo import DeviceRepository
from db.topology_repo import TopologyRepository
from models import Device
from services.client_factory import ClientFactory


class TopologyService:
    def __init__(self, devices_repo: DeviceRepository, topo_repo: TopologyRepository,
                 max_concurrency: int = config.MAX_CONCURRENT_CHECKS):
        self.devices_repo = devices_repo
        self.topo_repo = topo_repo
        self.clients = ClientFactory(devices_repo)
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def scan(self, site_id: Optional[int] = None, max_devices: int = 30) -> dict:
        all_devices: List[Device] = await asyncio.to_thread(self.devices_repo.list_all)

        by_ip = {d.ip: d.id for d in all_devices}
        by_name = {}
        for d in all_devices:
            name = (d.custom_identity or d.identity or "").strip().lower()
            if name:
                by_name[name] = d.id

        candidates = [d for d in all_devices if d.password and d.status != "offline"]
        if site_id:
            candidates = [d for d in candidates if d.site_id == site_id]
        candidates = candidates[:max_devices]

        pairs = await asyncio.gather(*(self._scan_one(d) for d in candidates))

        links: dict = {}
        unmanaged: dict = {}
        for device, neighbors in pairs:
            for nb in neighbors:
                addr = nb.get("address", "")
                identity = (nb.get("identity") or "").strip()
                iface = nb.get("interface", "")
                speed = nb.get("speed", "")

                target_id = by_ip.get(addr)
                if not target_id and identity:
                    target_id = by_name.get(identity.lower())

                if target_id and target_id != device.id:
                    a, b = sorted((device.id, target_id))
                    key = (a, b, iface)
                    if key not in links:
                        label = " · ".join(p for p in (iface, speed) if p) or "link"
                        links[key] = {"from": device.id, "to": target_id, "via": iface,
                                      "speed": speed, "label": label}
                elif not target_id and (addr or identity):
                    ukey = addr or identity.lower()
                    if ukey not in unmanaged:
                        unmanaged[ukey] = {
                            "name": identity or addr,
                            "ip": addr,
                            "mac": nb.get("mac", ""),
                            "board": nb.get("board") or nb.get("platform") or "",
                            "from_device": device.id,
                            "via": iface,
                            "speed": speed,
                        }

        link_list = list(links.values())
        unmanaged_list = list(unmanaged.values())

        # Devices tied to an agent are linked ONLY to that agent (below), so keep
        # them out of the generic per-site fallback chain.
        agent_related = set()
        for d in all_devices:
            if d.agent_id:
                agent_related.update((d.id, d.agent_id))

        if not link_list:
            # Nothing discovered (e.g. no LLDP/MNDP enabled anywhere) --
            # fall back to a simple per-site chain so the view isn't empty.
            by_site: dict = {}
            for d in all_devices:
                if d.id not in agent_related:
                    by_site.setdefault(d.site_id, []).append(d.id)
            for ids in by_site.values():
                for a, b in zip(ids, ids[1:]):
                    lo, hi = sorted((a, b))
                    link_list.append({"from": lo, "to": hi, "via": "", "speed": "", "label": "site"})

        # Agent relationships: draw agent -> served device (skip pairs already linked).
        linked = {frozenset((l["from"], l["to"])) for l in link_list}
        for d in all_devices:
            if d.agent_id and frozenset((d.agent_id, d.id)) not in linked:
                link_list.append({"from": d.agent_id, "to": d.id, "via": "", "speed": "",
                                  "label": "agent"})
                linked.add(frozenset((d.agent_id, d.id)))

        scanned_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        await asyncio.to_thread(self.topo_repo.save_cache, link_list, unmanaged_list, scanned_at)
        return {"links": link_list, "unmanaged": unmanaged_list, "scanned_at": scanned_at}

    async def _scan_one(self, device: Device):
        async with self._semaphore:
            try:
                client = await self.clients.for_device(device)
            except Exception:
                return device, []
            try:
                metrics = await client.collect_metrics()
                await asyncio.to_thread(self.devices_repo.update_metrics, device.id, metrics)
            except Exception:
                pass
            try:
                neighbors = await client.get_neighbors()
            except Exception:
                neighbors = []
            return device, neighbors
