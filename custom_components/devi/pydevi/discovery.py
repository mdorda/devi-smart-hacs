from __future__ import annotations

from dataclasses import dataclass, field

from .models import HouseConfig, ThermostatInfo
from .sdg.grid import GridSession


@dataclass(slots=True)
class DiscoveryRequest:
    client_name: str
    otp: str


@dataclass(slots=True)
class DiscoverySession:
    """Reusable discovery session.

    The DEVI share flow is flaky and often requires several fresh OTP codes.
    The legacy Java implementation keeps the same long-term identity while
    retrying OTPs in one process, so we mirror that here.
    """

    client_name: str
    private_key: str | None = None
    _grid: GridSession = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._grid = GridSession(private_key_hex=self.private_key, user_name=self.client_name)

    @property
    def private_key_hex(self) -> str:
        return self._grid.private_key_hex

    @property
    def public_key_hex(self) -> str:
        return self._grid.public_key_hex

    async def pair_and_discover(self, otp: str) -> HouseConfig:
        return await self._grid.pair_and_discover(DiscoveryRequest(client_name=self.client_name, otp=otp))

    async def disconnect(self) -> None:
        await self._grid.disconnect()


async def pair_and_discover(client_name: str, otp: str) -> HouseConfig:
    """Pair with the DEVI Smart house and return discovered thermostats.

    The grid transport scaffolding is implemented in ``pydevi.sdg``.
    Full wire-compatibility still needs packet-capture verification before this
    function can be considered production-ready.
    """

    session = DiscoverySession(client_name=client_name)
    try:
        return await session.pair_and_discover(otp)
    finally:
        await session.disconnect()


def house_config_from_dict(data: dict) -> HouseConfig:
    thermostats = [
        ThermostatInfo(
            peer_id=item["peer_id"],
            room_name=item["room_name"],
            serial_number=item.get("serial_number"),
            zone=item.get("zone"),
            metadata=item.get("metadata", {}),
        )
        for item in data["thermostats"]
    ]
    return HouseConfig(
        house_id=data.get("house_id"),
        user_name=data["user_name"],
        private_key=data["private_key"],
        thermostats=thermostats,
        house_name=data.get("house_name"),
    )
