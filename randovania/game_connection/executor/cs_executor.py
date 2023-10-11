import asyncio
import dataclasses
import json
import logging
import struct
from asyncio import StreamReader, StreamWriter
from enum import IntEnum
from typing import Self
from uuid import UUID

from tsc_utils.flags import flag_to_address
from tsc_utils.numbers import TscInput, tsc_value_to_num

from randovania.bitpacking.json_dataclass import JsonDataclass
from randovania.lib import enum_lib


class PacketType(IntEnum):
    SERVER_INFO = 0
    EXEC_SCRIPT = 1
    GET_FLAGS = 2
    QUEUE_EVENTS = 3
    READ_MEM = 4
    WRITE_MEM = 5
    GET_STATE = 6

    ERROR = -1  # receiving
    DISCONNECT = -1  # sending


class GameState(IntEnum):
    can_read_profile: bool

    INTRO = 0
    TITLE = 1
    GAMEPLAY = 2
    INVENTORY = 3
    TELEPORTER = 4
    MINIMAP = 5
    ISLAND_FALL = 6
    PAUSE_MENU = 7

    NONE = -1


enum_lib.add_per_enum_field(
    GameState,
    "can_read_profile",
    {
        GameState.INTRO: False,
        GameState.TITLE: False,
        GameState.GAMEPLAY: True,
        GameState.INVENTORY: True,
        GameState.TELEPORTER: True,
        GameState.MINIMAP: True,
        GameState.ISLAND_FALL: True,
        GameState.PAUSE_MENU: True,
        GameState.NONE: False,
    },
)


