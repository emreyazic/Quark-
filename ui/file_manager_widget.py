"""File manager widget — upload BOM files and assign board names."""

import os
import json
import copy
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal, QSize, QMimeData
from PyQt6.QtGui import QDragEnterEvent, QDropEvent, QIcon, QBrush, QColor, QDrag
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QAbstractItemView,
    QMessageBox,
    QTreeWidget,
    QTreeWidgetItem,
    QInputDialog,
)

from models.bom_item import BomFile
from core.bom_parser import BomParser
from ui.sheet_selector_dialog import SheetSelectorDialog
from models.project import Project, ProjectItem
from models.workspace import Workspace
from services.project_storage import (
    export_workspace_package,
    load_workspace,
    save_workspace,
)
from core.utils import get_resource_path

SUPPORTED_BOM_EXTENSIONS = (".xlsx", ".xlsm")


class DropArea(QFrame):
    """Drag-and-drop zone for Excel files."""
    files_dropped = pyqtSignal(list)  # list of file paths

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dropArea")
        self.setAcceptDrops(True)
        self.setMinimumHeight(100)
        
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        icon_label = QLabel("📂")
        icon_label.setObjectName("dropIcon")
        icon_label.setStyleSheet("font-size: 28px; background: transparent;")
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        text_label = QLabel("Drag & drop BOM Excel files here")
        text_label.setObjectName("dropTitle")
        text_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        sub_label = QLabel("or click Browse below")
        sub_label.setObjectName("dropHint")
        sub_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        layout.addWidget(icon_label)
        layout.addWidget(text_label)
        layout.addWidget(sub_label)
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            for url in urls:
                if url.toLocalFile().lower().endswith(SUPPORTED_BOM_EXTENSIONS):
                    event.acceptProposedAction()
                    self.setStyleSheet(
                        "#dropArea { border-color: #34d399; background-color: #102a28; }"
                    )
                    return
        event.ignore()

    def dragLeaveEvent(self, event):
        self.setStyleSheet("")

    def dropEvent(self, event: QDropEvent):
        self.setStyleSheet("")
        files = []
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith(SUPPORTED_BOM_EXTENSIONS):
                files.append(path)
        if files:
            self.files_dropped.emit(files)


class WorkspaceTreeWidget(QTreeWidget):
    """Tree widget that handles drag-and-drop directly onto Project rows."""
    files_dropped_on_project = pyqtSignal(str, list)  # project_name, list of file_paths
    files_dropped_on_empty = pyqtSignal(list)         # list of file_paths
    bom_moved = pyqtSignal(str, str, str)             # source_project_name, file_path, target_project_name

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setAlternatingRowColors(True)
        self.setMinimumHeight(200)

    def startDrag(self, supportedActions):
        item = self.currentItem()
        if not item or item.parent() is None:
            # Only BOM child rows should be draggable
            return
            
        source_project_name = item.parent().data(0, Qt.ItemDataRole.UserRole)
        file_path = item.data(0, Qt.ItemDataRole.UserRole)
        
        mime_data = QMimeData()
        payload = json.dumps({
            "source_project": source_project_name,
            "file_path": file_path
        }).encode('utf-8')
        mime_data.setData("application/x-bom-board-row", payload)
        
        drag = QDrag(self)
        drag.setMimeData(mime_data)
        drag.exec(supportedActions)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasFormat("application/x-bom-board-row"):
            event.acceptProposedAction()
            return
            
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            valid = any(
                url.toLocalFile().lower().endswith(SUPPORTED_BOM_EXTENSIONS)
                for url in urls
            )
            if valid:
                event.acceptProposedAction()
                return
        event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat("application/x-bom-board-row"):
            event.acceptProposedAction()
            return
            
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            valid = any(
                url.toLocalFile().lower().endswith(SUPPORTED_BOM_EXTENSIONS)
                for url in urls
            )
            if valid:
                event.acceptProposedAction()
                return
        event.ignore()

    def dropEvent(self, event: QDropEvent):
        if event.mimeData().hasFormat("application/x-bom-board-row"):
            data = event.mimeData().data("application/x-bom-board-row").data()
            try:
                payload = json.loads(data.decode('utf-8'))
                source_project = payload.get("source_project")
                file_path = payload.get("file_path")
                
                pos = event.position().toPoint()
                item = self.itemAt(pos)
                
                if item:
                    if item.parent() is None:
                        target_project = item.data(0, Qt.ItemDataRole.UserRole)
                    else:
                        target_project = item.parent().data(0, Qt.ItemDataRole.UserRole)
                        
                    self.bom_moved.emit(source_project, file_path, target_project)
                    event.acceptProposedAction()
            except Exception:
                pass
            return

        files = []
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith(SUPPORTED_BOM_EXTENSIONS):
                files.append(path)
                
        if not files:
            return
            
        pos = event.position().toPoint()
        item = self.itemAt(pos)
        
        if item:
            if item.parent() is None:
                # Dropped on a Project row
                project_name = item.data(0, Qt.ItemDataRole.UserRole)
            else:
                # Dropped on a BOM child row, use parent's project name
                project_name = item.parent().data(0, Qt.ItemDataRole.UserRole)
            self.files_dropped_on_project.emit(project_name, files)
        else:
            # Dropped on empty space
            self.files_dropped_on_empty.emit(files)


