from __future__ import annotations

import sys
from pathlib import Path


def _detect_app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_ROOT = _detect_app_root()


def app_path(*parts: str) -> Path:
    return APP_ROOT.joinpath(*parts)
