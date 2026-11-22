"""
SSH fallback for devices where the binary API is disabled/blocked.

Uses asyncssh so it is non-blocking like the API client -- this replaces
the old approach of `shell_exec("sshpass ... ssh ...")`, which spawned a
new OS process for every single command.
"""
import asyncio
import asyncssh

# Older RouterOS versions only offer legacy algorithms; widen the
# negotiation so we don't fail against a 10-year-old router.
_LEGACY_KEX = ("diffie-hellman-group14-sha1", "diffie-hellman-group1-sha1", "diffie-hellman-group-exchange-sha1")
_LEGACY_KEYS = ("ssh-rsa", "ssh-dss")


class MikrotikSshClient:
    def __init__(self, host: str, username: str, password: str,
                 port: int = 22, timeout: float = 8.0, tunnel=None):
        self._tunnel = tunnel          # an open asyncssh connection to route through (agent)
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.timeout = timeout
        self._conn: asyncssh.SSHClientConnection | None = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    @property
    def connection(self):
        return self._conn

    async def connect(self):
        extra = {"tunnel": self._tunnel} if self._tunnel is not None else {}
        try:
            self._conn = await asyncssh.connect(
                self.host, port=self.port, username=self.username,
                password=self.password, known_hosts=None,
                connect_timeout=self.timeout, **extra,
            )
        except asyncssh.ChannelOpenError:
            raise  # forwarding refused by the agent -- legacy algorithms won't help
        except asyncssh.Error:
            # Retry once with legacy algorithms enabled for old RouterOS.
            self._conn = await asyncssh.connect(
                self.host, port=self.port, username=self.username,
                password=self.password, known_hosts=None,
                connect_timeout=self.timeout,
                kex_algs=_LEGACY_KEX, server_host_key_algs=_LEGACY_KEYS, **extra,
            )

    async def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    async def run(self, command: str) -> str:
        result = await asyncio.wait_for(self._conn.run(command, check=False), timeout=self.timeout)
        return result.stdout or ""
