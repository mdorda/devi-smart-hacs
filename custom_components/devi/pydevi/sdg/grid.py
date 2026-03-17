from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import random
import struct

from ..models import HouseConfig, ThermostatInfo
from ..dominion.codec import DominionPacket
from . import control
from .control import Endpoint, PeerReply, ProtocolVersion
from .crypto import (
    CRYPTO_BOX_BEFORENMBYTES,
    CRYPTO_BOX_BOXZEROBYTES,
    CRYPTO_BOX_NONCEBYTES,
    CRYPTO_BOX_PUBLICKEYBYTES,
    CRYPTO_BOX_ZEROBYTES,
    CRYPTO_SCALARMULT_BYTES,
    CURVECP_COOKIEBYTES,
    build_long_term_nonce,
    build_random_long_term_nonce,
    build_short_term_nonce,
    calculate_public_key,
    crypto_box_afternm_old,
    crypto_box_beforenm,
    crypto_box_keypair,
    crypto_box_old,
    crypto_box_open_afternm_old,
    crypto_box_open_old,
    crypto_hash,
    crypto_scalarmult,
    crypto_scalarmult_base,
    crypto_stream_xor,
    generate_private_key,
)
from .transport import CMD_COOK, CMD_HELO, CMD_MESG, CMD_REDY, CMD_TELL, CMD_VOCH, CMD_WELC, PacketHeader, build_data_packet, build_header


DANFOSS_SERVERS = (
    Endpoint("77.66.11.90", 443),
    Endpoint("77.66.11.92", 443),
    Endpoint("5.179.92.180", 443),
    Endpoint("5.179.92.182", 443),
)

MODE_GRID = "grid"
MODE_PEER = "peer"
MODE_PAIRING = "pairing"

MSG_PAIRING_CHALLENGE = 3
MSG_PAIRING_RESPONSE = 4
MSG_PAIRING_RESULT = 5

LOGGER = logging.getLogger(__name__)

DEVICEGLOBAL = 0
DOMINION_SYSTEM = 5
GLOBAL_SERIALNUMBER = 22
SYSTEM_ROOM_NAME = 29250
SYSTEM_HOUSE_NAME = 29251
SYSTEM_ZONE_NAME = 29252


class SDGProtocolError(RuntimeError):
    pass


class PairingRefusedError(SDGProtocolError):
    pass


