"""
Builds everything the single-device page needs. The neighbor-grouping
logic mirrors the original tool's per-port resolution: dedupe by MAC
(falling back to identity/hostname/address), keep the best-scoring entry
per key, then prefer identity-class (MNDP/LLDP) entries over plain IP/MAC
noise once at least one true identity exists on that port.
"""
import asyncio
from typing import List

from db.devices_repo import DeviceRepository
from db.backups_repo import BackupRepository
from models import Device
from services.client_factory import ClientFactory

_SCORE_KEYS = ("mac", "address", "hostname", "identity")  # low -> high priority


def _best_score(entry: dict) -> int:
    if entry.get("identity"):
        return 4
    if entry.get("hostname"):
        return 3
    if entry.get("address"):
        return 2
    if entry.get("mac"):
        return 1
    return 0


def _norm_mac(mac: str) -> str:
    return (mac or "").strip().lower().replace("-", ":")


def _looks_like_ip(value: str) -> bool:
    parts = (value or "").split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def group_neighbors_by_interface(neighbors: List[dict], own_macs: set) -> dict:
    by_iface: dict = {}
    for nb in neighbors:
        iface_raw = (nb.get("interface") or "").strip().lower()
        if not iface_raw:
            continue
        iface = iface_raw.split()[0]

        identity = (nb.get("identity") or "").strip()
        hostname = (nb.get("hostname") or "").strip()
        address = (nb.get("address") or "").strip()
        mac = (nb.get("mac") or "").strip()
        mac_n = _norm_mac(mac)

        if mac_n and mac_n in own_macs:
            continue
        if not (identity or hostname or address or mac):
            continue

        if identity:
            label = identity
        elif hostname:
            label = hostname
        elif address and _looks_like_ip(address):
            label = address
        elif mac:
            label = mac
        else:
            continue

        key = mac_n or (identity or hostname or address or label).lower()
        entry = {"label": label, "identity": identity, "hostname": hostname,
                 "address": address, "mac": mac, "source": nb.get("source", "neighbor")}
        bucket = by_iface.setdefault(iface, {})
        prev = bucket.get(key)
        if not prev or _best_score(entry) >= _best_score(prev):
            bucket[key] = entry

    result = {}
    for iface, entries in by_iface.items():
        ordered = sorted(entries.values(), key=_best_score, reverse=True)
        if ordered and _best_score(ordered[0]) >= 2:
            ordered = [e for e in ordered if _best_score(e) >= 2]
        if any(e["identity"] for e in ordered):
            ordered = [e for e in ordered if e["identity"]]
        result[iface] = ordered
    return result


class DeviceDetailService:
    def __init__(self, devices_repo: DeviceRepository, backups_repo: BackupRepository):
        self.devices_repo = devices_repo
        self.backups_repo = backups_repo
        self.clients = ClientFactory(devices_repo)

    async def get_detail(self, device: Device) -> dict:
        detail = {
            "interfaces": [], "ip_addresses": [], "dhcp_leases": [],
            "neighbors_by_interface": {}, "watchdog": None, "error": None,
        }

        if device.kind != "generic" and device.password and device.status != "offline":
            try:
                client = await self.clients.for_device(device)
                full = await client.get_full_info()
                detail.update(full)
                own_macs = {_norm_mac(i.get("mac", "")) for i in full["interfaces"] if i.get("mac")}
                neighbors = await client.get_neighbors()
                detail["neighbors_by_interface"] = group_neighbors_by_interface(neighbors, own_macs)
            except Exception as exc:
                detail["error"] = str(exc)

        detail["backups"] = await asyncio.to_thread(self.backups_repo.list_for_device, device.id)
        return detail
