"""Model/proxy types for the main supplier results view."""

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt, QSortFilterProxyModel


class ResultsTableModel(QAbstractTableModel):
    HEADERS = (
        "Internal Code", "MPN", "Description", "Qty", "Best Source",
        "Unit Price", "Total", "Stock", "Status",
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = list(rows)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        value = row["display"][index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            return value
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() in (3, 5, 6, 7):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            return str(value)
        if role == Qt.ItemDataRole.UserRole:
            return row
        return None


class ResultsFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.search = ""
        self.status = "All"
        self.supplier = "All"
        self.only_shortage = False
        self.setDynamicSortFilter(True)

    def set_filters(self, search="", status="All", supplier="All", only_shortage=False):
        self.search = search.casefold().strip()
        self.status = status
        self.supplier = supplier
        self.only_shortage = only_shortage
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent):
        model = self.sourceModel()
        if not isinstance(model, ResultsTableModel):
            return False
        row = model.rows[source_row]
        if self.search and self.search not in row["search"]:
            return False
        if self.status != "All" and row["status"] != self.status:
            return False
        if self.supplier != "All" and row["supplier"] != self.supplier:
            return False
        if self.only_shortage and not row["shortage"]:
            return False
        return True
