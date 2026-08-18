import time

import pytest
from PyQt6.QtCore import QModelIndex
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLineEdit, QMessageBox, QPushButton, QWidget

from core.database_manager import DatabaseManager
from ui.approval_dialog import ApprovalDialog


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_modal_message_boxes(monkeypatch):
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "critical", lambda *args, **kwargs: QMessageBox.StandardButton.Ok)


def _insert_rows(db: DatabaseManager, count: int, *, audit: bool = False) -> None:
    now = time.time()
    with db._get_connection() as conn:
        conn.executemany(
            """INSERT INTO internal_mappings
               (comment_code, mpn, lcsc_code, approved, updated_at, digikey_code,
                last_found_lcsc, last_found_digikey, lcsc_approved, digikey_approved,
                lcsc_pending_change, digikey_pending_change, lcsc_approved_at, digikey_approved_at)
               VALUES (?, ?, ?, 1, ?, ?, ?, ?, 1, 1, 1, 1, ?, ?)""",
            [(f"R{i}", f"MPN-{i}", f"C{i}", now, f"DK-{i}", f"CNEW-{i}", f"DKNEW-{i}", now, now) for i in range(count)],
        )
        if audit:
            conn.executemany(
                """INSERT INTO mapping_audit_history
                   (comment_code, mpn, supplier, previous_code, candidate_code, selected_code, action, created_at)
                   VALUES (?, ?, 'JLCPCB', '', ?, ?, 'ACCEPT_CANDIDATE', ?)""",
                [(f"R{i}", f"MPN-{i}", f"C{i}", f"C{i}", now + i) for i in range(count)],
            )
        conn.commit()


def test_only_active_tab_queries_on_open_and_tabs_lazy_load(tmp_path, qapp, monkeypatch):
    db = DatabaseManager(str(tmp_path / "lazy.sqlite"))
    calls = {"pending": 0, "approved": 0, "history": 0}
    originals = (
        db.get_pending_supplier_mappings,
        db.get_approved_supplier_mappings,
        db.get_mapping_audit_history,
    )
    monkeypatch.setattr(db, "get_pending_supplier_mappings", lambda *a, **k: (calls.__setitem__("pending", calls["pending"] + 1) or originals[0](*a, **k)))
    monkeypatch.setattr(db, "get_approved_supplier_mappings", lambda *a, **k: (calls.__setitem__("approved", calls["approved"] + 1) or originals[1](*a, **k)))
    monkeypatch.setattr(db, "get_mapping_audit_history", lambda *a, **k: (calls.__setitem__("history", calls["history"] + 1) or originals[2](*a, **k)))

    dialog = ApprovalDialog(db)
    assert calls == {"pending": 1, "approved": 0, "history": 0}
    dialog.tabs.setCurrentIndex(1)
    assert calls == {"pending": 1, "approved": 1, "history": 0}
    dialog.tabs.setCurrentIndex(2)
    assert calls["history"] == 1


def test_approve_removes_only_row_without_model_reset(tmp_path, qapp):
    db = DatabaseManager(str(tmp_path / "row-update.sqlite"))
    db.insert_pending_suggestion("R1", "MPN", "C1", "")
    dialog = ApprovalDialog(db)
    resets = []
    dialog.pending_model.modelReset.connect(lambda: resets.append(True))
    before = dialog.pending_model.rowCount()
    jlc_row = next(
        row for row in range(dialog.pending_proxy.rowCount())
        if dialog.pending_proxy.index(row, 2).data() == "JLCPCB"
    )
    dialog._on_pending_action(dialog.pending_proxy.index(jlc_row, 5), "approve")
    assert dialog.pending_model.rowCount() == before - 1
    assert resets == []


def test_history_fetches_incrementally(tmp_path, qapp):
    db = DatabaseManager(str(tmp_path / "history-pages.sqlite"))
    _insert_rows(db, 450, audit=True)
    dialog = ApprovalDialog(db)
    dialog.tabs.setCurrentIndex(2)
    assert dialog.history_model.rowCount() == 200
    assert dialog.history_model.canFetchMore(QModelIndex())
    dialog.history_model.fetchMore(QModelIndex())
    assert dialog.history_model.rowCount() == 400
    dialog.history_model.fetchMore(QModelIndex())
    assert dialog.history_model.rowCount() == 450
    assert not dialog.history_model.canFetchMore(QModelIndex())


def test_search_is_debounced_to_one_final_filter_update(tmp_path, qapp):
    dialog = ApprovalDialog(DatabaseManager(str(tmp_path / "debounce.sqlite")))
    baseline = dialog._pending_filter_runs
    dialog.search_pending.setText("R")
    dialog.search_pending.setText("R1")
    dialog.search_pending.setText("R10")
    QTest.qWait(dialog.SEARCH_DELAY_MS + 50)
    assert dialog._pending_filter_runs == baseline + 1


def test_large_table_has_no_per_row_persistent_widgets(tmp_path, qapp):
    db = DatabaseManager(str(tmp_path / "widgets.sqlite"))
    _insert_rows(db, 600)
    dialog = ApprovalDialog(db)
    assert dialog.pending_model.rowCount() == 1200
    assert dialog.table_pending.findChildren(QLineEdit) == []
    assert dialog.table_pending.findChildren(QPushButton) == []
    assert len(dialog.table_pending.findChildren(QWidget)) < 20


def test_filter_uses_current_model_data_without_stale_cache(tmp_path, qapp):
    db = DatabaseManager(str(tmp_path / "filter.sqlite"))
    db.insert_pending_suggestion("R1", "FIRST", "C1", "")
    db.insert_pending_suggestion("R2", "SECOND", "C2", "")
    dialog = ApprovalDialog(db)
    dialog.search_pending.setText("SECOND")
    QTest.qWait(dialog.SEARCH_DELAY_MS + 50)
    assert dialog.pending_proxy.rowCount() == 2
    assert {dialog.pending_proxy.index(row, 1).data() for row in range(2)} == {"SECOND"}


def test_close_cancels_pending_search_callback(tmp_path, qapp):
    dialog = ApprovalDialog(DatabaseManager(str(tmp_path / "close.sqlite")))
    baseline = dialog._pending_filter_runs
    dialog.search_pending.setText("late")
    dialog.close()
    QTest.qWait(dialog.SEARCH_DELAY_MS + 50)
    assert dialog._pending_filter_runs == baseline


def test_sqlite_plans_use_targeted_pending_and_history_indexes(tmp_path):
    db = DatabaseManager(str(tmp_path / "plans.sqlite"))
    with db._get_connection() as conn:
        pending_plan = " ".join(
            row[3] for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT comment_code FROM internal_mappings WHERE lcsc_pending_change = 1"
            )
        )
        history_plan = " ".join(
            row[3] for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM mapping_audit_history ORDER BY created_at DESC, id DESC LIMIT 200"
            )
        )
    assert "idx_mapping_lcsc_pending" in pending_plan
    assert "idx_audit_created" in history_plan
