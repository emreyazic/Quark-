"""Manual offscreen benchmark for the approval dialog.

Run with: python tests/benchmark_approval_dialog.py 100 1000
"""

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLineEdit, QPushButton, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget

from core.database_manager import DatabaseManager
from ui.approval_dialog import ApprovalDialog


def seed(db: DatabaseManager, count: int) -> None:
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
        conn.executemany(
            """INSERT INTO mapping_audit_history
               (comment_code, mpn, supplier, previous_code, candidate_code, selected_code, action, created_at)
               VALUES (?, ?, 'JLCPCB', '', ?, ?, 'ACCEPT_CANDIDATE', ?)""",
            [(f"R{i}", f"MPN-{i}", f"C{i}", f"C{i}", now + i) for i in range(count)],
        )
        conn.commit()


def legacy_initial(db: DatabaseManager) -> tuple[float, int]:
    started = time.perf_counter()
    mappings = db.get_all_internal_mappings()
    pending = []
    for row in mappings:
        for supplier, current, candidate in (
            ("JLCPCB", row["lcsc_code"], row["last_found_lcsc"]),
            ("DigiKey", row["digikey_code"], row["last_found_digikey"]),
        ):
            pending.append((row["comment_code"], row["mpn"], supplier, current, candidate))
    approved = db.get_approved_supplier_mappings()
    history = db.get_mapping_audit_history()
    tables = [QTableWidget(len(pending), 6), QTableWidget(len(approved), 6), QTableWidget(len(history), 8)]
    for row, values in enumerate(pending):
        for column, value in enumerate(values[:4]):
            tables[0].setItem(row, column, QTableWidgetItem(str(value)))
        tables[0].setCellWidget(row, 4, QLineEdit(values[4]))
        actions = QWidget()
        box = QHBoxLayout(actions)
        box.addWidget(QPushButton("Approve"))
        box.addWidget(QPushButton("Keep"))
        tables[0].setCellWidget(row, 5, actions)
    for row, entry in enumerate(approved):
        tables[1].setCellWidget(row, 3, QLineEdit(entry["approved_code"]))
        actions = QWidget()
        box = QHBoxLayout(actions)
        box.addWidget(QPushButton("Save"))
        box.addWidget(QPushButton("Cancel"))
        tables[1].setCellWidget(row, 5, actions)
    for row, entry in enumerate(history):
        for column, key in enumerate(("created_at", "supplier", "comment_code", "mpn", "previous_code", "candidate_code", "selected_code", "action")):
            tables[2].setItem(row, column, QTableWidgetItem(str(entry.get(key, ""))))
    window = QDialog()
    layout = QVBoxLayout(window)
    tabs = QTabWidget()
    for label, table in zip(("Pending", "Approved", "History"), tables):
        tabs.addTab(table, label)
    layout.addWidget(tabs)
    window.show()
    QApplication.processEvents()
    widgets = sum(len(table.findChildren(QWidget)) for table in tables)
    elapsed = (time.perf_counter() - started) * 1000
    window.close()
    return elapsed, widgets


def optimized(db: DatabaseManager) -> dict[str, float | int]:
    started = time.perf_counter()
    dialog = ApprovalDialog(db)
    dialog.show()
    QApplication.processEvents()
    initial = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    dialog.tabs.setCurrentIndex(1)
    QApplication.processEvents()
    approved = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    dialog.tabs.setCurrentIndex(2)
    QApplication.processEvents()
    history = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    dialog.tabs.setCurrentIndex(0)
    dialog.search_pending.setText("MPN-999")
    QTest.qWait(dialog.SEARCH_DELAY_MS + 10)
    search = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    scrollbar = dialog.table_pending.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum())
    QApplication.processEvents()
    scroll = (time.perf_counter() - started) * 1000
    dialog.search_pending.clear()
    dialog._apply_pending_filter()
    jlc_row = next(
        row for row in range(dialog.pending_proxy.rowCount())
        if dialog.pending_proxy.index(row, 2).data() == "JLCPCB"
    )
    started = time.perf_counter()
    dialog._on_pending_action(dialog.pending_proxy.index(jlc_row, 5), "approve")
    QApplication.processEvents()
    row_update = (time.perf_counter() - started) * 1000
    widgets = len(dialog.table_pending.findChildren(QWidget))
    dialog.close()
    return {"initial_ms": initial, "approved_ms": approved, "history_ms": history,
            "search_ms": search, "scroll_ms": scroll, "row_update_ms": row_update, "widgets": widgets}


def main() -> None:
    app = QApplication.instance() or QApplication([])
    optimized_only = "--optimized-only" in sys.argv
    sizes = [int(value) for value in sys.argv[1:] if value.isdigit()] or [100, 1000]
    for size in sizes:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            db = DatabaseManager(str(Path(directory) / "benchmark.sqlite"))
            seed(db, size)
            baseline_ms, baseline_widgets = (0.0, 0) if optimized_only else legacy_initial(db)
            result = optimized(db)
            prefix = f"rows={size} "
            if not optimized_only:
                prefix += f"legacy_initial_ms={baseline_ms:.1f} legacy_widgets={baseline_widgets} "
            print(prefix
                  + f"optimized_initial_ms={result['initial_ms']:.1f} approved_ms={result['approved_ms']:.1f} "
                  f"history_ms={result['history_ms']:.1f} search_ms={result['search_ms']:.1f} "
                  f"scroll_ms={result['scroll_ms']:.1f} row_update_ms={result['row_update_ms']:.1f} "
                  f"optimized_widgets={result['widgets']}")


if __name__ == "__main__":
    main()
