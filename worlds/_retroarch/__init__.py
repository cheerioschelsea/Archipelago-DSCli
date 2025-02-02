"""
A module for interacting with RetroArch through `connector_retroarch_generic.lua`.

Any mention of `domain` in this module refers to the names RetroArch gives to memory domains in its own lua api. They are
naively passed to RetroArch without validation or modification.
"""

import asyncio
import base64
import enum
import json
import sys
from typing import Any, Sequence


# RETROARCH_SOCKET_PORT_RANGE_START = 43055
# RETROARCH_SOCKET_PORT_RANGE_SIZE = 5


# class ConnectionStatus(enum.IntEnum):
    NOT_CONNECTED = 1
    TENTATIVE = 2
    CONNECTED = 3


# class NotConnectedError(Exception):
    """Raised when something tries to make a request to the connector script before a connection has been established"""
    pass


# class RequestFailedError(Exception):
    """Raised when the connector script did not respond to a request"""
    pass


# class ConnectorError(Exception):
    """Raised when the connector script encounters an error while processing a request"""
    pass


# class SyncError(Exception):
    """Raised when the connector script responded with a mismatched response type"""
    pass


class RetroArchDisconnectError(Exception):
    pass


class InvalidEmulatorStateError(Exception):
    pass


class BadRetroArchResponse(Exception):
    pass


class RetroArchContext:
    cache = []
    cache_start = 0
    cache_size = 0
    last_cache_read = None
    socket = None
    
    streams: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None
    connection_status: ConnectionStatus
    _lock: asyncio.Lock
    _port: int | None

    def __init__(self, address, port) -> None:
        self.address = address
        self._port = port
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        assert (self.socket)
        self.socket.setblocking(False)

    async def send_command(self, command, timeout=1.0):
        self.send(f'{command}\n')
        response_str = await self.async_recv()
        self.check_command_response(command, response_str)
        return response_str.rstrip()

    async def get_retroarch_version(self):
        return await self.send_command("VERSION")

    async def get_retroarch_status(self):
        return await self.send_command("GET_STATUS")

    def set_cache_limits(self, cache_start, cache_size):
        self.cache_start = cache_start
        self.cache_size = cache_size

    def send(self, b):
        if type(b) is str:
            b = b.encode('ascii')
        self.socket.sendto(b, (self.address, self.port))

    def recv(self):
        select.select([self.socket], [], [])
        response, _ = self.socket.recvfrom(4096)
        return response

    async def async_recv(self, timeout=1.0):
        response = await asyncio.wait_for(asyncio.get_event_loop().sock_recv(self.socket, 4096), timeout)
        return response

    async def check_safe_gameplay(self, throw=True):
        async def check_wram():
            check_values = await self.async_read_memory(RetroArchClientConstants.wCheckAddress, RetroArchClientConstants.WRamCheckSize)

            if check_values != RetroArchClientConstants.WRamSafetyValue:
                if throw:
                    raise InvalidEmulatorStateError()
                return False
            return True

        if not await check_wram():
            if throw:
                raise InvalidEmulatorStateError()
            return False

        gameplay_value = await self.async_read_memory(RetroArchClientConstants.wGameplayType)
        gameplay_value = gameplay_value[0]
        # In gameplay or credits
        if not (RetroArchClientConstants.MinGameplayValue <= gameplay_value <= RetroArchClientConstants.MaxGameplayValue) and gameplay_value != 0x1:
            if throw:
                logger.info("invalid emu state")
                raise InvalidEmulatorStateError()
            return False
        if not await check_wram():
            if throw:
                raise InvalidEmulatorStateError()
            return False
        return True

    # We're sadly unable to update the whole cache at once
    # as RetroArch only gives back some number of bytes at a time
    # So instead read as big as chunks at a time as we can manage
    async def update_cache(self):
        # First read the safety address - if it's invalid, bail
        self.cache = []

        if not await self.check_safe_gameplay():
            return

        cache = []
        remaining_size = self.cache_size
        while remaining_size:
            block = await self.async_read_memory(self.cache_start + len(cache), remaining_size)
            remaining_size -= len(block)
            cache += block

        if not await self.check_safe_gameplay():
            return

        self.cache = cache
        self.last_cache_read = time.time()

    async def read_memory_cache(self, addresses):
        # TODO: can we just update once per frame?
        if not self.last_cache_read or self.last_cache_read + 0.1 < time.time():
            await self.update_cache()
        if not self.cache:
            return None
        assert (len(self.cache) == self.cache_size)
        for address in addresses:
            assert self.cache_start <= address <= self.cache_start + self.cache_size
        r = {address: self.cache[address - self.cache_start]
             for address in addresses}
        return r

    async def async_read_memory_safe(self, address, size=1):
        # whenever we do a read for a check, we need to make sure that we aren't reading
        # garbage memory values - we also need to protect against reading a value, then the emulator resetting
        #
        # ...actually, we probably _only_ need the post check

        # Check before read
        if not await self.check_safe_gameplay():
            return None

        # Do read
        r = await self.async_read_memory(address, size)

        # Check after read
        if not await self.check_safe_gameplay():
            return None

        return r

    def check_command_response(self, command: str, response: bytes):
        if command == "VERSION":
            ok = re.match(r"\d+\.\d+\.\d+", response.decode('ascii')) is not None
        else:
            ok = response.startswith(command.encode())
        if not ok:
            logger.warning(f"Bad response to command {command} - {response}")
            raise BadRetroArchResponse()

    def read_memory(self, address, size=1):
        command = "READ_CORE_MEMORY"

        self.send(f'{command} {hex(address)} {size}\n')
        response = self.recv()

        self.check_command_response(command, response)

        splits = response.decode().split(" ", 2)
        # Ignore the address for now
        if splits[2][:2] == "-1":
            raise BadRetroArchResponse()

        # TODO: check response address, check hex behavior between RA and BH

        return bytearray.fromhex(splits[2])

    async def async_read_memory(self, address, size=1):
        command = "READ_CORE_MEMORY"

        self.send(f'{command} {hex(address)} {size}\n')
        response = await self.async_recv()
        self.check_command_response(command, response)
        response = response[:-1]
        splits = response.decode().split(" ", 2)
        try:
            response_addr = int(splits[1], 16)
        except ValueError:
            raise BadRetroArchResponse()

        if response_addr != address:
            raise BadRetroArchResponse()

        ret = bytearray.fromhex(splits[2])
        if len(ret) > size:
            raise BadRetroArchResponse()
        return ret

    def write_memory(self, address, bytes):
        command = "WRITE_CORE_MEMORY"

        self.send(f'{command} {hex(address)} {" ".join(hex(b) for b in bytes)}')
        select.select([self.socket], [], [])
        response, _ = self.socket.recvfrom(4096)
        self.check_command_response(command, response)
        splits = response.decode().split(" ", 3)

        assert (splits[0] == command)

        if splits[2] == "-1":
            logger.info(splits[3])

