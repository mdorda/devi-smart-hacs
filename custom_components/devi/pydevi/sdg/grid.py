from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import logging
import random
import socket
import struct
import sys
from time import monotonic

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

# Connection monitor: nothing reads a socket between requests, so without it a remote close
# or unsolicited data is only noticed at the next use, hours later.
MONITOR_INTERVAL_S = 5.0
MONITOR_HEARTBEAT_S = 300.0
MONITOR_STUCK_OPERATION_S = 30.0

# Upper bound for a request that ends once every requested packet arrived; answers take ~0.2 s.
RESPONSE_MAX_DURATION_S = 10.0

# Every network wait is bounded, so a reconnect can fail but never hang. Observed durations are far
# below: TCP connect ~40 ms, handshake ~0.3 s (grid) and ~1.3 s (peer tunnel), grid reply ~50 ms.
CONNECT_TIMEOUT_S = 10.0
HANDSHAKE_TIMEOUT_S = 15.0
GRID_REPLY_TIMEOUT_S = 10.0
WRITE_TIMEOUT_S = 10.0

# The grid server closes a connection that stays silent (observed after 300 s), so it is pinged like
# OpenSDG's GridConnection does: the next ping goes out this long after the previous PONG.
GRID_PING_INTERVAL_S = 30.0

# The thermostat pushes its clock every second; an idle peer tunnel that stays silent longer is dead.
PEER_SILENCE_WARNING_S = 5.0

# struct tcp_info of linux/tcp.h up to tcpi_rttvar: 8 x u8, 17 x u32.
_TCP_INFO_FORMAT = "<8B17I"
_TCP_STATES = {
    1: "ESTABLISHED",
    2: "SYN_SENT",
    3: "SYN_RECV",
    4: "FIN_WAIT1",
    5: "FIN_WAIT2",
    6: "TIME_WAIT",
    7: "CLOSE",
    8: "CLOSE_WAIT",
    9: "LAST_ACK",
    10: "LISTEN",
    11: "CLOSING",
}


def _tcp_diagnostics(sock) -> str:
    """Kernel view of a TCP connection (Linux only), independent of what the application has read."""
    if sock is None or not sys.platform.startswith("linux") or not hasattr(socket, "TCP_INFO"):
        return "tcp=n/a"
    try:
        raw = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, struct.calcsize(_TCP_INFO_FORMAT))
        info = struct.unpack_from(_TCP_INFO_FORMAT, raw)
    except (OSError, TypeError, ValueError, struct.error):
        return "tcp=n/a"
    return (
        f"tcp_state={_TCP_STATES.get(info[0], info[0])} "
        f"tcp_last_recv_ms={info[19]} tcp_last_send_ms={info[17]} tcp_last_ack_recv_ms={info[20]} "
        f"tcp_unacked={info[12]} tcp_retransmits={info[2]} tcp_probes={info[3]} tcp_backoff={info[4]} "
        f"tcp_retrans={info[15]} tcp_rtt_ms={info[23] / 1000:.1f}"
    )


def _packet_ids(packets: list[DominionPacket]) -> list[str]:
    """Dominion packet identifiers (class:code) for diagnostics, without values."""
    return [f"{packet.msg_class}:{packet.msg_code}" for packet in packets]


# Longer payloads are strings and arrays (names, Wi-Fi credentials, keys); only their length is logged.
_MAX_LOGGED_PAYLOAD_BYTES = 8
# Pushed by the thermostat every second (clock and a counter); their values change in every chunk.
_TICKER_PACKET_IDS = frozenset({"5:29237", "5:29264"})


def _is_identifying_packet(packet: DominionPacket) -> bool:
    return (packet.msg_class, packet.msg_code) == (DEVICEGLOBAL, GLOBAL_SERIALNUMBER) or packet.msg_code in (
        SYSTEM_ROOM_NAME, SYSTEM_HOUSE_NAME, SYSTEM_ZONE_NAME,
    )


def _packet_value(packet: DominionPacket) -> str:
    if _is_identifying_packet(packet):
        return "redacted"
    if len(packet.payload) > _MAX_LOGGED_PAYLOAD_BYTES:
        return f"len{len(packet.payload)}"
    return packet.payload.hex()


