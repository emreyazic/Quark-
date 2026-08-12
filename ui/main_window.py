# pyrefly: ignore-file
"""Main application window for the JLCPCB BOM Enrichment Tool.

Key additions vs. original:
- Manual MPN Search tab with JLCPCB + DigiKey search
- Updated results page with new status categories
- Background worker for manual search (non-blocking UI)
"""

import os
from datetime import datetime
from typing import Optional

from PyQt6.QtCore import Qt, QSize, QThread, pyqtSignal, QMutex
from PyQt6.QtGui import QColor, QFont, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.bom_parser import BomParser
from core.excel_writer import ExcelWriter, UnavailableReportWriter
from core.jlcpcb_searcher import JlcpcbSearcher, SearchWorker
from core.digikey_searcher import DigiKeySearcher
from core.mpn_utils import clean_mpn_value, is_exact_mpn_match, compute_required_stock
from models.bom_item import BomFile, BomItem
from ui.column_mapper_widget import ColumnMapperDialog
from ui.file_manager_widget import FileManagerWidget
from ui.progress_widget import ProgressWidget

APP_ID = "610325957269491714"
ACCESS_KEY = "8a568b68cf754f46ac0c279920f8e9cb"
SECRET_KEY = "mbEtcCNB28Nf5N1GgnbmPmpNVOKjBbjI"

# ═══════════════════════════════════════════════════════════════════
#  Manual Search Worker
# ═══════════════════════════════════════════════════════════════════

class ManualSearchWorker(QThread):
    """Background worker for manual MPN search (JLCPCB + DigiKey)."""

    results_ready = pyqtSignal(list)  # list of result dicts
    error = pyqtSignal(str)

    def __init__(self, mpn: str, parent=None):
        super().__init__(parent)
        self.mpn = mpn

    def run(self):
        results = []
        mpn_clean = clean_mpn_value(self.mpn)

        if not mpn_clean:
            self.error.emit("Please enter a valid MPN to search.")
            return

        # ── JLCPCB Search ────────────────────────────────────────
        try:
            jlc = JlcpcbSearcher(APP_ID, ACCESS_KEY, SECRET_KEY)
            jlc_result = jlc.search_mpn(mpn_clean, required_stock=0)

            if jlc_result.error:
                results.append({
                    "source": "JLCPCB",
                    "searched_mpn": mpn_clean,
                    "matched_mpn": "",
                    "manufacturer": "",
                    "part_number": "",
                    "stock": "",
                    "unit_price": "",
                    "match_type": "",
                    "status": "Error",
                    "notes": jlc_result.error,
                })
            elif jlc_result.exact_match:
                results.append({
                    "source": "JLCPCB",
                    "searched_mpn": mpn_clean,
                    "matched_mpn": jlc_result.matched_mpn,
                    "manufacturer": "",
                    "part_number": jlc_result.lcsc_code,
                    "stock": str(jlc_result.stock),
                    "unit_price": f"${jlc_result.unit_price:.6f}" if jlc_result.unit_price else "",
                    "match_type": "✅ Exact Match",
                    "status": "Found",
                    "notes": f"{jlc_result.category} / {jlc_result.package}",
                })
            else:
                # Show candidates
                if jlc_result.candidates:
                    for c in jlc_result.candidates:
                        is_exact = is_exact_mpn_match(mpn_clean, c["mpn"])
                        results.append({
                            "source": "JLCPCB",
                            "searched_mpn": mpn_clean,
                            "matched_mpn": c["mpn"],
                            "manufacturer": "",
                            "part_number": c["lcsc"],
                            "stock": str(c["stock"]),
                            "unit_price": "",
                            "match_type": "✅ Exact" if is_exact else "⚠ Partial",
                            "status": "Candidate",
                            "notes": c.get("category", ""),
                        })
                else:
                    results.append({
                        "source": "JLCPCB",
                        "searched_mpn": mpn_clean,
                        "matched_mpn": "",
                        "manufacturer": "",
                        "part_number": "",
                        "stock": "",
                        "unit_price": "",
                        "match_type": "",
                        "status": "Not Found",
                        "notes": "No results from JLCPCB",
                    })

            jlc.close()

        except Exception as e:
            results.append({
                "source": "JLCPCB",
                "searched_mpn": mpn_clean,
                "matched_mpn": "",
                "manufacturer": "",
                "part_number": "",
                "stock": "",
                "unit_price": "",
                "match_type": "",
                "status": "Error",
                "notes": str(e),
            })

        # ── DigiKey Search ───────────────────────────────────────
        try:
            dk = DigiKeySearcher()
            dk_result = dk.search_mpn(mpn_clean)

            if dk_result.error:
                results.append({
                    "source": "DigiKey",
                    "searched_mpn": mpn_clean,
                    "matched_mpn": "",
                    "manufacturer": "",
                    "part_number": "",
                    "stock": "",
                    "unit_price": "",
                    "match_type": "",
                    "status": "Not Configured" if not dk_result.configured else "Error",
                    "notes": dk_result.error,
                })
            elif dk_result.exact_match:
                results.append({
                    "source": "DigiKey",
                    "searched_mpn": mpn_clean,
                    "matched_mpn": dk_result.matched_mpn,
                    "manufacturer": dk_result.manufacturer,
                    "part_number": "",  # Blank for DigiKey
                    "stock": str(dk_result.stock),
                    "unit_price": f"${dk_result.unit_price:.4f}" if dk_result.unit_price else "",
                    "match_type": "✅ Exact Match",
                    "status": "Found",
                    "notes": dk_result.description,
                })
            elif dk_result.candidates:
                for c in dk_result.candidates:
                    is_exact = is_exact_mpn_match(mpn_clean, c["mpn"])
                    results.append({
                        "source": "DigiKey",
                        "searched_mpn": mpn_clean,
                        "matched_mpn": c["mpn"],
                        "manufacturer": c.get("manufacturer", ""),
                        "part_number": "",  # Blank for DigiKey
                        "stock": str(c.get("stock", "")),
                        "unit_price": "",
                        "match_type": "✅ Exact" if is_exact else "⚠ Partial",
                        "status": "Candidate",
                        "notes": "",
                    })
            else:
                results.append({
                    "source": "DigiKey",
                    "searched_mpn": mpn_clean,
                    "matched_mpn": "",
                    "manufacturer": "",
                    "part_number": "",
                    "stock": "",
                    "unit_price": "",
                    "match_type": "",
                    "status": "Not Found",
                    "notes": "No results from DigiKey",
                })

            dk.close()

        except Exception as e:
            results.append({
                "source": "DigiKey",
                "searched_mpn": mpn_clean,
                "matched_mpn": "",
                "manufacturer": "",
                "part_number": "",
                "stock": "",
                "unit_price": "",
                "match_type": "",
                "status": "Error",
                "notes": str(e),
            })

        self.results_ready.emit(results)


