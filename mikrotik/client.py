"""
MikrotikClient is what the rest of the app talks to. It hides the choice
between the binary API and SSH, and turns raw device output into the
plain dict that DeviceRepository.update_metrics() expects.
"""
import asyncio
import re
import time
from contextlib import asynccontextmanager
from typing import List, Optional

import config
from models import Device
from mikrotik.routeros_api import RouterOsApiClient, RouterOsApiError
from mikrotik.ssh_client import MikrotikSshClient
from mikrotik.tunnel import open_agent_chain, channel_error, AgentForwardingError, AgentHopError
import asyncssh


async def probe_port(host: str, port: int, timeout: float = config.PORT_PROBE_TIMEOUT) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        writer.close()
        return True
    except Exception:
        return False


def _note(attempts, step: str, t0: float, exc=None, ok=None, error=None):
    """Record one attempt of a check (what was tried, how long, how it ended)."""
    if attempts is None:
        return
    if ok is None:
        ok = exc is None
    if error is None and exc is not None:
        error = (str(exc) or exc.__class__.__name__)
    attempts.append({"step": step, "ok": bool(ok), "ms": int((time.monotonic() - t0) * 1000),
                     "error": (error or None) and str(error)[:300]})


_SAFE_HOST = re.compile(r"^[A-Za-z0-9.\-:_]+$")


