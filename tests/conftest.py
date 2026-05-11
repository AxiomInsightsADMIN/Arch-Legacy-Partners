"""Pytest config — ensure the project root is importable so `from
orchestration.geocoder import ...` works from any test file regardless of how
pytest is invoked."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
