# pyrefly: ignore-file
"""Column mapper dialog — allows users to verify/override auto-detected column mappings.

Improvements vs. original:
- Shows warnings when auto-detection detects a possible manufacturer/MPN swap.
- Improved field labels with clearer descriptions.
- Highlights mapped columns with field-specific colors.
"""

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QAbstractItemView,
)

from models.bom_item import BomFile, ColumnMapping


# Standard field names and their descriptions
FIELDS = [
    ("mpn", "MPN (Manufacturer Part Number)", True),
    ("quantity", "Quantity", True),
    ("manufacturer", "Manufacturer", False),
    ("description", "Description", False),
    ("designator", "Designator", False),
    ("comment", "Comment / Internal Code", False),
    ("footprint", "Footprint", False),
    ("value", "Value", False),
    ("board_identifier", "Board Identifier (Kart)", False),
]


class ColumnMapperDialog(QDialog):
    """Dialog for mapping BOM file columns to standard fields."""

    def __init__(self, bom_file: BomFile, parent=None):
        super().__init__(parent)
        self.bom_file = bom_file
        self._combos: dict[str, QComboBox] = {}
        self._setup_ui()
        self._load_auto_detection()

    def _setup_ui(self):
        self.setWindowTitle(f"Column Mapping — {self.bom_file.board_name}")
        self.setMinimumWidth(750)
        self.setMinimumHeight(600)

        layout = QVBoxLayout(self)
        layout.setSpacing(16)

        # Info
        info_label = QLabel(
            f"<b>File:</b> {self.bom_file.file_path}<br>"
            f"<b>Sheet:</b> {self.bom_file.sheet_name} · "
            f"<b>Rows:</b> {self.bom_file.row_count}"
        )
        info_label.setWordWrap(True)
        layout.addWidget(info_label)

        confidence = 0
        if self.bom_file.column_mapping:
            confidence = self.bom_file.column_mapping.confidence

        confidence_pct = int(confidence * 100)
        if confidence_pct >= 70:
            conf_color = "#00b894"
            conf_text = "High"
        elif confidence_pct >= 40:
            conf_color = "#f39c12"
            conf_text = "Medium"
        else:
            conf_color = "#e74c3c"
            conf_text = "Low"

        conf_label = QLabel(
            f'Auto-detection confidence: '
            f'<span style="color: {conf_color}; font-weight: 700;">'
            f'{conf_text} ({confidence_pct}%)</span>'
        )
        layout.addWidget(conf_label)

        # Show warnings from auto-detection
        if self.bom_file.column_mapping and self.bom_file.column_mapping.warnings:
            for warning in self.bom_file.column_mapping.warnings:
                warn_label = QLabel(
                    f'<span style="color: #f39c12; font-weight: 600;">{warning}</span>'
                )
                warn_label.setWordWrap(True)
                warn_label.setStyleSheet(
                    "background-color: #2d2200; border: 1px solid #f39c12; "
                    "border-radius: 6px; padding: 8px; margin: 4px 0;"
                )
                layout.addWidget(warn_label)

        # Mapping grid
        mapping_group = QGroupBox("Column Mapping")
        mapping_layout = QGridLayout(mapping_group)
        mapping_layout.setSpacing(10)

        mapping_layout.addWidget(
            QLabel("<b>Field</b>"), 0, 0
        )
        mapping_layout.addWidget(
            QLabel("<b>Mapped Column</b>"), 0, 1
        )
        mapping_layout.addWidget(
            QLabel("<b>Required</b>"), 0, 2
        )

        not_mapped = "(Not mapped)"
        for row_idx, (field_name, field_label, required) in enumerate(FIELDS, start=1):
            label = QLabel(field_label)
            if required:
                label.setStyleSheet("font-weight: 600; color: #f39c12;")
            mapping_layout.addWidget(label, row_idx, 0)

            combo = QComboBox()
            combo.addItem(not_mapped, -1)
            for col_idx, header in enumerate(self.bom_file.headers):
                if header:
                    combo.addItem(f"Col {col_idx+1}: {header}", col_idx)
            combo.currentIndexChanged.connect(self._on_mapping_changed)
            self._combos[field_name] = combo
            mapping_layout.addWidget(combo, row_idx, 1)

            req_label = QLabel("✱ Required" if required else "Optional")
            req_label.setStyleSheet(
                f"color: {'#f39c12' if required else '#4a5f75'}; font-size: 11px;"
            )
            mapping_layout.addWidget(req_label, row_idx, 2)

        layout.addWidget(mapping_group)

        # Preview table
        preview_group = QGroupBox("Data Preview (first 5 rows)")
        preview_layout = QVBoxLayout(preview_group)

        self._preview_table = QTableWidget()
        self._preview_table.setColumnCount(len(self.bom_file.headers))
        self._preview_table.setHorizontalHeaderLabels(self.bom_file.headers)
        self._preview_table.setRowCount(len(self.bom_file.preview_rows))
        self._preview_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._preview_table.setAlternatingRowColors(True)
        self._preview_table.verticalHeader().setVisible(False)

        for row_idx, row_data in enumerate(self.bom_file.preview_rows):
            for col_idx, val in enumerate(row_data):
                item = QTableWidgetItem(str(val))
                self._preview_table.setItem(row_idx, col_idx, item)

        self._preview_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        preview_layout.addWidget(self._preview_table)
        layout.addWidget(preview_group)

        # Error/Validation label
        self._error_label = QLabel()
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet(
            "background-color: #2d1111; border: 1px solid #e74c3c; "
            "border-radius: 6px; padding: 8px; margin: 4px 0;"
        )
        self._error_label.setVisible(False)
        layout.addWidget(self._error_label)

        # Buttons
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self._on_accept)
        btn_box.rejected.connect(self.reject)
        self._btn_ok = btn_box.button(QDialogButtonBox.StandardButton.Ok)
        self._btn_ok.setText("Confirm Mapping")
        layout.addWidget(btn_box)

        self._validate()

    def _load_auto_detection(self):
        """Load auto-detected mapping into combo boxes."""
        if self.bom_file.column_mapping is None:
            return

        mapping = self.bom_file.column_mapping
        for field_name, _, _ in FIELDS:
            col_idx = getattr(mapping, field_name, None)
            if col_idx is not None:
                combo = self._combos[field_name]
                for i in range(combo.count()):
                    if combo.itemData(i) == col_idx:
                        combo.setCurrentIndex(i)
                        break

    def _on_mapping_changed(self):
        self._validate()
        self._highlight_mapped_columns()

    def _validate(self) -> bool:
        """Check if required fields are mapped and no duplicate mappings exist."""
        valid = True
        col_usage: dict[int, list[str]] = {}

        # 1. Collect column usage across all fields
        for field_name, _, required in FIELDS:
            combo = self._combos[field_name]
            col_idx = combo.currentData()

            if required and (col_idx is None or col_idx < 0):
                valid = False

            if col_idx is not None and col_idx >= 0:
                col_usage.setdefault(col_idx, []).append(field_name)

        # 2. Check for duplicate column mappings
        has_duplicates = any(len(fields) > 1 for fields in col_usage.values())
        if has_duplicates:
            valid = False

        # 3. Apply styles to all combo boxes based on duplicate status
        for field_name, _, _ in FIELDS:
            combo = self._combos[field_name]
            col_idx = combo.currentData()
            if col_idx is not None and col_idx >= 0 and len(col_usage.get(col_idx, [])) > 1:
                combo.setStyleSheet("border: 2px solid #e74c3c;")
            else:
                combo.setStyleSheet("")

        # 4. Show/update validation error label
        if has_duplicates:
            dup_messages = []
            for col_idx, field_names in col_usage.items():
                if len(field_names) > 1:
                    hdr_name = (
                        self.bom_file.headers[col_idx]
                        if col_idx < len(self.bom_file.headers)
                        else f"Col {col_idx+1}"
                    )
                    fields_str = ", ".join(field_names)
                    dup_messages.append(
                        f"Column '{hdr_name}' (Col {col_idx+1}) is mapped to multiple fields: {fields_str}."
                    )
            self._error_label.setText(
                "<span style='color: #e74c3c; font-weight: 600;'>"
                + "<br>".join(dup_messages)
                + "<br>Each spreadsheet column must be mapped to at most one field.</span>"
            )
            self._error_label.setVisible(True)
        elif not valid:
            self._error_label.setText(
                "<span style='color: #e74c3c; font-weight: 600;'>"
                "Please map all required fields (MPN and Quantity)."
                "</span>"
            )
            self._error_label.setVisible(True)
        else:
            self._error_label.setVisible(False)

        self._btn_ok.setEnabled(valid)
        return valid

    def _highlight_mapped_columns(self):
        """Highlight mapped columns in the preview table with field-specific colors."""
        # Field-specific highlight colors
        field_colors = {
            "mpn": QColor("#1a4a3a"),
            "quantity": QColor("#1a3a4a"),
            "manufacturer": QColor("#3a1a4a"),
            "description": QColor("#2a2a3a"),
            "designator": QColor("#3a2a1a"),
            "comment": QColor("#2a3a2a"),
            "footprint": QColor("#3a3a1a"),
            "value": QColor("#1a3a3a"),
        }

        # Reset all column backgrounds
        for col in range(self._preview_table.columnCount()):
            for row in range(self._preview_table.rowCount()):
                item = self._preview_table.item(row, col)
                if item:
                    item.setBackground(Qt.GlobalColor.transparent)

        # Highlight mapped columns
        for field_name, _, _ in FIELDS:
            combo = self._combos[field_name]
            col_idx = combo.currentData()
            if col_idx is not None and col_idx >= 0:
                color = field_colors.get(field_name, QColor("#1a3d42"))
                for row in range(self._preview_table.rowCount()):
                    item = self._preview_table.item(row, col_idx)
                    if item:
                        item.setBackground(color)

    def _on_accept(self):
        """Build the ColumnMapping from user selections and accept."""
        if not self._validate():
            return
        mapping = ColumnMapping()
        for field_name, _, _ in FIELDS:
            combo = self._combos[field_name]
            col_idx = combo.currentData()
            if col_idx is not None and col_idx >= 0:
                setattr(mapping, field_name, col_idx)

        if not mapping.is_valid():
            return

        mapping.confidence = 1.0  # User confirmed
        self.bom_file.column_mapping = mapping
        self.accept()

    def get_mapping(self) -> Optional[ColumnMapping]:
        """Return the confirmed column mapping."""
        return self.bom_file.column_mapping
