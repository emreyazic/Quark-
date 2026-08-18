from __future__ import annotations

from typing import Any, Callable, Optional

from PyQt6.QtCore import QModelIndex, QTimer, Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPushButton, QTabWidget, QTableView, QVBoxLayout, QWidget,
)

from core.database_manager import DatabaseManager
from ui.approval_models import ActionDelegate, ApprovedModel, HistoryModel, MappingFilterProxy, PendingModel


class _CompatItem:
    def __init__(self, index: QModelIndex):
        self._index = index

    def text(self) -> str:
        return str(self._index.data(Qt.ItemDataRole.DisplayRole) or "")

    def data(self, role):
        return self._index.data(role)

    def toolTip(self) -> str:
        return str(self._index.data(Qt.ItemDataRole.ToolTipRole) or "")


class MappingTableView(QTableView):
    """Model/view table; compatibility widgets are created only on explicit request."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.compat_widget_factory: Optional[Callable[[int, int], Optional[QWidget]]] = None
        self.before_visibility_check: Optional[Callable[[], None]] = None
        self.hidden_predicate: Optional[Callable[[int], bool]] = None
        self.ensure_loaded: Optional[Callable[[], None]] = None
        self._compat_widgets: dict[tuple[int, int], QWidget] = {}

    def rowCount(self) -> int:
        if self.ensure_loaded:
            self.ensure_loaded()
        return self.model().rowCount() if self.model() else 0

    def columnCount(self) -> int:
        return self.model().columnCount() if self.model() else 0

    def item(self, row: int, column: int) -> Optional[_CompatItem]:
        if self.ensure_loaded:
            self.ensure_loaded()
        index = self.model().index(row, column) if self.model() else QModelIndex()
        return _CompatItem(index) if index.isValid() else None

    def cellWidget(self, row: int, column: int) -> Optional[QWidget]:
        if self.ensure_loaded:
            self.ensure_loaded()
        key = (row, column)
        if key not in self._compat_widgets and self.compat_widget_factory:
            widget = self.compat_widget_factory(row, column)
            if widget is not None:
                self._compat_widgets[key] = widget
        return self._compat_widgets.get(key)

    def clear_compat_widgets(self) -> None:
        for widget in self._compat_widgets.values():
            widget.deleteLater()
        self._compat_widgets.clear()

    def isRowHidden(self, row: int) -> bool:
        if self.hidden_predicate:
            return self.hidden_predicate(row)
        if self.before_visibility_check:
            self.before_visibility_check()
        current_model = self.model()
        if isinstance(current_model, MappingFilterProxy):
            return not current_model.filterAcceptsRow(row, QModelIndex())
        return super().isRowHidden(row)


class ApprovalDialog(QDialog):
    """Lazy, model/view editor for pending, approved and audit mapping data."""

    SEARCH_DELAY_MS = 250
    DB_FILTER_THRESHOLD = 2000

    def __init__(self, db_manager: DatabaseManager, parent=None):
        super().__init__(parent)
        self.db_manager = db_manager
        self._closed = False
        self._loaded = [False, False, False]
        self._pending_filter_runs = 0
        self._approved_filter_runs = 0
        self._history_search_runs = 0
        self._pending_total_count = 0
        self._approved_total_count = 0
        self._pending_db_filter = False
        self._approved_db_filter = False
        self._pending_last_filter: Optional[str] = None
        self._approved_last_filter: Optional[tuple[str, str]] = None
        self.setWindowTitle("Manage Internal Mappings")
        self.resize(1180, 640)
        self.setStyleSheet(self._stylesheet())
        self._setup_ui()
        self._load_tab(0)

    @staticmethod
    def _stylesheet() -> str:
        return """
            QDialog { background-color: #1e272e; color: #f5f6fa; }
            QTabWidget::pane { border: 1px solid #353b48; background: #1e272e; }
            QTabBar::tab { background: #2f3640; color: #dcdde1; padding: 8px 22px;
                           margin-right: 4px; font-size: 13px; font-weight: 600; }
            QTabBar::tab:selected { background: #008f73; color: white; }
            QLabel { color: #f5f6fa; font-size: 13px; }
            QLineEdit, QComboBox { background: #2f3640; color: white; border: 1px solid #718093;
                                  border-radius: 5px; padding: 5px 10px; font-size: 13px; }
            QLineEdit:focus { border: 1px solid #00d2d3; }
            QTableView { background: #2f3640; color: #f5f6fa; gridline-color: #353b48;
                         border: 1px solid #353b48; selection-background-color: #4b6584; }
            QHeaderView::section { background: #1e272e; color: #dcdde1; padding: 6px;
                                   font-weight: 700; border: 1px solid #353b48; }
            QPushButton { background: #485460; color: white; border: 1px solid #718093;
                          border-radius: 5px; padding: 6px 14px; }
        """

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self._setup_pending_tab()
        self._setup_approved_tab()
        self._setup_history_tab()
        self.tabs.currentChanged.connect(self._on_tab_changed)
        footer = QHBoxLayout()
        footer.addStretch()
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.accept)
        footer.addWidget(self.btn_close)
        layout.addLayout(footer)

    def _new_table(self) -> MappingTableView:
        table = MappingTableView()
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed | QAbstractItemView.EditTrigger.SelectedClicked)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(38)
        return table

    def _setup_pending_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        bar = QHBoxLayout()
        self.lbl_pending_count = QLabel("Pending: 0")
        self.search_pending = QLineEdit()
        self.search_pending.setPlaceholderText("Search by internal code, MPN, supplier, code...")
        bar.addWidget(self.lbl_pending_count)
        bar.addWidget(self.search_pending, 1)
        layout.addLayout(bar)
        self.pending_model = PendingModel(self)
        self.pending_proxy = MappingFilterProxy((0, 1, 2, 3, 4), self)
        self.pending_proxy.setSourceModel(self.pending_model)
        self.table_pending = self._new_table()
        self.table_pending.setModel(self.pending_proxy)
        self.table_pending.compat_widget_factory = self._pending_compat_widget
        self.table_pending.before_visibility_check = self._apply_pending_filter
        delegate = ActionDelegate(lambda row: [("Approve", True), ("Keep", bool(row.get("has_approved")))], self.table_pending)
        delegate.actionRequested.connect(self._on_pending_action)
        self.table_pending.setItemDelegateForColumn(5, delegate)
        self._configure_header(self.table_pending, 5)
        layout.addWidget(self.table_pending)
        self.tabs.addTab(tab, "Pending")
        self._pending_timer = self._debounce(self._apply_pending_filter)
        self.search_pending.textChanged.connect(lambda _text: self._pending_timer.start())

    def _setup_approved_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        bar = QHBoxLayout()
        self.lbl_approved_count = QLabel("Approved: 0")
        self.combo_supplier_filter = QComboBox()
        self.combo_supplier_filter.addItems(["All Suppliers", "JLCPCB", "DigiKey"])
        self.search_approved = QLineEdit()
        self.search_approved.setPlaceholderText("Search by internal code, MPN, approved code...")
        bar.addWidget(self.lbl_approved_count)
        bar.addWidget(self.combo_supplier_filter)
        bar.addWidget(self.search_approved, 1)
        layout.addLayout(bar)
        self.approved_model = ApprovedModel(self)
        self.approved_proxy = MappingFilterProxy((0, 1, 3), self)
        self.approved_proxy.setSourceModel(self.approved_model)
        self.table_approved = self._new_table()
        self.table_approved.setModel(self.approved_proxy)
        self.table_approved.ensure_loaded = lambda: self._load_tab(1)
        self.table_approved.compat_widget_factory = self._approved_compat_widget
        self.table_approved.before_visibility_check = self._apply_approved_filter
        delegate = ActionDelegate(lambda row: [("Save", str(row.get("approved_code", "")).strip() != row.get("_original_code", "")), ("Cancel", row.get("approved_code", "") != row.get("_original_code", ""))], self.table_approved)
        delegate.actionRequested.connect(self._on_approved_action)
        self.table_approved.setItemDelegateForColumn(5, delegate)
        self._configure_header(self.table_approved, 5)
        layout.addWidget(self.table_approved)
        self.tabs.addTab(tab, "Approved")
        self._approved_timer = self._debounce(self._apply_approved_filter)
        self.search_approved.textChanged.connect(lambda _text: self._approved_timer.start())
        self.combo_supplier_filter.currentTextChanged.connect(lambda _text: self._approved_timer.start())

    def _setup_history_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        bar = QHBoxLayout()
        self.lbl_history_count = QLabel("Total Records: 0")
        self.search_history = QLineEdit()
        self.search_history.setPlaceholderText("Search history by code, MPN, supplier, action...")
        bar.addWidget(self.lbl_history_count)
        bar.addWidget(self.search_history, 1)
        layout.addLayout(bar)
        self.history_model = HistoryModel(self.db_manager, self)
        self.table_history = self._new_table()
        self.table_history.setModel(self.history_model)
        self.table_history.ensure_loaded = lambda: self._load_tab(2)
        self.table_history.hidden_predicate = self._history_row_hidden
        self._configure_header(self.table_history)
        layout.addWidget(self.table_history)
        self.tabs.addTab(tab, "History")
        self._history_timer = self._debounce(self._apply_history_search)
        self.search_history.textChanged.connect(lambda _text: self._history_timer.start())

    def _debounce(self, callback: Callable[[], None]) -> QTimer:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(self.SEARCH_DELAY_MS)
        timer.timeout.connect(callback)
        return timer

    @staticmethod
    def _configure_header(table: QTableView, action_column: Optional[int] = None) -> None:
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        fixed_widths = {0: 145, 2: 95}
        for column, width in fixed_widths.items():
            if column < table.model().columnCount():
                table.setColumnWidth(column, width)
        for column in range(table.model().columnCount()):
            if column in (1, 3, 4):
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        if action_column is not None:
            header.setSectionResizeMode(action_column, QHeaderView.ResizeMode.Fixed)
            table.setColumnWidth(action_column, 170)

    def _on_tab_changed(self, index: int) -> None:
        self._load_tab(index)

    def _load_tab(self, index: int) -> None:
        if self._closed or self._loaded[index]:
            return
        (self._load_pending_data, self._load_approved_data, self._load_history_data)[index]()
        self._loaded[index] = True

    def _load_data(self) -> None:
        index = self.tabs.currentIndex()
        self._loaded[index] = False
        self._load_tab(index)

    def _load_pending_data(self) -> None:
        self.table_pending.clear_compat_widgets()
        rows = self.db_manager.get_pending_supplier_mappings()
        self.pending_model.replace(rows)
        self._pending_total_count = len(rows)
        self._pending_db_filter = len(rows) > self.DB_FILTER_THRESHOLD
        self._pending_last_filter = ""
        self._apply_pending_filter()

    def _load_approved_data(self) -> None:
        self.table_approved.clear_compat_widgets()
        rows = self.db_manager.get_approved_supplier_mappings()
        self.approved_model.replace(rows)
        self._approved_total_count = len(rows)
        self._approved_db_filter = len(rows) > self.DB_FILTER_THRESHOLD
        self._approved_last_filter = ("", "All Suppliers")
        self._apply_approved_filter()

    def _load_history_data(self) -> None:
        self.history_model.reload(self.search_history.text())
        self._update_history_count()

    def _apply_pending_filter(self) -> None:
        self._pending_timer.stop()
        self._pending_filter_runs += 1
        query = self.search_pending.text().strip()
        if self._pending_db_filter:
            if query != self._pending_last_filter:
                self.table_pending.clear_compat_widgets()
                self.pending_model.replace(self.db_manager.get_pending_supplier_mappings(query or None))
                self._pending_last_filter = query
            self.pending_proxy.set_query("")
            shown, total = self.pending_model.rowCount(), self._pending_total_count
        else:
            self.pending_proxy.set_query(query)
            shown, total = self.pending_proxy.rowCount(), self.pending_model.rowCount()
        self.lbl_pending_count.setText(f"Pending: {shown} of {total}" if shown != total else f"Pending: {total}")

    def _apply_approved_filter(self) -> None:
        self._approved_timer.stop()
        self._approved_filter_runs += 1
        query = self.search_approved.text().strip()
        supplier = self.combo_supplier_filter.currentText()
        if self._approved_db_filter:
            current_filter = (query, supplier)
            if current_filter != self._approved_last_filter:
                self.table_approved.clear_compat_widgets()
                self.approved_model.replace(
                    self.db_manager.get_approved_supplier_mappings(query or None, supplier)
                )
                self._approved_last_filter = current_filter
            self.approved_proxy.set_query("")
            self.approved_proxy.set_supplier("All Suppliers")
            shown, total = self.approved_model.rowCount(), self._approved_total_count
        else:
            self.approved_proxy.set_query(query)
            self.approved_proxy.set_supplier(supplier)
            shown, total = self.approved_proxy.rowCount(), self.approved_model.rowCount()
        self.lbl_approved_count.setText(f"Approved: {shown} of {total}" if shown != total else f"Approved: {total}")

    def _apply_history_search(self) -> None:
        self._history_timer.stop()
        self._history_search_runs += 1
        if self._loaded[2] and not self._closed:
            self.history_model.reload(self.search_history.text())
            self._update_history_count()

    def _update_history_count(self) -> None:
        loaded, total = self.history_model.rowCount(), self.history_model.total_count
        prefix = "Records" if self.history_model.search else "Total Records"
        self.lbl_history_count.setText(f"{prefix}: {loaded} of {total}" if loaded < total else f"{prefix}: {total}")

    def _history_row_hidden(self, row: int) -> bool:
        query = self.search_history.text().strip().casefold()
        if not query or not 0 <= row < self.history_model.rowCount():
            return False
        visible = sum(
            any(
                query in str(self.history_model.index(candidate, column).data() or "").casefold()
                for column in range(self.history_model.columnCount())
            )
            for candidate in range(self.history_model.rowCount())
        )
        self.lbl_history_count.setText(f"Records: {visible} of {self.history_model.rowCount()}")
        return not any(
            query in str(self.history_model.index(row, column).data() or "").casefold()
            for column in range(self.history_model.columnCount())
        )

    @staticmethod
    def _source_row(index: QModelIndex) -> int:
        model = index.model()
        return model.mapToSource(index).row() if isinstance(model, MappingFilterProxy) else index.row()

    def _on_pending_action(self, index: QModelIndex, action: str) -> None:
        source_row = self._source_row(index)
        entry = self.pending_model.rows[source_row]
        try:
            if action == "approve":
                code = str(entry.get("candidate_code", "")).strip()
                if not code:
                    QMessageBox.warning(self, "Validation Error", "Code cannot be empty.")
                    return
                self.db_manager.approve_supplier_mapping(entry["comment_code"], entry["supplier"], code, entry["mpn"], entry.get("_candidate_code", ""))
            elif action == "keep":
                self.db_manager.keep_supplier_current_mapping(entry["comment_code"], entry["supplier"])
            else:
                return
        except Exception as exc:
            QMessageBox.critical(self, "Database Error", str(exc))
            return
        self.pending_model.remove_source_row(source_row)
        self._pending_total_count = max(0, self._pending_total_count - 1)
        self.table_pending.clear_compat_widgets()
        self._apply_pending_filter()
        self._loaded[1] = False
        self._loaded[2] = False

    def _on_approved_action(self, index: QModelIndex, action: str) -> None:
        source_row = self._source_row(index)
        entry = self.approved_model.rows[source_row]
        if action == "cancel":
            self.approved_model.cancel_edit(source_row)
            return
        if action != "save":
            return
        code = str(entry.get("approved_code", "")).strip()
        if not code:
            QMessageBox.warning(self, "Validation Error", "Approved code cannot be empty.")
            return
        try:
            self.db_manager.update_approved_supplier_code(entry["comment_code"], entry["supplier"], code, entry["mpn"])
        except Exception as exc:
            QMessageBox.critical(self, "Database Error", str(exc))
            return
        entry["approved_code"] = code
        self.approved_model.accept_edit(source_row)
        self.approved_model.dataChanged.emit(self.approved_model.index(source_row, 3), self.approved_model.index(source_row, 5))
        self._loaded[2] = False

    def _on_pending_search_changed(self, _text: str) -> None:
        self._pending_timer.start()

    def _on_approved_filter_changed(self, *_args) -> None:
        self._approved_timer.start()

    def _on_history_search_changed(self, _text: str) -> None:
        self._history_timer.start()

    def _on_approve_clicked(self, entry: dict[str, Any], line_edit: QLineEdit) -> None:
        entry["candidate_code"] = line_edit.text()
        row = self.pending_model.rows.index(entry)
        self._on_pending_action(self.pending_proxy.mapFromSource(self.pending_model.index(row, 5)), "approve")

    def _on_keep_clicked(self, entry: dict[str, Any]) -> None:
        row = self.pending_model.rows.index(entry)
        self._on_pending_action(self.pending_proxy.mapFromSource(self.pending_model.index(row, 5)), "keep")

    def _pending_compat_widget(self, row: int, column: int) -> Optional[QWidget]:
        source = self.pending_proxy.mapToSource(self.pending_proxy.index(row, column)).row()
        entry = self.pending_model.rows[source]
        if column == 4:
            editor = QLineEdit(str(entry.get("candidate_code", "")))
            editor.setObjectName("newCodeInput")
            editor.setProperty("edited", "false")
            original = editor.text()
            def sync_pending(text: str, e=entry) -> None:
                e["candidate_code"] = text
                edited = text.strip() != original
                editor.setProperty("edited", "true" if edited else "false")
                editor.setToolTip(f"Edited: {text} (Original Candidate: {original})" if edited else text)
            editor.textChanged.connect(sync_pending)
            sync_pending(editor.text())
            return editor
        if column == 5:
            widget = QWidget()
            box = QHBoxLayout(widget)
            approve = QPushButton("Approve")
            approve.setObjectName("actionApproveBtn")
            approve.clicked.connect(lambda _checked=False, e=entry: self._approve_compat_entry(e, row))
            keep = QPushButton("Keep")
            keep.setObjectName("actionKeepBtn")
            keep.setEnabled(bool(entry.get("has_approved")))
            keep.setToolTip("Keep current code and dismiss candidate" if keep.isEnabled() else "Cannot keep: no previously approved code exists")
            keep.clicked.connect(lambda _checked=False, e=entry: self._on_keep_clicked(e))
            box.addWidget(approve)
            box.addWidget(keep)
            return widget
        return None

    def _approve_compat_entry(self, entry: dict[str, Any], proxy_row: int) -> None:
        editor = self.table_pending.cellWidget(proxy_row, 4)
        if isinstance(editor, QLineEdit):
            entry["candidate_code"] = editor.text()
        self._on_approve_clicked(entry, editor if isinstance(editor, QLineEdit) else QLineEdit(str(entry.get("candidate_code", ""))))

    def _approved_compat_widget(self, row: int, column: int) -> Optional[QWidget]:
        source = self.approved_proxy.mapToSource(self.approved_proxy.index(row, column)).row()
        entry = self.approved_model.rows[source]
        if column == 3:
            editor = QLineEdit(str(entry.get("approved_code", "")))
            editor.setObjectName("approvedCodeInput")
            editor.setToolTip(editor.text())
            editor.setProperty("edited", "false")
            def sync_approved(text: str, e=entry) -> None:
                e["approved_code"] = text
                edited = text.strip() != str(e.get("_original_code", ""))
                editor.setProperty("edited", "true" if edited else "false")
                editor.setToolTip(
                    f"Edited: {text} (Original Approved: {e.get('_original_code', '')})" if edited else text
                )
            editor.textChanged.connect(sync_approved)
            return editor
        if column == 5:
            widget = QWidget()
            box = QHBoxLayout(widget)
            save = QPushButton("Save")
            save.setObjectName("actionSaveBtn")
            cancel = QPushButton("Cancel")
            cancel.setObjectName("actionCancelBtn")
            editor = self.table_approved.cellWidget(row, 3)
            save.clicked.connect(lambda _checked=False, idx=self.approved_proxy.index(row, 5): self._on_approved_action(idx, "save"))
            cancel.clicked.connect(lambda _checked=False, idx=self.approved_proxy.index(row, 5): self._cancel_compat(idx, editor))
            if isinstance(editor, QLineEdit):
                def sync_buttons(text: str) -> None:
                    changed = text.strip() != str(entry.get("_original_code", ""))
                    save.setEnabled(changed)
                    cancel.setEnabled(changed)
                editor.textChanged.connect(sync_buttons)
                sync_buttons(editor.text())
            box.addWidget(save)
            box.addWidget(cancel)
            return widget
        return None

    def _cancel_compat(self, index: QModelIndex, editor: Optional[QWidget]) -> None:
        source = self._source_row(index)
        self.approved_model.cancel_edit(source)
        if isinstance(editor, QLineEdit):
            editor.setText(str(self.approved_model.rows[source].get("approved_code", "")))

    def closeEvent(self, event) -> None:
        self._closed = True
        for timer in (self._pending_timer, self._approved_timer, self._history_timer):
            timer.stop()
        super().closeEvent(event)
