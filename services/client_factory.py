"""
Single place that decides HOW to reach a device: directly, or through its
chain of agents. Services ask this factory for a client instead of building
one, so agents (one level or two) never leak into their core logic.
"""
import asyncio
from typing import List

import config
from db.devices_repo import DeviceRepository
from models import Device
from mikrotik.client import MikrotikClient
from mikrotik.tunnel import AgentChainError


class ClientFactory:
    def __init__(self, devices_repo: DeviceRepository):
        self.repo = devices_repo

    async def resolve_chain(self, device: Device) -> List[Device]:
        """
        Follow "Via agent" links upward from `device`; return the agents OUTERMOST
        FIRST (the one the server reaches directly comes first). Empty = direct.

        At most MAX_AGENT_DEPTH agents are followed. This is a hard stop, not loop
        detection: a mis-configured loop (A via B, B via A) just runs into it.
        """
        chain: List[Device] = []
        current = device
        while current.agent_id:
            if len(chain) >= config.MAX_AGENT_DEPTH:
                raise AgentChainError(f"agent chain too deep (max {config.MAX_AGENT_DEPTH})")
            agent = await asyncio.to_thread(self.repo.get, current.agent_id)
            if agent is None:
                raise AgentChainError(f"agent #{current.agent_id} no longer exists")
            if not agent.password:
                raise AgentChainError(f"agent {agent.display_name} has no credentials")
            chain.append(agent)
            current = agent
        chain.reverse()
        return chain

    async def for_device(self, device: Device) -> MikrotikClient:
        return MikrotikClient(device, via=await self.resolve_chain(device))