@dataclass(slots=True)
class _RemoteConnection:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    client_public_key: bytes
    client_secret: bytes
    mode: str
    protocol: str
    peer_id_hex: str | None = None
    tunnel_id: bytes | None = None
    connected: bool = False
    ping_sequence: int = 0
    ping_delay: int | None = None
    last_ping_ms: float | None = None
    client_temp_public_key: bytes = b""
    client_temp_secret: bytes = b""
    server_public_key: bytes = b""
    server_cookie: bytes = b""
    shared_key: bytes = b""
    _welc_key: bytes = b""
    _pending_reply: PeerReply | None = None
    _pending_message: tuple[int, bytes] | None = None
    _pairing_expected: bytes | None = None
    _nonce: int = 0
    _discard_first_bytes: int = 0

    def __post_init__(self) -> None:
        self.client_temp_public_key = b""
        self.client_temp_secret = b""
        self.server_public_key = bytes(CRYPTO_BOX_PUBLICKEYBYTES)
        self.server_cookie = bytes(CURVECP_COOKIEBYTES)
        self.shared_key = bytes(CRYPTO_BOX_BEFORENMBYTES)
        self._nonce = 0
        self._discard_first_bytes = 1 if self.mode == MODE_PEER and self.protocol == "dominion-1.0" else 0
        LOGGER.debug(
            "SDG init mode=%s protocol=%s peer_id=%s tunnel=%s",
            self.mode,
            self.protocol,
            self.peer_id_hex,
            self.tunnel_id.hex() if self.tunnel_id is not None else None,
        )

    @property
    def peer_id_bytes(self) -> bytes:
        return bytes.fromhex(self.peer_id_hex) if self.peer_id_hex else bytes(CRYPTO_BOX_PUBLICKEYBYTES)

    async def close(self) -> None:
        LOGGER.debug("SDG close mode=%s protocol=%s peer_id=%s", self.mode, self.protocol, self.peer_id_hex)
        self.writer.close()
        await self.writer.wait_closed()
        self.connected = False

    async def send_raw(self, payload: bytes) -> None:
        self.writer.write(payload)
        await self.writer.drain()

    async def read_frame(self) -> bytes:
        size_prefix = await self.reader.readexactly(2)
        size = struct.unpack(">H", size_prefix)[0]
        return size_prefix + await self.reader.readexactly(size)

    def _next_nonce_value(self) -> int:
        value = self._nonce
        self._nonce += 1
        return value

    def _encrypt_old(self, message: bytes, nonce: bytes, public_key: bytes, secret_key: bytes) -> bytes:
        return crypto_box_old(message, nonce, public_key, secret_key)[CRYPTO_BOX_BOXZEROBYTES:]

    def _decrypt_old(self, ciphertext: bytes, nonce: bytes, public_key: bytes, secret_key: bytes) -> bytes:
        return crypto_box_open_old((b"\x00" * CRYPTO_BOX_BOXZEROBYTES) + ciphertext, nonce, public_key, secret_key)

    def _encrypt_afternm(self, message: bytes, nonce: bytes) -> bytes:
        return crypto_box_afternm_old(message, nonce, self.shared_key)[CRYPTO_BOX_BOXZEROBYTES:]

    def _decrypt_afternm(self, ciphertext: bytes, nonce: bytes) -> bytes:
        return crypto_box_open_afternm_old((b"\x00" * CRYPTO_BOX_BOXZEROBYTES) + ciphertext, nonce, self.shared_key)

    async def _send_tell(self) -> None:
        LOGGER.debug("SDG send TELL mode=%s", self.mode)
        await self.send_raw(build_header(CMD_TELL, 10))

    async def _send_packet(self, command: bytes, payload: bytes) -> None:
        LOGGER.debug("SDG send packet mode=%s command=%s payload_len=%d", self.mode, command.decode(), len(payload))
        await self.send_raw(build_header(command, 10 + len(payload)) + payload)

    async def _send_mesg(self, msg_type: int, payload: bytes) -> None:
        data_packet = build_data_packet(msg_type, payload)
        message = (b"\x00" * CRYPTO_BOX_ZEROBYTES) + data_packet
        nonce = build_short_term_nonce(b"CurveCP-client-M", self._next_nonce_value())
        ciphertext = self._encrypt_afternm(message, nonce)
        LOGGER.debug(
            "SDG send MESG mode=%s type=%d payload_len=%d nonce=%d ciphertext_len=%d",
            self.mode,
            msg_type,
            len(payload),
            int.from_bytes(nonce[16:24], "big"),
            len(ciphertext),
        )
        await self._send_packet(CMD_MESG, nonce[16:24] + ciphertext)

    async def _send_forward_remote(self) -> None:
        if self.tunnel_id is None:
            raise SDGProtocolError("tunnel id is not available")
        payload = build_data_packet(control.MSG_FORWARD_REMOTE, control.encode_forward_remote(self.tunnel_id))
        LOGGER.debug("SDG send FORWARD_REMOTE tunnel=%s payload_len=%d", self.tunnel_id.hex(), len(payload))
        await self.send_raw(payload)

    async def _send_helo(self) -> None:
        self.server_public_key = self._welc_key
        self.client_temp_public_key, self.client_temp_secret = crypto_box_keypair()
        nonce = build_short_term_nonce(b"CurveCP-client-H", self._next_nonce_value())
        zero_message = b"\x00" * (80 + CRYPTO_BOX_BOXZEROBYTES)
        ciphertext = self._encrypt_old(zero_message, nonce, self.server_public_key, self.client_temp_secret)
        LOGGER.debug(
            "SDG send HELO mode=%s nonce=%d temp_pub=%s ciphertext_len=%d",
            self.mode,
            int.from_bytes(nonce[16:24], "big"),
            self.client_temp_public_key.hex(),
            len(ciphertext),
        )
        payload = self.client_temp_public_key + nonce[16:24] + ciphertext
        await self._send_packet(CMD_HELO, payload)

    async def _handle_cook(self, payload: bytes) -> None:
        nonce = build_long_term_nonce(b"CurveCPK", struct.unpack(">QQ", payload[:16]))
        cookie = self._decrypt_old(payload[16:], nonce, self.server_public_key, self.client_temp_secret)
        server_short_term_public_key = cookie[32:64]
        self.server_cookie = cookie[64:160]
        self.shared_key = crypto_box_beforenm(server_short_term_public_key, self.client_temp_secret)
        LOGGER.debug(
            "SDG recv COOK mode=%s short_server_pub=%s cookie=%s",
            self.mode,
            server_short_term_public_key.hex(),
            self.server_cookie.hex(),
        )

        inner_message = (b"\x00" * 32) + self.client_temp_public_key
        inner_nonce = build_random_long_term_nonce()
        inner_ciphertext = self._encrypt_old(inner_message, inner_nonce, self.server_public_key, self.client_secret)

        outer_plain = (
            (b"\x00" * 32)
            + self.client_public_key
            + inner_nonce[8:24]
            + inner_ciphertext
            + b"\x00"
        )
        outer_nonce = build_short_term_nonce(b"CurveCP-client-I", self._next_nonce_value())
        outer_ciphertext = self._encrypt_afternm(outer_plain, outer_nonce)
        LOGGER.debug(
            "SDG send VOCH mode=%s nonce=%d inner_ciphertext_len=%d outer_ciphertext_len=%d",
            self.mode,
            int.from_bytes(outer_nonce[16:24], "big"),
            len(inner_ciphertext),
            len(outer_ciphertext),
        )
        voch_payload = self.server_cookie + outer_nonce[16:24] + outer_ciphertext
        await self._send_packet(CMD_VOCH, voch_payload)

    async def _send_protocol_version(self) -> None:
        LOGGER.debug("SDG send protocol version")
        await self._send_mesg(control.MSG_PROTOCOL_VERSION, control.encode_protocol_version(ProtocolVersion()))

    async def _send_ping(self) -> None:
        LOGGER.debug("SDG send ping seq=%d delay=%s", self.ping_sequence, self.ping_delay)
        await self._send_mesg(control.MSG_PING, control.encode_ping(self.ping_sequence, self.ping_delay))
        self.ping_sequence += 1

    def _decrypt_mesg(self, payload: bytes, prefix: bytes) -> bytes:
        nonce = build_short_term_nonce(prefix, int.from_bytes(payload[:8], "big"))
        plain = self._decrypt_afternm(payload[8:], nonce)
        return plain[32:]

    async def run_handshake(self) -> None:
        LOGGER.debug("SDG handshake start mode=%s protocol=%s", self.mode, self.protocol)
        if self.tunnel_id is not None:
            await self._send_forward_remote()
        else:
            await self._send_tell()

        while not self.connected:
            frame = await self.read_frame()
            LOGGER.debug("SDG recv raw frame mode=%s frame_len=%d", self.mode, len(frame))
            if frame[2] == control.MSG_FORWARD_HOLD and len(frame) == 3:
                LOGGER.debug("SDG recv FORWARD_HOLD")
                continue
            if frame[2] == control.MSG_FORWARD_REPLY:
                signature = control.decode_forward_reply(frame[3:])
                if signature != control.FORWARD_REMOTE_SIGNATURE:
                    raise SDGProtocolError("unexpected forwarding signature")
                LOGGER.debug("SDG recv FORWARD_REPLY signature=%s", signature)
                await self._send_tell()
                continue
            if frame[2] == control.MSG_FORWARD_ERROR:
                code = control.decode_forward_error(frame[3:])
                LOGGER.debug("SDG recv FORWARD_ERROR code=%d", code)
                raise SDGProtocolError(f"forwarding error {code}")

            header = PacketHeader.parse(frame)
            if header.magic != PacketHeader.MAGIC:
                raise SDGProtocolError("invalid packet magic")
            LOGGER.debug("SDG recv packet mode=%s command=%s payload_len=%d", self.mode, header.command.decode(), len(frame) - 10)

            payload = frame[10:]
            if header.command == CMD_WELC:
                self._welc_key = payload[:CRYPTO_BOX_PUBLICKEYBYTES]
                LOGGER.debug("SDG recv WELC server_pub=%s", self._welc_key.hex())
                await self._send_helo()
            elif header.command == CMD_COOK:
                await self._handle_cook(payload)
            elif header.command == CMD_REDY:
                LOGGER.debug("SDG recv REDY mode=%s", self.mode)
                if self.mode == MODE_GRID:
                    await self._send_protocol_version()
                elif self.mode == MODE_PEER:
                    self.connected = True
            elif header.command == CMD_MESG:
                mesg = self._decrypt_mesg(payload, b"CurveCP-server-M")
                if self.mode == MODE_GRID:
                    data_size = struct.unpack(">H", mesg[:2])[0]
                    data_type = mesg[2]
                    data = mesg[3 : 3 + data_size - 1]
                    LOGGER.debug("SDG recv MESG mode=%s type=%d payload_len=%d", self.mode, data_type, len(data))
                    await self._handle_grid_message(data_type, data)
                else:
                    payload_size = struct.unpack(">H", mesg[:2])[0]
                    data = mesg[2 : 2 + payload_size]
                    LOGGER.debug("SDG recv peer MESG mode=%s payload_len=%d", self.mode, len(data))
                    self._pending_message = (0, data)
                    if self.mode == MODE_PAIRING:
                        await self._handle_pairing_message(data)
            else:
                raise SDGProtocolError(f"unexpected command {header.command!r}")

    async def _handle_grid_message(self, msg_type: int, payload: bytes) -> None:
        LOGGER.debug("SDG grid message type=%d payload_len=%d", msg_type, len(payload))
        if msg_type == control.MSG_PROTOCOL_VERSION:
            version = control.decode_protocol_version(payload)
            if version.magic != control.PROTOCOL_VERSION_MAGIC:
                raise SDGProtocolError("invalid protocol version magic")
            await self._send_ping()
        elif msg_type == control.MSG_PONG:
            LOGGER.debug("SDG recv PONG")
            control.decode_pong(payload)
            self.connected = True
        elif msg_type in (control.MSG_REMOTE_REPLY, control.MSG_PAIR_REMOTE_REPLY):
            self._pending_reply = control.decode_peer_reply(payload)
            LOGGER.debug(
                "SDG recv peer reply type=%d id=%d result=%d peer=%s",
                msg_type,
                self._pending_reply.id,
                self._pending_reply.result,
                self._pending_reply.peer,
            )
        elif msg_type == control.MSG_INCOMING_CALL:
            LOGGER.debug("SDG recv INCOMING_CALL")
            await self._send_mesg(control.MSG_INCOMING_CALL_REPLY, control.encode_incoming_call_reply(0, 0))

    async def _handle_pairing_message(self, payload: bytes) -> None:
        if not payload:
            return
        code = payload[0]
        LOGGER.debug("SDG pairing message code=%d payload_len=%d", code, len(payload))
        if code == MSG_PAIRING_CHALLENGE:
            challenge_x = payload[1:33]
            challenge_nonce = payload[33:65]
            challenge_y = payload[65:97]

            buf = self.protocol.encode() + self.client_public_key + self.server_public_key
            hashed = crypto_hash(buf)
            hash_input = hashed + challenge_nonce
            hash2 = crypto_hash(hash_input)
            xor = crypto_stream_xor(challenge_y, challenge_nonce, hash2[:32])
            base = crypto_scalarmult_base(self.shared_key)
            p1 = crypto_scalarmult(xor, base)
            rnd = generate_private_key()
            x_response = crypto_scalarmult(rnd, p1)
            common = crypto_scalarmult(rnd, challenge_x)
            y_response = crypto_hash(crypto_hash(challenge_x) + common)[:32]
            self._pairing_expected = crypto_hash(crypto_hash(x_response) + common)[:32]
            response = bytes([MSG_PAIRING_RESPONSE]) + x_response + y_response
            LOGGER.debug("SDG send pairing response expected=%s", self._pairing_expected.hex())
            await self.send_peer_data(response)
        elif code == MSG_PAIRING_RESULT:
            result = payload[1:33]
            LOGGER.debug("SDG recv pairing result result=%s expected=%s", result.hex(), getattr(self, "_pairing_expected", b"").hex())
            if result != getattr(self, "_pairing_expected", None):
                raise SDGProtocolError("pairing result verification failed")
            self.connected = True

    async def send_peer_data(self, payload: bytes) -> None:
        await self._send_mesg_data(payload)

    async def _send_mesg_data(self, payload: bytes) -> None:
        data_packet = struct.pack(">H", len(payload)) + payload
        message = (b"\x00" * CRYPTO_BOX_ZEROBYTES) + data_packet
        nonce = build_short_term_nonce(b"CurveCP-client-M", self._next_nonce_value())
        ciphertext = self._encrypt_afternm(message, nonce)
        await self._send_packet(CMD_MESG, nonce[16:24] + ciphertext)

    async def request_peer_data(
        self,
        payload: bytes,
        first_chunk_timeout: float = 5.0,
        idle_timeout: float = 1.0,
    ) -> list[bytes]:
        LOGGER.debug(
            "SDG request peer data payload_len=%d first_chunk_timeout=%.2f idle_timeout=%.2f",
            len(payload),
            first_chunk_timeout,
            idle_timeout,
        )
        await self._send_mesg_data(payload)
        responses: list[bytes] = []
        while True:
            try:
                timeout = first_chunk_timeout if not responses else idle_timeout
                frame = await asyncio.wait_for(self.read_frame(), timeout)
            except asyncio.IncompleteReadError:
                LOGGER.debug("SDG peer data EOF chunks=%d", len(responses))
                break
            except TimeoutError:
                LOGGER.debug("SDG peer data wait timeout chunks=%d", len(responses))
                break
            header = PacketHeader.parse(frame)
            if header.command != CMD_MESG:
                LOGGER.debug("SDG ignore non-MESG command=%s while waiting peer data", header.command.decode())
                continue
            payload_data = self._decrypt_mesg(frame[10:], b"CurveCP-server-M")
            length = struct.unpack(">H", payload_data[:2])[0]
            data = payload_data[2 : 2 + length]
            if self._discard_first_bytes:
                data = data[self._discard_first_bytes :]
                self._discard_first_bytes = 0
            responses.append(data)
            LOGGER.debug("SDG peer data chunk len=%d chunks=%d", len(data), len(responses))
        return responses


