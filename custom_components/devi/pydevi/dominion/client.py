from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import fields
from datetime import UTC
from enum import Enum
import logging
import struct
from time import monotonic

from ..models import AwayInterval, ControlState, HVACMode, PresetMode, ThermostatState, WeeklySchedule
from .codec import DominionPacket
from . import constants as c

LOGGER = logging.getLogger(__name__)

# Every packet the integration's state is made of; the thermostat sends all of them right after connecting.
STATE_PACKET_IDS: tuple[tuple[int, int], ...] = (
    (c.DOMINION_HEATING, c.HEATING_TEMPERATURE_ROOM),
    (c.DOMINION_HEATING, c.HEATING_TEMPERATURE_FLOOR),
    (c.DOMINION_SYSTEM, c.SYSTEM_HEATING_INFO),
    (c.DOMINION_SYSTEM, c.SYSTEM_ALARM_INFO),
    (c.DOMINION_SYSTEM, c.SYSTEM_INFO_FLOOR_SENSOR_CONNECTED),
    (c.DOMINION_SYSTEM, c.SYSTEM_ROOM_NAME),
    (c.DOMINION_SYSTEM, c.SYSTEM_HOUSE_NAME),
    (c.DOMINION_SCHEDULER, c.SCHEDULER_CONTROL_INFO),
    (c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_COMFORT),
    (c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_ECONOMY),
    (c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_MANUAL),
    (c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_TEMPORARY),
    (c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_AWAY),
    (c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_FROST_PROTECTION),
    (c.DOMINION_SCHEDULER, c.SCHEDULER_AWAY_ISPLANNED),
    (c.DOMINION_SCHEDULER, c.SCHEDULER_AWAY),
    (c.DOMINION_SCHEDULER, c.SCHEDULER_WEEK),
    (c.DOMINION_SCHEDULER, c.SCHEDULER_WEEK_2),
    (c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_FLOOR_COMFORT),
    (c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_FLOOR_COMFORT_ENABLED),
    (c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_MAX_FLOOR),
    (c.DOMINION_SYSTEM, c.SYSTEM_WINDOW_OPEN),
    (c.DOMINION_SYSTEM, c.SYSTEM_INFO_FORECAST_ENABLED),
    (c.DOMINION_SYSTEM, c.SYSTEM_INFO_WINDOW_OPEN_DETECTION),
    (c.DOMINION_SYSTEM, c.SYSTEM_UI_SAFETY_LOCK),
    (c.MDG, c.MDG_CONNECTED_TO_SERVER),
    (c.DEVICEGLOBAL, c.GLOBAL_HARDWAREREVISION),
    (c.DEVICEGLOBAL, c.GLOBAL_SOFTWAREREVISION),
    (c.DEVICEGLOBAL, c.GLOBAL_SOFTWAREBUILDREVISION),
    (c.DEVICEGLOBAL, c.GLOBAL_SERIALNUMBER),
    (c.DEVICEGLOBAL, c.GLOBAL_PRODUCTIONDATE),
)

# A cheap read request whose answer shows the tunnel is alive.
PROBE_REQUEST = DominionPacket.read_request(c.DOMINION_HEATING, c.HEATING_TEMPERATURE_FLOOR).to_wire_bytes()
# How long after connecting the thermostat may take to send every state packet before they are requested.
READY_TIMEOUT_S = 15.0
# How long a command waits for the thermostat to push the value it wrote.
COMMAND_CONFIRM_S = 10.0
# Values identifying the home or its owner stay out of logs.
_REDACTED_FIELDS = frozenset({"room_name", "house_name", "serial_number", "zone_name"})
_STATE_FIELDS = tuple(item.name for item in fields(ThermostatState))


def _packet_id(packet_id: tuple[int, int]) -> str:
    return f"{packet_id[0]}:{packet_id[1]}"


def _loggable(value: object) -> tuple[bool, object]:
    if isinstance(value, Enum):
        return True, value.value
    return isinstance(value, (bool, int, float, str, type(None))), value


def _describe_change(name: str, old: object, new: object) -> str:
    old_ok, old_value = _loggable(old)
    new_ok, new_value = _loggable(new)
    if name in _REDACTED_FIELDS or not (old_ok and new_ok):
        return name
    return f"{name}:{old_value}->{new_value}"


