"""Sheet selector dialog for multi-sheet BOM workbooks.

Allows users to preview sheet names, row counts, and detected column mappings,
highlights probable duplicate sheets, and allows per-sheet column mapping customization.
"""

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QBrush
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
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

from models.bom_item import BomFile
from ui.column_mapper_widget import ColumnMapperDialog


class SheetSelectorDialog(QDialog):
    """Dialog for inspecting and selecting worksheets from a multi-sheet BOM workbook."""

    def __init__(self, sheets: list[BomFile], parent=None):
        super().__init__(parent)
        self.sheets = sheets
        self._checkboxes: list[QCheckBox] = []
        self._setup_ui()

    def _setup_ui(self):
        file_name = self.sheets[0].file_path if self.sheets else "Workbook"
        self.setWindowTitle("Select BOM Worksheets to Process")
        self.setMinimumWidth(850)
        self.setMinimumHeight(600)

        layout = QVBoxLayout(self)
        layout.setSpacing(14)

        # Header Info
        header_label = QLabel(
            f"<b>Workbook:</b> {file_name}<br>"
            f"<span>This workbook contains <b>{len(self.sheets)}</b> visible sheet(s). "
            f"Select which sheets to import into your project.</span>"
        )
        header_label.setWordWrap(True)
        layout.addWidget(header_label)

        # Duplicate Warning Banner if any duplicates exist
        duplicate_sheets = [s for s in self.sheets if s.duplicate_of]
        if duplicate_sheets:
            dup_names = ", ".join(f"'{s.sheet_name}' (copy of '{s.duplicate_of}')" for s in duplicate_sheets)
            warn_banner = QLabel(
                f"⚠ <b>Probable duplicate sheet(s) detected:</b> {dup_names}.<br>"
                "These have been unchecked by default to prevent accidental double-counting. "
                "Check them only if you intentionally want to include both."
            )
            warn_banner.setWordWrap(True)
            warn_banner.setStyleSheet(
                "background-color: #2d2200; border: 1px solid #f39c12; "
                "border-radius: 6px; padding: 10px; color: #f39c12;"
            )
            layout.addWidget(warn_banner)

        # Sheet Selection Table
        sheets_group = QGroupBox("Available Worksheets")
        sheets_layout = QVBoxLayout(sheets_group)

        self._sheets_table = QTableWidget()
        self._sheets_table.setColumnCount(6)
        self._sheets_table.setHorizontalHeaderLabels([
            "Select", "Sheet Name", "Data Rows", "Detected Columns", "Status / Notes", "Mapping"
        ])
        self._sheets_table.setRowCount(len(self.sheets))
        self._sheets_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._sheets_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._sheets_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._sheets_table.verticalHeader().setVisible(False)

        header = self._sheets_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self._sheets_table.setColumnWidth(0, 55)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self._sheets_table.setColumnWidth(1, 140)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self._sheets_table.setColumnWidth(2, 90)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        self._sheets_table.setColumnWidth(4, 210)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self._sheets_table.setColumnWidth(5, 110)

        for row_idx, sheet in enumerate(self.sheets):
            # 0. Checkbox
            cb = QCheckBox()
            # Checked by default if valid and NOT a duplicate
            is_checked = sheet.is_valid and not sheet.duplicate_of
            cb.setChecked(is_checked)
            cb.stateChanged.connect(self._on_selection_changed)
            self._checkboxes.append(cb)

            cb_widget = QWidget()
            cb_layout = QHBoxLayout(cb_widget)
            cb_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cb_layout.setContentsMargins(0, 0, 0, 0)
            cb_layout.addWidget(cb)
            self._sheets_table.setCellWidget(row_idx, 0, cb_widget)

            # 1. Sheet Name
            name_item = QTableWidgetItem(sheet.sheet_name)
            font = name_item.font()
            font.setBold(True)
            name_item.setFont(font)
            self._sheets_table.setItem(row_idx, 1, name_item)

            # 2. Row Count
            rows_item = QTableWidgetItem(f"{sheet.row_count} rows")
            rows_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._sheets_table.setItem(row_idx, 2, rows_item)

            # 3. Detected Columns
            cols_item = QTableWidgetItem(self._format_mapping_summary(sheet))
            self._sheets_table.setItem(row_idx, 3, cols_item)

            # 4. Status / Warnings
            status_text = self._format_status_text(sheet)
            status_item = QTableWidgetItem(status_text)
            if sheet.duplicate_of:
                status_item.setForeground(QBrush(QColor("#f39c12")))
            elif not sheet.is_valid:
                status_item.setForeground(QBrush(QColor("#e74c3c")))
            else:
                status_item.setForeground(QBrush(QColor("#00b894")))
            self._sheets_table.setItem(row_idx, 4, status_item)

            # 5. Mapping Button
            btn_map = QPushButton("Edit Mapping")
            btn_map.setStyleSheet("font-size: 11px; padding: 2px 6px;")
            btn_map.clicked.connect(lambda _, idx=row_idx: self._open_mapper_for_sheet(idx))
            self._sheets_table.setCellWidget(row_idx, 5, btn_map)

        self._sheets_table.itemSelectionChanged.connect(self._on_table_row_selected)
        sheets_layout.addWidget(self._sheets_table)

        # Quick select buttons
        btn_row = QHBoxLayout()
        btn_select_all = QPushButton("Select All")
        btn_select_all.clicked.connect(self._select_all)
        btn_row.addWidget(btn_select_all)

        btn_select_non_dups = QPushButton("Select Non-Duplicates Only")
        btn_select_non_dups.clicked.connect(self._select_non_duplicates)
        btn_row.addWidget(btn_select_non_dups)

        btn_deselect_all = QPushButton("Deselect All")
        btn_deselect_all.clicked.connect(self._deselect_all)
        btn_row.addWidget(btn_deselect_all)

        btn_row.addStretch()
        sheets_layout.addLayout(btn_row)
        layout.addWidget(sheets_group)

        # Preview of selected sheet
        preview_group = QGroupBox("Sheet Data Preview (First 5 Rows)")
        preview_layout = QVBoxLayout(preview_group)
        self._preview_table = QTableWidget()
        self._preview_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._preview_table.setAlternatingRowColors(True)
        self._preview_table.verticalHeader().setVisible(False)
        self._preview_table.setMaximumHeight(140)
        preview_layout.addWidget(self._preview_table)
        layout.addWidget(preview_group)

        # Bottom Buttons
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        self._btn_ok = btn_box.button(QDialogButtonBox.StandardButton.Ok)
        self._btn_ok.setText("Import Selected Sheets")
        layout.addWidget(btn_box)

        # Select first row by default to show preview
        if self.sheets:
            self._sheets_table.selectRow(0)
            self._update_preview(self.sheets[0])

        self._on_selection_changed()

    def _format_mapping_summary(self, sheet: BomFile) -> str:
        if not sheet.column_mapping:
            return "(No mapping)"
        m = sheet.column_mapping
        parts = []
        if m.mpn is not None and m.mpn >= 0:
            hdr = sheet.headers[m.mpn] if m.mpn < len(sheet.headers) else f"Col {m.mpn+1}"
            parts.append(f"MPN: {hdr}")
        if m.quantity is not None and m.quantity >= 0:
            hdr = sheet.headers[m.quantity] if m.quantity < len(sheet.headers) else f"Col {m.quantity+1}"
            parts.append(f"Qty: {hdr}")
        if m.designator is not None and m.designator >= 0:
            hdr = sheet.headers[m.designator] if m.designator < len(sheet.headers) else f"Col {m.designator+1}"
            parts.append(f"Des: {hdr}")
        if m.description is not None and m.description >= 0:
            hdr = sheet.headers[m.description] if m.description < len(sheet.headers) else f"Col {m.description+1}"
            parts.append(f"Desc: {hdr}")
        return " | ".join(parts) if parts else "(No columns mapped)"

    def _format_status_text(self, sheet: BomFile) -> str:
        if sheet.duplicate_of:
            return f"⚠ Duplicate of '{sheet.duplicate_of}'"
        if not sheet.is_valid:
            return "⚠ Incomplete (MPN/Qty missing)"
        conf = int(sheet.column_mapping.confidence * 100) if sheet.column_mapping else 0
        return f"✓ Valid mapping ({conf}%)"

    def _on_table_row_selected(self):
        selected_rows = self._sheets_table.selectionModel().selectedRows()
        if selected_rows:
            row_idx = selected_rows[0].row()
            if 0 <= row_idx < len(self.sheets):
                self._update_preview(self.sheets[row_idx])

    def _update_preview(self, sheet: BomFile):
        self._preview_table.clear()
        self._preview_table.setColumnCount(len(sheet.headers))
        self._preview_table.setHorizontalHeaderLabels(sheet.headers)
        self._preview_table.setRowCount(len(sheet.preview_rows))

        for r_idx, row in enumerate(sheet.preview_rows):
            for c_idx, val in enumerate(row):
                self._preview_table.setItem(r_idx, c_idx, QTableWidgetItem(str(val)))

        self._preview_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )

    def _open_mapper_for_sheet(self, sheet_idx: int):
        sheet = self.sheets[sheet_idx]
        dlg = ColumnMapperDialog(sheet, self)
        if dlg.exec() == ColumnMapperDialog.DialogCode.Accepted:
            # Refresh row summary and validity
            sheet.is_valid = sheet.column_mapping.is_valid() if sheet.column_mapping else False
            self._sheets_table.setItem(
                sheet_idx, 3, QTableWidgetItem(self._format_mapping_summary(sheet))
            )
            status_item = QTableWidgetItem(self._format_status_text(sheet))
            if sheet.duplicate_of:
                status_item.setForeground(QBrush(QColor("#f39c12")))
            elif not sheet.is_valid:
                status_item.setForeground(QBrush(QColor("#e74c3c")))
            else:
                status_item.setForeground(QBrush(QColor("#00b894")))
            self._sheets_table.setItem(sheet_idx, 4, status_item)
            self._update_preview(sheet)
            self._on_selection_changed()

    def _select_all(self):
        for cb in self._checkboxes:
            cb.setChecked(True)

    def _select_non_duplicates(self):
        for idx, cb in enumerate(self._checkboxes):
            cb.setChecked(self.sheets[idx].is_valid and not self.sheets[idx].duplicate_of)

    def _deselect_all(self):
        for cb in self._checkboxes:
            cb.setChecked(False)

    def _on_selection_changed(self):
        selected_count = sum(1 for cb in self._checkboxes if cb.isChecked())
        self._btn_ok.setEnabled(selected_count > 0)
        self._btn_ok.setText(
            f"Import Selected ({selected_count} Sheet{'s' if selected_count != 1 else ''})"
            if selected_count > 0
            else "Select at least 1 Sheet"
        )

    def get_selected_sheets(self) -> list[BomFile]:
        """Return the list of BomFile objects selected by the user."""
        return [
            sheet
            for idx, sheet in enumerate(self.sheets)
            if self._checkboxes[idx].isChecked()
        ]