class MikrotikClient:
    def __init__(self, device: Device, via: Optional[List[Device]] = None):
        self.device = device
        # Agents to go through, outermost first. Empty = connect directly.
        self.via: List[Device] = list(via or [])

    # -- connection helpers: direct, or through the chain of agents ----------

    @asynccontextmanager
    async def _api(self):
        d = self.device
        if not self.via:
            async with RouterOsApiClient(d.ip, d.username, d.password,
                                         port=d.api_port, timeout=config.API_TIMEOUT) as api:
                yield api
            return
        async with open_agent_chain(self.via) as tunnel:
            async def connector():
                try:
                    return await tunnel.open_connection(d.ip, d.api_port)
                except asyncssh.ChannelOpenError as exc:
                    raise channel_error(self.via[-1], exc, f"{d.ip}:{d.api_port}") from exc
            async with RouterOsApiClient(d.ip, d.username, d.password, port=d.api_port,
                                         timeout=config.API_TIMEOUT, connector=connector) as api:
                yield api

    @asynccontextmanager
    async def _ssh(self):
        d = self.device
        if not self.via:
            async with MikrotikSshClient(d.ip, d.username, d.password,
                                         port=d.port, timeout=config.SSH_TIMEOUT) as ssh:
                yield ssh
            return
        async with open_agent_chain(self.via) as tunnel:
            ssh = MikrotikSshClient(d.ip, d.username, d.password, port=d.port,
                                    timeout=config.SSH_TIMEOUT, tunnel=tunnel)
            try:
                await ssh.connect()
            except asyncssh.ChannelOpenError as exc:
                raise channel_error(self.via[-1], exc, f"{d.ip}:{d.port}") from exc
            try:
                yield ssh
            finally:
                await ssh.close()

    async def ping(self, address: str, count: int = 3, attempts=None) -> dict:
        """
        Ping `address` FROM this router (use it on an agent). Returns
        {"sent": n, "received": n, "avg_ms": float|None}.
        """
        if not _SAFE_HOST.match(address or ""):
            raise ValueError(f"invalid address: {address!r}")
        if self.device.protocol in ("auto", "api"):
            t0 = time.monotonic()
            try:
                async with self._api() as api:
                    rows = await api.talk("/ping", {"address": address, "count": str(count)})
                result = _ping_from_rows(rows, count)
            except Exception as exc:
                _note(attempts, "ping-api", t0, exc)
                if self.device.protocol == "api":
                    raise
            else:
                _note(attempts, "ping-api", t0, ok=result["received"] > 0,
                      error=None if result["received"] else f"no reply (0 of {result['sent']})")
                return result
        t0 = time.monotonic()
        try:
            async with self._ssh() as ssh:
                text = await ssh.run(f"/ping {address} count={count}")
            result = _ping_from_text(text, count)
        except Exception as exc:
            _note(attempts, "ping-ssh", t0, exc)
            raise
        _note(attempts, "ping-ssh", t0, ok=result["received"] > 0,
              error=None if result["received"] else f"no reply (0 of {result['sent']})")
        return result

    async def is_reachable(self, attempts=None) -> bool:
        """Cheap TCP probe, tries the relevant ports concurrently."""
        t0 = time.monotonic()
        ports = []
        if self.device.protocol in ("auto", "api"):
            ports.append(self.device.api_port)
        if self.device.protocol in ("auto", "ssh"):
            ports.append(self.device.port)
        if not ports:
            ports = [self.device.port]
        results = await asyncio.gather(*(probe_port(self.device.ip, p) for p in ports))
        reachable = any(results)
        _note(attempts, "probe", t0, ok=reachable,
              error=None if reachable else f"no answer on TCP port {', '.join(map(str, ports))}")
        return reachable

    async def collect_metrics(self, extra_commands: bool = False, attempts=None) -> dict:
        """Try the API first (fast, structured), fall back to SSH."""
        api_error = None
        if self.device.protocol in ("auto", "api"):
            t0 = time.monotonic()
            try:
                data = await self._collect_via_api(extra_commands)
                _note(attempts, "api", t0)
                return data
            except Exception as exc:
                _note(attempts, "api", t0, exc)
                if self.device.protocol == "api":
                    raise
                api_error = exc
        t0 = time.monotonic()
        try:
            data = await self._collect_via_ssh(extra_commands)
            _note(attempts, "ssh", t0)
            return data
        except Exception as ssh_error:
            _note(attempts, "ssh", t0, ssh_error)
            if api_error is None:
                raise
            # Both failed. A specific agent-hop error (forwarding refused, agent down)
            # is the real cause and must not be hidden; otherwise name BOTH failures.
            for err in (ssh_error, api_error):
                if isinstance(err, (AgentForwardingError, AgentHopError)):
                    raise err
            raise RuntimeError(f"API: {api_error}; SSH: {ssh_error}") from ssh_error

    async def _collect_via_api(self, extra_commands: bool) -> dict:
        async with self._api() as api:
            resource = (await api.talk("/system/resource/print")) or [{}]
            identity = (await api.talk("/system/identity/print")) or [{}]
            health = (await api.talk("/system/health/print")) or [{}]
            data = _parse_resource(resource[0], identity[0], health)

            if extra_commands:
                try:
                    poe = await api.talk("/interface/ethernet/poe/print")
                    data.update(_parse_poe(poe))
                except RouterOsApiError:
                    pass
            return data

    async def _collect_via_ssh(self, extra_commands: bool) -> dict:
        async with self._ssh() as ssh:
            resource_txt = await ssh.run("/system resource print")
            identity_txt = await ssh.run("/system identity print")
            data = _parse_kv_text(resource_txt)
            data["identity"] = _parse_kv_text(identity_txt).get("name", "")
            data = _normalize_ssh_resource(data)

            if extra_commands:
                try:
                    poe_txt = await ssh.run("/interface ethernet poe print detail without-paging")
                    data.update(_parse_poe_text(poe_txt))
                except Exception:
                    pass
            return data

    async def run_commands(self, commands: list[str]) -> str:
        """Run a multi-line RouterOS script, return combined text output."""
        if self.device.protocol in ("auto", "api"):
            try:
                async with self._api() as api:
                    out_lines = []
                    for cmd in commands:
                        cmd = cmd.strip()
                        if not cmd:
                            continue
                        rows = await api.talk(cmd)
                        for row in rows:
                            if "ret" in row and len(row) == 1:
                                out_lines.append(row["ret"])
                            else:
                                out_lines.append(str(row))
                    return "\n".join(out_lines)
            except Exception as api_exc:
                if self.device.protocol == "api":
                    return f"[error] {api_exc}"
        async with self._ssh() as ssh:
            out_lines = []
            for cmd in commands:
                cmd = cmd.strip()
                if not cmd:
                    continue
                out_lines.append(await ssh.run(cmd))
            return "\n".join(out_lines)

    async def get_full_info(self) -> dict:
        """Interfaces, IP addresses, DHCP leases, and watchdog -- for the device detail page."""
        if self.device.protocol in ("auto", "api"):
            try:
                return await self._full_info_via_api()
            except Exception:
                if self.device.protocol == "api":
                    raise
        return await self._full_info_via_ssh()

    async def _full_info_via_api(self) -> dict:
        async with self._api() as api:
            interfaces = [_interface_from_row(r) for r in await api.talk("/interface/print")]
            try:
                rates = await self._eth_rates_via_api(api)
                for iface in interfaces:
                    if iface["name"] in rates:
                        iface["rate"] = rates[iface["name"]]
            except Exception:
                pass

            ip_addresses = [
                {"address": r.get("address", ""), "interface": r.get("interface", ""),
                 "network": r.get("network", "")}
                for r in await api.talk("/ip/address/print")
            ]

            dhcp_leases = []
            try:
                for r in await api.talk("/ip/dhcp-server/lease/print"):
                    dhcp_leases.append({
                        "address": r.get("address", ""), "mac": r.get("mac-address", ""),
                        "hostname": r.get("host-name", ""), "status": r.get("status", ""),
                    })
            except RouterOsApiError:
                pass

            watchdog = None
            try:
                rows = await api.talk("/system/watchdog/print")
                if rows:
                    watchdog = _watchdog_from_kv(rows[0])
            except RouterOsApiError:
                pass

            return {"interfaces": interfaces, "ip_addresses": ip_addresses,
                    "dhcp_leases": dhcp_leases, "watchdog": watchdog}

    async def _full_info_via_ssh(self) -> dict:
        async with self._ssh() as ssh:
            iface_text = await ssh.run("/interface print terse without-paging")
            interfaces = [_interface_from_row(r) for r in _parse_terse(iface_text)]
            try:
                rates = await self._eth_rates_via_ssh(ssh)
                for iface in interfaces:
                    if iface["name"] in rates:
                        iface["rate"] = rates[iface["name"]]
            except Exception:
                pass

            ip_text = await ssh.run("/ip address print terse without-paging")
            ip_addresses = [
                {"address": r.get("address", ""), "interface": r.get("interface", ""),
                 "network": r.get("network", "")}
                for r in _parse_terse(ip_text)
            ]

            dhcp_leases = []
            try:
                dhcp_text = await ssh.run("/ip dhcp-server lease print terse without-paging")
                for r in _parse_terse(dhcp_text):
                    dhcp_leases.append({
                        "address": r.get("address", ""), "mac": r.get("mac-address", ""),
                        "hostname": r.get("host-name", ""), "status": r.get("status", ""),
                    })
            except Exception:
                pass

            watchdog = None
            try:
                wd_text = await ssh.run("/system watchdog print")
                watchdog = _watchdog_from_kv(_parse_kv_text(wd_text))
            except Exception:
                pass

            return {"interfaces": interfaces, "ip_addresses": ip_addresses,
                    "dhcp_leases": dhcp_leases, "watchdog": watchdog}

    async def export_config(self) -> str:
        return await self.run_commands(["/export"])

    async def get_neighbors(self) -> list:
        if self.device.protocol in ("auto", "api"):
            try:
                return await self._neighbors_via_api()
            except Exception:
                if self.device.protocol == "api":
                    raise
        return await self._neighbors_via_ssh()

    async def _neighbors_via_api(self) -> list:
        async with self._api() as api:
            neighbors = [_neighbor_from_row(r) for r in await api.talk("/ip/neighbor/print")]

            try:
                for row in await api.talk("/interface/bridge/host/print"):
                    if _truthy(row.get("local")):
                        continue
                    neighbors.append(_neighbor_from_row({
                        "mac-address": row.get("mac-address", ""),
                        "interface": row.get("on-interface") or row.get("interface", ""),
                    }))
            except RouterOsApiError:
                pass

            try:
                rates = await self._eth_rates_via_api(api)
                for n in neighbors:
                    n["speed"] = rates.get(n["interface"], "")
            except Exception:
                pass
            return neighbors

    async def _eth_rates_via_api(self, api: RouterOsApiClient) -> dict:
        iface_rows = await api.talk("/interface/ethernet/print")
        ids = [r[".id"] for r in iface_rows if ".id" in r]
        if not ids:
            return {}
        rows = await api.talk("/interface/ethernet/monitor",
                               {"numbers": ",".join(ids), "once": ""})
        rates = {}
        for row in rows:
            name, rate = row.get("name"), row.get("rate")
            if name and rate:
                rates[name] = _format_rate_label(rate)
        return rates

    async def _neighbors_via_ssh(self) -> list:
        async with self._ssh() as ssh:
            text = await ssh.run("/ip neighbor print terse without-paging")
            neighbors = [_neighbor_from_row(r) for r in _parse_terse(text)]

            try:
                bh_text = await ssh.run("/interface bridge host print terse without-paging")
                for row in _parse_terse(bh_text):
                    if _truthy(row.get("local")):
                        continue
                    neighbors.append(_neighbor_from_row({
                        "mac-address": row.get("mac-address", ""),
                        "interface": row.get("on-interface") or row.get("interface", ""),
                    }))
            except Exception:
                pass

            try:
                rates = await self._eth_rates_via_ssh(ssh)
                for n in neighbors:
                    n["speed"] = rates.get(n["interface"], "")
            except Exception:
                pass
            return neighbors

    async def _eth_rates_via_ssh(self, ssh: MikrotikSshClient) -> dict:
        text = await ssh.run("/interface ethernet monitor [find] once")
        rates = {}
        for block in text.split("\n\n"):
            name_m = re.search(r"name:\s*(\S+)", block)
            rate_m = re.search(r"rate:\s*(\S+)", block)
            if name_m and rate_m:
                rates[name_m.group(1)] = _format_rate_label(rate_m.group(1))
        return rates


