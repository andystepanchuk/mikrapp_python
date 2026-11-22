"""
Reaching a device THROUGH one or more agent MikroTiks.

An agent is a router sitting inside a remote network. We SSH to it and ask
it to open TCP connections on our behalf (standard SSH port forwarding,
"direct-tcpip"). That lets the server talk to hosts it cannot route to.

With two agents the sessions are nested:

    server --SSH--> A1 --(forwarded)--> SSH to A2 --(forwarded)--> target

Requirement on EVERY agent that has to forward (RouterOS disables it by default):

    /ip ssh set forwarding-enabled=local
"""
from contextlib import asynccontextmanager
from typing import List

import asyncssh

import config
from models import Device
from mikrotik.ssh_client import MikrotikSshClient

OPEN_ADMINISTRATIVELY_PROHIBITED = 1     # SSH channel-open failure code for "forwarding not allowed"


class AgentChainError(Exception):
    """The chain of agents cannot be built (agent missing, too deep, no credentials)."""


class AgentHopError(Exception):
    """An agent on the path cannot be reached, or could not connect any further."""


class AgentForwardingError(Exception):
    """An agent refused to forward a connection (forwarding disabled)."""


def forwarding_message(agent: Device) -> str:
    return (f"agent {agent.display_name} refused to forward the connection - "
            f"on {agent.display_name} run: /ip ssh set forwarding-enabled=local")


def channel_error(agent: Device, exc: Exception, target: str) -> Exception:
    """Turn an asyncssh channel-open failure into a message that names the hop."""
    if getattr(exc, "code", None) == OPEN_ADMINISTRATIVELY_PROHIBITED:
        return AgentForwardingError(forwarding_message(agent))
    reason = getattr(exc, "reason", "") or str(exc)
    return AgentHopError(f"agent {agent.display_name} could not connect to {target}: {reason}")


@asynccontextmanager
async def open_agent_chain(agents: List[Device]):
    """
    Open the nested SSH sessions through `agents` (outermost first) and yield the
    INNERMOST asyncssh connection; it is the one that can forward to the final target.
    Sessions are closed again in reverse order.
    """
    if not agents:
        raise ValueError("open_agent_chain needs at least one agent")
    opened: List[MikrotikSshClient] = []
    try:
        tunnel = None
        for i, agent in enumerate(agents):
            ssh = MikrotikSshClient(agent.ip, agent.username, agent.password,
                                    port=agent.port, timeout=config.SSH_TIMEOUT, tunnel=tunnel)
            try:
                await ssh.connect()
            except asyncssh.ChannelOpenError as exc:
                if i == 0:
                    raise AgentHopError(f"agent {agent.display_name} unreachable: {exc}") from exc
                # the previous agent was asked to forward to this one and could not
                raise channel_error(agents[i - 1], exc,
                                    f"agent {agent.display_name} ({agent.ip}:{agent.port})") from exc
            except Exception as exc:
                via = f" through {' \u2192 '.join(a.display_name for a in agents[:i])}" if i else ""
                raise AgentHopError(f"agent {agent.display_name} unreachable{via}: {exc}") from exc
            opened.append(ssh)
            tunnel = ssh.connection
        yield tunnel
    finally:
        for ssh in reversed(opened):
            await ssh.close()
