import sys
import os
from pathlib import Path


def get_resource_path(relative_path: str) -> str:
    """Get absolute path to resource, works for dev, installed packages, and PyInstaller."""
    # 1. PyInstaller creates a temp folder and stores path in _MEIPASS
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(getattr(sys, "_MEIPASS"), relative_path)

    # 2. If not bundled, resolve relative to project / package root
    base_path = Path(__file__).resolve().parent.parent
    direct = base_path / relative_path
    if direct.exists():
        return str(direct)

    return str(base_path / relative_path)