class PeerTransport:
    async def connect(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def disconnect(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def send(self, payload: bytes) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def request(self, payload: bytes, timeout: float = 1.0) -> list[bytes]:  # pragma: no cover - interface
        raise NotImplementedError

    def start(self, on_chunk: Callable[[bytes], None], on_connection: Callable[[bool], None]) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def stop(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class ThermostatClient:
    NUMBER_SETTINGS: dict[str, tuple[int, int]] = {
        "comfort_temperature": (c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_COMFORT),
        "economy_temperature": (c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_ECONOMY),
        "away_temperature": (c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_AWAY),
        "frost_protection_temperature": (c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_FROST_PROTECTION),
        "floor_comfort_temperature": (c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_FLOOR_COMFORT),
        "max_floor_temperature": (c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_MAX_FLOOR),
    }
    SWITCH_SETTINGS: dict[str, tuple[int, int]] = {
        "forecast_heating_enabled": (c.DOMINION_SYSTEM, c.SYSTEM_INFO_FORECAST_ENABLED),
        "window_open_detection_enabled": (c.DOMINION_SYSTEM, c.SYSTEM_INFO_WINDOW_OPEN_DETECTION),
        "safety_lock_enabled": (c.DOMINION_SYSTEM, c.SYSTEM_UI_SAFETY_LOCK),
        "floor_comfort_enabled": (c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_FLOOR_COMFORT_ENABLED),
    }

    def __init__(self, peer: PeerTransport) -> None:
        self._peer = peer
        self._state = ThermostatState()
        self._push = False
        self._on_update: Callable[[ThermostatState], None] | None = None
        self._on_connection: Callable[[bool], None] | None = None
        self._seen: set[tuple[int, int]] = set()
        self._ready = asyncio.Event()
        self._connected_at = 0.0
        self._ready_check: asyncio.Task | None = None
        self._unconfirmed: dict[tuple[int, int], float] = {}

    @property
    def state(self) -> ThermostatState:
        return self._state

    @property
    def ready(self) -> bool:
        """Whether every state packet arrived since the tunnel connected."""
        return self._ready.is_set()

    def start(
        self, on_update: Callable[[ThermostatState], None], on_connection: Callable[[bool], None],
    ) -> None:
        """Keep the tunnel open and apply everything the thermostat pushes."""
        self._push = True
        self._on_update = on_update
        self._on_connection = on_connection
        self._peer.start(self._handle_chunk, self._handle_connection)

    async def stop(self) -> None:
        self._cancel_ready_check()
        await self._peer.stop()

    async def wait_ready(self, timeout: float) -> bool:
        try:
            async with asyncio.timeout(timeout):
                await self._ready.wait()
        except TimeoutError:
            return False
        return True

    def _handle_connection(self, connected: bool) -> None:
        self._cancel_ready_check()
        self._ready.clear()
        if connected:
            self._seen.clear()
            self._connected_at = monotonic()
            self._ready_check = asyncio.get_running_loop().create_task(self._request_missing_state())
        if self._on_connection is not None:
            self._on_connection(connected)

    def _cancel_ready_check(self) -> None:
        task, self._ready_check = self._ready_check, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _request_missing_state(self) -> None:
        await asyncio.sleep(READY_TIMEOUT_S)
        missing = [packet_id for packet_id in STATE_PACKET_IDS if packet_id not in self._seen]
        if not missing:
            return
        LOGGER.warning(
            "DEVI link ready incomplete peer=%s after_s=%.0f missing=%s",
            self._peer_id(), READY_TIMEOUT_S, ",".join(_packet_id(packet_id) for packet_id in missing),
        )
        try:
            await self._peer.send(b"".join(DominionPacket.read_request(*packet_id).to_wire_bytes() for packet_id in missing))
        except Exception as exc:
            LOGGER.debug("DEVI request of missing state failed peer=%s error=%s", self._peer_id(), type(exc).__name__)

    def _handle_chunk(self, data: bytes) -> None:
        try:
            packets = DominionPacket.parse_stream(data)
        except (ValueError, struct.error):
            LOGGER.debug("DEVI ignore unparsable chunk peer=%s len=%d", self._peer_id(), len(data))
            return
        state = self._state
        before = {name: getattr(state, name) for name in _STATE_FIELDS}
        now = monotonic()
        for packet in packets:
            self.apply_packet(packet)
            packet_id = (packet.msg_class, packet.msg_code)
            self._seen.add(packet_id)
            sent_at = self._unconfirmed.pop(packet_id, None)
            if sent_at is not None:
                LOGGER.debug(
                    "DEVI command confirmed by push peer=%s id=%s after_s=%.2f",
                    self._peer_id(), _packet_id(packet_id), now - sent_at,
                )
        if not self._ready.is_set() and self._seen.issuperset(STATE_PACKET_IDS):
            self._ready.set()
            LOGGER.debug("DEVI link ready peer=%s after_s=%.2f", self._peer_id(), now - self._connected_at)
        changes = [
            _describe_change(name, before[name], getattr(state, name))
            for name in _STATE_FIELDS
            if before[name] != getattr(state, name)
        ]
        if changes:
            LOGGER.debug("DEVI push changed peer=%s fields=%s", self._peer_id(), ",".join(changes))
            if self._on_update is not None:
                self._on_update(state)

    async def _send(self, packets: list[DominionPacket]) -> None:
        await self._peer.send(b"".join(packet.to_wire_bytes() for packet in packets))
        if not self._push:
            return
        sent_at = monotonic()
        packet_ids = [(packet.msg_class, packet.msg_code) for packet in packets]
        for packet_id in packet_ids:
            self._unconfirmed[packet_id] = sent_at
        LOGGER.debug("DEVI command sent peer=%s ids=%s", self._peer_id(), ",".join(map(_packet_id, packet_ids)))
        asyncio.get_running_loop().call_later(COMMAND_CONFIRM_S, self._check_confirmed, packet_ids, sent_at)

    def _check_confirmed(self, packet_ids: list[tuple[int, int]], sent_at: float) -> None:
        missing = [packet_id for packet_id in packet_ids if self._unconfirmed.get(packet_id) == sent_at]
        for packet_id in missing:
            del self._unconfirmed[packet_id]
        if missing:
            LOGGER.warning(
                "DEVI command not confirmed by push peer=%s within_s=%.0f ids=%s",
                self._peer_id(), COMMAND_CONFIRM_S, ",".join(map(_packet_id, missing)),
            )

    def _peer_id(self) -> str | None:
        return getattr(self._peer, "peer_id", None)

    async def connect(self) -> None:
        await self._peer.connect()

    async def disconnect(self) -> None:
        await self._peer.disconnect()

    async def refresh_state(self) -> ThermostatState:
        packets = [DominionPacket.read_request(*packet_id) for packet_id in STATE_PACKET_IDS]
        responses = await self._peer.request(b"".join(packet.to_wire_bytes() for packet in packets))
        for chunk in responses:
            for packet in DominionPacket.parse_stream(chunk):
                self.apply_packet(packet)
        return self._state

    async def set_target_temperature(self, value_c: float) -> None:
        code: int | None = None
        if self._state.preset_mode == PresetMode.SCHEDULE:
            await self.set_preset_mode(PresetMode.OVERRIDE)
            code = c.SCHEDULER_SETPOINT_TEMPORARY
        elif self._state.preset_mode == PresetMode.OFF:
            await self.set_preset_mode(PresetMode.MANUAL)
            code = c.SCHEDULER_SETPOINT_MANUAL
        if code is None:
            code = self._active_setpoint_code()
        await self._send([DominionPacket.from_decimal(c.DOMINION_SCHEDULER, code, value_c)])
        self._state.target_temperature_c = value_c

    async def set_number_setting(self, setting: str, value_c: float) -> None:
        if setting not in self.NUMBER_SETTINGS:
            raise ValueError(f"unsupported number setting: {setting}")
        endpoint, code = self.NUMBER_SETTINGS[setting]
        await self._send([DominionPacket.from_decimal(endpoint, code, value_c)])
        if setting == "comfort_temperature":
            self._state.comfort_temperature_c = value_c
        elif setting == "economy_temperature":
            self._state.economy_temperature_c = value_c
        elif setting == "away_temperature":
            self._state.away_temperature_c = value_c
        elif setting == "frost_protection_temperature":
            self._state.frost_protection_temperature_c = value_c
        elif setting == "floor_comfort_temperature":
            self._state.floor_comfort_temperature_c = value_c
        elif setting == "max_floor_temperature":
            self._state.max_floor_temperature_c = value_c

    async def set_switch_setting(self, setting: str, enabled: bool) -> None:
        if setting not in self.SWITCH_SETTINGS:
            raise ValueError(f"unsupported switch setting: {setting}")
        endpoint, code = self.SWITCH_SETTINGS[setting]
        await self._send([DominionPacket.from_bool(endpoint, code, enabled)])
        if setting == "forecast_heating_enabled":
            self._state.forecast_heating_enabled = enabled
        elif setting == "window_open_detection_enabled":
            self._state.window_open_detection_enabled = enabled
        elif setting == "safety_lock_enabled":
            self._state.safety_lock_enabled = enabled
        elif setting == "floor_comfort_enabled":
            self._state.floor_comfort_enabled = enabled

    async def set_weekly_schedule(self, week_1_raw: bytes, week_2_raw: bytes | None = None) -> None:
        packets = [DominionPacket(c.DOMINION_SCHEDULER, c.SCHEDULER_WEEK, week_1_raw)]
        if week_2_raw is not None:
            packets.append(DominionPacket(c.DOMINION_SCHEDULER, c.SCHEDULER_WEEK_2, week_2_raw))
        await self._send(packets)
        self._state.weekly_schedule = WeeklySchedule(week_1_raw=bytes(week_1_raw), week_2_raw=bytes(week_2_raw) if week_2_raw is not None else None)

    async def set_away_interval(self, away: AwayInterval | None, is_planned: bool = True) -> None:
        away_packet = DominionPacket.from_away_interval(
            c.DOMINION_SCHEDULER,
            c.SCHEDULER_AWAY,
            away or AwayInterval(start=None, end=None),
        )
        planned_packet = DominionPacket.from_bool(c.DOMINION_SCHEDULER, c.SCHEDULER_AWAY_ISPLANNED, is_planned)
        await self._send([away_packet, planned_packet])
        self._state.away_interval = away
        self._state.away_planned = is_planned

    async def set_preset_mode(self, mode: PresetMode) -> None:
        await self._send(self._preset_packets(mode))
        self._state.preset_mode = mode
        self._state.hvac_mode = HVACMode.OFF if mode == PresetMode.OFF else HVACMode.HEAT

    async def set_hvac_mode(self, mode: HVACMode) -> None:
        if mode == HVACMode.OFF:
            await self.set_preset_mode(PresetMode.OFF)
        elif self._state.preset_mode == PresetMode.OFF:
            await self.set_preset_mode(PresetMode.MANUAL)
        self._state.hvac_mode = mode

    async def read_diagnostics(self) -> ThermostatState:
        await self.refresh_state()
        return self._state

    def apply_packet(self, packet: DominionPacket) -> ThermostatState:
        state = self._state
        if packet.msg_code == c.HEATING_TEMPERATURE_ROOM:
            state.room_temperature_c = packet.get_decimal()
            if state.current_temperature_c is None:
                state.current_temperature_c = state.room_temperature_c
        elif packet.msg_code == c.HEATING_TEMPERATURE_FLOOR:
            state.floor_temperature_c = packet.get_decimal()
            if state.current_temperature_c is None:
                state.current_temperature_c = state.floor_temperature_c
        elif packet.msg_code == c.SYSTEM_HEATING_INFO:
            state.heating_active = packet.get_bool()
        elif packet.msg_code == c.MDG_CONNECTED_TO_SERVER:
            state.cloud_connected = packet.get_bool()
        elif packet.msg_code == c.SCHEDULER_CONTROL_INFO:
            self._apply_control_info(packet.get_byte())
        elif packet.msg_code == c.SCHEDULER_SETPOINT_COMFORT:
            state.comfort_temperature_c = packet.get_decimal()
            self._update_target("comfort", packet.get_decimal())
        elif packet.msg_code == c.SCHEDULER_SETPOINT_ECONOMY:
            state.economy_temperature_c = packet.get_decimal()
            self._update_target("economy", packet.get_decimal())
        elif packet.msg_code == c.SCHEDULER_SETPOINT_MANUAL:
            self._update_target("manual", packet.get_decimal())
        elif packet.msg_code == c.SCHEDULER_SETPOINT_TEMPORARY:
            self._update_target("temporary", packet.get_decimal())
        elif packet.msg_code == c.SCHEDULER_SETPOINT_AWAY:
            state.away_temperature_c = packet.get_decimal()
            self._update_target("away", packet.get_decimal())
        elif packet.msg_code == c.SCHEDULER_SETPOINT_FROST_PROTECTION:
            state.frost_protection_temperature_c = packet.get_decimal()
            self._update_target("antifreeze", packet.get_decimal())
        elif packet.msg_code == c.SCHEDULER_AWAY_ISPLANNED:
            state.away_planned = packet.get_bool()
        elif packet.msg_code == c.SCHEDULER_AWAY:
            state.away_interval = packet.get_away_interval()
        elif packet.msg_code == c.SCHEDULER_WEEK:
            schedule = state.weekly_schedule or WeeklySchedule(week_1_raw=b"")
            state.weekly_schedule = WeeklySchedule(week_1_raw=bytes(packet.payload), week_2_raw=schedule.week_2_raw)
        elif packet.msg_code == c.SCHEDULER_WEEK_2:
            schedule = state.weekly_schedule or WeeklySchedule(week_1_raw=b"")
            state.weekly_schedule = WeeklySchedule(week_1_raw=schedule.week_1_raw, week_2_raw=bytes(packet.payload))
        elif packet.msg_code == c.SCHEDULER_SETPOINT_FLOOR_COMFORT:
            state.floor_comfort_temperature_c = packet.get_decimal()
        elif packet.msg_code == c.SCHEDULER_SETPOINT_FLOOR_COMFORT_ENABLED:
            state.floor_comfort_enabled = packet.get_bool()
        elif packet.msg_code == c.SCHEDULER_SETPOINT_MAX_FLOOR:
            state.max_floor_temperature_c = packet.get_decimal()
        elif packet.msg_code == c.SYSTEM_ALARM_INFO:
            alarms = packet.get_byte()
            state.warning_disconnected = bool(alarms & c.ALARM_DISCONNECTED)
            state.warning_shorted = bool(alarms & c.ALARM_SHORT_CIRCUITED)
            state.warning_short_circuited = state.warning_shorted
            state.warning_overheat = bool(alarms & c.ALARM_OVERHEAT)
            state.warning_unrecoverable = bool(alarms & c.ALARM_UNRECOVERABLE)
        elif packet.msg_code == c.SYSTEM_WINDOW_OPEN:
            state.window_open_active = packet.get_bool()
        elif packet.msg_code == c.SYSTEM_INFO_FLOOR_SENSOR_CONNECTED:
            state.floor_sensor_connected = packet.get_bool()
        elif packet.msg_code == c.SYSTEM_INFO_FORECAST_ENABLED:
            state.forecast_heating_enabled = packet.get_bool()
        elif packet.msg_code == c.SYSTEM_INFO_WINDOW_OPEN_DETECTION:
            state.window_open_detection_enabled = packet.get_bool()
        elif packet.msg_code == c.SYSTEM_UI_SAFETY_LOCK:
            state.safety_lock_enabled = packet.get_bool()
        elif packet.msg_code == c.GLOBAL_HARDWAREREVISION:
            state.hardware_version = packet.get_version()
        elif packet.msg_code == c.GLOBAL_SOFTWAREREVISION:
            state.firmware_version = packet.get_version()
        elif packet.msg_code == c.GLOBAL_SOFTWAREBUILDREVISION:
            build = packet.get_short()
            if state.firmware_version:
                state.firmware_version = f"{state.firmware_version}.{build}"
        elif packet.msg_code == c.GLOBAL_SERIALNUMBER:
            state.serial_number = str(packet.get_int())
        elif packet.msg_code == c.GLOBAL_PRODUCTIONDATE:
            state.production_date = packet.get_date().astimezone(UTC).isoformat()
        elif packet.msg_code == c.SYSTEM_ROOM_NAME:
            state.room_name = packet.get_string()
        elif packet.msg_code == c.SYSTEM_HOUSE_NAME:
            state.house_name = packet.get_string()

        state.available = True
        return state

    def _apply_control_info(self, info: int) -> None:
        if info == c.CONTROL_STATE_MANUAL:
            self._state.preset_mode = PresetMode.MANUAL
            self._state.control_state = ControlState.MANUAL
            self._state.active_setpoint_key = "manual"
        elif info == c.CONTROL_STATE_AT_HOME:
            self._state.preset_mode = PresetMode.SCHEDULE
            self._state.control_state = ControlState.HOME
            self._state.active_setpoint_key = "comfort"
        elif info == c.CONTROL_STATE_AWAY:
            self._state.preset_mode = PresetMode.SCHEDULE
            self._state.control_state = ControlState.AWAY
            self._state.active_setpoint_key = "economy"
        elif info == c.CONTROL_STATE_VACATION:
            self._state.preset_mode = PresetMode.VACATION
            self._state.control_state = ControlState.VACATION
            self._state.active_setpoint_key = "away"
        elif info == c.CONTROL_STATE_PAUSE:
            self._state.preset_mode = PresetMode.PAUSE
            self._state.control_state = ControlState.PAUSE
            self._state.active_setpoint_key = "antifreeze"
        elif info == c.CONTROL_STATE_OFF:
            self._state.preset_mode = PresetMode.OFF
            self._state.control_state = ControlState.OFF
            self._state.active_setpoint_key = None
        elif info == c.CONTROL_STATE_AT_HOME_OVERRIDE:
            self._state.preset_mode = PresetMode.OVERRIDE
            self._state.control_state = ControlState.OVERRIDE
            self._state.active_setpoint_key = "temporary"
        elif info == c.CONTROL_STATE_CONFIGURING:
            self._state.control_state = ControlState.CONFIGURING
        elif info == c.CONTROL_STATE_FATAL:
            self._state.control_state = ControlState.FATAL
        else:
            self._state.control_state = ControlState.UNKNOWN

        self._state.hvac_mode = HVACMode.OFF if self._state.preset_mode == PresetMode.OFF else HVACMode.HEAT

    def _update_target(self, key: str, value: float | None) -> None:
        if self._state.active_setpoint_key == key:
            self._state.target_temperature_c = value

    def _active_setpoint_code(self) -> int:
        mapping = {
            "comfort": c.SCHEDULER_SETPOINT_COMFORT,
            "economy": c.SCHEDULER_SETPOINT_ECONOMY,
            "manual": c.SCHEDULER_SETPOINT_MANUAL,
            "temporary": c.SCHEDULER_SETPOINT_TEMPORARY,
            "away": c.SCHEDULER_SETPOINT_AWAY,
            "antifreeze": c.SCHEDULER_SETPOINT_FROST_PROTECTION,
        }
        return mapping.get(self._state.active_setpoint_key or "manual", c.SCHEDULER_SETPOINT_MANUAL)

    def _preset_packets(self, mode: PresetMode) -> list[DominionPacket]:
        if mode == PresetMode.MANUAL:
            return [DominionPacket.from_byte(c.DOMINION_SCHEDULER, c.SCHEDULER_CONTROL_MODE, c.CONTROL_MODE_WEEKLY_SCHEDULE_OFF)]
        if mode == PresetMode.OVERRIDE:
            return [DominionPacket.from_byte(c.DOMINION_SCHEDULER, c.SCHEDULER_CONTROL_MODE, c.CONTROL_MODE_TEMPORARY_HOME_ON)]
        if mode == PresetMode.SCHEDULE:
            return [DominionPacket.from_byte(c.DOMINION_SCHEDULER, c.SCHEDULER_CONTROL_MODE, c.CONTROL_MODE_WEEKLY_SCHEDULE_ON)]
        if mode == PresetMode.VACATION:
            payload = struct.pack("<B", 0) + (b"\x00" * 6) + struct.pack("<B", 0)
            return [
                DominionPacket(c.DOMINION_SCHEDULER, c.SCHEDULER_AWAY, payload),
                DominionPacket.from_bool(c.DOMINION_SCHEDULER, c.SCHEDULER_AWAY_ISPLANNED, True),
            ]
        if mode == PresetMode.PAUSE:
            return [DominionPacket.from_byte(c.DOMINION_SCHEDULER, c.SCHEDULER_CONTROL_MODE, c.CONTROL_MODE_FROST_PROTECTION_ON)]
        if mode == PresetMode.OFF:
            return [DominionPacket.from_byte(c.DOMINION_SCHEDULER, c.SCHEDULER_CONTROL_MODE, c.CONTROL_MODE_OFF_STATE_ON)]
        raise ValueError(f"unsupported preset mode: {mode}")
