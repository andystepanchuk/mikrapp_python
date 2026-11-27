"""
Checks devices concurrently (bounded by a semaphore). A device is checked
either DIRECTLY, or -- when it has an agent -- THROUGH that agent MikroTik:

  * generic device (not a MikroTik): the agent pings it; only status and
    latency are recorded.
  * MikroTik behind an agent: the agent pings it first; if it answers we
    open the RouterOS API through an SSH tunnel on the agent and read
    metrics as usual.

With a chain of agents (server -> A1 -> A2 -> device) the INNERMOST agent
(A2, the one in the device's own network) does the pinging, and the API
connection goes through nested tunnels over every agent on the path.

Every check also produces ONE CheckRecord (CR-013): what was tried (the attempts),
how it ended, and how long it took. It is handed to the recorder, which writes the
log file and the database without ever holding the check up.
"""
import asyncio
import logging
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import config
from db.devices_repo import DeviceRepository
from db.topology_repo import TopologyRepository
from models import Device, CheckResult, CheckRecord
from mikrotik.client import MikrotikClient
from mikrotik.tunnel import AgentChainError, AgentHopError, AgentForwardingError
from services.check_recorder import CheckRecorder, utc_now_iso
from services.port_traffic_service import PortTrafficService
from services.client_factory import ClientFactory

log = logging.getLogger("mikrapp.checks")


@dataclass
class CheckContext:
    """What one check collects on the way, for its history record."""
    trigger: str = "manual"
    route: str = "direct"
    attempts: list = field(default_factory=list)


