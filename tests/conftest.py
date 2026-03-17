from __future__ import annotations

from pathlib import Path
import sys


# Make the vendored runtime package importable as top-level `pydevi` for tests.
VENDORED_RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "devi"
sys.path.insert(0, str(VENDORED_RUNTIME_ROOT))