class GridSession:
    def __init__(self, private_key_hex: str | None = None, user_name: str | None = None) -> None:
        self.private_key = bytes.fromhex(private_key_hex) if private_key_hex else generate_private_key()
        self.public_key = calculate_public_key(self.private_key)
        self.user_name = user_name or "Home Assistant"
        self._grid_conn: _RemoteConnection | None = None
        self._pending_request_ids: set[int] = set()
        self._lingering_config_conns: list[_RemoteConnection] = []
        self._connect_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()

    @property
    def private_key_hex(self) -> str:
        return self.private_key.hex()

    @property
    def public_key_hex(self) -> str:
        return self.public_key.hex()

    def _allocate_request_id(self) -> int:
        request_id = 0
        while request_id in self._pending_request_ids:
            request_id += 1
        self._pending_request_ids.add(request_id)
        return request_id

    async def connect(self) -> None:
        async with self._connect_lock:
            if self._grid_conn and self._grid_conn.connected:
                LOGGER.debug("SDG reuse grid connection")
                return

            last_error = None
            servers = list(DANFOSS_SERVERS)
            random.shuffle(servers)
            for endpoint in servers:
                try:
                    LOGGER.debug("SDG connect grid host=%s port=%d", endpoint.host, endpoint.port)
                    reader, writer = await asyncio.open_connection(endpoint.host, endpoint.port)
                    conn = _RemoteConnection(
                        reader=reader,
                        writer=writer,
                        client_public_key=self.public_key,
                        client_secret=self.private_key,
                        mode=MODE_GRID,
                        protocol="grid",
                    )
                    await conn.run_handshake()
                    self._grid_conn = conn
                    LOGGER.debug("SDG grid connected host=%s port=%d", endpoint.host, endpoint.port)
                    return
                except Exception as exc:  # pragma: no cover - network dependent
                    LOGGER.debug("SDG grid connect failed host=%s port=%d error=%r", endpoint.host, endpoint.port, exc)
                    last_error = exc
            if last_error is not None:
                raise last_error

    async def disconnect(self) -> None:
        while self._lingering_config_conns:
            lingering = self._lingering_config_conns.pop()
            LOGGER.debug("SDG disconnect lingering config connection")
            await lingering.close()
        if self._grid_conn is not None:
            LOGGER.debug("SDG disconnect grid")
            await self._grid_conn.close()
            self._grid_conn = None

    async def _reset_grid_connection(self) -> None:
        if self._grid_conn is not None:
            try:
                await self._grid_conn.close()
            except Exception:
                LOGGER.debug("SDG ignore grid close error during reset", exc_info=True)
            self._grid_conn = None

    async def _peer_request(self, request_id: int, msg_type: int, payload: bytes) -> PeerReply:
        async with self._request_lock:
            try:
                for attempt in range(2):
                    await self.connect()
                    LOGGER.debug(
                        "SDG peer request id=%d type=%d payload_len=%d attempt=%d",
                        request_id,
                        msg_type,
                        len(payload),
                        attempt + 1,
                    )
                    try:
                        await self._grid_conn._send_mesg(msg_type, payload)
                        while True:
                            frame = await self._grid_conn.read_frame()
                            header = PacketHeader.parse(frame)
                            if header.command != CMD_MESG:
                                continue
                            mesg = self._grid_conn._decrypt_mesg(frame[10:], b"CurveCP-server-M")
                            data_size = struct.unpack(">H", mesg[:2])[0]
                            data_type = mesg[2]
                            data = mesg[3 : 3 + data_size - 1]
                            await self._grid_conn._handle_grid_message(data_type, data)
                            reply = getattr(self._grid_conn, "_pending_reply", None)
                            if reply is not None and reply.id == request_id:
                                self._grid_conn._pending_reply = None
                                LOGGER.debug("SDG peer request done id=%d result=%d peer=%s", request_id, reply.result, reply.peer)
                                return reply
                    except (asyncio.IncompleteReadError, ConnectionError, OSError):
                        LOGGER.debug("SDG peer request retry after grid disconnect id=%d", request_id, exc_info=True)
                        await self._reset_grid_connection()
                        if attempt == 1:
                            raise
            finally:
                self._pending_request_ids.discard(request_id)

    async def connect_to_remote(self, peer_id_hex: str, protocol: str = "dominion-1.0") -> _RemoteConnection:
        await self.connect()
        request_id = self._allocate_request_id()
        LOGGER.debug("SDG connect_to_remote peer=%s protocol=%s request_id=%d", peer_id_hex, protocol, request_id)
        reply = await self._peer_request(
            request_id,
            control.MSG_CALL_REMOTE,
            control.encode_connect_to_peer(request_id, peer_id_hex, protocol),
        )
        if reply.result or reply.peer is None:
            raise SDGProtocolError("remote connection refused")
        LOGGER.debug("SDG remote tunnel host=%s port=%d peer=%s", reply.peer.server.host, reply.peer.server.port, reply.peer.peer_id)
        reader, writer = await asyncio.open_connection(reply.peer.server.host, reply.peer.server.port)
        conn = _RemoteConnection(
            reader=reader,
            writer=writer,
            client_public_key=self.public_key,
            client_secret=self.private_key,
            mode=MODE_PEER,
            protocol=protocol,
            peer_id_hex=reply.peer.peer_id or peer_id_hex,
            tunnel_id=reply.peer.tunnel_id,
        )
        conn.server_public_key = bytes.fromhex(reply.peer.peer_id)
        await conn.run_handshake()
        LOGGER.debug("SDG remote connected peer=%s protocol=%s", reply.peer.peer_id, protocol)
        return conn

    async def pair_and_discover(self, request) -> HouseConfig:
        await self.connect()
        digits = "".join(char for char in request.otp if char.isdigit())
        if len(digits) < 7:
            raise ValueError("OTP is too short")
        LOGGER.debug("SDG pair_and_discover client_name=%s otp_len=%d", request.client_name, len(digits))

        request_id = self._allocate_request_id()
        server_part = digits[:-3]
        LOGGER.debug("SDG pair request id=%d server_part=%s", request_id, server_part)
        reply = await self._peer_request(
            request_id,
            control.MSG_PAIR_REMOTE,
            control.encode_pair_remote(request_id, server_part),
        )
        if reply.result or reply.peer is None:
            raise PairingRefusedError("pairing refused")
        LOGGER.debug("SDG pair accepted peer=%s host=%s port=%d", reply.peer.peer_id, reply.peer.server.host, reply.peer.server.port)

        reader, writer = await asyncio.open_connection(reply.peer.server.host, reply.peer.server.port)
        pairing_conn = _RemoteConnection(
            reader=reader,
            writer=writer,
            client_public_key=self.public_key,
            client_secret=self.private_key,
            mode=MODE_PAIRING,
            protocol=digits,
            peer_id_hex=reply.peer.peer_id,
            tunnel_id=reply.peer.tunnel_id,
        )
        pairing_conn.server_public_key = bytes.fromhex(reply.peer.peer_id)
        await pairing_conn.run_handshake()
        paired_peer_id = pairing_conn.server_public_key.hex()
        LOGGER.debug("SDG pairing tunnel complete peer=%s", paired_peer_id)
        await pairing_conn.close()

        request_json = json.dumps(
            {
                "phoneName": self.user_name,
                "phonePublicKey": self.public_key_hex,
                "chunkedMessage": True,
            },
            separators=(",", ":"),
        ).encode()
        LOGGER.debug("SDG config request json=%s", request_json.decode())
        config_payload = b""
        config_conn = None
        try:
            config_conn = await self.connect_to_remote(paired_peer_id, "dominion-configuration-1.0")
            responses = await config_conn.request_peer_data(
                request_json,
                first_chunk_timeout=10.0,
                idle_timeout=2.0,
            )
            config_payload = b"".join(responses)
            LOGGER.debug("SDG config payload len=%d chunks=%d", len(config_payload), len(responses))
        finally:
            if config_conn is not None and config_payload:
                while self._lingering_config_conns:
                    await self._lingering_config_conns.pop().close()
                await config_conn.close()
            elif config_conn is not None:
                LOGGER.debug("SDG preserving failed config connection for Java-parity retry behavior")
                self._lingering_config_conns.append(config_conn)
        if not config_payload:
            raise SDGProtocolError("empty config payload")
        parsed = self._parse_config_payload(config_payload)
        thermostats = [
            ThermostatInfo(
                peer_id=room["peerId"],
                room_name=room["roomName"],
                zone=room.get("zone"),
            )
            for room in parsed.get("rooms", [])
        ]
        await self._enrich_thermostats(thermostats)
        return HouseConfig(
            house_id=parsed.get("housePeerId"),
            house_name=parsed.get("houseName"),
            user_name=self.user_name,
            private_key=self.private_key_hex,
            thermostats=thermostats,
        )

    @staticmethod
    def _parse_config_payload(payload: bytes) -> dict:
        if len(payload) > 8 and payload[:4] == b"\x00\x00\x00\x00":
            length = struct.unpack("<I", payload[4:8])[0]
            json_payload = payload[8 : 8 + length]
        else:
            json_payload = payload
        return json.loads(json_payload.decode())

    async def _enrich_thermostats(self, thermostats: list[ThermostatInfo]) -> None:
        for thermostat in thermostats:
            try:
                await self._enrich_thermostat_info(thermostat)
            except Exception as exc:  # pragma: no cover - network dependent
                LOGGER.debug("SDG enrichment failed peer=%s error=%r", thermostat.peer_id, exc)

    async def _enrich_thermostat_info(self, thermostat: ThermostatInfo) -> None:
        conn = await self.connect_to_remote(thermostat.peer_id, "dominion-1.0")
        request_payload = b"".join(
            (
                DominionPacket.read_request(DEVICEGLOBAL, GLOBAL_SERIALNUMBER).to_wire_bytes(),
                DominionPacket.read_request(DOMINION_SYSTEM, SYSTEM_ROOM_NAME).to_wire_bytes(),
                DominionPacket.read_request(DOMINION_SYSTEM, SYSTEM_HOUSE_NAME).to_wire_bytes(),
                DominionPacket.read_request(DOMINION_SYSTEM, SYSTEM_ZONE_NAME).to_wire_bytes(),
            )
        )
        responses: list[bytes] = []
        try:
            responses = await conn.request_peer_data(
                request_payload,
                first_chunk_timeout=3.0,
                idle_timeout=1.0,
            )
        finally:
            await conn.close()
        for chunk in responses:
            for packet in self._parse_dominion_wire_stream(chunk):
                if packet.msg_code == GLOBAL_SERIALNUMBER and len(packet.payload) >= 4:
                    thermostat.serial_number = str(packet.get_int())
                    thermostat.metadata["serial_number"] = thermostat.serial_number
                elif packet.msg_code == SYSTEM_ROOM_NAME:
                    room_name = packet.get_string()
                    if room_name:
                        thermostat.room_name = room_name
                        thermostat.metadata["room_name"] = room_name
                elif packet.msg_code == SYSTEM_HOUSE_NAME:
                    house_name = packet.get_string()
                    if house_name:
                        thermostat.metadata["house_name"] = house_name
                elif packet.msg_code == SYSTEM_ZONE_NAME:
                    zone_name = packet.get_string()
                    thermostat.zone = zone_name or None
                    if zone_name:
                        thermostat.metadata["zone"] = zone_name

    @staticmethod
    def _parse_dominion_wire_stream(data: bytes) -> list[DominionPacket]:
        packets: list[DominionPacket] = []
        offset = 0
        while offset < len(data):
            if len(data) - offset < 5:
                break
            # Dominion payloads may include a single-byte prefix per packet.
            if data[offset] in (0, 1):
                offset += 1
            if len(data) - offset < 4:
                break
            payload_len = data[offset + 3]
            packet_len = 4 + payload_len
            if len(data) - offset < packet_len:
                break
            packets.append(DominionPacket.from_bytes(data[offset : offset + packet_len]))
            offset += packet_len
        return packets