# ═══════════════════════════════════════════════════════════════════
#  Main Window
# ═══════════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    """Main window for the BOM Enrichment Tool."""

    def __init__(self):
        super().__init__()
        self._parser = BomParser()
        self._search_worker: Optional[SearchWorker] = None
        self._manual_search_worker: Optional[ManualSearchWorker] = None
        self._all_items: list[BomItem] = []
        self._project_aggregation_result = None
        self._search_item_component_keys: list[str] = []
        self._processed_project = None
        self._processed_workspace = None
        self._workspace_aggregation_result = None

        self.setWindowTitle("Workspace BOM Aggregation Tool")
        self.setMinimumSize(1200, 800)
        self.resize(1400, 900)

        self._setup_ui()

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(20, 16, 20, 16)
        main_layout.setSpacing(0)

        # ── Header ──────────────────────────────────────────────────
        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 12)

        # # Profile Image
        title_block = QVBoxLayout()
        title_label = QLabel("Workspace BOM Aggregation Tool")
        title_label.setObjectName("titleLabel")
        title_block.addWidget(title_label)

        subtitle = QLabel("Process Altium BOM files · Aggregate across workspace · Generate purchasing reports")
        subtitle.setObjectName("subtitleLabel")
        title_block.addWidget(subtitle)

        header_layout.addLayout(title_block)
        header_layout.addStretch()

        # Version badge
        version_label = QLabel("v1.0")
        version_label.setStyleSheet(
            "background: #1a6b8a; color: white; padding: 4px 14px; "
            "border-radius: 12px; font-size: 11px; font-weight: 600;"
        )
        header_layout.addWidget(version_label)

        main_layout.addWidget(header)

        # ── Separator ──────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background-color: #1e3a5f; max-height: 1px;")
        main_layout.addWidget(sep)
        main_layout.addSpacing(12)

        # ── Content Area (Stacked: Setup ↔ Progress ↔ Results) ──
        self._stack = QStackedWidget()
        main_layout.addWidget(self._stack, stretch=1)

        # Page 0: Setup
        self._setup_page = self._build_setup_page()
        self._stack.addWidget(self._setup_page)

        # Page 1: Progress
        self._progress_page = self._build_progress_page()
        self._stack.addWidget(self._progress_page)

        # Page 2: Results
        self._results_page = self._build_results_page()
        self._stack.addWidget(self._results_page)

        self._stack.setCurrentIndex(0)

        # ── Footer ──────────────────────────────────────────────────
        footer = QLabel("Powered by jlcsearch.tscircuit.com community API")
        footer.setStyleSheet(
            "color: #3a5068; font-size: 10px; padding: 8px 0 0 0; background: transparent;"
        )
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(footer)

    # ═══════════════════════════════════════════════════════════════
    #  PAGE 0: Setup
    # ═══════════════════════════════════════════════════════════════

    def _build_setup_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        # Splitter: Left (file manager) | Right (preview)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left panel — file manager
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 8, 0)

        self._file_manager = FileManagerWidget()
        self._file_manager.files_changed.connect(self._on_files_changed)
        left_layout.addWidget(self._file_manager)

        splitter.addWidget(left_panel)

        # Right panel — preview
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(8, 0, 0, 0)

        preview_title = QLabel("📊  Data Preview")
        preview_title.setObjectName("sectionTitle")
        right_layout.addWidget(preview_title)

        self._preview_table = QTableWidget()
        self._preview_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._preview_table.setAlternatingRowColors(True)
        self._preview_table.verticalHeader().setVisible(False)
        self._preview_table.setMinimumHeight(200)
        right_layout.addWidget(self._preview_table)

        self._preview_info = QLabel("Load BOM files to see a preview here")
        self._preview_info.setObjectName("statusLabel")
        right_layout.addWidget(self._preview_info)

        splitter.addWidget(right_panel)
        splitter.setSizes([450, 750])

        layout.addWidget(splitter, stretch=1)

        # ── Action bar ──────────────────────────────────────────
        action_bar = QFrame()
        action_bar.setStyleSheet(
            "QFrame { background: #14222f; border: 1px solid #1e3a5f; "
            "border-radius: 10px; }"
        )
        action_layout = QHBoxLayout(action_bar)
        action_layout.setContentsMargins(16, 12, 16, 12)
        action_layout.setSpacing(12)

        # Output path
        out_label = QLabel("Output:")
        out_label.setStyleSheet("color: #7ec8e3; font-weight: 600; background: transparent;")
        action_layout.addWidget(out_label)

        self._output_path = QLineEdit()
        self._output_path.setPlaceholderText("Select output Excel file path...")
        self._output_path.setReadOnly(True)
        action_layout.addWidget(self._output_path, stretch=1)

        btn_browse_out = QPushButton("Browse...")
        btn_browse_out.clicked.connect(self._browse_output)
        action_layout.addWidget(btn_browse_out)

        # Column mapping button
        self._btn_map_columns = QPushButton("🗂  Map Columns")
        self._btn_map_columns.setToolTip("Verify or override auto-detected column mappings")
        self._btn_map_columns.clicked.connect(self._open_column_mapper)
        self._btn_map_columns.setEnabled(False)
        action_layout.addWidget(self._btn_map_columns)

        # Process button
        self._btn_process = QPushButton("🚀  Process BOM")
        self._btn_process.setObjectName("btnProcess")
        self._btn_process.clicked.connect(self._start_processing)
        self._btn_process.setEnabled(False)
        action_layout.addWidget(self._btn_process)

        layout.addWidget(action_bar)

        return page

    # ═══════════════════════════════════════════════════════════════
    #  PAGE 1: Progress
    # ═══════════════════════════════════════════════════════════════

    def _build_progress_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)

        self._progress_widget = ProgressWidget()
        self._progress_widget.cancel_requested.connect(self._cancel_processing)
        layout.addWidget(self._progress_widget)

        # Back button (hidden during processing)
        btn_row = QHBoxLayout()
        self._btn_back_setup = QPushButton("← Back to Setup")
        self._btn_back_setup.clicked.connect(lambda: self._stack.setCurrentIndex(0))
        self._btn_back_setup.setVisible(False)
        btn_row.addWidget(self._btn_back_setup)
        btn_row.addStretch()

        self._btn_view_results = QPushButton("View Results →")
        self._btn_view_results.setObjectName("btnProcess")
        self._btn_view_results.clicked.connect(lambda: self._stack.setCurrentIndex(2))
        self._btn_view_results.setVisible(False)
        btn_row.addWidget(self._btn_view_results)

        layout.addLayout(btn_row)
        return page

    # ═══════════════════════════════════════════════════════════════
    #  PAGE 2: Results (with Manual Search tab)
    # ═══════════════════════════════════════════════════════════════

    def _build_results_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        # Title row
        title_row = QHBoxLayout()
        results_title = QLabel("📊  Results")
        results_title.setObjectName("sectionTitle")
        title_row.addWidget(results_title)
        title_row.addStretch()

        self._btn_open_excel = QPushButton("📂  Open Excel File")
        self._btn_open_excel.clicked.connect(self._open_output_file)
        title_row.addWidget(self._btn_open_excel)

        self._btn_open_unavail = QPushButton("⚠️  Open Unavailable Components")
        self._btn_open_unavail.clicked.connect(self._open_unavailable_file)
        title_row.addWidget(self._btn_open_unavail)

        btn_new = QPushButton("🔄  New Session")
        btn_new.clicked.connect(self._new_session)
        title_row.addWidget(btn_new)

        layout.addLayout(title_row)

        # Summary cards
        self._summary_frame = QFrame()
        self._summary_frame.setStyleSheet(
            "QFrame { background: #14222f; border: 1px solid #1e3a5f; border-radius: 10px; }"
        )
        summary_layout = QHBoxLayout(self._summary_frame)
        summary_layout.setContentsMargins(16, 16, 16, 16)
        summary_layout.setSpacing(12)

        self._card_total = self._make_stat_card("Total", "0", "#1a6b8a")
        self._card_found = self._make_stat_card("Found", "0", "#00b894")
        self._card_not_found = self._make_stat_card("Not Found", "0", "#e74c3c")
        self._card_mismatch = self._make_stat_card("Mismatch", "0", "#f39c12")
        self._card_insuff = self._make_stat_card("Low Stock", "0", "#e67e22")
        self._card_manual = self._make_stat_card("Manual", "0", "#6c5ce7")

        summary_layout.addWidget(self._card_total)
        summary_layout.addWidget(self._card_found)
        summary_layout.addWidget(self._card_not_found)
        summary_layout.addWidget(self._card_mismatch)
        summary_layout.addWidget(self._card_insuff)
        summary_layout.addWidget(self._card_manual)

        layout.addWidget(self._summary_frame)

        # Tabs with filtered views + manual search
        self._result_tabs = QTabWidget()

        self._tab_all = QTableWidget()
        self._tab_found = QTableWidget()
        self._tab_not_found = QTableWidget()
        self._tab_manual = QTableWidget()

        for table in [self._tab_all, self._tab_found, self._tab_not_found, self._tab_manual]:
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setAlternatingRowColors(True)
            table.verticalHeader().setVisible(False)
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            table.setSortingEnabled(True)

        self._result_tabs.addTab(self._tab_all, "All Components")
        self._result_tabs.addTab(self._tab_found, "✅ Available")
        self._result_tabs.addTab(self._tab_not_found, "❌ Not Found / Mismatch")
        self._result_tabs.addTab(self._tab_manual, "🔍 Manual Review")

        # ── Manual MPN Search tab ────────────────────────────────
        self._manual_search_tab = self._build_manual_search_tab()
        self._result_tabs.addTab(self._manual_search_tab, "🔎 Manual MPN Search")

        layout.addWidget(self._result_tabs, stretch=1)

        return page

    def _build_manual_search_tab(self) -> QWidget:
        """Build the manual MPN search panel."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)

        # Search bar
        search_bar = QFrame()
        search_bar.setStyleSheet(
            "QFrame { background: #14222f; border: 1px solid #1e3a5f; "
            "border-radius: 10px; }"
        )
        search_layout = QHBoxLayout(search_bar)
        search_layout.setContentsMargins(16, 12, 16, 12)
        search_layout.setSpacing(12)

        search_label = QLabel("🔎  MPN:")
        search_label.setStyleSheet("color: #7ec8e3; font-weight: 600; font-size: 14px; background: transparent;")
        search_layout.addWidget(search_label)

        self._manual_mpn_input = QLineEdit()
        self._manual_mpn_input.setPlaceholderText("Enter Manufacturer Part Number to search...")
        self._manual_mpn_input.returnPressed.connect(self._do_manual_search)
        search_layout.addWidget(self._manual_mpn_input, stretch=1)

        self._btn_manual_search = QPushButton("🔍  Search")
        self._btn_manual_search.setObjectName("btnProcess")
        self._btn_manual_search.clicked.connect(self._do_manual_search)
        search_layout.addWidget(self._btn_manual_search)

        layout.addWidget(search_bar)

        # Search status
        self._manual_search_status = QLabel("Enter an MPN and click Search to query JLCPCB and DigiKey")
        self._manual_search_status.setObjectName("statusLabel")
        layout.addWidget(self._manual_search_status)

        # Results table
        self._manual_results_table = QTableWidget()
        self._manual_results_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._manual_results_table.setAlternatingRowColors(True)
        self._manual_results_table.verticalHeader().setVisible(False)
        self._manual_results_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)

        manual_headers = [
            "Source", "Searched MPN", "Matched MPN", "Manufacturer",
            "JLCPCB Part Number", "Unit Price", "Match Type", "Status"
        ]
        self._manual_results_table.setColumnCount(len(manual_headers))
        self._manual_results_table.setHorizontalHeaderLabels(manual_headers)
        self._manual_results_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )

        layout.addWidget(self._manual_results_table, stretch=1)

        return tab

    def _make_stat_card(self, label: str, value: str, color: str) -> QFrame:
        """Create a summary stat card widget."""
        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background: transparent; border: 1px solid {color}; "
            f"border-radius: 8px; padding: 6px; }}"
        )
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 8, 12, 8)
        card_layout.setSpacing(4)

        val_label = QLabel(value)
        val_label.setStyleSheet(
            f"font-size: 28px; font-weight: 700; color: {color}; background: transparent;"
        )
        val_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        val_label.setObjectName(f"cardValue_{label.replace(' ', '')}")
        card_layout.addWidget(val_label)

        name_label = QLabel(label)
        name_label.setStyleSheet(
            "font-size: 11px; color: #6b8299; font-weight: 500; background: transparent;"
        )
        name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(name_label)

        return card

    # ═══════════════════════════════════════════════════════════════
    #  Slots / Logic
    # ═══════════════════════════════════════════════════════════════

    def _on_files_changed(self):
        """Called when files are added or removed."""
        has_files = self._file_manager.has_files()
        self._btn_map_columns.setEnabled(has_files)

        # Update preview
        bom_files = self._file_manager.get_bom_files()
        if bom_files:
            self._show_preview(bom_files[0])

            # Auto-set output path
            if not self._output_path.text():
                first_dir = os.path.dirname(bom_files[0].file_path)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                default_name = f"BOM_Enriched_{timestamp}.xlsx"
                self._output_path.setText(os.path.join(first_dir, default_name))
        else:
            self._preview_table.setRowCount(0)
            self._preview_table.setColumnCount(0)
            self._preview_info.setText("Load BOM files to see a preview here")

        self._validate_ready()

    def _show_preview(self, bom_file: BomFile):
        """Show a preview of the first BOM file."""
        headers = bom_file.headers
        rows = bom_file.preview_rows

        self._preview_table.setColumnCount(len(headers))
        self._preview_table.setHorizontalHeaderLabels(headers)
        self._preview_table.setRowCount(len(rows))

        for row_idx, row_data in enumerate(rows):
            for col_idx, val in enumerate(row_data):
                item = QTableWidgetItem(str(val))
                self._preview_table.setItem(row_idx, col_idx, item)

                # Highlight MPN column
                if (bom_file.column_mapping
                        and bom_file.column_mapping.mpn == col_idx):
                    item.setBackground(QColor("#1a4a3a"))
                # Highlight Manufacturer column
                elif (bom_file.column_mapping
                        and bom_file.column_mapping.manufacturer == col_idx):
                    item.setBackground(QColor("#3a1a4a"))

        self._preview_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self._preview_info.setText(
            f"Showing preview of: {os.path.basename(bom_file.file_path)} "
            f"({bom_file.row_count} rows)"
        )

    def _browse_output(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Enriched BOM",
            self._output_path.text() or "",
            "Excel Files (*.xlsx);;All Files (*)",
        )
        if path:
            if not path.lower().endswith(".xlsx"):
                path += ".xlsx"
            self._output_path.setText(path)
            self._validate_ready()

    def _validate_ready(self):
        """Enable/disable the Process button based on readiness."""
        ready = (
            self._file_manager.has_files()
            and bool(self._output_path.text())
        )
        self._btn_process.setEnabled(ready)

    def _open_column_mapper(self):
        """Open column mapping dialogs for all loaded files."""
        bom_files = self._file_manager.get_bom_files()
        for bf in bom_files:
            dlg = ColumnMapperDialog(bf, self)
            result = dlg.exec()
            if result != ColumnMapperDialog.DialogCode.Accepted:
                return

        # Refresh preview
        if bom_files:
            self._show_preview(bom_files[0])

        QMessageBox.information(
            self, "Column Mapping",
            f"Column mappings confirmed for {len(bom_files)} file(s)."
        )

    def _clear_processed_state(self):
        """Clear state from previous processing runs."""
        self._processed_workspace = None
        self._workspace_aggregation_result = None
        self._processed_project = None
        self._project_aggregation_result = None
        self._search_item_component_keys = []

    def _start_processing(self):
        """Parse all BOM files, then start JLCPCB search."""
        self._clear_processed_state()
        bom_files = self._file_manager.get_all_bom_files()

        # Check mappings
        needs_mapping = []
        for bf in bom_files:
            if bf.column_mapping is None or not bf.column_mapping.is_valid():
                needs_mapping.append(bf)

        if needs_mapping:
            reply = QMessageBox.question(
                self, "Column Mapping Required",
                f"{len(needs_mapping)} file(s) need column mapping. "
                "Open the column mapper now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            )
            if reply == QMessageBox.StandardButton.Yes:
                for bf in needs_mapping:
                    dlg = ColumnMapperDialog(bf, self)
                    result = dlg.exec()
                    if result != ColumnMapperDialog.DialogCode.Accepted:
                        return
            else:
                return

        # Get Workspace
        workspace = self._file_manager.get_workspace()
        
        if not workspace.projects:
            QMessageBox.warning(self, "Empty Workspace", "Please add at least one project and BOM file.")
            return
            
        if not workspace.has_boards():
            QMessageBox.warning(self, "Empty Workspace", "Workspace contains no BOM files.")
            return

        # Re-parse items with latest mappings
        for project in workspace.projects:
            for board in project.board_items:
                bf = next((b for b in bom_files if b.file_path == board.file_path), None)
                if bf:
                    try:
                        board.bom_items = self._parser.parse_bom_items(bf)
                    except Exception as e:
                        QMessageBox.critical(
                            self, "Parse Error",
                            f"Failed to parse {bf.file_path}:\n{e}"
                        )
                        return
                else:
                    QMessageBox.critical(
                        self, "Missing File",
                        f"Failed to find loaded file: {board.file_path}"
                    )
                    return

        from services.project_aggregation import aggregate_workspace, aggregate_project
        import copy
        
        # New Workspace processing state
        self._workspace_aggregation_result = aggregate_workspace(workspace)
        self._processed_workspace = copy.deepcopy(workspace)
        
        # Temporary bridge for ProjectExcelWriter compatibility
        from models.project import Project, ProjectItem
        self._processed_project = Project("Workspace Export")
        for proj in workspace.projects:
            for board in proj.board_items:
                cloned_board = copy.deepcopy(board)
                cloned_board.file_path = f"{proj.project_name}::{board.file_path}"
                cloned_board.board_name = f"[{proj.project_name}] {board.board_name}"
                self._processed_project.add_board(cloned_board)
        self._project_aggregation_result = aggregate_project(self._processed_project)
        
        # Warnings
        if self._workspace_aggregation_result.warnings:
            warns = self._workspace_aggregation_result.warnings[:10]
            if len(self._workspace_aggregation_result.warnings) > 10:
                warns.append(f"...and {len(self._workspace_aggregation_result.warnings) - 10} more.")
            warn_msg = "\n".join(warns)
            QMessageBox.warning(self, "Aggregation Warnings", f"Some items were skipped during aggregation:\n\n{warn_msg}")
            
        # Search Items extraction
        self._all_items = []
        self._search_item_component_keys = []
        for comp in self._workspace_aggregation_result.components:
            search_item = copy.deepcopy(comp.representative_item)
            search_item.quantity = int(comp.total_quantity)
            self._all_items.append(search_item)
            self._search_item_component_keys.append(comp.component_key)

        if not self._all_items:
            QMessageBox.warning(self, "No Data", "No valid BOM items found to process.")
            return

        # Switch to progress page
        self._stack.setCurrentIndex(1)
        self._btn_back_setup.setVisible(False)
        self._btn_view_results.setVisible(False)
        self._progress_widget.reset(len(self._all_items))

        # Start search worker
        self._search_worker = SearchWorker(self._all_items, APP_ID, ACCESS_KEY, SECRET_KEY)
        self._search_worker.progress.connect(self._on_search_progress)
        self._search_worker.finished_all.connect(self._on_search_finished)
        self._search_worker.error.connect(self._on_search_error)
        self._search_worker.start()

    def _on_search_progress(self, current: int, total: int, mpn: str, status: str):
        self._progress_widget.update_progress(current, total, mpn, status)

    def _on_search_finished(self, items: list[BomItem]):
        self._all_items = items
        self._progress_widget.set_finished(True)
        self._btn_back_setup.setVisible(True)
        self._btn_view_results.setVisible(True)

        # Write Excel output
        output_path = self._output_path.text()
        try:
            if self._processed_workspace and self._workspace_aggregation_result and self._search_item_component_keys:
                from core.workspace_excel_writer import WorkspaceExcelWriter
                writer = WorkspaceExcelWriter(
                    workspace=self._processed_workspace,
                    aggregation_result=self._workspace_aggregation_result,
                    enriched_items=self._all_items,
                    component_keys=self._search_item_component_keys
                )
                writer.write(output_path)
            elif self._project_aggregation_result and self._search_item_component_keys and self._processed_project:
                from core.project_excel_writer import ProjectExcelWriter
                writer = ProjectExcelWriter(
                    project=self._processed_project,
                    aggregation_result=self._project_aggregation_result,
                    enriched_items=self._all_items,
                    component_keys=self._search_item_component_keys
                )
                writer.write(output_path)
            else:
                writer = ExcelWriter(self._all_items)
                writer.write(output_path)
            self._progress_widget._log.appendPlainText(
                f"\n✅  Main Excel saved to: {output_path}"
            )

            # 2. Unavailable Components Report
            unavail_writer = UnavailableReportWriter(self._all_items)
            if unavail_writer.unavailable_items:
                base_dir = os.path.dirname(output_path)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                unavail_path = os.path.join(base_dir, f"JLCPCB_Not_Found_And_Unavailable_Components_{timestamp}.xlsx")
                unavail_writer.write(unavail_path)
                self._last_unavail_path = unavail_path
                self._progress_widget._log.appendPlainText(
                    f"\n✅  Unavailable Report saved to: {unavail_path}"
                )
            else:
                self._last_unavail_path = None
                self._progress_widget._log.appendPlainText(
                    f"\n🎉  No unavailable components found! Skipped separate report."
                )

        except Exception as e:
            self._progress_widget._log.appendPlainText(
                f"\n❌  Failed to save Excel: {e}"
            )
            QMessageBox.critical(self, "Save Error", f"Failed to save Excel:\n{e}")

        # Populate results page
        self._populate_results()

    def _on_search_error(self, error_msg: str):
        self._progress_widget.set_finished(False)
        self._progress_widget._log.appendPlainText(f"\n❌  Error: {error_msg}")
        self._btn_back_setup.setVisible(True)
        QMessageBox.critical(self, "Search Error", f"An error occurred:\n{error_msg}")

    def _cancel_processing(self):
        if self._search_worker and self._search_worker.isRunning():
            self._search_worker.cancel()
            self._progress_widget.set_cancelled()
            self._btn_back_setup.setVisible(True)

    def _populate_results(self):
        """Fill the results page with processed data."""
        items = self._all_items
        headers = BomItem.get_headers()

        # Summary cards
        total = len(items)
        found = sum(1 for i in items if i.status == "" and i.jlcpcb_part_number)
        not_found_count = sum(1 for i in items if i.status == "JLCPCB not found")
        mismatch = sum(1 for i in items if i.status == "No exact JLCPCB match")
        insuff = sum(1 for i in items if i.status == "Insufficient JLCPCB stock")
        manual = total - found - not_found_count - mismatch - insuff

        self._update_card_value(self._card_total, str(total))
        self._update_card_value(self._card_found, str(found))
        self._update_card_value(self._card_not_found, str(not_found_count))
        self._update_card_value(self._card_mismatch, str(mismatch))
        self._update_card_value(self._card_insuff, str(insuff))
        self._update_card_value(self._card_manual, str(manual))

        # All items tab
        self._fill_result_table(self._tab_all, items, headers)

        # Found tab
        found_items = [i for i in items if i.status == "" and i.jlcpcb_part_number]
        self._fill_result_table(self._tab_found, found_items, headers)

        # Not found / mismatch tab
        nf_items = [
            i for i in items
            if i.status in ("JLCPCB not found", "No exact JLCPCB match", "Insufficient JLCPCB stock")
        ]
        self._fill_result_table(self._tab_not_found, nf_items, headers)

        # Manual review tab
        manual_items = [
            i for i in items
            if i.status not in ("JLCPCB not found", "No exact JLCPCB match", "Insufficient JLCPCB stock") and not (i.status == "" and i.jlcpcb_part_number)
        ]
        self._fill_result_table(self._tab_manual, manual_items, headers)

        # Update tab labels with counts
        self._result_tabs.setTabText(0, f"All Components ({total})")
        self._result_tabs.setTabText(1, f"✅ Available ({found})")
        self._result_tabs.setTabText(2, f"❌ Not Found / Mismatch ({len(nf_items)})")
        self._result_tabs.setTabText(3, f"🔍 Manual Review ({len(manual_items)})")

    def _fill_result_table(
        self, table: QTableWidget, items: list[BomItem], headers: list[str]
    ):
        """Fill a QTableWidget with BomItem data."""
        table.setSortingEnabled(False)
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setRowCount(len(items))

        status_col = next(
            (i for i, h in enumerate(headers) if h == "Status"), None
        )

        for row_idx, item in enumerate(items):
            row_data = item.to_row()
            for col_idx, val in enumerate(row_data):
                cell = QTableWidgetItem(str(val) if val is not None else "")
                # Make numeric columns sortable
                if isinstance(val, (int, float)) and val is not None:
                    cell.setData(Qt.ItemDataRole.EditRole, val)
                table.setItem(row_idx, col_idx, cell)

                # Color-code by status (apply to whole row)
                color = self._status_color(item.status)
                if color:
                    cell.setBackground(color)

        table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        table.setSortingEnabled(True)

    def _status_color(self, status: str) -> Optional[QColor]:
        status_text = status or ""
        if status_text == "":
            return QColor("#1a4a3a")
        elif status_text == "JLCPCB not found":
            return QColor("#4a1a1a")
        elif status_text == "No exact JLCPCB match":
            return QColor("#4a4a1a")
        elif status_text == "Insufficient JLCPCB stock":
            return QColor("#4a3a1a")
        elif "error" in status_text.lower():
            return QColor("#6a1a1a")
        else:
            return QColor("#2a2a4a")

    def _update_card_value(self, card: QFrame, value: str):
        """Update the value label in a stat card."""
        for child in card.findChildren(QLabel):
            if child.objectName().startswith("cardValue_"):
                child.setText(value)
                break

    # ── Manual MPN Search ────────────────────────────────────────

    def _do_manual_search(self):
        """Start a manual MPN search in the background."""
        mpn = self._manual_mpn_input.text().strip()
        if not mpn:
            QMessageBox.warning(self, "Empty MPN", "Please enter an MPN to search.")
            return

        self._btn_manual_search.setEnabled(False)
        self._manual_search_status.setText(f"🔍 Searching for '{mpn}'...")
        self._manual_results_table.setRowCount(0)

        self._manual_search_worker = ManualSearchWorker(mpn)
        self._manual_search_worker.results_ready.connect(self._on_manual_results)
        self._manual_search_worker.error.connect(self._on_manual_error)
        self._manual_search_worker.start()

    def _on_manual_results(self, results: list[dict]):
        """Handle manual search results."""
        self._btn_manual_search.setEnabled(True)

        if not results:
            self._manual_search_status.setText("No results found.")
            return

        self._manual_search_status.setText(f"Found {len(results)} result(s)")
        self._manual_results_table.setRowCount(len(results))

        columns = [
            "source", "searched_mpn", "matched_mpn", "manufacturer",
            "part_number", "unit_price", "match_type", "status"
        ]

        for row_idx, result in enumerate(results):
            for col_idx, key in enumerate(columns):
                val = str(result.get(key, ""))
                cell = QTableWidgetItem(val)
                self._manual_results_table.setItem(row_idx, col_idx, cell)

                # Color-code status
                status = result.get("status", "")
                if status == "Found":
                    cell.setBackground(QColor("#1a4a3a"))
                elif status == "Candidate":
                    cell.setBackground(QColor("#4a4a1a"))
                elif status == "Not Found":
                    cell.setBackground(QColor("#4a1a1a"))
                elif status in ("Error", "Not Configured"):
                    cell.setBackground(QColor("#3a1a4a"))

    def _on_manual_error(self, error_msg: str):
        """Handle manual search error."""
        self._btn_manual_search.setEnabled(True)
        self._manual_search_status.setText(f"❌ Error: {error_msg}")

    # ── File Operations ──────────────────────────────────────────

    def _open_output_file(self):
        """Open the output Excel file with the system default application."""
        path = self._output_path.text()
        if path and os.path.exists(path):
            os.startfile(path)
        else:
            QMessageBox.warning(
                self, "File Not Found",
                "The output file was not found. Please process the BOM first."
            )

    def _open_unavailable_file(self):
        """Open the unavailable components report with the system default application."""
        if not hasattr(self, '_last_unavail_path') or not self._last_unavail_path:
            QMessageBox.information(
                self, "No Report",
                "No unavailable components report was generated in the last run (all components might have been found)."
            )
            return

        if os.path.exists(self._last_unavail_path):
            os.startfile(self._last_unavail_path)
        else:
            QMessageBox.warning(
                self, "File Not Found",
                "The unavailable components report file was not found."
            )

    def _new_session(self):
        """Reset to start a new processing session."""
        self._clear_processed_state()
        self._all_items.clear()
        self._stack.setCurrentIndex(0)
