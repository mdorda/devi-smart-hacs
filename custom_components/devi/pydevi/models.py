from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class PresetMode(str, Enum):
    MANUAL = "manual"
    SCHEDULE = "schedule"
    OVERRIDE = "override"
    VACATION = "vacation"
    PAUSE = "pause"
    OFF = "off"


class HVACMode(str, Enum):
    HEAT = "heat"
    OFF = "off"


class ControlState(str, Enum):
    CONFIGURING = "configuring"
    MANUAL = "manual"
    HOME = "home"
    AWAY = "away"
    VACATION = "vacation"
    FATAL = "fatal"
    PAUSE = "pause"
    OFF = "off"
    OVERRIDE = "override"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class ThermostatInfo:
    peer_id: str
    room_name: str
    serial_number: str | None = None
    zone: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class AwayInterval:
    start: datetime | None
    end: datetime | None


@dataclass(slots=True)
class WeeklySchedule:
    week_1_raw: bytes
    week_2_raw: bytes | None = None


@dataclass(slots=True)
class HouseConfig:
    house_id: str | None
    user_name: str
    private_key: str
    thermostats: list[ThermostatInfo]
    house_name: str | None = None


@dataclass(slots=True)
class ThermostatState:
    available: bool = False
    room_temperature_c: float | None = None
    floor_temperature_c: float | None = None
    current_temperature_c: float | None = None
    target_temperature_c: float | None = None
    preset_mode: PresetMode | None = None
    hvac_mode: HVACMode = HVACMode.HEAT
    control_state: ControlState = ControlState.UNKNOWN
    heating_active: bool | None = None
    cloud_connected: bool | None = None
    warning_disconnected: bool = False
    warning_shorted: bool = False
    warning_short_circuited: bool = False
    warning_overheat: bool = False
    warning_unrecoverable: bool = False
    active_setpoint_key: str | None = None
    firmware_version: str | None = None
    hardware_version: str | None = None
    serial_number: str | None = None
    production_date: str | None = None
    room_name: str | None = None
    house_name: str | None = None
    away_planned: bool | None = None
    away_interval: AwayInterval | None = None
    weekly_schedule: WeeklySchedule | None = None
    comfort_temperature_c: float | None = None
    economy_temperature_c: float | None = None
    away_temperature_c: float | None = None
    frost_protection_temperature_c: float | None = None
    floor_comfort_temperature_c: float | None = None
    max_floor_temperature_c: float | None = None
    floor_comfort_enabled: bool | None = None
    floor_sensor_connected: bool | None = None
    window_open_active: bool | None = None
    forecast_heating_enabled: bool | None = None
    window_open_detection_enabled: bool | None = None
    safety_lock_enabled: bool | None = None
    zone_name: str | None = None

    @property
    def preset_name(self) -> str | None:
        return self.preset_mode.value if self.preset_mode else None