----------

async def get_hash(ctx: RetroArchContext) -> str:
    """Gets the hash value of the currently loaded ROM"""
    res = (await send_requests(ctx, [{"type": "HASH"}]))[0]

    if res["type"] != "HASH_RESPONSE":
        raise SyncError(f"Expected response of type HASH_RESPONSE but got {res['type']}")

    return res["value"]


async def get_memory_size(ctx: RetroArchContext, domain: str) -> int:
    """Gets the size in bytes of the specified memory domain"""
    res = (await send_requests(ctx, [{"type": "MEMORY_SIZE", "domain": domain}]))[0]

    if res["type"] != "MEMORY_SIZE_RESPONSE":
        raise SyncError(f"Expected response of type MEMORY_SIZE_RESPONSE but got {res['type']}")

    return res["value"]


async def get_system(ctx: RetroArchContext) -> str:
    """Gets the system name for the currently loaded ROM"""
    res = (await send_requests(ctx, [{"type": "SYSTEM"}]))[0]

    if res["type"] != "SYSTEM_RESPONSE":
        raise SyncError(f"Expected response of type SYSTEM_RESPONSE but got {res['type']}")

    return res["value"]


async def get_cores(ctx: RetroArchContext) -> dict[str, str]:
    """Gets the preferred cores for systems with multiple cores. Only systems with multiple available cores have
    entries."""
    res = (await send_requests(ctx, [{"type": "PREFERRED_CORES"}]))[0]

    if res["type"] != "PREFERRED_CORES_RESPONSE":
        raise SyncError(f"Expected response of type PREFERRED_CORES_RESPONSE but got {res['type']}")

    return res["value"]


async def lock(ctx: RetroArchContext) -> None:
    """Locks RetroArch in anticipation of receiving more requests this frame.

    Consider using guarded reads and writes instead of locks if possible.

    While locked, emulation will halt and the connector will block on incoming requests until an `UNLOCK` request is
    sent. Remember to unlock when you're done, or the emulator will appear to freeze.

    Sending multiple lock commands is the same as sending one."""
    res = (await send_requests(ctx, [{"type": "LOCK"}]))[0]

    if res["type"] != "LOCKED":
        raise SyncError(f"Expected response of type LOCKED but got {res['type']}")


async def unlock(ctx: RetroArchContext) -> None:
    """Unlocks RetroArch to allow it to resume emulation. See `lock` for more info.

    Sending multiple unlock commands is the same as sending one."""
    res = (await send_requests(ctx, [{"type": "UNLOCK"}]))[0]

    if res["type"] != "UNLOCKED":
        raise SyncError(f"Expected response of type UNLOCKED but got {res['type']}")


