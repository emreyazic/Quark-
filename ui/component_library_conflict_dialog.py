"""Dialog for resolving component library import conflicts (internal code vs multiple MPNs).

Ensures that when a single internal code has multiple conflicting MPNs in a library file
or differs from an existing database mapping, the user explicitly reviews and resolves the
conflict rather than relying on silent first/last-wins overwrites.
"""

from typing import List, Tuple, Dict, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QBrush
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.component_library import ConflictItem


class ComponentLibraryConflictDialog(QDialog):
    """Interactive resolution dialog for component library MPN conflicts."""

    def __init__(self, conflicts: List[ConflictItem], parent=None):
        super().__init__(parent)
        self.conflicts = conflicts
        self._combos: Dict[str, QComboBox] = {}
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle("Resolve Component Library Import Conflicts")
        self.setMinimumWidth(880)
        self.setMinimumHeight(550)

        layout = QVBoxLayout(self)
        layout.setSpacing(14)

        # Header Info Banner
        header = QLabel(
            f"<h3>⚠ Component Library Conflicts Detected</h3>"
            f"<span>Found <b>{len(self.conflicts)}</b> internal code(s) with conflicting Part Numbers. "
            f"Please choose which MPN to use for each code. "
            f"<b>Unresolved / skipped codes will not be imported.</b></span>"
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        # Conflict Table Group
        group = QGroupBox("Conflicting Internal Codes")
        group_layout = QVBoxLayout(group)

        self._table = QTableWidget()
        self._table.setColumnCount(4)
        self._table.setHorizontalHeaderLabels([
            "Internal Code", "Conflict Type", "Resolution Selection", "Conflicting Values"
        ])
        self._table.setRowCount(len(self.conflicts))
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.verticalHeader().setVisible(False)

        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self._table.setColumnWidth(0, 160)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self._table.setColumnWidth(1, 170)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self._table.setColumnWidth(2, 280)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

        for row_idx, conflict in enumerate(self.conflicts):
            # 0. Internal Code
            code_item = QTableWidgetItem(conflict.internal_code)
            font = code_item.font()
            font.setBold(True)
            code_item.setFont(font)
            self._table.setItem(row_idx, 0, code_item)

            # 1. Conflict Type
            if conflict.conflict_type == "FILE_CONFLICT":
                type_str = f"File Conflict ({len(conflict.candidate_mpns)} MPNs)"
                type_color = QColor("#f39c12")
            else:
                type_str = "Differs from DB Mapping"
                type_color = QColor("#e67e22")

            type_item = QTableWidgetItem(type_str)
            type_item.setForeground(QBrush(type_color))
            self._table.setItem(row_idx, 1, type_item)

            # 2. Resolution ComboBox
            combo = QComboBox()
            # Default option: Skip (Safe by default)
            combo.addItem("⚠ [Skip — Do not import]", "")

            # Candidate MPNs from file
            for mpn in conflict.candidate_mpns:
                rows_str = ", ".join(map(str, conflict.row_numbers.get(mpn, [])))
                row_label = f" (Row {rows_str})" if rows_str else ""
                combo.addItem(f"Use File MPN: {mpn}{row_label}", mpn)

            # Option to keep existing DB MPN if present
            if conflict.existing_db_mpn:
                combo.addItem(f"Keep Existing DB: {conflict.existing_db_mpn}", conflict.existing_db_mpn)

            self._combos[conflict.internal_code] = combo
            self._table.setCellWidget(row_idx, 2, combo)

            # 3. Conflicting Values Detail
            details = []
            for mpn in conflict.candidate_mpns:
                rows_str = ", ".join(map(str, conflict.row_numbers.get(mpn, [])))
                details.append(f"File: '{mpn}' (Row {rows_str})")
            if conflict.existing_db_mpn:
                details.append(f"DB: '{conflict.existing_db_mpn}'")

            detail_item = QTableWidgetItem(" | ".join(details))
            self._table.setItem(row_idx, 3, detail_item)

        group_layout.addWidget(self._table)

        # Quick action buttons
        btn_row = QHBoxLayout()
        btn_skip_all = QPushButton("Skip All Conflicting")
        btn_skip_all.clicked.connect(self._skip_all)
        btn_row.addWidget(btn_skip_all)

        btn_use_first = QPushButton("Use First File MPN for All")
        btn_use_first.clicked.connect(self._use_first_for_all)
        btn_row.addWidget(btn_use_first)

        btn_row.addStretch()
        group_layout.addLayout(btn_row)
        layout.addWidget(group)

        # Dialog Buttons
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        self._btn_ok = btn_box.button(QDialogButtonBox.StandardButton.Ok)
        self._btn_ok.setText("Apply Resolutions & Continue")
        layout.addWidget(btn_box)

    def _skip_all(self):
        for combo in self._combos.values():
            combo.setCurrentIndex(0)

    def _use_first_for_all(self):
        for combo in self._combos.values():
            if combo.count() > 1:
                combo.setCurrentIndex(1)

    def get_resolved_mappings(self) -> List[Tuple[str, str]]:
        """Return the resolved (internal_code, mpn) list for chosen conflicts.

        Entries left on [Skip] are excluded.
        """
        resolved: List[Tuple[str, str]] = []
        for conflict in self.conflicts:
            combo = self._combos.get(conflict.internal_code)
            if combo is None:
                continue
            chosen_mpn = combo.currentData()
            if chosen_mpn and str(chosen_mpn).strip():
                resolved.append((conflict.internal_code, str(chosen_mpn).strip()))
        return resolved