class DeviceService:
    def __init__(self, devices_repo: DeviceRepository,
                 max_concurrency: int = config.MAX_CONCURRENT_CHECKS,
                 recorder: Optional[CheckRecorder] = None,
                 topology_repo: Optional[TopologyRepository] = None,
                 traffic: Optional[PortTrafficService] = None):
        self.repo = devices_repo
        self.recorder = recorder
        self.topology_repo = topology_repo
        self.traffic = traffic
        self.clients = ClientFactory(devices_repo)
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._agent_limits: Dict[int, asyncio.Semaphore] = {}

    def _agent_limit(self, agent_id: int) -> asyncio.Semaphore:
        if agent_id not in self._agent_limits:
            self._agent_limits[agent_id] = asyncio.Semaphore(config.AGENT_CONCURRENCY)
        return self._agent_limits[agent_id]

    async def _mark_offline(self, device: Device, error: str) -> CheckResult:
        await asyncio.to_thread(self.repo.set_status, device.id, "offline", error)
        return CheckResult(device.id, "offline", error=error or None)

    async def check_device(self, device: Device, full: bool = False,
                           trigger: str = "manual") -> CheckResult:
        async with self._semaphore:
            ctx = CheckContext(trigger=trigger,
                               route=device.agent_path or ("agent" if device.agent_id else "direct"))
            previous = None
            if self.recorder is not None:
                previous = await asyncio.to_thread(self.repo.get_status, device.id)
            t0 = time.monotonic()
            result, failure = None, None
            try:
                if device.agent_id:
                    result = await self._check_via_agent(device, full, ctx)
                else:
                    result = await self._check_direct(device, full, ctx)
                return result
            except Exception as exc:
                failure = exc
                raise
            finally:
                self._record(device, ctx, previous, result, failure, t0)

    def _record(self, device, ctx, previous, result, failure, t0):
        """Hand the finished check to the recorder. Never raises, never waits."""
        if self.recorder is None:
            return
        try:
            status = result.status if result is not None else "error"
            error = (result.error if result is not None and result.error else "")
            if failure is not None:
                error = f"check failed: {failure}"
            prev = previous or ""
            self.recorder.record(CheckRecord(
                device_id=device.id, checked_at=utc_now_iso(), device_name=device.display_name,
                ip=device.ip, route=ctx.route, trigger=ctx.trigger, status=status,
                previous_status=prev,
                # the very first check is not a change, and neither is a failure of our own
                status_changed=bool(prev) and prev != "checking" and status != "error" and prev != status,
                latency_ms=(result.data or {}).get("latency_ms") if result is not None else None,
                error=error, duration_ms=int((time.monotonic() - t0) * 1000),
                attempts=ctx.attempts,
            ))
        except Exception:
            log.exception("could not record the check of device %s", device.id)

    # -- port counters for topology links (CR-010) ----------------------------

    async def _link_ports(self, device: Device) -> List[str]:
        """Ports of this device that are end A of a link. Empty for everything else, so devices
        that are not end A of any link never have their counters read."""
        if self.topology_repo is None or device.kind == "generic":
            return []
        return await asyncio.to_thread(self.topology_repo.end_a_ports, device.id)

    async def _store_traffic(self, device: Device, ports: List[str], data: dict):
        counters = data.pop("port_counters", None)
        if not ports or self.traffic is None:
            return
        try:
            await self.traffic.record(device.id, ports, counters or {})
        except Exception:
            log.exception("could not store the port readings of device %s", device.id)

    # -- direct ---------------------------------------------------------------

    async def _check_direct(self, device: Device, full: bool, ctx: CheckContext) -> CheckResult:
        if device.kind == "generic":
            error = "generic device has no agent - choose an agent router to ping it"
            ctx.attempts.append({"step": "setup", "ok": False, "ms": 0, "error": error})
            return await self._mark_offline(device, error)

        client = MikrotikClient(device)
        if not device.password:
            reachable = await client.is_reachable(attempts=ctx.attempts)
            status = "online" if reachable else "offline"
            await asyncio.to_thread(self.repo.set_status, device.id, status)
            return CheckResult(device.id, status)

        ports = await self._link_ports(device)
        extra = {"ports": ports} if ports else {}
        try:
            data = await client.collect_metrics(extra_commands=full, attempts=ctx.attempts, **extra)
        except Exception as exc:
            reachable = await client.is_reachable(attempts=ctx.attempts)
            status = "online" if reachable else "offline"
            await asyncio.to_thread(self.repo.set_status, device.id, status, str(exc))
            return CheckResult(device.id, status, error=str(exc))

        await self._store_traffic(device, ports, data)
        await asyncio.to_thread(self.repo.update_metrics, device.id, data)
        return CheckResult(device.id, "online", data=data)

    # -- through an agent (or a chain of two) ---------------------------------

    @staticmethod
    def _agent_error(agent: Device, exc: Exception) -> str:
        # hop errors already name the exact agent that failed
        if isinstance(exc, (AgentHopError, AgentForwardingError)):
            return str(exc)
        return f"agent {agent.display_name} unreachable: {exc}"

    async def _check_via_agent(self, device: Device, full: bool, ctx: CheckContext) -> CheckResult:
        try:
            chain = await self.clients.resolve_chain(device)     # outermost first
        except AgentChainError as exc:
            ctx.attempts.append({"step": "agents", "ok": False, "ms": 0, "error": str(exc)})
            return await self._mark_offline(device, str(exc))
        pinger = chain[-1]                                       # innermost: in the device's network

        async with AsyncExitStack() as stack:
            for agent in chain:                  # always outermost first -> no lock-order cycles
                await stack.enter_async_context(self._agent_limit(agent.id))

            try:
                ping = await MikrotikClient(pinger, via=chain[:-1]).ping(
                    device.ip, attempts=ctx.attempts)
            except Exception as exc:
                return await self._mark_offline(device, self._agent_error(pinger, exc))

            if ping["received"] == 0:
                return await self._mark_offline(
                    device, f"no ping reply via agent {pinger.display_name}")

            if device.kind == "generic" or not device.password:
                data = {"latency_ms": ping["avg_ms"]}
                await asyncio.to_thread(self.repo.update_metrics, device.id, data)
                return CheckResult(device.id, "online", data=data)

            ports = await self._link_ports(device)
            extra = {"ports": ports} if ports else {}
            try:
                data = await MikrotikClient(device, via=chain).collect_metrics(
                    extra_commands=full, attempts=ctx.attempts, **extra)
            except Exception as exc:
                # answers ping but we can't read it (bad login, API off, forwarding off...)
                await asyncio.to_thread(self.repo.set_status, device.id, "online", str(exc))
                return CheckResult(device.id, "online", error=str(exc),
                                   data={"latency_ms": ping["avg_ms"]})

            data["latency_ms"] = ping["avg_ms"]
            await self._store_traffic(device, ports, data)
            await asyncio.to_thread(self.repo.update_metrics, device.id, data)
            return CheckResult(device.id, "online", data=data)

    # -- bulk -----------------------------------------------------------------

    async def check_many(self, devices: List[Device], full: bool = False,
                         trigger: str = "manual") -> List[CheckResult]:
        if not devices:
            return []
        return await asyncio.gather(*(self._safe_check(d, full, trigger) for d in devices))

    async def _safe_check(self, device: Device, full: bool, trigger: str) -> CheckResult:
        """An unexpected error in ONE device's check must never lose the others' results."""
        try:
            return await self.check_device(device, full=full, trigger=trigger)
        except Exception as exc:
            log.exception("check of device %s (%s) failed unexpectedly", device.id, device.ip)
            return CheckResult(device.id, device.status, error=f"check failed: {exc}")

    async def check_all(self, full: bool = False, trigger: str = "manual") -> List[CheckResult]:
        devices = await asyncio.to_thread(self.repo.list_all)
        return await self.check_many(devices, full=full, trigger=trigger)

    async def run_commands(self, devices: List[Device], commands: List[str]) -> dict:
        """Run the same command list on many devices concurrently."""
        async def _run_one(device: Device):
            if device.kind == "generic":
                return device.id, "[skipped] generic device - ping only, no commands"
            async with self._semaphore:
                try:
                    client = await self.clients.for_device(device)
                    output = await client.run_commands(commands)
                except Exception as exc:
                    output = f"[error] {exc}"
                return device.id, output

        pairs = await asyncio.gather(*(_run_one(d) for d in devices))
        return dict(pairs)