# -- parsing helpers --------------------------------------------------------

def _to_float(value: str):
    try:
        return float(re.sub(r"[^\d.\-]", "", value))
    except (TypeError, ValueError):
        return None


def _parse_resource(resource: dict, identity: dict, health_rows: list) -> dict:
    total_mem = _to_float(resource.get("total-memory", "0")) or 0
    free_mem = _to_float(resource.get("free-memory", "0")) or 0
    ram_usage = round((1 - free_mem / total_mem) * 100, 1) if total_mem else 0.0

    temperature = voltage = None
    for row in health_rows:
        name = (row.get("name") or "").lower()
        if "temperature" in name:
            temperature = _to_float(row.get("value"))
        elif "voltage" in name:
            voltage = _to_float(row.get("value"))

    return {
        "identity": identity.get("name", ""),
        "routeros_version": resource.get("version", ""),
        "board": resource.get("board-name", ""),
        "cpu_usage": _to_float(resource.get("cpu-load", "0")) or 0.0,
        "ram_usage": ram_usage,
        "temperature": temperature,
        "voltage": voltage,
        "uptime": resource.get("uptime", ""),
    }


def _parse_poe(rows: list) -> dict:
    if not rows:
        return {}
    powered = sum(1 for r in rows if (r.get("poe-out") or "").lower() not in ("off", ""))
    total_power = sum(_to_float(r.get("poe-out-power", "0")) or 0 for r in rows)
    return {
        "poe_ok": powered > 0,
        "poe_power": round(total_power, 1),
        "poe_count": powered,
        "poe_ports": len(rows),
    }


