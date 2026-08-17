import json

import pytest

from core import atomic_io


class _FailingWorkbook:
    def save(self, path):
        with open(path, "wb") as stream:
            stream.write(b"partial")
        raise RuntimeError("save failed")


def test_atomic_workbook_save_preserves_existing_file_on_failure(tmp_path):
    destination = tmp_path / "report.xlsx"
    destination.write_bytes(b"original")

    with pytest.raises(RuntimeError, match="save failed"):
        atomic_io.atomic_save_workbook(_FailingWorkbook(), str(destination))

    assert destination.read_bytes() == b"original"
    assert list(tmp_path.glob(".report.*")) == []


def test_atomic_json_write_preserves_existing_file_when_replace_fails(tmp_path, monkeypatch):
    destination = tmp_path / "workspace.json"
    destination.write_text('{"old": true}', encoding="utf-8")

    def fail_replace(source, target):
        raise PermissionError("locked")

    monkeypatch.setattr(atomic_io.os, "replace", fail_replace)
    with pytest.raises(PermissionError, match="locked"):
        atomic_io.atomic_write_json({"new": True}, destination)

    assert json.loads(destination.read_text(encoding="utf-8")) == {"old": True}
    assert list(tmp_path.glob(".workspace.*")) == []
