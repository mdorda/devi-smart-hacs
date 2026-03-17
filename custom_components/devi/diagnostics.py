from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from .const import DOMAIN

try:  # pragma: no cover - exercised in HA runtime
    from homeassistant.components.diagnostics import async_redact_data
except Exception:  # pragma: no cover - unittest fallback without HA package
    def async_redact_data(data: Any, to_redact: set[str]) -> Any:
        if isinstance(data, dict):
            return {
                key: ("REDACTED" if key in to_redact else async_redact_data(value, to_redact))
                for key, value in data.items()
            }
        if isinstance(data, list):
            return [async_redact_data(item, to_redact) for item in data]
        return data


TO_REDACT = {
    "private_key",
    "private_key_hex",
    "otp",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "phonePublicKey",
}


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, set):
        return sorted(_to_jsonable(item) for item in value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return value


def _runtime_snapshot(entry: Any) -> dict[str, Any]:
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return {}

    house = getattr(runtime, "house", None)
    grid = getattr(runtime, "grid", None)
    clients = getattr(runtime, "clients", {})

    grid_conn = getattr(grid, "_grid_conn", None)
    grid_connected = bool(grid_conn and getattr(grid_conn, "connected", False))

    client_states: dict[str, Any] = {}
    for peer_id, client in clients.items():
        peer_transport = getattr(client, "_peer", None)
        client_states[peer_id] = {
            "connected": bool(getattr(peer_transport, "connected", False)),
            "protocol": getattr(peer_transport, "protocol", None),
            "state": _to_jsonable(getattr(client, "state", None)),
        }

    return {
        "house": _to_jsonable(house),
        "grid": {
            "user_name": getattr(grid, "user_name", None),
            "public_key_hex": getattr(grid, "public_key_hex", None),
            "connected": grid_connected,
            "lingering_config_connections": len(getattr(grid, "_lingering_config_conns", []) or []),
        },
        "clients": client_states,
    }


async def async_get_config_entry_diagnostics(hass: Any, entry: Any) -> dict[str, Any]:
    domain_data = getattr(hass, "data", {}).get(DOMAIN, {})
    discovery_sessions = domain_data.get("discovery_sessions", {})

    payload = {
        "entry": {
            "entry_id": getattr(entry, "entry_id", None),
            "title": getattr(entry, "title", None),
            "data": _to_jsonable(getattr(entry, "data", {})),
            "options": _to_jsonable(getattr(entry, "options", {})),
        },
        "runtime": _runtime_snapshot(entry),
        "integration": {
            "discovery_session_count": len(discovery_sessions),
        },
    }
    return async_redact_data(payload, TO_REDACT)