def _response_packets(data: bytes) -> tuple[list[DominionPacket], list[str], str, str]:
    """Packets, their identifiers, an id=value description and a key for deduplicating repeated chunks."""
    try:
        packets = DominionPacket.parse_stream(data)
    except (ValueError, struct.error):
        return [], ["unparsed"], f"unparsed_len={len(data)}", f"unparsed_len={len(data)}"
    ids = _packet_ids(packets)
    values = [f"{packet_id}={_packet_value(packet)}" for packet_id, packet in zip(ids, packets)]
    dedup_key = ",".join(
        packet_id if packet_id in _TICKER_PACKET_IDS else value for packet_id, value in zip(ids, values)
    )
    return packets, ids, ",".join(values), dedup_key


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
    _created_at: float = field(default_factory=monotonic)
    _last_rx_at: float | None = None
    _last_tx_at: float | None = None
    _rx_frames: int = 0
    _tx_frames: int = 0
    _busy_since: float | None = None
    _busy_operation: str | None = None
    _monitor_task: asyncio.Task | None = None
    _known_packets: dict[str, DominionPacket] = field(default_factory=dict)

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

    def diagnostic_context(self) -> str:
        """Describe transport health without logging keys or message contents."""
        now = monotonic()
        rx_idle = f"{now - self._last_rx_at:.1f}" if self._last_rx_at is not None else "never"
        tx_idle = f"{now - self._last_tx_at:.1f}" if self._last_tx_at is not None else "never"
        return (
            f"mode={self.mode} peer={self.peer_id_hex} "
            f"remote={self.writer.get_extra_info('peername')} "
            f"age_s={now - self._created_at:.1f} "
            f"rx_idle_s={rx_idle} tx_idle_s={tx_idle} "
            f"rx_frames={self._rx_frames} tx_frames={self._tx_frames} "
            f"rx_buffer={self._rx_buffered()} busy={self._busy_operation} "
            f"closing={self.writer.is_closing()} eof={self._remote_closed()} "
            f"reader_error={self._reader_error()} "
            f"{_tcp_diagnostics(self.writer.get_extra_info('socket'))}"
        )

    def _rx_buffered(self) -> int:
        """Bytes received by the transport but not read by the application yet."""
        buffer = getattr(self.reader, "_buffer", None)
        return len(buffer) if isinstance(buffer, (bytes, bytearray)) else 0

    def _remote_closed(self) -> bool:
        """Whether the remote side has closed the stream, also while unread data is still buffered."""
        eof = getattr(self.reader, "_eof", None)
        return eof if isinstance(eof, bool) else bool(self.reader.at_eof())

    def _reader_error(self) -> str | None:
        """The error the transport was lost with (e.g. a reset), None for a clean close."""
        try:
            exc = self.reader.exception()
        except Exception:  # pragma: no cover - defensive, diagnostics only
            return None
        if not isinstance(exc, BaseException):
            return None
        return f"{type(exc).__name__}(errno={getattr(exc, 'errno', None)})"

    def _mark_busy(self, operation: str) -> None:
        self._busy_since = monotonic()
        self._busy_operation = operation

    def _mark_idle(self) -> None:
        self._busy_since = None
        self._busy_operation = None

    def start_monitor(self) -> None:
        if self._monitor_task is None:
            self._monitor_task = asyncio.get_running_loop().create_task(self._monitor())

    def stop_monitor(self) -> None:
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            self._monitor_task = None

    async def run_monitored_handshake(self, operation: str) -> None:
        """Run the handshake with a timeout; a failed handshake closes the socket."""
        self.start_monitor()
        self._mark_busy(operation)
        started_at = monotonic()
        try:
            await asyncio.wait_for(self.run_handshake(), HANDSHAKE_TIMEOUT_S)
        except BaseException as exc:
            if isinstance(exc, TimeoutError):
                LOGGER.warning(
                    "SDG handshake timeout operation=%s timeout_s=%.1f elapsed_s=%.2f %s",
                    operation, HANDSHAKE_TIMEOUT_S, monotonic() - started_at, self.diagnostic_context(),
                )
            self.stop_monitor()
            self.connected = False
            self.writer.close()
            raise
        finally:
            self._mark_idle()

    async def _monitor(self) -> None:
        """Log what happens to the connection while nothing reads it, without consuming any data."""
        reported_close = False
        reported_stuck_since: float | None = None
        reported_unread = False
        reported_silent = False
        last_buffered = 0
        last_rx_growth = monotonic()
        last_heartbeat = monotonic()
        LOGGER.debug("SDG monitor started %s", self.diagnostic_context())
        while True:
            await asyncio.sleep(MONITOR_INTERVAL_S)
            now = monotonic()
            closed = self._remote_closed() or self.writer.is_closing() or self._reader_error() is not None
            if closed and not reported_close:
                reported_close = True
                LOGGER.warning("SDG monitor detected closed connection %s", self.diagnostic_context())
            buffered = self._rx_buffered()
            # Once per idle period; the total shows up as buffered_before of the next request.
            if self._busy_since is not None:
                reported_unread = False
            elif buffered and not reported_unread:
                reported_unread = True
                LOGGER.debug("SDG monitor unread data while idle bytes=%d %s", buffered, self.diagnostic_context())
            # A live peer tunnel grows the buffer every second while idle; a request drains it.
            if self._busy_since is not None or buffered != last_buffered:
                last_buffered = buffered
                last_rx_growth = now
                reported_silent = False
            elif self.mode == MODE_PEER and not reported_silent and now - last_rx_growth >= PEER_SILENCE_WARNING_S:
                reported_silent = True
                LOGGER.warning(
                    "SDG monitor peer silent while idle silent_s=%.1f %s", now - last_rx_growth, self.diagnostic_context(),
                )
            if (
                self._busy_since is not None
                and now - self._busy_since > MONITOR_STUCK_OPERATION_S
                and reported_stuck_since != self._busy_since
            ):
                reported_stuck_since = self._busy_since
                LOGGER.warning(
                    "SDG monitor operation stuck operation=%s busy_s=%.1f %s",
                    self._busy_operation, now - self._busy_since, self.diagnostic_context(),
                )
            if now - last_heartbeat >= MONITOR_HEARTBEAT_S:
                last_heartbeat = now
                LOGGER.debug("SDG monitor heartbeat %s", self.diagnostic_context())

    async def close(self) -> None:
        LOGGER.debug("SDG close mode=%s protocol=%s peer_id=%s %s", self.mode, self.protocol, self.peer_id_hex, self.diagnostic_context())
        self.stop_monitor()
        self.connected = False
        self.writer.close()
        try:
            await asyncio.wait_for(self.writer.wait_closed(), timeout=5.0)
        except (OSError, TimeoutError) as exc:
            LOGGER.debug("SDG close failed error=%s errno=%s %s", type(exc).__name__, getattr(exc, "errno", None), self.diagnostic_context())

    async def send_raw(self, payload: bytes) -> None:
        try:
            self.writer.write(payload)
            await asyncio.wait_for(self.writer.drain(), WRITE_TIMEOUT_S)
        except OSError as exc:
            LOGGER.warning(
                "SDG write failed error=%s errno=%s bytes=%d %s",
                type(exc).__name__, exc.errno, len(payload), self.diagnostic_context(),
            )
            raise
        self._last_tx_at = monotonic()
        self._tx_frames += 1

    async def read_frame(self) -> bytes:
        try:
            size_prefix = await self.reader.readexactly(2)
            size = struct.unpack(">H", size_prefix)[0]
            frame = size_prefix + await self.reader.readexactly(size)
        except OSError as exc:
            LOGGER.warning(
                "SDG read failed error=%s errno=%s %s",
                type(exc).__name__, exc.errno, self.diagnostic_context(),
            )
            raise
        self._last_rx_at = monotonic()
        self._rx_frames += 1
        return frame

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
        self._mark_busy("peer_send")
        try:
            await self._send_mesg_data(payload)
        finally:
            self._mark_idle()

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
        stop_when_answered: bool = False,
    ) -> list[bytes]:
        """Send a request and collect the chunks that follow.

        The thermostat pushes its clock every second, so idle time alone rarely ends a response.
        With stop_when_answered the request ends once every requested Dominion packet has arrived,
        or after RESPONSE_MAX_DURATION_S.
        """
        self._mark_busy("peer_request")
        try:
            return await self._request_peer_data(payload, first_chunk_timeout, idle_timeout, stop_when_answered)
        finally:
            self._mark_idle()

    async def _request_peer_data(
        self, payload: bytes, first_chunk_timeout: float, idle_timeout: float, stop_when_answered: bool,
    ) -> list[bytes]:
        # Data buffered before the request is not a reply to it, but it is read as one.
        buffered_before = self._rx_buffered()
        try:
            requested_order = _packet_ids(GridSession._parse_dominion_wire_stream(payload))
        except (ValueError, struct.error):
            requested_order = []
        requested = set(requested_order)
        LOGGER.debug(
            "SDG request peer data payload_len=%d first_chunk_timeout=%.2f idle_timeout=%.2f buffered_before=%d requested=%d",
            len(payload),
            first_chunk_timeout,
            idle_timeout,
            buffered_before,
            len(requested),
        )
        started_at = monotonic()
        stop_when_answered = stop_when_answered and bool(requested)
        await self._send_mesg_data(payload)
        responses: list[bytes] = []
        # Diagnostics only: how long until every requested packet arrived, and what arrives unasked.
        read_bytes = 0
        buffered_chunks = 0
        answered: set[str] = set()
        all_answered: str | None = None
        unsolicited: dict[str, int] = {}
        # The peer streams the same packets nonstop; log each distinct chunk once per request.
        logged_chunks: set[str] = set()
        repeated_chunks = 0
        # Values the answer changed although the thermostat did not push the change: what only polling finds.
        # The answer lists the requested packets in request order. Pushes of requested packets (measurements)
        # may arrive before it, even in the same chunk, and are not the answer.
        answer_start = requested_order[:2]
        answer_started = False
        poll_checked: set[str] = set()
        poll_changes: list[str] = []

        def log_incomplete() -> None:
            LOGGER.warning(
                "SDG peer response incomplete after max_duration_s=%.1f chunks=%d missing=%s %s",
                RESPONSE_MAX_DURATION_S, len(responses), ",".join(sorted(requested - answered)),
                self.diagnostic_context(),
            )
        while True:
            timeout = first_chunk_timeout if not responses else idle_timeout
            if stop_when_answered:
                remaining = RESPONSE_MAX_DURATION_S - (monotonic() - started_at)
                if remaining <= 0:
                    log_incomplete()
                    break
                timeout = min(timeout, remaining)
            try:
                frame = await asyncio.wait_for(self.read_frame(), timeout)
            except asyncio.IncompleteReadError as exc:
                # The remote side closed the tunnel; the connection cannot be used for the next request.
                self.connected = False
                LOGGER.warning(
                    "SDG peer EOF chunks=%d partial_bytes=%d expected_bytes=%s elapsed_s=%.2f %s",
                    len(responses), len(exc.partial), exc.expected,
                    monotonic() - started_at, self.diagnostic_context(),
                )
                break
            except TimeoutError:
                if stop_when_answered and monotonic() - started_at >= RESPONSE_MAX_DURATION_S:
                    log_incomplete()
                    break
                # Idle after response chunks is the normal end-of-response delimiter.
                log = LOGGER.debug if responses else LOGGER.warning
                log(
                    "SDG peer timeout phase=%s timeout_s=%.2f chunks=%d elapsed_s=%.2f %s",
                    "response_end" if responses else "first_response", timeout,
                    len(responses), monotonic() - started_at, self.diagnostic_context(),
                )
                break
            read_bytes += len(frame)
            from_buffer = read_bytes <= buffered_before
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
            packets, packet_ids, packet_values, dedup_key = _response_packets(data)
            buffered_chunks += from_buffer
            for packet_id in packet_ids:
                if packet_id in requested and not from_buffer:
                    answered.add(packet_id)
                else:
                    unsolicited[packet_id] = unsolicited.get(packet_id, 0) + 1
            for index, (packet_id, packet) in enumerate(zip(packet_ids, packets)):
                if not answer_started and not from_buffer and answer_start:
                    answer_started = packet_ids[index : index + len(answer_start)] == answer_start
                in_answer = answer_started and packet_id in requested
                known = self._known_packets.get(packet_id)
                if (
                    in_answer and packet_id not in poll_checked
                    and known is not None and known.payload != packet.payload
                ):
                    poll_changes.append(f"{packet_id}:{_packet_value(known)}->{_packet_value(packet)}")
                if in_answer:
                    poll_checked.add(packet_id)
                self._known_packets[packet_id] = packet
            if all_answered is None and requested and answered >= requested:
                all_answered = f"{monotonic() - started_at:.2f}@chunk{len(responses)}"
            if dedup_key in logged_chunks:
                repeated_chunks += 1
            else:
                logged_chunks.add(dedup_key)
                LOGGER.debug(
                    "SDG peer data chunk len=%d chunks=%d buffered=%s packets=%s",
                    len(data), len(responses), from_buffer, packet_values,
                )
            if stop_when_answered and all_answered is not None:
                break
        LOGGER.debug(
            "SDG peer data done chunks=%d repeated_chunks=%d bytes=%d elapsed_s=%.2f buffered_chunks=%d requested=%d answered=%d "
            "all_answered=%s missing=%s poll_changes=%s unsolicited=%s %s",
            len(responses), repeated_chunks, sum(len(chunk) for chunk in responses), monotonic() - started_at,
            buffered_chunks, len(requested), len(answered), all_answered,
            ",".join(sorted(requested - answered)) or "none",
            ",".join(poll_changes) or "none",
            ",".join(f"{packet_id}x{count}" for packet_id, count in sorted(unsolicited.items(), key=lambda item: -item[1])) or "none",
            self.diagnostic_context(),
        )
        if poll_changes:
            LOGGER.info("SDG poll found changes the thermostat did not push changes=%s", ",".join(poll_changes))
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
        self._grid_connections = 0
        self._keepalive_task: asyncio.Task | None = None

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
                started_at = monotonic()
                phase = "tcp_connect"
                try:
                    LOGGER.debug("SDG connect grid host=%s port=%d", endpoint.host, endpoint.port)
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(endpoint.host, endpoint.port), CONNECT_TIMEOUT_S,
                    )
                    conn = _RemoteConnection(
                        reader=reader,
                        writer=writer,
                        client_public_key=self.public_key,
                        client_secret=self.private_key,
                        mode=MODE_GRID,
                        protocol="grid",
                    )
                    phase = "handshake"
                    await conn.run_monitored_handshake("grid_handshake")
                    self._grid_conn = conn
                    self._grid_connections += 1
                    self._start_keepalive(conn)
                    LOGGER.info(
                        "SDG grid connected host=%s port=%d connection_no=%d elapsed_s=%.2f",
                        endpoint.host, endpoint.port, self._grid_connections, monotonic() - started_at,
                    )
                    return
                except Exception as exc:  # pragma: no cover - network dependent
                    LOGGER.warning(
                        "SDG grid connect failed host=%s port=%d phase=%s elapsed_s=%.2f error=%s errno=%s",
                        endpoint.host, endpoint.port, phase, monotonic() - started_at,
                        type(exc).__name__, getattr(exc, "errno", None),
                    )
                    last_error = exc
            if last_error is not None:
                raise last_error

    async def disconnect(self) -> None:
        while self._lingering_config_conns:
            lingering = self._lingering_config_conns.pop()
            LOGGER.debug("SDG disconnect lingering config connection")
            await lingering.close()
        self._stop_keepalive()
        if self._grid_conn is not None:
            LOGGER.debug("SDG disconnect grid")
            await self._grid_conn.close()
            self._grid_conn = None

    async def _reset_grid_connection(self) -> None:
        self._stop_keepalive()
        if self._grid_conn is not None:
            LOGGER.debug("SDG reset grid connection %s", self._grid_conn.diagnostic_context())
            try:
                await self._grid_conn.close()
            except Exception:
                LOGGER.debug("SDG ignore grid close error during reset", exc_info=True)
            self._grid_conn = None

    def _start_keepalive(self, grid_conn: _RemoteConnection) -> None:
        self._stop_keepalive()
        self._keepalive_task = asyncio.get_running_loop().create_task(self._keepalive(grid_conn))

    def _stop_keepalive(self) -> None:
        task, self._keepalive_task = self._keepalive_task, None
        # The keepalive resets a failed connection itself and must not cancel its own cleanup.
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _keepalive(self, grid_conn: _RemoteConnection) -> None:
        """Ping the grid until it fails; a failed ping drops the connection so the next use reconnects."""
        while True:
            await asyncio.sleep(GRID_PING_INTERVAL_S)
            # Grid requests read the same socket, so a ping must not run in the middle of one.
            async with self._request_lock:
                if self._grid_conn is not grid_conn or not grid_conn.connected:
                    return
                grid_conn._mark_busy("grid_ping")
                try:
                    await asyncio.wait_for(self._ping(grid_conn), GRID_REPLY_TIMEOUT_S)
                except Exception as exc:
                    LOGGER.warning(
                        "SDG grid ping failed seq=%d error=%s errno=%s %s",
                        grid_conn.ping_sequence - 1, type(exc).__name__, getattr(exc, "errno", None),
                        grid_conn.diagnostic_context(),
                    )
                    await self._reset_grid_connection()
                    return
                finally:
                    grid_conn._mark_idle()

    async def _ping(self, grid_conn: _RemoteConnection) -> None:
        sequence = grid_conn.ping_sequence
        started_at = monotonic()
        await grid_conn._send_ping()
        while True:
            msg_type, data = await self._read_grid_message(grid_conn)
            # A PONG of an earlier ping that timed out is stale; only the current one counts.
            if msg_type == control.MSG_PONG and control.decode_pong(data) == sequence:
                grid_conn.ping_delay = round((monotonic() - started_at) * 1000)
                LOGGER.debug("SDG grid ping roundtrip seq=%d ms=%d", sequence, grid_conn.ping_delay)
                return

    async def _peer_request(self, request_id: int, msg_type: int, payload: bytes) -> PeerReply:
        async with self._request_lock:
            try:
                for attempt in range(2):
                    await self.connect()
                    grid_conn = self._grid_conn
                    # Data buffered before the request was sent by the server while the connection was idle.
                    LOGGER.debug(
                        "SDG peer request id=%d type=%d payload_len=%d attempt=%d buffered_before=%d",
                        request_id,
                        msg_type,
                        len(payload),
                        attempt + 1,
                        grid_conn._rx_buffered(),
                    )
                    grid_conn._mark_busy("grid_request")
                    try:
                        await grid_conn._send_mesg(msg_type, payload)
                        # A timeout is an OSError, so it resets the grid connection and retries like a drop.
                        return await asyncio.wait_for(self._read_grid_reply(grid_conn, request_id), GRID_REPLY_TIMEOUT_S)
                    except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
                        LOGGER.warning(
                            "SDG grid request failed id=%d attempt=%d error=%s errno=%s %s",
                            request_id, attempt + 1, type(exc).__name__, getattr(exc, "errno", None),
                            self._grid_conn.diagnostic_context(),
                        )
                        await self._reset_grid_connection()
                        if attempt == 1:
                            raise
                    finally:
                        grid_conn._mark_idle()
            finally:
                self._pending_request_ids.discard(request_id)

    async def _read_grid_message(self, grid_conn: _RemoteConnection) -> tuple[int, bytes]:
        """Read and handle the next grid message, skipping anything that is not a MESG."""
        while True:
            frame = await grid_conn.read_frame()
            header = PacketHeader.parse(frame)
            if header.command != CMD_MESG:
                LOGGER.debug("SDG ignore non-MESG command=%r while waiting grid reply", header.command)
                continue
            mesg = grid_conn._decrypt_mesg(frame[10:], b"CurveCP-server-M")
            data_size = struct.unpack(">H", mesg[:2])[0]
            data_type = mesg[2]
            data = mesg[3 : 3 + data_size - 1]
            await grid_conn._handle_grid_message(data_type, data)
            return data_type, data

    async def _read_grid_reply(self, grid_conn: _RemoteConnection, request_id: int) -> PeerReply:
        while True:
            await self._read_grid_message(grid_conn)
            reply = getattr(grid_conn, "_pending_reply", None)
            if reply is not None and reply.id == request_id:
                grid_conn._pending_reply = None
                LOGGER.debug("SDG peer request done id=%d result=%d peer=%s", request_id, reply.result, reply.peer)
                return reply

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
            LOGGER.warning("SDG remote connection refused peer=%s result=%d has_peer=%s", peer_id_hex, reply.result, reply.peer is not None)
            raise SDGProtocolError("remote connection refused")
        LOGGER.debug("SDG remote tunnel host=%s port=%d peer=%s", reply.peer.server.host, reply.peer.server.port, reply.peer.peer_id)
        started_at = monotonic()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(reply.peer.server.host, reply.peer.server.port), CONNECT_TIMEOUT_S,
            )
        except OSError as exc:
            LOGGER.warning(
                "SDG remote tunnel connect failed host=%s port=%d elapsed_s=%.2f error=%s errno=%s",
                reply.peer.server.host, reply.peer.server.port, monotonic() - started_at,
                type(exc).__name__, getattr(exc, "errno", None),
            )
            raise
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
        await conn.run_monitored_handshake("peer_handshake")
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