def _parse_kv_text(text: str) -> dict:
    """Parse RouterOS's ' key: value' console output into a flat dict."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


def _normalize_ssh_resource(raw: dict) -> dict:
    total_mem = _to_float(raw.get("total-memory", "0")) or 0
    free_mem = _to_float(raw.get("free-memory", "0")) or 0
    ram_usage = round((1 - free_mem / total_mem) * 100, 1) if total_mem else 0.0
    return {
        "identity": raw.get("identity", ""),
        "routeros_version": raw.get("version", ""),
        "board": raw.get("board-name", ""),
        "cpu_usage": _to_float(raw.get("cpu-load", "0")) or 0.0,
        "ram_usage": ram_usage,
        "temperature": None,
        "voltage": None,
        "uptime": raw.get("uptime", ""),
    }


def _parse_poe_text(text: str) -> dict:
    blocks = text.split("\n\n")
    powered = sum(1 for b in blocks if "poe-out: " in b and "off" not in b.lower())
    total = len([b for b in blocks if "poe-out" in b])
    return {"poe_ok": powered > 0, "poe_count": powered, "poe_ports": total, "poe_power": None}


# -- neighbor discovery / topology helpers ----------------------------------

_TERSE_TOKEN = re.compile(r'([\w.\-]+)=("(?:[^"\\]|\\.)*"|\S*)')


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("true", "yes", "1")


def _parse_terse(text: str) -> list:
    """Parse RouterOS `print terse` output (`key=value key2="a b"`) into dicts."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("flags:"):
            continue
        pairs = _TERSE_TOKEN.findall(line)
        if not pairs:
            continue
        row = {}
        for key, val in pairs:
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            row[key] = val
        rows.append(row)
    return rows