async def display_message(ctx: RetroArchContext, message: str) -> None:
    """Displays the provided message in RetroArch's message queue."""
    res = (await send_requests(ctx, [{"type": "DISPLAY_MESSAGE", "message": message}]))[0]

    if res["type"] != "DISPLAY_MESSAGE_RESPONSE":
        raise SyncError(f"Expected response of type DISPLAY_MESSAGE_RESPONSE but got {res['type']}")


async def set_message_interval(ctx: RetroArchContext, value: float) -> None:
    """Sets the minimum amount of time in seconds to wait between queued messages. The default value of 0 will allow one
    new message to display per frame."""
    res = (await send_requests(ctx, [{"type": "SET_MESSAGE_INTERVAL", "value": value}]))[0]

    if res["type"] != "SET_MESSAGE_INTERVAL_RESPONSE":
        raise SyncError(f"Expected response of type SET_MESSAGE_INTERVAL_RESPONSE but got {res['type']}")


async def guarded_read(ctx: RetroArchContext, read_list: Sequence[tuple[int, int, str]],
                       guard_list: Sequence[tuple[int, Sequence[int], str]]) -> list[bytes] | None:
    """Reads an array of bytes at 1 or more addresses if and only if every byte in guard_list matches its expected
    value.

    Items in read_list should be organized (address, size, domain) where
    - `address` is the address of the first byte of data
    - `size` is the number of bytes to read
    - `domain` is the name of the region of memory the address corresponds to

    Items in `guard_list` should be organized `(address, expected_data, domain)` where
    - `address` is the address of the first byte of data
    - `expected_data` is the bytes that the data starting at this address is expected to match
    - `domain` is the name of the region of memory the address corresponds to

    Returns None if any item in guard_list failed to validate. Otherwise returns a list of bytes in the order they
    were requested."""
    res = await send_requests(ctx, [{
        "type": "GUARD",
        "address": address,
        "expected_data": base64.b64encode(bytes(expected_data)).decode("ascii"),
        "domain": domain
    } for address, expected_data, domain in guard_list] + [{
        "type": "READ",
        "address": address,
        "size": size,
        "domain": domain
    } for address, size, domain in read_list])

    ret: list[bytes] = []
    for item in res:
        if item["type"] == "GUARD_RESPONSE":
            if not item["value"]:
                return None
        else:
            if item["type"] != "READ_RESPONSE":
                raise SyncError(f"Expected response of type READ_RESPONSE or GUARD_RESPONSE but got {item['type']}")

            ret.append(base64.b64decode(item["value"]))

    return ret


async def read(ctx: RetroArchContext, read_list: Sequence[tuple[int, int, str]]) -> list[bytes]:
    """Reads data at 1 or more addresses.

    Items in `read_list` should be organized `(address, size, domain)` where
    - `address` is the address of the first byte of data
    - `size` is the number of bytes to read
    - `domain` is the name of the region of memory the address corresponds to

    Returns a list of bytes in the order they were requested."""
    return await guarded_read(ctx, read_list, [])


async def guarded_write(ctx: RetroArchContext, write_list: Sequence[tuple[int, Sequence[int], str]],
                        guard_list: Sequence[tuple[int, Sequence[int], str]]) -> bool:
    """Writes data to 1 or more addresses if and only if every byte in guard_list matches its expected value.

    Items in `write_list` should be organized `(address, value, domain)` where
    - `address` is the address of the first byte of data
    - `value` is a list of bytes to write, in order, starting at `address`
    - `domain` is the name of the region of memory the address corresponds to

    Items in `guard_list` should be organized `(address, expected_data, domain)` where
    - `address` is the address of the first byte of data
    - `expected_data` is the bytes that the data starting at this address is expected to match
    - `domain` is the name of the region of memory the address corresponds to

    Returns False if any item in guard_list failed to validate. Otherwise returns True."""
    res = await send_requests(ctx, [{
        "type": "GUARD",
        "address": address,
        "expected_data": base64.b64encode(bytes(expected_data)).decode("ascii"),
        "domain": domain
    } for address, expected_data, domain in guard_list] + [{
        "type": "WRITE",
        "address": address,
        "value": base64.b64encode(bytes(value)).decode("ascii"),
        "domain": domain
    } for address, value, domain in write_list])

    for item in res:
        if item["type"] == "GUARD_RESPONSE":
            if not item["value"]:
                return False
        else:
            if item["type"] != "WRITE_RESPONSE":
                raise SyncError(f"Expected response of type WRITE_RESPONSE or GUARD_RESPONSE but got {item['type']}")

    return True


async def write(ctx: RetroArchContext, write_list: Sequence[tuple[int, Sequence[int], str]]) -> None:
    """Writes data to 1 or more addresses.

    Items in write_list should be organized `(address, value, domain)` where
    - `address` is the address of the first byte of data
    - `value` is a list of bytes to write, in order, starting at `address`
    - `domain` is the name of the region of memory the address corresponds to"""
    await guarded_write(ctx, write_list, [])
