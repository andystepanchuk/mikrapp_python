"""
Minimal async client for the RouterOS binary API (default port 8728).

This talks the native protocol directly over asyncio streams instead of
spawning a process per command (which is what the old PHP tool did via
`shell_exec`/sshpass). One TCP connection handles login plus as many
commands as needed, and many devices can be talked to concurrently
because everything here is non-blocking.

Protocol reference: https://help.mikrotik.com/docs/display/ROS/API
"""
import asyncio
import hashlib
import binascii
from typing import Optional


class RouterOsApiError(Exception):
    pass


class RouterOsAuthError(RouterOsApiError):
    pass


def _encode_length(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    elif length < 0x4000:
        length |= 0x8000
        return bytes([(length >> 8) & 0xFF, length & 0xFF])
    elif length < 0x200000:
        length |= 0xC00000
        return bytes([(length >> 16) & 0xFF, (length >> 8) & 0xFF, length & 0xFF])
    elif length < 0x10000000:
        length |= 0xE0000000
        return bytes([(length >> 24) & 0xFF, (length >> 16) & 0xFF,
                       (length >> 8) & 0xFF, length & 0xFF])
    else:
        return bytes([0xF0]) + length.to_bytes(4, "big")


def _encode_word(word: str) -> bytes:
    data = word.encode("utf-8")
    return _encode_length(len(data)) + data


class RouterOsApiClient:
    def __init__(self, host: str, username: str, password: str,
                 port: int = 8728, timeout: float = 6.0, connector=None):
        # `connector` is an optional async callable returning (reader, writer).
        # When given, it is used INSTEAD of a direct TCP connection -- this is how
        # we reach a device through an agent router (SSH port-forward).
        self._connector = connector
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.timeout = timeout
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def connect(self):
        if self._connector is not None:
            opener = self._connector()
        else:
            opener = asyncio.open_connection(self.host, self.port)
        self._reader, self._writer = await asyncio.wait_for(opener, timeout=self.timeout)
        await self._login()

    async def close(self):
        if self._writer:
            try:
                self._writer.close()
            except Exception:
                pass
        self._reader = None
        self._writer = None

    # -- wire protocol -----------------------------------------------------

    async def _read_length(self) -> int:
        first = (await self._reader.readexactly(1))[0]
        if (first & 0x80) == 0x00:
            return first
        if (first & 0xC0) == 0x80:
            b = await self._reader.readexactly(1)
            return ((first & ~0xC0) << 8) + b[0]
        if (first & 0xE0) == 0xC0:
            b = await self._reader.readexactly(2)
            return ((first & ~0xE0) << 16) + (b[0] << 8) + b[1]
        if (first & 0xF0) == 0xE0:
            b = await self._reader.readexactly(3)
            return ((first & ~0xF0) << 24) + (b[0] << 16) + (b[1] << 8) + b[2]
        b = await self._reader.readexactly(4)
        return (b[0] << 24) + (b[1] << 16) + (b[2] << 8) + b[3]

    async def _read_sentence(self) -> list:
        words = []
        while True:
            length = await self._read_length()
            if length == 0:
                return words
            data = await self._reader.readexactly(length)
            words.append(data.decode("utf-8", errors="replace"))

    def _write_sentence(self, words: list):
        buf = b"".join(_encode_word(w) for w in words) + b"\x00"
        self._writer.write(buf)

    async def _flush(self):
        await self._writer.drain()

    # -- high level ----------------------------------------------------

    async def _login(self):
        # Modern RouterOS (>=6.43): plain login in one round trip.
        self._write_sentence(["/login", f"=name={self.username}", f"=password={self.password}"])
        await self._flush()
        reply = await asyncio.wait_for(self._read_sentence(), timeout=self.timeout)

        if reply and reply[0] == "!trap":
            raise RouterOsAuthError(_words_to_dict(reply).get("message", "login failed"))

        if reply and reply[0] == "!done":
            attrs = _words_to_dict(reply)
            if "ret" not in attrs:
                return  # modern login succeeded, nothing else needed
            # Old-style challenge/response login
            challenge = binascii.unhexlify(attrs["ret"])
            md5 = hashlib.md5()
            md5.update(b"\x00")
            md5.update(self.password.encode("utf-8"))
            md5.update(challenge)
            response = "00" + md5.hexdigest()
            self._write_sentence(["/login", f"=name={self.username}", f"=response={response}"])
            await self._flush()
            reply2 = await asyncio.wait_for(self._read_sentence(), timeout=self.timeout)
            if reply2 and reply2[0] == "!trap":
                raise RouterOsAuthError(_words_to_dict(reply2).get("message", "login failed"))
            return

        raise RouterOsApiError(f"unexpected login reply: {reply}")

    async def talk(self, command: str, params: Optional[dict] = None) -> list:
        """Run one RouterOS API command, return list of !re rows as dicts."""
        words = [command]
        for k, v in (params or {}).items():
            words.append(f"={k}={v}")
        self._write_sentence(words)
        await self._flush()

        rows = []
        while True:
            sentence = await asyncio.wait_for(self._read_sentence(), timeout=self.timeout)
            if not sentence:
                continue
            tag = sentence[0]
            if tag == "!re":
                rows.append(_words_to_dict(sentence))
            elif tag == "!trap":
                raise RouterOsApiError(_words_to_dict(sentence).get("message", "command failed"))
            elif tag == "!done":
                trailer = _words_to_dict(sentence)
                if "ret" in trailer and not rows:
                    # commands like /export return raw text via =ret=
                    rows.append({"ret": trailer["ret"]})
                return rows
            elif tag == "!fatal":
                raise RouterOsApiError("fatal: " + ", ".join(sentence[1:]))


def _words_to_dict(words: list) -> dict:
    out = {}
    for w in words[1:]:
        if w.startswith("="):
            body = w[1:]
            if "=" in body:
                k, v = body.split("=", 1)
                out[k] = v
    return out
