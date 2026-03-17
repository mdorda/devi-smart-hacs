from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

_CONST_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "devi" / "const.py"
_SPEC = importlib.util.spec_from_file_location("devi_const", _CONST_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

DEFAULT_ENABLE_ADVANCED_ENTITIES = _MODULE.DEFAULT_ENABLE_ADVANCED_ENTITIES
DEFAULT_ENABLE_DIAGNOSTIC_ENTITIES = _MODULE.DEFAULT_ENABLE_DIAGNOSTIC_ENTITIES
DEFAULT_SCAN_INTERVAL_SECONDS = _MODULE.DEFAULT_SCAN_INTERVAL_SECONDS
MAX_SCAN_INTERVAL_SECONDS = _MODULE.MAX_SCAN_INTERVAL_SECONDS
MIN_SCAN_INTERVAL_SECONDS = _MODULE.MIN_SCAN_INTERVAL_SECONDS
OPT_ENABLE_ADVANCED_ENTITIES = _MODULE.OPT_ENABLE_ADVANCED_ENTITIES
OPT_ENABLE_DIAGNOSTIC_ENTITIES = _MODULE.OPT_ENABLE_DIAGNOSTIC_ENTITIES
OPT_SCAN_INTERVAL_SECONDS = _MODULE.OPT_SCAN_INTERVAL_SECONDS
option_enabled = _MODULE.option_enabled
option_scan_interval_seconds = _MODULE.option_scan_interval_seconds


class OptionsConstTests(unittest.TestCase):
    def test_option_enabled_defaults_when_missing(self) -> None:
        self.assertEqual(
            option_enabled({}, OPT_ENABLE_ADVANCED_ENTITIES, DEFAULT_ENABLE_ADVANCED_ENTITIES),
            DEFAULT_ENABLE_ADVANCED_ENTITIES,
        )
        self.assertEqual(
            option_enabled({}, OPT_ENABLE_DIAGNOSTIC_ENTITIES, DEFAULT_ENABLE_DIAGNOSTIC_ENTITIES),
            DEFAULT_ENABLE_DIAGNOSTIC_ENTITIES,
        )

    def test_option_enabled_ignores_non_bool_values(self) -> None:
        self.assertFalse(option_enabled({OPT_ENABLE_ADVANCED_ENTITIES: "yes"}, OPT_ENABLE_ADVANCED_ENTITIES, False))
        self.assertTrue(option_enabled({OPT_ENABLE_DIAGNOSTIC_ENTITIES: 1}, OPT_ENABLE_DIAGNOSTIC_ENTITIES, True))

    def test_scan_interval_defaults_and_clamps(self) -> None:
        self.assertEqual(option_scan_interval_seconds({}), DEFAULT_SCAN_INTERVAL_SECONDS)
        self.assertEqual(option_scan_interval_seconds({OPT_SCAN_INTERVAL_SECONDS: "30"}), DEFAULT_SCAN_INTERVAL_SECONDS)
        self.assertEqual(option_scan_interval_seconds({OPT_SCAN_INTERVAL_SECONDS: MIN_SCAN_INTERVAL_SECONDS - 1}), MIN_SCAN_INTERVAL_SECONDS)
        self.assertEqual(option_scan_interval_seconds({OPT_SCAN_INTERVAL_SECONDS: MAX_SCAN_INTERVAL_SECONDS + 1}), MAX_SCAN_INTERVAL_SECONDS)
        self.assertEqual(option_scan_interval_seconds({OPT_SCAN_INTERVAL_SECONDS: 45}), 45)


if __name__ == "__main__":
    unittest.main()