def _neighbor_from_row(row: dict) -> dict:
    return {
        "address": (row.get("address") or "").strip(),
        "mac": row.get("mac-address") or row.get("mac", ""),
        "identity": (row.get("identity") or "").strip(),
        "hostname": (row.get("host-name") or row.get("hostname") or "").strip(),
        "interface": row.get("interface") or row.get("on-interface", "") or "",
        "board": row.get("board", ""),
        "platform": row.get("platform", ""),
        "speed": "",
    }


def _format_rate_label(rate: str) -> str:
    return re.sub(
        r"(gbps|mbps|kbps)", lambda m: m.group(1).capitalize(), rate, flags=re.IGNORECASE
    )


def _interface_from_row(row: dict) -> dict:
    name = row.get("name", "")
    itype = (row.get("type") or "").lower()
    haystack = f"{itype} {name.lower()}"
    if "ether" in haystack:
        kind = "ether"
    elif any(t in haystack for t in ("wifi", "wlan", "cap")):
        kind = "wifi"
    else:
        kind = "other"

    running = _truthy(row.get("running"))
    disabled = _truthy(row.get("disabled"))
    status = "disabled" if disabled else ("up" if running else "down")

    return {
        "name": name, "type": itype, "kind": kind, "status": status,
        "mac": row.get("mac-address", ""), "rate": "",
    }


def _watchdog_from_kv(kv: dict) -> dict:
    watch_address = kv.get("watch-address", "")
    return {
        "watch_address": watch_address,
        "ping_start_after": kv.get("ping-start-after", ""),
        "ping_timeout": kv.get("ping-timeout", ""),
        "enabled": bool(watch_address) and watch_address.lower() not in ("none", "0.0.0.0", ""),
    }


# -- ping parsing -----------------------------------------------------------

def _parse_rtt_ms(value) -> Optional[float]:
    """'3ms', '2ms437us', '1s200ms' -> milliseconds."""
    if not value:
        return None
    total, found = 0.0, False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|us|s)", str(value)):
        found = True
        n = float(num)
        total += n * 1000 if unit == "s" else (n if unit == "ms" else n / 1000)
    return round(total, 2) if found else None


def _ping_from_rows(rows: list, count: int) -> dict:
    last = next((r for r in reversed(rows) if "received" in r), None)
    sent, received = count, 0
    avg = None
    if last:
        sent = int(_to_float(last.get("sent")) or count)
        received = int(_to_float(last.get("received")) or 0)
        avg = _parse_rtt_ms(last.get("avg-rtt"))
    if avg is None:
        times = [t for t in (_parse_rtt_ms(r.get("time")) for r in rows) if t is not None]
        if times:
            avg = round(sum(times) / len(times), 2)
            if not last:
                received = len(times)
    return {"sent": sent, "received": received, "avg_ms": avg}


def _ping_from_text(text: str, count: int) -> dict:
    sent_m = re.search(r"sent=(\d+)", text)
    recv_m = re.search(r"received=(\d+)", text)
    avg_m = re.search(r"avg-rtt=(\S+)", text)
    sent = int(sent_m.group(1)) if sent_m else count
    if recv_m:
        received = int(recv_m.group(1))
    else:  # no summary line: count reply lines
        received = len(re.findall(r"\bttl=|\d+\s+\S+\s+\d+\s+\d+\s+\d+ms", text))
    return {"sent": sent, "received": received,
            "avg_ms": _parse_rtt_ms(avg_m.group(1)) if avg_m else None}
