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
        self.resize(1100, 600)
        self.setStyleSheet("""
            #TableEditor {
                background-color: #2d3436;
                color: #ffffff;
                border: 1px solid #0984e3;
                border-radius: 0px;
                padding: 2px;
                margin: 0px;
            }
            #actionApproveBtn {
                background-color: #00b894; color: white; font-weight: bold; padding: 6px; text-align: center; border-radius: 4px;
            }
            #actionUpdateBtn {
                background-color: #0984e3; color: white; font-weight: bold; padding: 6px; text-align: center; border-radius: 4px;
            }
            #actionDeleteBtn {
                background-color: #d63031; color: white; font-weight: bold; padding: 6px; text-align: center; border-radius: 4px;
            }
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
        
        info_label_pending = QLabel("The following internal codes require your approval. Enter MPN and LCSC code, then click Approve.")
        layout_pending.addWidget(info_label_pending)

        self.table_pending = QTableWidget()
        self.table_pending.setColumnCount(6)
        self.table_pending.setHorizontalHeaderLabels(["Internal Code (Comment)", "Suggested MPN", "Suggested LCSC", "Suggested DigiKey", "Last Updated", "Action"])
        self.table_pending.setItemDelegate(TableEditorDelegate(self.table_pending))
        self._configure_table(self.table_pending, action_width=140)
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

    def _load_data(self):
        mappings = self.db_manager.get_all_internal_mappings()
        
        pending = [m for m in mappings if m.get("approved") == 0]
        approved = [m for m in mappings if m.get("approved") == 1]
        
        self._populate_pending_table(pending)
        self._populate_approved_table(approved)
        
    def _populate_pending_table(self, pending_list):
        self.table_pending.setRowCount(len(pending_list))
        for row, mapping in enumerate(pending_list):
            self._fill_row(self.table_pending, row, mapping)
            
            btn_approve = QPushButton("✅ Approve")
            btn_approve.setObjectName("actionApproveBtn")
            btn_approve.setMinimumSize(100, 32)
            btn_approve.clicked.connect(lambda checked, r=row: self._on_approve_clicked(r))
            
            container = QWidget()
            h_layout = QHBoxLayout(container)
            h_layout.setContentsMargins(4, 2, 4, 2)
            h_layout.addWidget(btn_approve)
            self.table_pending.setCellWidget(row, 5, container)

    def _populate_approved_table(self, approved_list):
        self.table_approved.setRowCount(len(approved_list))
        for row, mapping in enumerate(approved_list):
            self._fill_row(self.table_approved, row, mapping)
            
            # Action Buttons: Update & Delete
            btn_update = QPushButton("💾 Update")
            btn_update.setObjectName("actionUpdateBtn")
            btn_update.setMinimumSize(90, 32)
            btn_update.clicked.connect(lambda checked, r=row: self._on_update_clicked(r))
            
            btn_delete = QPushButton("🗑️ Delete")
            btn_delete.setObjectName("actionDeleteBtn")
            btn_delete.setMinimumSize(90, 32)
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

    def _on_approve_clicked(self, row: int):
        self._process_upsert(self.table_pending, row, is_approve_action=True)

    def _on_update_clicked(self, row: int):
        self._process_upsert(self.table_approved, row, is_approve_action=False)
        
    def _process_upsert(self, table: QTableWidget, row: int, is_approve_action: bool):
        comment = table.item(row, 0).text().strip()
        mpn = table.item(row, 1).text().strip()
        lcsc = table.item(row, 2).text().strip()
        digikey = table.item(row, 3).text().strip()
        
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