class FileManagerWidget(QWidget):
    """Widget for managing BOM file uploads and workspace projects."""

    files_changed = pyqtSignal()  # Emitted when files are added/removed/quantities changed
    component_library_import_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bom_files: list[BomFile] = []
        self._parsed_boms: dict[str, BomFile] = {}
        self._board_status: dict[str, str] = {}
        self._workspace = Workspace("Workspace")
        self._parser = BomParser()
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        # Title
        header_layout = QHBoxLayout()
        title = QLabel("📋  BOM Files")
        title.setObjectName("sectionTitle")
        header_layout.addWidget(title)
        header_layout.addStretch()
        layout.addLayout(header_layout)
        
        # Workspace Controls (Row 1)
        ws_btn_layout1 = QHBoxLayout()
        self._btn_add_project = QPushButton("Add Project")
        self._btn_rename_project = QPushButton("Rename Project")
        self._btn_remove_project = QPushButton("Remove Project")
        
        self._btn_add_project.clicked.connect(self._add_project_clicked)
        self._btn_rename_project.clicked.connect(self._rename_project_clicked)
        self._btn_remove_project.clicked.connect(self._remove_project_clicked)
        
        ws_btn_layout1.addWidget(self._btn_add_project)
        ws_btn_layout1.addWidget(self._btn_rename_project)
        ws_btn_layout1.addWidget(self._btn_remove_project)
        ws_btn_layout1.addStretch()
        layout.addLayout(ws_btn_layout1)

        # Workspace Controls (Row 2)
        ws_btn_layout2 = QHBoxLayout()
        self._btn_load_ws = QPushButton("Load Workspace")
        self._btn_save_ws = QPushButton("Save Workspace")
        self._btn_export_package = QPushButton("Export Portable Package")
        self._btn_export_package.setToolTip(
            "Create a ZIP containing workspace.json and all BOM files"
        )
        self._btn_clear_ws = QPushButton("Clear Workspace")
        self._btn_clear_ws.setObjectName("btnDanger")
        
        self._btn_load_ws.clicked.connect(self._load_workspace_clicked)
        self._btn_save_ws.clicked.connect(self._save_workspace_clicked)
        self._btn_export_package.clicked.connect(
            self._export_workspace_package_clicked
        )
        self._btn_clear_ws.clicked.connect(self._clear_workspace_clicked)
        
        ws_btn_layout2.addWidget(self._btn_load_ws)
        ws_btn_layout2.addWidget(self._btn_save_ws)
        ws_btn_layout2.addWidget(self._btn_export_package)
        ws_btn_layout2.addStretch()
        ws_btn_layout2.addWidget(self._btn_clear_ws)
        layout.addLayout(ws_btn_layout2)

        # Drop area
        self._drop_area = DropArea()
        self._drop_area.files_dropped.connect(self._add_files)
        layout.addWidget(self._drop_area)

        # Browse button
        btn_row = QHBoxLayout()
        self._btn_browse = QPushButton("Browse Files...")
        self._btn_browse.clicked.connect(self._browse_files)
        btn_row.addWidget(self._btn_browse)
        self._btn_import_library = QPushButton("Import Component Library...")
        self._btn_import_library.setToolTip(
            "Search LCSC and DigiKey codes from an Altium component-library Excel file and add results to Pending Approvals"
        )
        self._btn_import_library.clicked.connect(self._browse_component_library)
        btn_row.addWidget(self._btn_import_library)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # Workspace Tree
        self._tree = WorkspaceTreeWidget()
        self._tree.setColumnCount(4)
        self._tree.setHeaderLabels(["Name", "Quantity", "Status", "Action"])
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self._tree.setColumnWidth(1, 120)
        self._tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self._tree.setColumnWidth(3, 70)
        
        self._tree.files_dropped_on_project.connect(self._add_files_to_project)
        self._tree.files_dropped_on_empty.connect(self._add_files_to_empty)
        self._tree.bom_moved.connect(self._move_board_between_projects)
        
        layout.addWidget(self._tree)
        
        self._refresh_tree()

    def _browse_component_library(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Altium Component Library",
            "",
            "Excel Files (*.xlsx)",
        )
        if path:
            self.component_library_import_requested.emit(path)

    def set_component_library_import_enabled(self, enabled: bool):
        self._btn_import_library.setEnabled(enabled)

    def _refresh_tree(self):
        """Refresh the workspace tree display."""
        self._tree.clear()

        for project in self._workspace.projects:
            proj_item = QTreeWidgetItem(self._tree)
            proj_item.setText(0, project.project_name)
            proj_item.setData(0, Qt.ItemDataRole.UserRole, project.project_name)
            
            # Use bold font for project headers
            font = proj_item.font(0)
            font.setBold(True)
            proj_item.setFont(0, font)
            
            # Set folder icon if available in OS theme, otherwise just bold is fine
            # BUG FIX: QIcon.hasThemeIcon("folder") causes ~18 minute freeze on some Windows systems
            # if QIcon.hasThemeIcon("folder"):
            #     proj_item.setIcon(0, QIcon.fromTheme("folder"))
            
            # Status column for Project
            num_boards = len(project.board_items)
            proj_item.setText(2, f"{num_boards} BOM file{'s' if num_boards != 1 else ''}")
            proj_item.setForeground(2, QBrush(QColor("#6b8299")))
            
            proj_item.setExpanded(True)
            
            for board in project.board_items:
                board_item = QTreeWidgetItem(proj_item)
                board_item.setText(0, board.board_name)
                board_item.setData(0, Qt.ItemDataRole.UserRole, board.file_path)
                
                # Quantity (editable via buttons)
                self._tree.setItemWidget(board_item, 1, self._create_quantity_widget(board))
                
                # Status
                status_text = self._board_status.get(board.file_path, "Unknown")
                board_item.setText(2, status_text)
                
                # Apply colors
                if status_text.startswith("Loaded"):
                    board_item.setForeground(2, QBrush(QColor("#00b894")))
                elif "error" in status_text.lower() or status_text == "Missing file":
                    board_item.setForeground(2, QBrush(QColor("#e74c3c")))
                elif status_text == "Unknown":
                    board_item.setForeground(2, QBrush(QColor("#6b8299")))
                
                # Action
                self._tree.setItemWidget(board_item, 3, self._create_remove_button_widget(project.project_name, board.file_path))
                
        has_projects = len(self._workspace.projects) > 0
        has_boards = self.has_files()
        
        self._btn_save_ws.setEnabled(has_projects or has_boards)
        self._btn_export_package.setEnabled(has_boards)
        self._btn_clear_ws.setEnabled(has_projects or has_boards)

    def _get_selected_project_name(self) -> Optional[str]:
        selected = self._tree.selectedItems()
        if not selected:
            return None
        
        item = selected[0]
        if item.parent() is None:
            # It's a project node
            return item.data(0, Qt.ItemDataRole.UserRole)
        else:
            # It's a board node, return parent's name
            return item.parent().data(0, Qt.ItemDataRole.UserRole)

    def _next_project_name(self) -> str:
        i = 1
        while True:
            name = f"project_{i}"
            if not self._workspace.get_project(name):
                return name
            i += 1

    def _add_project_clicked(self):
        default_name = self._next_project_name()
        name, ok = QInputDialog.getText(self, "Add Project", "Project Name:", text=default_name)
        if ok and name.strip():
            try:
                self._workspace.add_project(Project(name))
                self._refresh_tree()
                self.files_changed.emit()
            except ValueError as e:
                QMessageBox.warning(self, "Invalid Name", str(e))

    def _rename_project_clicked(self):
        selected = self._get_selected_project_name()
        if not selected:
            QMessageBox.information(self, "Select Project", "Please select a project to rename.")
            return
            
        new_name, ok = QInputDialog.getText(self, "Rename Project", "New Project Name:", text=selected)
        if ok and new_name.strip():
            try:
                self._workspace.rename_project(selected, new_name)
                self._refresh_tree()
                self.files_changed.emit()
            except ValueError as e:
                QMessageBox.warning(self, "Invalid Name", str(e))

    def _remove_project_clicked(self):
        selected = self._get_selected_project_name()
        if not selected:
            QMessageBox.information(self, "Select Project", "Please select a project to remove.")
            return
            
        reply = QMessageBox.question(
            self, "Confirm Remove", 
            f"Are you sure you want to remove project '{selected}' and all its files?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            proj = self._workspace.get_project(selected)
            if proj:
                file_paths = [board.file_path for board in proj.board_items]
                self._workspace.remove_project(selected)
                for file_path in file_paths:
                    in_use = any(p.get_board_by_file_path(file_path) for p in self._workspace.projects)
                    if not in_use:
                        self._cleanup_board_cache(file_path)
            self._refresh_tree()
            self.files_changed.emit()

    def _cleanup_board_cache(self, file_path: str):
        if file_path in self._parsed_boms:
            bf = self._parsed_boms.pop(file_path)
            if bf in self._bom_files:
                self._bom_files.remove(bf)
        self._board_status.pop(file_path, None)

    def _remove_board(self, project_name: str, file_path: str):
        project = self._workspace.get_project(project_name)
        if project and project.get_board_by_file_path(file_path):
            project.remove_board(file_path)
            
        in_use = any(p.get_board_by_file_path(file_path) for p in self._workspace.projects)
        if not in_use:
            self._cleanup_board_cache(file_path)
            
        self._refresh_tree()
        self.files_changed.emit()

    def _clear_workspace_clicked(self):
        reply = QMessageBox.question(
            self, "Clear Workspace", 
            "Are you sure you want to clear the entire workspace?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._workspace = Workspace("Workspace")
            self._bom_files.clear()
            self._parsed_boms.clear()
            self._board_status.clear()
            self._refresh_tree()
            self.files_changed.emit()

    def _browse_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Select BOM Excel Files",
            "",
            "Excel Files (*.xlsx *.xlsm)",
        )
        if files:
            self._add_files(files)

    def _add_files(self, file_paths: list[str]):
        """Handler for DropArea and Browse files."""
        target_project_name = self._get_selected_project_name()
        
        if not target_project_name:
            if not self._workspace.projects:
                target_project_name = self._next_project_name()
                self._workspace.add_project(Project(target_project_name))
                self._refresh_tree()
            else:
                if len(self._workspace.projects) == 1:
                    target_project_name = self._workspace.projects[0].project_name
                else:
                    QMessageBox.warning(self, "No Project Selected", "Please select a project to add files to.")
                    return
                    
        self._add_files_to_project(target_project_name, file_paths)

    def _add_files_to_empty(self, file_paths: list[str]):
        """Handler for tree drops onto empty space."""
        if not self._workspace.projects:
            target_project_name = self._next_project_name()
            self._workspace.add_project(Project(target_project_name))
            self._refresh_tree()
            self._add_files_to_project(target_project_name, file_paths)
        elif len(self._workspace.projects) == 1:
            self._add_files_to_project(self._workspace.projects[0].project_name, file_paths)
        else:
            QMessageBox.warning(self, "Ambiguous Drop", "Please drop files onto a specific project.")

    def _move_board_between_projects(self, source_project_name: str, file_path: str, target_project_name: str):
        if source_project_name == target_project_name:
            return

        source_project = self._workspace.get_project(source_project_name)
        target_project = self._workspace.get_project(target_project_name)

        if not source_project or not target_project:
            return

        board = source_project.get_board_by_file_path(file_path)
        if not board:
            return

        if target_project.get_board_by_file_path(file_path):
            QMessageBox.warning(
                self,
                "Duplicate BOM",
                "This BOM already exists in the target project."
            )
            return

        source_project.remove_board(file_path)
        target_project.add_board(board)

        self._refresh_tree()
        self.files_changed.emit()

    def _add_files_to_project(self, project_name: str, file_paths: list[str]):
        target_project = self._workspace.get_project(project_name)
        if not target_project:
            return

        for path in file_paths:
            if not path.lower().endswith(SUPPORTED_BOM_EXTENSIONS):
                QMessageBox.warning(
                    self,
                    "Unsupported BOM File",
                    f"'{os.path.basename(path)}' cannot be read. Save legacy "
                    ".xls files as .xlsx before importing.",
                )
                continue

            try:
                sheets = self._parser.inspect_sheets(path)
            except Exception as e:
                print(f"Error inspecting {path}: {e}")
                QMessageBox.critical(
                    self,
                    "Workbook Error",
                    f"Failed to read workbook '{os.path.basename(path)}':\n{e}",
                )
                continue

            if not sheets:
                continue

            # If multi-sheet or duplicate sheet detected, present SheetSelectorDialog
            if len(sheets) > 1 or any(s.duplicate_of for s in sheets):
                dlg = SheetSelectorDialog(sheets, self)
                if dlg.exec() != SheetSelectorDialog.DialogCode.Accepted:
                    continue
                selected_sheets = dlg.get_selected_sheets()
                if not selected_sheets:
                    continue
            else:
                selected_sheets = [sheets[0]]

            for bom_file in selected_sheets:
                if len(selected_sheets) == 1 and len(sheets) == 1:
                    board_name = os.path.splitext(os.path.basename(path))[0]
                else:
                    base = os.path.splitext(os.path.basename(path))[0]
                    board_name = f"{base} ({bom_file.sheet_name})"

                # Set board name on the BomFile
                bom_file.board_name = board_name

                # Store metadata
                self._parsed_boms[path] = bom_file
                if bom_file not in self._bom_files:
                    self._bom_files.append(bom_file)

                try:
                    bom_items = self._parse_items_for_board(bom_file, board_name)
                    self._board_status[path] = f"Loaded ({len(bom_items)} items)"
                    project_item = ProjectItem(
                        file_path=path,
                        board_name=board_name,
                        board_quantity=1,
                        bom_items=bom_items,
                    )
                    target_project.add_board(project_item)
                except Exception as e:
                    print(f"Error parsing items for {path} ({bom_file.sheet_name}): {e}")
                    self._board_status[path] = "Parse error"
                    project_item = ProjectItem(
                        file_path=path,
                        board_name=board_name,
                        board_quantity=1,
                        bom_items=[],
                    )
                    target_project.add_board(project_item)

        self._refresh_tree()
        self.files_changed.emit()

    def _save_workspace_clicked(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Workspace",
            "workspace.json",
            "JSON Files (*.json);;All Files (*)",
        )
        if path:
            if not path.lower().endswith(".json"):
                path += ".json"
            try:
                save_workspace(self._workspace, path)
                QMessageBox.information(self, "Success", f"Workspace saved to:\n{path}")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to save workspace:\n{e}")

    def _export_workspace_package_clicked(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Portable Workspace Package",
            "bom_workspace_package.zip",
            "ZIP Packages (*.zip)",
        )
        if not path:
            return
        if not path.lower().endswith(".zip"):
            path += ".zip"
        try:
            export_workspace_package(self._workspace, path)
            QMessageBox.information(
                self,
                "Portable Package Created",
                "Workspace and BOM files were packaged successfully:\n"
                f"{path}\n\nExtract the ZIP before loading workspace.json.",
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Package Export Error",
                f"Failed to create portable package:\n{exc}",
            )

    def _load_workspace_clicked(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load Workspace",
            "",
            "JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return
            
        try:
            temp_ws = load_workspace(path)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load workspace:\n{e}")
            return
            
        self._workspace = temp_ws
        
        self._parsed_boms.clear()
        self._bom_files.clear()
        self._board_status.clear()
        
        for project in self._workspace.projects:
            for board in project.board_items:
                if os.path.exists(board.file_path):
                    try:
                        if board.file_path in self._parsed_boms:
                            bom_file = self._parsed_boms[board.file_path]
                        else:
                            bom_file = self._parser.load_file(board.file_path)
                            self._parsed_boms[board.file_path] = bom_file
                            self._bom_files.append(bom_file)

                        board.bom_items = self._parse_items_for_board(
                            bom_file, board.board_name
                        )
                            
                        self._board_status[board.file_path] = f"Loaded ({len(board.bom_items)} items)"
                    except Exception as e:
                        print(f"Error parsing loaded file {board.file_path}: {e}")
                        self._board_status[board.file_path] = "Parse error"
                else:
                    print(f"File missing: {board.file_path}")
                    self._board_status[board.file_path] = "Missing file"
                
        self._refresh_tree()
        self.files_changed.emit()

    def get_bom_files(self) -> list[BomFile]:
        """Return unique cached file metadata without applying board context."""
        return self._bom_files

    def get_all_bom_files(self) -> list[BomFile]:
        """Return all currently parsed BOM files."""
        return self.get_bom_files()

    def get_bom_file_for_board(self, file_path: str, board_name: str) -> Optional[BomFile]:
        """Return an isolated BomFile carrying one board's display context."""
        bom_files = getattr(self, "_bom_files", [])
        for bf in bom_files:
            if bf.file_path == file_path and (
                bf.board_name == board_name
                or f"({bf.sheet_name})" in board_name
                or bf.sheet_name == board_name
            ):
                contextual_file = copy.deepcopy(bf)
                contextual_file.board_name = board_name
                return contextual_file

        bom_file = getattr(self, "_parsed_boms", {}).get(file_path)
        if bom_file is None:
            return None
        contextual_file = copy.deepcopy(bom_file)
        contextual_file.board_name = board_name
        return contextual_file

    def _parse_items_for_board(self, bom_file: BomFile, board_name: str):
        """Parse fresh, independent BomItem objects for a project board."""
        contextual_file = copy.deepcopy(bom_file)
        contextual_file.board_name = board_name
        return self._parser.parse_bom_items(contextual_file)

    def get_workspace(self) -> Workspace:
        return self._workspace

    def get_project(self) -> Project:
        """
        Legacy compatibility bridge for ProjectExcelWriter fallback.
        Returns a flattened synthetic project with unique temporary file paths.
        New code should use get_workspace().
        """
        synthetic_project = Project("Workspace")
        for project in self._workspace.projects:
            for board in project.board_items:
                # Clone the board to avoid mutating the real workspace
                import copy
                cloned_board = copy.deepcopy(board)
                cloned_board.file_path = f"{project.project_name}::{board.file_path}"
                cloned_board.board_name = f"[{project.project_name}] {board.board_name}"
                synthetic_project.add_board(cloned_board)
        return synthetic_project

    def has_files(self) -> bool:
        return self._workspace.has_boards()

    def _create_quantity_widget(self, board: ProjectItem) -> QWidget:
        wrapper = QWidget()
        wrapper_layout = QHBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.setSpacing(0)
        
        stepper = QWidget()
        stepper.setObjectName("stepperContainer")
        stepper.setFixedSize(90, 26)
        stepper.setStyleSheet("""
            QWidget#stepperContainer {
                background-color: #0f1d2a;
                border: 1px solid #24506f;
                border-radius: 4px;
            }
        """)
        
        layout = QHBoxLayout(stepper)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(2)
        
        btn_style = """
            QToolButton {
                background-color: transparent;
                color: #dceeff;
                border: none;
                font-weight: bold;
                font-size: 14px;
                border-radius: 4px;
            }
            QToolButton:hover {
                background-color: #1f4f70;
            }
            QToolButton:pressed {
                background-color: #176c93;
            }
        """

        btn_minus = QToolButton()
        btn_minus.setFixedSize(22, 22)
        btn_minus.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_minus.setStyleSheet(btn_style)
        btn_minus.setText("-")
        
        lbl_qty = QLabel(str(board.board_quantity))
        lbl_qty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_qty.setFixedWidth(24)
        lbl_qty.setStyleSheet("color: white; font-weight: bold; background: transparent; border: none; padding: 0px;")
        
        btn_plus = QToolButton()
        btn_plus.setFixedSize(22, 22)
        btn_plus.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_plus.setStyleSheet(btn_style)
        
        plus_icon_path = get_resource_path(os.path.join("resources", "plus.png"))
        if os.path.exists(plus_icon_path):
            btn_plus.setIcon(QIcon(plus_icon_path))
            btn_plus.setIconSize(QSize(14, 14))
        else:
            btn_plus.setText("+")

        layout.addWidget(btn_minus)
        layout.addWidget(lbl_qty)
        layout.addWidget(btn_plus)

        def update_qty(delta):
            new_qty = board.board_quantity + delta
            if new_qty >= 1:
                board.board_quantity = new_qty
                lbl_qty.setText(str(new_qty))
                self.files_changed.emit()

        btn_minus.clicked.connect(lambda: update_qty(-1))
        btn_plus.clicked.connect(lambda: update_qty(1))

        wrapper_layout.addStretch()
        wrapper_layout.addWidget(stepper)
        wrapper_layout.addStretch()
        
        return wrapper

    def _create_remove_button_widget(self, project_name: str, file_path: str) -> QWidget:
        wrapper = QWidget()
        layout = QHBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        remove_btn = QToolButton()
        remove_btn.setFixedSize(24, 24)
        remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_btn.setToolTip("Remove BOM from project")
        remove_btn.setAutoRaise(True)
        
        icon_path = get_resource_path(os.path.join("resources", "remove.png"))
        if os.path.exists(icon_path):
            remove_btn.setIcon(QIcon(icon_path))
            remove_btn.setIconSize(QSize(16, 16))
            remove_btn.setStyleSheet("""
                QToolButton {
                    background-color: transparent;
                    border: none;
                    border-radius: 4px;
                }
                QToolButton:hover {
                    background-color: #3d1c1c;
                }
                QToolButton:pressed {
                    background-color: #5c2020;
                }
            """)
        else:
            remove_btn.setText("×")
            remove_btn.setStyleSheet("""
                QToolButton {
                    background-color: transparent;
                    color: #e74c3c;
                    font-weight: bold;
                    font-size: 16px;
                    border: none;
                    border-radius: 4px;
                    padding-bottom: 2px;
                }
                QToolButton:hover {
                    background-color: #3d1c1c;
                }
                QToolButton:pressed {
                    background-color: #5c2020;
                }
            """)
        
        remove_btn.clicked.connect(lambda checked: self._remove_board(project_name, file_path))
        
        layout.addStretch()
        layout.addWidget(remove_btn)
        layout.addStretch()
        
        return wrapper
