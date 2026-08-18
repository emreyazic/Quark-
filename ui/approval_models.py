from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Optional

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QMouseEvent, QPainter
from PyQt6.QtWidgets import QStyle, QStyleOptionButton, QStyledItemDelegate

from core.database_manager import DatabaseManager


def _display_timestamp(value: Any) -> str:
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S") if value else "—"


class MappingTableModel(QAbstractTableModel):
    def __init__(self, headers: list[str], keys: list[str], parent=None):
        super().__init__(parent)
        self.headers = headers
        self.keys = keys
        self.rows: list[dict[str, Any]] = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.headers[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        key = self.keys[index.column()]
        if role == Qt.ItemDataRole.UserRole:
            return row
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if key == "actions":
                return ""
            if key == "approved_at":
                return _display_timestamp(row.get(key))
            return row.get(key, "") or ("—" if key in {"current_code", "previous_code", "candidate_code", "selected_code"} else "")
        if role == Qt.ItemDataRole.ToolTipRole:
            value = row.get(key, "")
            if key == "current_code" and not value:
                return "No previously approved code"
            return str(value or "")
        return None

    def replace(self, rows: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def remove_source_row(self, row: int) -> None:
        if not 0 <= row < len(self.rows):
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        del self.rows[row]
        self.endRemoveRows()


class PendingModel(MappingTableModel):
    def __init__(self, parent=None):
        super().__init__(
            ["Internal Code", "MPN", "Supplier", "Current", "New Code", "Actions"],
            ["comment_code", "mpn", "supplier", "current_code", "candidate_code", "actions"],
            parent,
        )

    def flags(self, index):
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.isValid() and index.column() == 4:
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def replace(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            row["_candidate_code"] = row.get("candidate_code", "")
        super().replace(rows)

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole) -> bool:
        if role != Qt.ItemDataRole.EditRole or not index.isValid() or index.column() != 4:
            return False
        self.rows[index.row()]["candidate_code"] = str(value)
        self.dataChanged.emit(index, index, [role, Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole])
        return True


class ApprovedModel(MappingTableModel):
    def __init__(self, parent=None):
        super().__init__(
            ["Internal Code", "MPN", "Supplier", "Approved Code", "Approved At", "Actions"],
            ["comment_code", "mpn", "supplier", "approved_code", "approved_at", "actions"],
            parent,
        )

    def replace(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            row["_original_code"] = row.get("approved_code", "")
        super().replace(rows)

    def flags(self, index):
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.isValid() and index.column() == 3:
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole) -> bool:
        if role != Qt.ItemDataRole.EditRole or not index.isValid() or index.column() != 3:
            return False
        self.rows[index.row()]["approved_code"] = str(value)
        self.dataChanged.emit(index, index, [role, Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole])
        return True

    def cancel_edit(self, row: int) -> None:
        entry = self.rows[row]
        entry["approved_code"] = entry.get("_original_code", "")
        index = self.index(row, 3)
        self.dataChanged.emit(index, index)

    def accept_edit(self, row: int) -> None:
        self.rows[row]["_original_code"] = self.rows[row].get("approved_code", "")


class MappingFilterProxy(QSortFilterProxyModel):
    def __init__(self, columns: tuple[int, ...], parent=None):
        super().__init__(parent)
        self._columns = columns
        self._query = ""
        self._supplier = "All Suppliers"
        self.setDynamicSortFilter(True)

    def set_query(self, query: str) -> None:
        self._query = query.strip().casefold()
        self.invalidateFilter()

    def set_supplier(self, supplier: str) -> None:
        self._supplier = supplier
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        if model is None:
            return False
        if self._supplier != "All Suppliers":
            supplier = str(model.index(source_row, 2, source_parent).data() or "")
            if supplier.casefold() != self._supplier.casefold():
                return False
        if not self._query:
            return True
        return any(
            self._query in str(model.index(source_row, col, source_parent).data() or "").casefold()
            for col in self._columns
        )


class HistoryModel(MappingTableModel):
    PAGE_SIZE = 200
    ACTION_LABELS = {
        "ACCEPT_CANDIDATE": "Approved",
        "KEEP_CURRENT": "Kept",
        "MANUAL_EDIT": "Edited",
        "AUTO_INVALIDATED_PREORDER": "Pre-order invalidated",
    }

    def __init__(self, db: DatabaseManager, parent=None):
        super().__init__(
            ["Date", "Supplier", "Internal Code", "MPN", "Previous", "Candidate", "Selected", "Action"],
            ["created_at", "supplier", "comment_code", "mpn", "previous_code", "candidate_code", "selected_code", "action"],
            parent,
        )
        self.db = db
        self.search = ""
        self.total_count = 0

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if index.isValid() and index.column() == 7:
            action = str(self.rows[index.row()].get("action", "")).strip().upper()
            if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
                return self.ACTION_LABELS.get(action, action)
            if role == Qt.ItemDataRole.ForegroundRole:
                return {"ACCEPT_CANDIDATE": QColor("green"), "MANUAL_EDIT": QColor("cyan"), "KEEP_CURRENT": QColor("yellow")}.get(action)
        return super().data(index, role)

    def reload(self, search: str = "") -> None:
        self.beginResetModel()
        self.search = search.strip()
        self.total_count = self.db.count_mapping_audit_history(self.search or None)
        self.rows = self.db.get_mapping_audit_history(search=self.search or None, limit=self.PAGE_SIZE)
        self.endResetModel()

    def canFetchMore(self, parent=QModelIndex()) -> bool:
        return not parent.isValid() and len(self.rows) < self.total_count

    def fetchMore(self, parent=QModelIndex()) -> None:
        if parent.isValid() or not self.canFetchMore(parent):
            return
        records = self.db.get_mapping_audit_history(
            search=self.search or None, limit=self.PAGE_SIZE, offset=len(self.rows)
        )
        if not records:
            self.total_count = len(self.rows)
            return
        start = len(self.rows)
        self.beginInsertRows(QModelIndex(), start, start + len(records) - 1)
        self.rows.extend(records)
        self.endInsertRows()


class ActionDelegate(QStyledItemDelegate):
    actionRequested = pyqtSignal(QModelIndex, str)

    def __init__(self, actions: Callable[[dict[str, Any]], list[tuple[str, bool]]], parent=None):
        super().__init__(parent)
        self._actions = actions

    def _button_rects(self, option, entry):
        actions = self._actions(entry)
        width = max(1, option.rect.width() // max(1, len(actions)))
        return [(option.rect.adjusted(i * width + 3, 4, -(option.rect.width() - (i + 1) * width) - 3, -4), action) for i, action in enumerate(actions)]

    def paint(self, painter: QPainter, option, index) -> None:
        entry = index.data(Qt.ItemDataRole.UserRole) or {}
        for rect, (label, enabled) in self._button_rects(option, entry):
            button = QStyleOptionButton()
            button.rect = rect
            button.text = label
            button.state = QStyle.StateFlag.State_Enabled if enabled else QStyle.StateFlag.State_None
            option.widget.style().drawControl(QStyle.ControlElement.CE_PushButton, button, painter, option.widget)

    def editorEvent(self, event, model, option, index) -> bool:
        if isinstance(event, QMouseEvent) and event.type() == QMouseEvent.Type.MouseButtonRelease:
            entry = index.data(Qt.ItemDataRole.UserRole) or {}
            for rect, (label, enabled) in self._button_rects(option, entry):
                if enabled and rect.contains(event.position().toPoint()):
                    self.actionRequested.emit(index, label.casefold())
                    return True
        return False
