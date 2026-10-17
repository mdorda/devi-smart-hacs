from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import types
import unittest

from pydevi.models import HouseConfig, PresetMode, ThermostatInfo, ThermostatState


_COMPONENTS_PACKAGE = types.ModuleType("custom_components")
_DEVI_PACKAGE = types.ModuleType("custom_components.devi")
_REPO_ROOT = Path(__file__).resolve().parents[1]
_COMPONENTS_PACKAGE.__path__ = [str(_REPO_ROOT / "custom_components")]
_DEVI_PACKAGE.__path__ = [str(_REPO_ROOT / "custom_components" / "devi")]
sys.modules.setdefault("custom_components", _COMPONENTS_PACKAGE)
sys.modules.setdefault("custom_components.devi", _DEVI_PACKAGE)

_CONST_PATH = _REPO_ROOT / "custom_components" / "devi" / "const.py"
_CONST_SPEC = importlib.util.spec_from_file_location("custom_components.devi.const", _CONST_PATH)
assert _CONST_SPEC is not None and _CONST_SPEC.loader is not None
_CONST_MODULE = importlib.util.module_from_spec(_CONST_SPEC)
sys.modules["custom_components.devi.const"] = _CONST_MODULE
_CONST_SPEC.loader.exec_module(_CONST_MODULE)

_DIAG_PATH = _REPO_ROOT / "custom_components" / "devi" / "diagnostics.py"
_DIAG_SPEC = importlib.util.spec_from_file_location("custom_components.devi.diagnostics", _DIAG_PATH)
assert _DIAG_SPEC is not None and _DIAG_SPEC.loader is not None
_DIAG_MODULE = importlib.util.module_from_spec(_DIAG_SPEC)
sys.modules["custom_components.devi.diagnostics"] = _DIAG_MODULE
_DIAG_SPEC.loader.exec_module(_DIAG_MODULE)

async_get_config_entry_diagnostics = _DIAG_MODULE.async_get_config_entry_diagnostics


class DiagnosticsExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_diagnostics_redacts_private_key_and_exports_runtime(self) -> None:
        state = ThermostatState(
            available=True,
            preset_mode=PresetMode.MANUAL,
            room_temperature_c=21.5,
            target_temperature_c=22.0,
            cloud_connected=True,
        )
        state.room_name = "Bathroom"
        state.serial_number = "12345678"

        runtime = SimpleNamespace(
            house=HouseConfig(
                house_id="house-peer-id",
                house_name="Home",
                user_name="ha-user",
                private_key="abcdef0123456789",
                thermostats=[ThermostatInfo(peer_id="peer-1", room_name="Bathroom", serial_number="12345678")],
            ),
            grid=SimpleNamespace(
                user_name="ha-user",
                public_key_hex="publichex",
                _grid_conn=SimpleNamespace(connected=True),
                _lingering_config_conns=[object(), object()],
            ),
            clients={
                "peer-1": SimpleNamespace(
                    state=state,
                    _peer=SimpleNamespace(connected=True, protocol="dominion-1.0"),
                )
            },
        )

        entry = SimpleNamespace(
            entry_id="entry-1",
            title="DEVI",
            data={
                "user_name": "ha-user",
                "private_key": "0123456789abcdef",
                "thermostats": [{"peer_id": "peer-1", "room_name": "Bathroom"}],
            },
            options={"scan_interval_seconds": 30},
            runtime_data=runtime,
        )
        hass = SimpleNamespace(data={"devi": {"discovery_sessions": {"flow-1": object()}}})

        payload = await async_get_config_entry_diagnostics(hass, entry)

        self.assertEqual(payload["entry"]["data"]["private_key"], "**REDACTED**")
        self.assertEqual(payload["entry"]["entry_id"], "entry-1")
        self.assertEqual(payload["integration"]["discovery_session_count"], 1)
        self.assertEqual(payload["runtime"]["grid"]["connected"], True)
        self.assertEqual(payload["runtime"]["grid"]["lingering_config_connections"], 2)
        self.assertIn("peer-1", payload["runtime"]["clients"])
        self.assertEqual(payload["runtime"]["clients"]["peer-1"]["protocol"], "dominion-1.0")
        self.assertEqual(payload["runtime"]["clients"]["peer-1"]["state"]["room_temperature_c"], 21.5)
        self.assertEqual(payload["runtime"]["house"]["private_key"], "**REDACTED**")

    async def test_diagnostics_handles_missing_runtime_data(self) -> None:
        entry = SimpleNamespace(
            entry_id="entry-2",
            title="DEVI",
            data={"private_key": "deadbeef"},
            options={},
        )
        hass = SimpleNamespace(data={"devi": {}})

        payload = await async_get_config_entry_diagnostics(hass, entry)

        self.assertEqual(payload["entry"]["data"]["private_key"], "**REDACTED**")
        self.assertEqual(payload["runtime"], {})
        self.assertEqual(payload["integration"]["discovery_session_count"], 0)


if __name__ == "__main__":
    unittest.main()
