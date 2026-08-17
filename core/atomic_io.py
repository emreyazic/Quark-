"""Atomic persistence helpers that preserve an existing file on failure."""

import json
import os
import tempfile
from pathlib import Path


def atomic_save_workbook(workbook, output_path: str) -> None:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{destination.stem}.",
        suffix=destination.suffix or ".xlsx",
        dir=destination.parent,
    )
    os.close(fd)
    try:
        workbook.save(temp_path)
        os.replace(temp_path, destination)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(data, output_path) -> None:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{destination.stem}.",
        suffix=".json.tmp",
        dir=destination.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, destination)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise
