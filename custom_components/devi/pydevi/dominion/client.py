from __future__ import annotations

from datetime import UTC
import struct

from ..models import AwayInterval, ControlState, HVACMode, PresetMode, ThermostatState, WeeklySchedule
from .codec import DominionPacket
from . import constants as c


class PeerTransport:
    async def connect(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def disconnect(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def send(self, payload: bytes) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def request(self, payload: bytes, timeout: float = 1.0) -> list[bytes]:  # pragma: no cover - interface
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

    @property
    def state(self) -> ThermostatState:
        return self._state

    async def connect(self) -> None:
        await self._peer.connect()

    async def disconnect(self) -> None:
        await self._peer.disconnect()

    async def refresh_state(self) -> ThermostatState:
        packets = [
            DominionPacket.read_request(c.DOMINION_HEATING, c.HEATING_TEMPERATURE_ROOM),
            DominionPacket.read_request(c.DOMINION_HEATING, c.HEATING_TEMPERATURE_FLOOR),
            DominionPacket.read_request(c.DOMINION_SYSTEM, c.SYSTEM_HEATING_INFO),
            DominionPacket.read_request(c.DOMINION_SYSTEM, c.SYSTEM_ALARM_INFO),
            DominionPacket.read_request(c.DOMINION_SYSTEM, c.SYSTEM_INFO_FLOOR_SENSOR_CONNECTED),
            DominionPacket.read_request(c.DOMINION_SYSTEM, c.SYSTEM_ROOM_NAME),
            DominionPacket.read_request(c.DOMINION_SYSTEM, c.SYSTEM_HOUSE_NAME),
            DominionPacket.read_request(c.DOMINION_SCHEDULER, c.SCHEDULER_CONTROL_INFO),
            DominionPacket.read_request(c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_COMFORT),
            DominionPacket.read_request(c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_ECONOMY),
            DominionPacket.read_request(c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_MANUAL),
            DominionPacket.read_request(c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_TEMPORARY),
            DominionPacket.read_request(c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_AWAY),
            DominionPacket.read_request(c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_FROST_PROTECTION),
            DominionPacket.read_request(c.DOMINION_SCHEDULER, c.SCHEDULER_AWAY_ISPLANNED),
            DominionPacket.read_request(c.DOMINION_SCHEDULER, c.SCHEDULER_AWAY),
            DominionPacket.read_request(c.DOMINION_SCHEDULER, c.SCHEDULER_WEEK),
            DominionPacket.read_request(c.DOMINION_SCHEDULER, c.SCHEDULER_WEEK_2),
            DominionPacket.read_request(c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_FLOOR_COMFORT),
            DominionPacket.read_request(c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_FLOOR_COMFORT_ENABLED),
            DominionPacket.read_request(c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_MAX_FLOOR),
            DominionPacket.read_request(c.DOMINION_SYSTEM, c.SYSTEM_WINDOW_OPEN),
            DominionPacket.read_request(c.DOMINION_SYSTEM, c.SYSTEM_INFO_FORECAST_ENABLED),
            DominionPacket.read_request(c.DOMINION_SYSTEM, c.SYSTEM_INFO_WINDOW_OPEN_DETECTION),
            DominionPacket.read_request(c.DOMINION_SYSTEM, c.SYSTEM_UI_SAFETY_LOCK),
            DominionPacket.read_request(c.MDG, c.MDG_CONNECTED_TO_SERVER),
            DominionPacket.read_request(c.DEVICEGLOBAL, c.GLOBAL_HARDWAREREVISION),
            DominionPacket.read_request(c.DEVICEGLOBAL, c.GLOBAL_SOFTWAREREVISION),
            DominionPacket.read_request(c.DEVICEGLOBAL, c.GLOBAL_SOFTWAREBUILDREVISION),
            DominionPacket.read_request(c.DEVICEGLOBAL, c.GLOBAL_SERIALNUMBER),
            DominionPacket.read_request(c.DEVICEGLOBAL, c.GLOBAL_PRODUCTIONDATE),
        ]
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
        packet = DominionPacket.from_decimal(c.DOMINION_SCHEDULER, code, value_c)
        await self._peer.send(packet.to_wire_bytes())
        self._state.target_temperature_c = value_c

    async def set_number_setting(self, setting: str, value_c: float) -> None:
        if setting not in self.NUMBER_SETTINGS:
            raise ValueError(f"unsupported number setting: {setting}")
        endpoint, code = self.NUMBER_SETTINGS[setting]
        packet = DominionPacket.from_decimal(endpoint, code, value_c)
        await self._peer.send(packet.to_wire_bytes())
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
        packet = DominionPacket.from_bool(endpoint, code, enabled)
        await self._peer.send(packet.to_wire_bytes())
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
        await self._peer.send(b"".join(packet.to_wire_bytes() for packet in packets))
        self._state.weekly_schedule = WeeklySchedule(week_1_raw=bytes(week_1_raw), week_2_raw=bytes(week_2_raw) if week_2_raw is not None else None)

    async def set_away_interval(self, away: AwayInterval | None, is_planned: bool = True) -> None:
        away_packet = DominionPacket.from_away_interval(
            c.DOMINION_SCHEDULER,
            c.SCHEDULER_AWAY,
            away or AwayInterval(start=None, end=None),
        )
        planned_packet = DominionPacket.from_bool(c.DOMINION_SCHEDULER, c.SCHEDULER_AWAY_ISPLANNED, is_planned)
        await self._peer.send(away_packet.to_wire_bytes() + planned_packet.to_wire_bytes())
        self._state.away_interval = away
        self._state.away_planned = is_planned

    async def set_preset_mode(self, mode: PresetMode) -> None:
        packets = self._preset_packets(mode)
        await self._peer.send(b"".join(packet.to_wire_bytes() for packet in packets))
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
