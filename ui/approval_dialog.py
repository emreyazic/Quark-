import sys
from datetime import datetime
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QTableWidget, 
    QTableWidgetItem, QHeaderView, QMessageBox, QLabel, QWidget, QTabWidget,
    QStyledItemDelegate, QLineEdit
)

class TableEditorDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        editor = super().createEditor(parent, option, index)
        if isinstance(editor, QLineEdit):
            editor.setObjectName("TableEditor")
        return editor

from core.database_manager import DatabaseManager


class ApprovalDialog(QDialog):
    def __init__(self, db_manager: DatabaseManager, parent=None):
        super().__init__(parent)
        self.db_manager = db_manager
        self.setWindowTitle("Manage Internal Mappings")
        self.resize(1450, 600)
        self.setStyleSheet("""
            #TableEditor {
                background-color: #2d3436;
                color: #ffffff;
                border: 1px solid #0984e3;
                border-radius: 0px;
                padding: 2px;
                margin: 0px;
            }
            QPushButton#actionApproveBtn {
                background: #008f73;
                color: #ffffff;
                border: 1px solid #39e6bd;
                border-radius: 6px;
                padding: 4px 10px;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton#actionApproveBtn:hover { background: #00b894; }
            QPushButton#actionApproveBtn:pressed { background: #00745e; }
            QPushButton#actionUpdateBtn {
                background: #096fb8;
                color: #ffffff;
                border: 1px solid #58b8ff;
                border-radius: 6px;
                padding: 4px 10px;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton#actionUpdateBtn:hover { background: #0984e3; }
            QPushButton#actionUpdateBtn:pressed { background: #07578f; }
            QPushButton#actionDeleteBtn {
                background: #b82425;
                color: #ffffff;
                border: 1px solid #ff7675;
                border-radius: 6px;
                padding: 4px 10px;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton#actionDeleteBtn:hover { background: #d63031; }
            QPushButton#actionDeleteBtn:pressed { background: #941d1e; }
        """)
        self._setup_ui()
        self._load_data()

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)
        
        # --- TAB 1: Pending Approvals ---
        self.tab_pending = QWidget()
        layout_pending = QVBoxLayout(self.tab_pending)
        
        info_label_pending = QLabel("Review the approved value against the previous and latest automatic search results. Automatic checks never overwrite approved values.")
        layout_pending.addWidget(info_label_pending)

        self.table_pending = QTableWidget()
        self.table_pending.setColumnCount(10)
        self.table_pending.setHorizontalHeaderLabels([
            "Internal Code (Comment)", "MPN", "Approved LCSC", "Previous Auto LCSC",
            "New Auto LCSC", "Approved DigiKey", "Previous Auto DigiKey",
            "New Auto DigiKey", "Last Updated", "Action"
        ])
        self.table_pending.setItemDelegate(TableEditorDelegate(self.table_pending))
        self.table_pending.verticalHeader().setMinimumSectionSize(44)
        self.table_pending.verticalHeader().setDefaultSectionSize(44)
        self._configure_pending_table()
        layout_pending.addWidget(self.table_pending)
        
        self.tabs.addTab(self.tab_pending, "Pending Approvals")
        
        # --- TAB 2: Approved Mappings ---
        self.tab_approved = QWidget()
        layout_approved = QVBoxLayout(self.tab_approved)
        
        info_label_approved = QLabel("Manage your existing approved mappings. Edit cells and click Update to save, or Delete to remove.")
        layout_approved.addWidget(info_label_approved)
        
        self.table_approved = QTableWidget()
        self.table_approved.setColumnCount(6)
        self.table_approved.setHorizontalHeaderLabels(["Internal Code (Comment)", "MPN", "LCSC Code", "DigiKey Code", "Last Updated", "Action"])
        self.table_approved.setItemDelegate(TableEditorDelegate(self.table_approved))
        self.table_approved.verticalHeader().setMinimumSectionSize(44)
        self.table_approved.verticalHeader().setDefaultSectionSize(44)
        self._configure_table(self.table_approved, action_width=240)
        
        # Force redraw on edit
        self.table_pending.itemChanged.connect(lambda item: self.table_pending.viewport().update())
        self.table_approved.itemChanged.connect(lambda item: self.table_approved.viewport().update())
        
        layout_approved.addWidget(self.table_approved)
        
        self.tabs.addTab(self.tab_approved, "Approved Mappings")

        # --- Footer ---
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.accept)
        btn_layout.addWidget(self.btn_close)
        
        main_layout.addLayout(btn_layout)

    def _configure_table(self, table: QTableWidget, action_width: int = 100):
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        table.horizontalHeader().setMinimumSectionSize(action_width)
        table.setColumnWidth(5, action_width)

    def _configure_pending_table(self):
        header = self.table_pending.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in range(2, 8):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(8, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(9, QHeaderView.ResizeMode.Fixed)
        self.table_pending.setColumnWidth(9, 220)

    def _load_data(self):
        mappings = self.db_manager.get_all_internal_mappings()
        
        pending = [m for m in mappings if m.get("lcsc_pending_change") or m.get("digikey_pending_change")]
        approved = [m for m in mappings if m.get("lcsc_approved") or m.get("digikey_approved")]
        
        self._populate_pending_table(pending)
        self._populate_approved_table(approved)
        
    def _populate_pending_table(self, pending_list):
        self.table_pending.setRowCount(len(pending_list))
        for row, mapping in enumerate(pending_list):
            self._fill_pending_row(row, mapping)
            
            container = QWidget()
            h_layout = QHBoxLayout(container)
            h_layout.setContentsMargins(2, 2, 2, 2)
            h_layout.setSpacing(4)
            
            has_lcsc_pending = bool(mapping.get("lcsc_pending_change"))
            has_dk_pending = bool(mapping.get("digikey_pending_change"))
            
            if has_lcsc_pending and not has_dk_pending:
                btn_app = QPushButton("Approve LCSC")
                btn_app.setObjectName("actionApproveBtn")
                btn_app.clicked.connect(lambda checked, r=row: self._on_approve_supplier_clicked(r, "JLCPCB"))
                btn_rej = QPushButton("Reject LCSC")
                btn_rej.setObjectName("actionDeleteBtn")
                btn_rej.clicked.connect(lambda checked, r=row: self._on_reject_supplier_clicked(r, "JLCPCB"))
                h_layout.addWidget(btn_app)
                h_layout.addWidget(btn_rej)
            elif has_dk_pending and not has_lcsc_pending:
                btn_app = QPushButton("Approve DK")
                btn_app.setObjectName("actionApproveBtn")
                btn_app.clicked.connect(lambda checked, r=row: self._on_approve_supplier_clicked(r, "DIGIKEY"))
                btn_rej = QPushButton("Reject DK")
                btn_rej.setObjectName("actionDeleteBtn")
                btn_rej.clicked.connect(lambda checked, r=row: self._on_reject_supplier_clicked(r, "DIGIKEY"))
                h_layout.addWidget(btn_app)
                h_layout.addWidget(btn_rej)
            else:
                btn_app_all = QPushButton("Approve All")
                btn_app_all.setObjectName("actionApproveBtn")
                btn_app_all.clicked.connect(lambda checked, r=row: self._on_approve_clicked(r))
                btn_rej_all = QPushButton("Reject All")
                btn_rej_all.setObjectName("actionDeleteBtn")
                btn_rej_all.clicked.connect(lambda checked, r=row: self._on_reject_clicked(r))
                h_layout.addWidget(btn_app_all)
                h_layout.addWidget(btn_rej_all)
                
            self.table_pending.setCellWidget(row, 9, container)

    def _populate_approved_table(self, approved_list):
        self.table_approved.setRowCount(len(approved_list))
        for row, mapping in enumerate(approved_list):
            self._fill_row(self.table_approved, row, mapping)
            
            # Action Buttons: Update & Delete
            btn_update = QPushButton("Update")
            btn_update.setObjectName("actionUpdateBtn")
            btn_update.setMinimumWidth(90)
            btn_update.setFixedHeight(34)
            btn_update.clicked.connect(lambda checked, r=row: self._on_update_clicked(r))
            
            btn_delete = QPushButton("Delete")
            btn_delete.setObjectName("actionDeleteBtn")
            btn_delete.setMinimumWidth(90)
            btn_delete.setFixedHeight(34)
            btn_delete.clicked.connect(lambda checked, r=row: self._on_delete_clicked(r))
            
            container = QWidget()
            h_layout = QHBoxLayout(container)
            h_layout.setContentsMargins(4, 2, 4, 2)
            h_layout.setSpacing(4)
            h_layout.addWidget(btn_update)
            h_layout.addWidget(btn_delete)
            self.table_approved.setCellWidget(row, 5, container)

    def _fill_row(self, table: QTableWidget, row: int, mapping: dict):
        comment = mapping.get("comment_code", "")
        mpn = mapping.get("mpn", "")
        lcsc = mapping.get("lcsc_code", "")
        digikey = mapping.get("digikey_code", "")
        updated_at = mapping.get("updated_at")
        updated_text = datetime.fromtimestamp(updated_at).strftime("%Y-%m-%d %H:%M") if updated_at else "—"
        
        item_comment = QTableWidgetItem(comment)
        item_comment.setFlags(item_comment.flags() & ~Qt.ItemFlag.ItemIsEditable)
        table.setItem(row, 0, item_comment)
        
        table.setItem(row, 1, QTableWidgetItem(mpn))
        table.setItem(row, 2, QTableWidgetItem(lcsc))
        table.setItem(row, 3, QTableWidgetItem(digikey))
        item_updated = QTableWidgetItem(updated_text)
        item_updated.setFlags(item_updated.flags() & ~Qt.ItemFlag.ItemIsEditable)
        table.setItem(row, 4, item_updated)

    def _fill_pending_row(self, row: int, mapping: dict):
        values = [
            mapping.get("comment_code", ""), mapping.get("mpn", ""),
            mapping.get("lcsc_code", ""), mapping.get("previous_found_lcsc", ""),
            mapping.get("last_found_lcsc", ""), mapping.get("digikey_code", ""),
            mapping.get("previous_found_digikey", ""), mapping.get("last_found_digikey", ""),
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value or "")
            if column in (0, 3, 4, 6, 7):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table_pending.setItem(row, column, item)
        self.table_pending.item(row, 0).setData(Qt.ItemDataRole.UserRole, mapping)
        updated_at = mapping.get("updated_at")
        updated_text = datetime.fromtimestamp(updated_at).strftime("%Y-%m-%d %H:%M") if updated_at else "—"
        item_updated = QTableWidgetItem(updated_text)
        item_updated.setFlags(item_updated.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table_pending.setItem(row, 8, item_updated)

    def _on_approve_clicked(self, row: int):
        self._process_upsert(self.table_pending, row, is_approve_action=True)

    def _on_approve_supplier_clicked(self, row: int, supplier: str):
        comment = self.table_pending.item(row, 0).text().strip()
        mpn = self.table_pending.item(row, 1).text().strip()
        mapping = self.table_pending.item(row, 0).data(Qt.ItemDataRole.UserRole) or {}
        if supplier.upper() in ("JLCPCB", "LCSC"):
            code = self.table_pending.item(row, 2).text().strip()
            if not code or code == (mapping.get("lcsc_code", "") or "").strip():
                code = self.table_pending.item(row, 4).text().strip()
        else:
            code = self.table_pending.item(row, 5).text().strip()
            if not code or code == (mapping.get("digikey_code", "") or "").strip():
                code = self.table_pending.item(row, 7).text().strip()
        try:
            self.db_manager.approve_supplier_mapping(comment, supplier, code, mpn)
            QMessageBox.information(self, "Success", f"{supplier} mapping for '{comment}' has been approved.")
            self._load_data()
        except Exception as e:
            QMessageBox.critical(self, "Database Error", str(e))

    def _on_reject_supplier_clicked(self, row: int, supplier: str):
        comment = self.table_pending.item(row, 0).text().strip()
        try:
            self.db_manager.reject_supplier_pending_change(comment, supplier)
            self._load_data()
        except Exception as e:
            QMessageBox.critical(self, "Database Error", str(e))

    def _on_reject_clicked(self, row: int):
        comment = self.table_pending.item(row, 0).text().strip()
        try:
            self.db_manager.reject_pending_changes(comment)
            self._load_data()
        except Exception as e:
            QMessageBox.critical(self, "Database Error", str(e))

    def _on_update_clicked(self, row: int):
        self._process_upsert(self.table_approved, row, is_approve_action=False)
        
    def _process_upsert(self, table: QTableWidget, row: int, is_approve_action: bool):
        comment = table.item(row, 0).text().strip()
        mpn = table.item(row, 1).text().strip()
        lcsc = table.item(row, 2).text().strip()
        digikey_column = 5 if table is self.table_pending else 3
        digikey = table.item(row, digikey_column).text().strip()
        if is_approve_action:
            mapping = table.item(row, 0).data(Qt.ItemDataRole.UserRole) or {}
            # Clicking Approve accepts each pending supplier's latest candidate.
            # A deliberate edit of the approved-value cell remains a manual override.
            if mapping.get("lcsc_pending_change") and lcsc == (mapping.get("lcsc_code", "") or "").strip():
                lcsc = table.item(row, 4).text().strip()
            if mapping.get("digikey_pending_change") and digikey == (mapping.get("digikey_code", "") or "").strip():
                digikey = table.item(row, 7).text().strip()
        
        if not mpn and not lcsc and not digikey:
            QMessageBox.warning(self, "Validation Error", "Please provide at least one part number (MPN, LCSC, or DigiKey).")
            return
            
        try:
            self.db_manager.upsert_internal_mapping(comment, mpn, lcsc, True, digikey)
            if is_approve_action:
                QMessageBox.information(self, "Success", f"Mapping for '{comment}' has been approved.")
            else:
                QMessageBox.information(self, "Success", f"Mapping for '{comment}' has been updated.")
            self._load_data()
        except Exception as e:
            QMessageBox.critical(self, "Database Error", str(e))

    def _on_delete_clicked(self, row: int):
        comment = self.table_approved.item(row, 0).text().strip()
        
        reply = QMessageBox.question(
            self, "Confirm Delete",
            f"Are you sure you want to delete the mapping for '{comment}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            try:
                self.db_manager.delete_internal_mapping(comment)
                QMessageBox.information(self, "Success", f"Mapping for '{comment}' deleted.")
                self._load_data()
            except Exception as e:
                QMessageBox.critical(self, "Database Error", str(e))