class TSCError(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class CSServerInfo(JsonDataclass):
    api_version: int
    platform: str
    uuid: UUID
    offsets: dict[str, int]


@dataclasses.dataclass()
class CSSocketHolder:
    reader: StreamReader
    writer: StreamWriter


@dataclasses.dataclass(frozen=True)
class Packet:
    type: PacketType
    message: bytes = b""

    HEADER_FMT = "<bi"

    @property
    def to_stream(self) -> bytes:
        header = struct.pack(Packet.HEADER_FMT, self.type.value, len(self.message))
        return header + self.message

    @classmethod
    async def from_stream(cls, stream: StreamReader) -> Self:
        header: bytes = await asyncio.wait_for(stream.read(5), None)
        type, size = struct.unpack(Packet.HEADER_FMT, header)
        if size > 0:
            msg = await asyncio.wait_for(stream.read(size), None)
        else:
            msg = b""
        return Packet(PacketType(type), msg)


@dataclasses.dataclass(frozen=True)
class WeaponData:
    weapon_id: int
    level: int
    exp: int
    capacity: int
    ammo: int

    @classmethod
    def from_stream(cls, data: bytes) -> Self:
        weapon_id, level, exp, capacity, ammo = struct.unpack("<5i", data)
        return WeaponData(
            weapon_id,
            level,
            exp,
            capacity,
            ammo,
        )


def _resolve_tsc_value(value: int | TscInput) -> int:
    return tsc_value_to_num(value) if isinstance(value, TscInput) else value


def _message_for_tsc_value_list(values: list[int | TscInput]) -> bytes:
    return struct.pack(f"<{len(values)}i", *[_resolve_tsc_value(value) for value in values])


class CSExecutor:
    _port = 5451
    _socket: CSSocketHolder | None = None
    _socket_error: Exception | None = None
    server_info: CSServerInfo | None = None

    def __init__(self, ip: str) -> None:
        self.logger = logging.getLogger(type(self).__name__)
        self._ip = ip

    @property
    def ip(self):
        return self._ip

    @property
    def lock_identifier(self) -> str | None:
        return None

    async def connect(self) -> str | None:
        if self.is_connected():
            return None

        try:
            self._socket_error = None

            self.logger.debug("Connecting to %s:%d.", self._ip, self._port)
            reader, writer = await asyncio.open_connection(self._ip, self._port)
            self._socket = CSSocketHolder(reader, writer)

            self.logger.debug("Connection open. Requesting API details...")
            server_info = await self.get_server_info()
            self.logger.debug(
                "Server replied with API level %s, platform %s, uuid %s, and offsets %s. Connection successful.",
                server_info.api_version,
                server_info.platform,
                server_info.uuid,
                server_info.offsets,
            )
            self.server_info = server_info

            self.logger.info("Connected")

            return None

        except (
            OSError,
            AttributeError,
            asyncio.TimeoutError,
            struct.error,
            UnicodeError,
            RuntimeError,
            ValueError,
            TSCError,
        ) as e:
            # UnicodeError is for some invalid ip addresses
            self._socket = None
            message = f"Unable to connect to {self._ip}:{self._port} - ({type(e).__name__}) {e}"
            self._socket_error = e
            return message

    def disconnect(self):
        socket = self._socket
        self._socket = None
        if socket is not None:
            socket.writer.close()

    def is_connected(self) -> bool:
        return self._socket is not None

    async def _send_request(self, packet: Packet) -> Packet:
        self._socket.writer.write(packet.to_stream)
        await asyncio.wait_for(self._socket.writer.drain(), timeout=30)

        if packet.type == PacketType.DISCONNECT:
            return packet  # no response from server when dc'ing

        response = await Packet.from_stream(self._socket.reader)
        if response.type == PacketType.ERROR:
            raise TSCError(response.message.decode("cp1252"))
        if response.type != packet.type:
            raise TSCError(f"Expected {packet.type}, got {response.type}")
        return response

    async def get_server_info(self) -> CSServerInfo:
        response = await self._send_request(Packet(PacketType.SERVER_INFO))
        return CSServerInfo.from_json(json.loads(response.message.decode("cp1252")))

    async def exec_script(self, script: str):
        await self._send_request(Packet(PacketType.EXEC_SCRIPT, script.encode("cp1252")))

    async def get_flags(self, flags: list[int | TscInput]) -> list[bool]:
        msg = _message_for_tsc_value_list(flags)
        response = await self._send_request(Packet(PacketType.GET_FLAGS, msg))
        return [f != 0 for f in response.message]

    async def queue_events(self, events: list[int | TscInput]):
        msg = _message_for_tsc_value_list(events)
        await self._send_request(Packet(PacketType.QUEUE_EVENTS, msg))

    async def request_disconnect(self):
        await self._send_request(Packet(PacketType.DISCONNECT))
        self.disconnect()

    async def read_memory(self, offset: int, size: int, *, base_offset: str | None = None) -> bytes:
        if base_offset is not None:
            offset += self.server_info.offsets[base_offset]

        msg = struct.pack("<iH", offset, size)
        response = await self._send_request(Packet(PacketType.READ_MEM, msg))
        return response.message

    async def write_memory(self, offset: int, data: bytes, *, base_offset: str | None = None):
        if base_offset is not None:
            offset += self.server_info.offsets[base_offset]

        msg = struct.pack(f"<i{len(data)}s", offset, data)
        await self._send_request(Packet(PacketType.WRITE_MEM, msg))

    async def read_memory_flags(self, start_flag: int | TscInput, size: int) -> bytes:
        """
        Please only use with byte-aligned flags
        """
        address = flag_to_address(start_flag, self.server_info.offsets["flags"])
        return await self.read_memory(address.offset, size)

    async def write_memory_flags(self, start_flag: int | TscInput, data: bytes):
        address = flag_to_address(start_flag, self.server_info.offsets["flags"])
        await self.write_memory(address.offset, data)

    async def get_flag(self, flag: int | TscInput) -> bool:
        return await self.get_flags([flag])[0]

    async def set_flag(self, flag: int | TscInput, value: bool):
        address = flag_to_address(flag, self.server_info.offsets["flags"])
        byte = await self.read_memory(address.offset, 1)
        if value:
            byte |= 1 << address.bit
        else:
            byte &= ~(1 << address.bit)
        await self.write_memory(address.offset, byte)

    async def get_game_state(self) -> GameState:
        response = await self._send_request(Packet(PacketType.GET_STATE))
        return GameState(struct.unpack("b", response.message))

    async def get_profile_uuid(self) -> UUID:
        response = await self.read_memory(112, 16, base_offset="map_flags")
        return UUID(bytes=response)

    async def get_weapons(self) -> list[WeaponData]:
        weapons = []

        arms_table = await self.read_memory(0, 160, base_offset="arms_table")
        while arms_table:
            arm, arms_table = arms_table[:8], arms_table[8:]
            weapons.append(WeaponData.from_stream(arm))

        return weapons

    async def get_received_items(self) -> int:
        return struct.unpack("b", await self.read_memory_flags(7400, 1))

    async def set_received_items(self, received: int):
        await self.write_memory_flags(7400, struct.pack("b", received))
