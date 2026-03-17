from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


class DeviCoordinatorEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, thermostat) -> None:
        super().__init__(coordinator)
        self.thermostat = thermostat

    @property
    def device_info(self) -> DeviceInfo:
        state = self.coordinator.data
        return DeviceInfo(
            identifiers={(DOMAIN, self.thermostat.peer_id)},
            name=self.thermostat.room_name,
            model="DEVIReg Smart Thermostat",
            manufacturer="Danfoss",
            serial_number=(state.serial_number if state else self.thermostat.serial_number),
            sw_version=(state.firmware_version if state else None),
            hw_version=(state.hardware_version if state else None),
        )
