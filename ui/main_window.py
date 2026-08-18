"""Main application window for the JLCPCB BOM Enrichment Tool."""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional
from openpyxl import load_workbook

from PyQt6.QtCore import Qt, QSize, QThread, pyqtSignal, QMutex, QTimer
from PyQt6.QtGui import QColor, QFont, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QInputDialog,
    QPushButton,
    QSizePolicy,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.bom_parser import BomParser
from core.excel_writer import ExcelWriter, UnavailableReportWriter
from core.jlcpcb_searcher import JlcpcbSearcher, SearchWorker, LibrarySyncWorker
from core.digikey_searcher import DigiKeySearcher
from core.mpn_utils import clean_mpn_value, is_exact_mpn_match, compute_required_stock
from models.bom_item import BomFile, BomItem
from ui.column_mapper_widget import ColumnMapperDialog
from ui.file_manager_widget import FileManagerWidget
from ui.progress_widget import ProgressWidget
from ui.approval_dialog import ApprovalDialog
from ui.component_library_conflict_dialog import ComponentLibraryConflictDialog
from core.component_library import read_component_library_file, detect_library_conflicts
from core.database_manager import DatabaseManager
from core.logger import get_logger
from ui.results_model import ResultsFilterProxy, ResultsTableModel

logger = get_logger(__name__)

APP_ID = os.getenv("JLCPCB_APP_ID", "610325957269491714")
ACCESS_KEY = os.getenv("JLCPCB_ACCESS_KEY", "8a568b68cf754f46ac0c279920f8e9cb")
SECRET_KEY = os.getenv("JLCPCB_SECRET_KEY", "mbEtcCNB28Nf5N1GgnbmPmpNVOKjBbjI")

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
        self._cancelled = threading.Event()

    def cancel(self):
        self._cancelled.set()

    def run(self):
        results = []
        mpn_clean = clean_mpn_value(self.mpn)

        if not mpn_clean:
            self.error.emit("Please enter a valid MPN to search.")
            return
        if self._cancelled.is_set():
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
        finally:
            if "jlc" in locals():
                jlc.close()

        if self._cancelled.is_set():
            return

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
                    "part_number": dk_result.digikey_part_number,
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
                        "part_number": c.get("digikey_pn", ""),
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
        finally:
            if "dk" in locals():
                dk.close()

        if not self._cancelled.is_set():
            self.results_ready.emit(results)


class MappingRefreshWorker(QThread):
    """Checks mappings against the last automatic result, not the approved value."""

    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(int)
    error = pyqtSignal(str)
    warning = pyqtSignal(str)

    def __init__(self, db_manager: DatabaseManager, parent=None):
        super().__init__(parent)
        self.db_manager = db_manager
        self._cancelled = threading.Event()

    def cancel(self):
        self._cancelled.set()

    def run(self):
        searcher = JlcpcbSearcher(APP_ID, ACCESS_KEY, SECRET_KEY, self.db_manager)
        digikey_searcher = DigiKeySearcher()
        updated_count = 0
        lookup_errors = []
        try:
            mappings = self.db_manager.get_all_internal_mappings()
            eligible_mappings = [mapping for mapping in mappings if mapping.get("mpn", "").strip()]
            total = len(eligible_mappings)

            for index, mapping in enumerate(eligible_mappings, start=1):
                if self._cancelled.is_set():
                    return
                comment_code = mapping["comment_code"]
                mpn = mapping["mpn"].strip()
                self.progress.emit(index, total, f"Refreshing {comment_code} ({mpn})")

                # Resolve only the MPN→LCSC mapping. A lookup failure must not
                # clear the existing code, which is enforced in the DB method.
                digikey_result = None
                try:
                    resolved_lcsc = searcher._resolve_lcsc_from_mpn(mpn)
                    lcsc_code = None if searcher.last_resolution_failed else (resolved_lcsc or "")
                except Exception as exc:
                    lcsc_code = None
                    lookup_errors.append(f"{comment_code} / JLCPCB: {exc}")
                if getattr(searcher, "last_resolution_failed", False):
                    lookup_errors.append(
                        f"{comment_code} / JLCPCB: "
                        f"{getattr(searcher, 'last_resolution_error', None) or 'lookup failed'}"
                    )

                try:
                    digikey_result = digikey_searcher.search_mpn(mpn, include_live_data=False)
                    digikey_code = (
                        None if digikey_result.error
                        else (digikey_result.digikey_part_number or "")
                    )
                except Exception as exc:
                    digikey_code = None
                    lookup_errors.append(f"{comment_code} / DigiKey: {exc}")
                if digikey_result is not None and digikey_result.error:
                    lookup_errors.append(
                        f"{comment_code} / DigiKey: {digikey_result.error}"
                    )

                if self.db_manager.refresh_mapping_codes(comment_code, lcsc_code, digikey_code):
                    updated_count += 1

            if not self._cancelled.is_set():
                if lookup_errors:
                    self.warning.emit(self._format_api_warnings(lookup_errors))
                self.finished.emit(updated_count)
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            searcher.close()
            digikey_searcher.close()

    @staticmethod
    def _format_api_warnings(errors):
        visible = errors[:10]
        if len(errors) > len(visible):
            visible.append(f"... and {len(errors) - len(visible)} more")
        return "\n".join(visible)


class ComponentLibraryImportWorker(QThread):
    """Imports Altium library rows and searches supplier codes without processing a BOM."""

    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(int, int, int)  # searched, pending/changed, skipped
    error = pyqtSignal(str)
    warning = pyqtSignal(str)

    INVALID_MPN_VALUES = {"", "*", "-", "N/A", "NA", "NONE"}
    LOOKUP_CACHE_MAX_AGE = 24 * 60 * 60
    LCSC_MAX_WORKERS = 8
    DIGIKEY_MAX_WORKERS = 4

    def __init__(
        self,
        file_path: str,
        db_manager: DatabaseManager,
        parent=None,
        pre_resolved_components: Optional[list[tuple[str, str]]] = None,
        pre_resolved_skipped: int = 0,
    ):
        super().__init__(parent)
        self.file_path = file_path
        self.db_manager = db_manager
        self.pre_resolved_components = pre_resolved_components
        self.pre_resolved_skipped = pre_resolved_skipped
        self._cancelled = threading.Event()

    def cancel(self):
        self._cancelled.set()

    @staticmethod
    def _normalize_header(value) -> str:
        return " ".join(str(value or "").strip().upper().split())

    def _read_components(self) -> tuple[list[tuple[str, str]], int]:
        if self.pre_resolved_components is not None:
            return self.pre_resolved_components, self.pre_resolved_skipped

        raw_rows, invalid_skipped = read_component_library_file(self.file_path)
        clean_components, conflicts, merged_duplicates = detect_library_conflicts(
            raw_rows, self.db_manager
        )
        total_skipped = invalid_skipped + merged_duplicates + len(conflicts)
        return clean_components, total_skipped

    def run(self):
        jlc_searchers = []
        digikey_searchers = []
        try:
            components, skipped = self._read_components()
            if not components:
                raise ValueError("No valid component rows with an internal code and MPN were found.")

            # Search each MPN only once even when multiple internal codes use it.
            unique_mpns = {}
            for _, mpn in components:
                unique_mpns.setdefault(mpn.upper(), mpn)

            results = {}
            network_jobs = []
            cached_supplier_results = 0
            for key, mpn in unique_mpns.items():
                if self._cancelled.is_set():
                    return
                cached = self.db_manager.get_mpn_lookup_cache(mpn, self.LOOKUP_CACHE_MAX_AGE)
                result = {"mpn": mpn, "lcsc": None, "digikey": None}
                if cached and cached["lcsc_fresh"]:
                    result["lcsc"] = cached["lcsc_code"]
                    cached_supplier_results += 1
                else:
                    network_jobs.append(("lcsc", key, mpn))
                if cached and cached["digikey_fresh"]:
                    result["digikey"] = cached["digikey_code"]
                    cached_supplier_results += 1
                else:
                    network_jobs.append(("digikey", key, mpn))
                results[key] = result

            local_state = threading.local()
            searcher_lock = threading.Lock()
            api_errors = []
            digikey_worker_counter = 0

            def search_lcsc(mpn: str) -> Optional[str]:
                if not hasattr(local_state, "jlc_searcher"):
                    local_state.jlc_searcher = JlcpcbSearcher(
                        APP_ID, ACCESS_KEY, SECRET_KEY, self.db_manager
                    )
                    with searcher_lock:
                        jlc_searchers.append(local_state.jlc_searcher)
                resolved = local_state.jlc_searcher._resolve_lcsc_from_mpn(mpn)
                if getattr(local_state.jlc_searcher, "last_resolution_failed", False):
                    raise RuntimeError(
                        local_state.jlc_searcher.last_resolution_error
                        or "JLCPCB lookup failed"
                    )
                return resolved or ""

            def search_digikey(mpn: str) -> Optional[str]:
                nonlocal digikey_worker_counter
                if not hasattr(local_state, "digikey_searcher"):
                    with searcher_lock:
                        credential_index = digikey_worker_counter
                        digikey_worker_counter += 1
                    local_state.digikey_searcher = DigiKeySearcher(
                        credential_start_index=credential_index
                    )
                    with searcher_lock:
                        digikey_searchers.append(local_state.digikey_searcher)
                result = local_state.digikey_searcher.search_mpn(mpn, include_live_data=False)
                if result.error:
                    raise RuntimeError(result.error)
                if not result.configured:
                    return None
                return result.digikey_part_number or ""

            lcsc_executor = ThreadPoolExecutor(
                max_workers=self.LCSC_MAX_WORKERS, thread_name_prefix="lcsc-import"
            )
            digikey_executor = ThreadPoolExecutor(
                max_workers=self.DIGIKEY_MAX_WORKERS, thread_name_prefix="digikey-import"
            )
            try:
                future_jobs = {}
                for source, key, mpn in network_jobs:
                    executor = lcsc_executor if source == "lcsc" else digikey_executor
                    function = search_lcsc if source == "lcsc" else search_digikey
                    future_jobs[executor.submit(function, mpn)] = (source, key, mpn)

                total_jobs = len(future_jobs)
                cache_updates = []
                for completed, future in enumerate(as_completed(future_jobs), start=1):
                    if self._cancelled.is_set():
                        for pending_future in future_jobs:
                            pending_future.cancel()
                        return
                    source, key, mpn = future_jobs[future]
                    try:
                        value = future.result()
                    except Exception as exc:
                        value = None
                        api_errors.append(f"{mpn} / {source}: {exc}")
                    results[key][source] = value
                    if value is not None:
                        if source == "lcsc":
                            cache_updates.append((mpn, value, None))
                        else:
                            cache_updates.append((mpn, None, value))
                    self.progress.emit(
                        completed,
                        total_jobs,
                        f"Searching suppliers for {mpn} ({cached_supplier_results} cached results)",
                    )
                self.db_manager.bulk_upsert_mpn_lookup_cache(cache_updates)
            finally:
                lcsc_executor.shutdown(wait=True)
                digikey_executor.shutdown(wait=True)

            pending_count = 0
            total = len(components)
            existing_by_code = {
                mapping["comment_code"]: mapping
                for mapping in self.db_manager.get_all_internal_mappings()
            }
            new_pending_records = []

            for index, (internal_code, mpn) in enumerate(components, start=1):
                if self._cancelled.is_set():
                    return
                lookup = results[mpn.upper()]
                lcsc_code = lookup["lcsc"]
                digikey_code = lookup["digikey"]

                existing = existing_by_code.get(internal_code)
                if existing is None:
                    new_pending_records.append(
                        (
                            internal_code,
                            mpn,
                            lcsc_code or "",
                            digikey_code or "",
                        )
                    )
                    pending_count += 1
                elif self.db_manager.refresh_mapping_codes(internal_code, lcsc_code, digikey_code):
                    pending_count += 1
                if index % 25 == 0 or index == total:
                    self.progress.emit(index, total, f"Saving pending mappings ({index}/{total})")

            self.db_manager.bulk_insert_new_pending_suggestions(new_pending_records)

            if api_errors:
                self.warning.emit(MappingRefreshWorker._format_api_warnings(api_errors))
            self.finished.emit(total, pending_count, skipped)
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            for searcher in jlc_searchers:
                searcher.close()
            for searcher in digikey_searchers:
                searcher.close()


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
        self._mapping_refresh_worker: Optional[MappingRefreshWorker] = None
        self._component_library_import_worker: Optional[ComponentLibraryImportWorker] = None
        self._sync_worker: Optional[LibrarySyncWorker] = None
        self._approval_dialog: Optional[ApprovalDialog] = None
        self._all_items: list[BomItem] = []
        self._project_aggregation_result = None
        self._search_item_component_keys: list[str] = []
        self._processed_project = None
        self._processed_workspace = None
        self._workspace_aggregation_result = None
        self._input_revision = 0
        self._processed_input_revision = None
        self._build_quantity = 1
        self._pricing_mode = "unit"

        self.setWindowTitle("Workspace BOM Aggregation Tool")
        # Keep the application usable on smaller screens; wide tables can
        # scroll horizontally instead of preventing the window from resizing.
        self.setMinimumSize(800, 560)
        self.resize(1400, 900)

        self._database_manager = DatabaseManager()

        self._setup_ui()

    def _background_workers(self):
        """Return each currently referenced worker exactly once."""
        workers = []
        seen = set()
        for attribute in (
            "_search_worker",
            "_manual_search_worker",
            "_mapping_refresh_worker",
            "_component_library_import_worker",
            "_sync_worker",
        ):
            worker = getattr(self, attribute, None)
            if worker is not None and id(worker) not in seen:
                seen.add(id(worker))
                workers.append(worker)
        return workers

    def _stop_background_workers(self, timeout_ms: int = 60_000) -> bool:
        """Cooperatively cancel workers and wait before allowing destruction."""
        workers = self._background_workers()
        for worker in workers:
            worker.blockSignals(True)
            cancel = getattr(worker, "cancel", None)
            if callable(cancel):
                cancel()
            else:
                worker.requestInterruption()

        deadline = time.monotonic() + max(timeout_ms, 0) / 1000
        all_stopped = True
        for worker in workers:
            if not worker.isRunning():
                continue
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            if remaining_ms == 0 or not worker.wait(remaining_ms):
                all_stopped = False
        return all_stopped

    def closeEvent(self, event):
        """Never destroy the window while one of its QThreads is running."""
        if self._stop_background_workers():
            event.accept()
            return

        event.ignore()
        QMessageBox.warning(
            self,
            "Background Task Still Stopping",
            "A background request is still shutting down. Please wait a moment "
            "and close the application again.",
        )

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(20, 16, 20, 16)
        main_layout.setSpacing(0)

        # ── Header ──────────────────────────────────────────────────
        header = QWidget()
        header.setObjectName("appHeader")
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
        version_label.setObjectName("versionBadge")
        header_layout.addWidget(version_label)

        main_layout.addWidget(header)

        # ── Separator ──────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setObjectName("headerSeparator")
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
        footer = QLabel("Supplier data: JLCPCB/LCSC and DigiKey")
        footer.setObjectName("footerLabel")
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

        # Splitter: file manager and compact input summary.
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left panel — file manager
        left_panel = QWidget()
        left_panel.setObjectName("contentPanel")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(16, 16, 16, 16)

        self._file_manager = FileManagerWidget()
        self._file_manager.files_changed.connect(self._on_files_changed)
        self._file_manager.component_library_import_requested.connect(self._import_component_library)
        left_layout.addWidget(self._file_manager)

        splitter.addWidget(left_panel)

        # Right panel — summary only; raw BOM rows are not rendered.
        right_panel = QWidget()
        right_panel.setObjectName("contentPanel")
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(16, 16, 16, 16)

        summary_title = QLabel("Input Summary")
        summary_title.setObjectName("sectionTitle")
        right_layout.addWidget(summary_title)
        self._input_summary = QLabel("Select a BOM file to begin")
        self._input_summary.setWordWrap(True)
        self._input_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._input_summary.linkActivated.connect(self._show_input_issues)
        self._input_issues = []
        right_layout.addWidget(self._input_summary)
        right_layout.addStretch()

        splitter.addWidget(right_panel)
        splitter.setSizes([450, 750])

        layout.addWidget(splitter, stretch=1)

        # ── Action bar ──────────────────────────────────────────
        action_bar = QFrame()
        action_bar.setObjectName("actionBar")
        action_layout = QVBoxLayout(action_bar)
        action_layout.setContentsMargins(16, 14, 16, 14)
        action_layout.setSpacing(10)

        output_row = QHBoxLayout()
        output_row.setSpacing(10)

        # Output path
        out_label = QLabel("Output:")
        out_label.setObjectName("fieldLabel")
        output_row.addWidget(out_label)

        self._output_path = QLineEdit()
        self._output_path.setPlaceholderText("Select output Excel file path...")
        self._output_path.setReadOnly(True)
        output_row.addWidget(self._output_path, stretch=1)

        btn_browse_out = QPushButton("Browse...")
        btn_browse_out.clicked.connect(self._browse_output)
        output_row.addWidget(btn_browse_out)
        action_layout.addLayout(output_row)

        tools_row = QHBoxLayout()
        tools_row.setSpacing(8)

        # Column mapping button
        self._btn_map_columns = QPushButton("🗂  Map Columns")
        self._btn_map_columns.setToolTip("Verify or override auto-detected column mappings")
        self._btn_map_columns.clicked.connect(self._open_column_mapper)
        self._btn_map_columns.setEnabled(False)
        tools_row.addWidget(self._btn_map_columns)

        # Pending Approvals button
        self._btn_approvals = QPushButton("📋 Pending Approvals")
        self._btn_approvals.setToolTip("Manage pending internal code mappings")
        self._btn_approvals.clicked.connect(self._open_approval_dialog)
        tools_row.addWidget(self._btn_approvals)

        # Sync JLC Library button
        self._btn_sync_library = QPushButton("🗄 Sync JLC Library")
        self._btn_sync_library.setToolTip("Download JLC component library to local DB for offline MPN→LCSC lookup")
        self._btn_sync_library.clicked.connect(self._sync_jlc_library)
        tools_row.addWidget(self._btn_sync_library)

        # Refresh Mappings button
        self._btn_refresh_mappings = QPushButton("🔄 Refresh Mappings")
        self._btn_refresh_mappings.setToolTip("Update only changed LCSC and DigiKey codes; changed mappings return to pending approval")
        self._btn_refresh_mappings.clicked.connect(self._refresh_mappings)
        tools_row.addWidget(self._btn_refresh_mappings)
        tools_row.addStretch()
        action_layout.addLayout(tools_row)

        primary_row = QHBoxLayout()
        primary_row.setSpacing(8)
        primary_row.addStretch()

        # Refresh Data button
        self._btn_refresh = QPushButton("🔄 Refresh Stock & Prices")
        self._btn_refresh.setToolTip("Ignore API cache and fetch latest data from JLCPCB API")
        self._btn_refresh.clicked.connect(self._refresh_stock_prices)
        self._btn_refresh.setEnabled(False)
        primary_row.addWidget(self._btn_refresh)

        # Process button
        self._btn_process = QPushButton("🚀  Process BOM")
        self._btn_process.setObjectName("btnProcess")
        self._btn_process.clicked.connect(lambda: self._start_processing(force_refresh=False))
        self._btn_process.setEnabled(False)
        primary_row.addWidget(self._btn_process)
        action_layout.addLayout(primary_row)

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

        self._btn_open_excel = QPushButton("Export Excel")
        self._btn_open_excel.clicked.connect(self._open_output_file)
        title_row.addWidget(self._btn_open_excel)

        self._btn_open_unavail = QPushButton("⚠️  Open Unavailable Components")
        self._btn_open_unavail.clicked.connect(self._open_unavailable_file)
        title_row.addWidget(self._btn_open_unavail)

        btn_new = QPushButton("🔄  New Session")
        btn_new.clicked.connect(self._new_session)
        title_row.addWidget(btn_new)

        layout.addLayout(title_row)

        self._partial_banner = QLabel("Partial results — processing was cancelled")
        self._partial_banner.setStyleSheet("background:#4a3a1a; color:#ffd166; padding:8px; font-weight:600")
        self._partial_banner.hide()
        layout.addWidget(self._partial_banner)

        # Summary cards
        self._summary_frame = QFrame()
        self._summary_frame.setObjectName("summaryFrame")
        summary_layout = QHBoxLayout(self._summary_frame)
        summary_layout.setContentsMargins(16, 16, 16, 16)
        summary_layout.setSpacing(12)

        self._card_total = self._make_stat_card("Components", "0", "#1a6b8a")
        self._card_found = self._make_stat_card("Available", "0", "#00b894")
        self._card_manual = self._make_stat_card("Needs Review", "0", "#f39c12")
        self._card_not_found = self._make_stat_card("Unavailable", "0", "#e74c3c")

        summary_layout.addWidget(self._card_total)
        summary_layout.addWidget(self._card_found)
        summary_layout.addWidget(self._card_not_found)
        summary_layout.addWidget(self._card_manual)
        self._card_best_total = self._make_stat_card("Best Sourcing Total", "—", "#1a6b8a")
        summary_layout.addWidget(self._card_best_total)

        layout.addWidget(self._summary_frame)

        filters = QHBoxLayout()
        self._result_search = QLineEdit(); self._result_search.setPlaceholderText("Search")
        self._status_filter = QComboBox(); self._status_filter.addItems(["All", "Available", "Needs Review", "Unavailable"])
        self._supplier_filter = QComboBox(); self._supplier_filter.addItems(["All", "JLCPCB", "DigiKey", "Mixed"])
        self._only_shortage = QCheckBox("Only Shortage")
        reset = QPushButton("Reset Filters"); reset.clicked.connect(self._reset_result_filters)
        for widget in (self._result_search, self._status_filter, self._supplier_filter, self._only_shortage, reset): filters.addWidget(widget)
        self._showing_label = QLabel("Showing 0 of 0 components"); filters.addWidget(self._showing_label)
        layout.addLayout(filters)
        self._filter_timer = QTimer(self); self._filter_timer.setSingleShot(True); self._filter_timer.setInterval(250)
        self._result_search.textChanged.connect(lambda: self._filter_timer.start())
        self._filter_timer.timeout.connect(self._apply_result_filters)
        self._status_filter.currentTextChanged.connect(self._apply_result_filters)
        self._supplier_filter.currentTextChanged.connect(self._apply_result_filters)
        self._only_shortage.toggled.connect(self._apply_result_filters)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self._results_model = ResultsTableModel(self)
        self._results_proxy = ResultsFilterProxy(self); self._results_proxy.setSourceModel(self._results_model)
        self._results_table = QTableView(); self._results_table.setModel(self._results_proxy)
        self._results_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._results_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._results_table.setSortingEnabled(True)
        self._results_table.doubleClicked.connect(self._show_supplier_details)
        splitter.addWidget(self._results_table)
        self._supplier_details = QLabel("Double-click a component to view Supplier Details")
        self._supplier_details.setWordWrap(True); self._supplier_details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        splitter.addWidget(self._supplier_details); splitter.setSizes([850, 350])
        layout.addWidget(splitter, stretch=1)

        return page

    def _build_project_pricing_tab(self) -> QWidget:
        """Build the Excel-independent project pricing result area."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        explanation = QLabel(
            "Independent purchasing scenarios. Supplier, MOQ, order multiple and "
            "price tier are recalculated for every quantity."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        self._pricing_scenario_tabs = QTabWidget()
        self._pricing_views: dict[int, tuple[QLabel, QTableWidget, object]] = {}
        self._pricing_empty_label = QLabel(
            "Process a BOM to calculate the 1, 10, 100 and 1000 project scenarios."
        )
        self._pricing_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._pricing_empty_label)
        layout.addWidget(self._pricing_scenario_tabs, stretch=1)
        self._pricing_scenario_tabs.hide()
        return tab

    def _build_manual_search_tab(self) -> QWidget:
        """Build the manual MPN search panel."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)

        # Search bar
        search_bar = QFrame()
        search_bar.setObjectName("actionBar")
        search_layout = QHBoxLayout(search_bar)
        search_layout.setContentsMargins(16, 12, 16, 12)
        search_layout.setSpacing(12)

        search_label = QLabel("🔎  MPN:")
        search_label.setObjectName("fieldLabel")
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
        card.setObjectName("statCard")
        card.setStyleSheet(
            f"QFrame#statCard {{ background: #111c2e; border: 1px solid {color}; "
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
            "font-size: 11px; color: #a8b3c7; font-weight: 600; background: transparent;"
        )
        name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(name_label)

        return card

    # ═══════════════════════════════════════════════════════════════
    #  Slots / Logic
    # ═══════════════════════════════════════════════════════════════

    def _on_files_changed(self):
        """Called when files are added or removed."""
        # File membership and board quantities are inputs to aggregation and
        # pricing.  Any change invalidates the last processed snapshot; stock
        # refresh must not reuse its component list or quantities.
        self._input_revision += 1
        self._clear_processed_state()
        has_files = self._file_manager.has_files()
        self._btn_map_columns.setEnabled(has_files)
        self._btn_process.setEnabled(has_files)
        self._btn_refresh.setEnabled(False)
        bom_files = self._file_manager.get_bom_files()
        if bom_files:
            self._show_input_summary(bom_files)

            # Auto-set output path
            if not self._output_path.text():
                first_dir = os.path.dirname(bom_files[0].file_path)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                default_name = f"BOM_Enriched_{timestamp}.xlsx"
                self._output_path.setText(os.path.join(first_dir, default_name))
        else:
            self._input_summary.setText("Select a BOM file to begin")

        self._validate_ready()

    def _show_input_summary(self, bom_files):
        self._input_issues = [warning for bom in bom_files for warning in bom.warnings]
        self._input_issues.extend(b.error_message for b in bom_files if not b.is_valid and b.error_message)
        issues = len(self._input_issues)
        workspace = self._file_manager.get_workspace()
        bom_items = [item for project in workspace.projects for board in project.board_items for item in board.bom_items]
        components = len({(item.mpn or item.comment or item.description).strip().casefold() for item in bom_items})
        rows = sum(b.row_count for b in bom_files)
        files = ", ".join(os.path.basename(b.file_path) for b in bom_files)
        sheets = ", ".join(b.sheet_name or "Default" for b in bom_files)
        validation = f'{issues} issues found — <a href="review">Review</a>' if issues else "Valid"
        self._input_summary.setText(
            f"File: {files}\nSelected Sheet(s): {sheets}\nComponents: {components}\n"
            f"Total Line Items: {rows}\nBoard Quantity: Set per board\nValidation Status: {validation}"
        )

    def _show_input_issues(self, link):
        if link == "review" and self._input_issues:
            QMessageBox.warning(self, "Input Issues", "\n".join(self._input_issues))

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
        self._btn_refresh.setEnabled(ready and self._has_processed_bom())

    def _has_processed_bom(self) -> bool:
        return bool(
            self._all_items
            and self._search_item_component_keys
            and self._processed_input_revision == self._input_revision
            and (
                self._processed_workspace is not None
                or self._processed_project is not None
            )
        )

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
            self._show_input_summary(bom_files)

        QMessageBox.information(
            self, "Column Mapping",
            f"Column mappings confirmed for {len(bom_files)} file(s)."
        )

    def _open_approval_dialog(self):
        # Keep mapping management available alongside the main window instead
        # of using ``exec()``, which starts a modal event loop and blocks all
        # interaction with the rest of the application.
        if self._approval_dialog is None:
            self._approval_dialog = ApprovalDialog(self._database_manager, self)
            self._approval_dialog.setModal(False)
            self._approval_dialog.finished.connect(self._on_approval_dialog_closed)
        else:
            self._approval_dialog._load_data()

        self._approval_dialog.show()
        self._approval_dialog.raise_()
        self._approval_dialog.activateWindow()

    def _on_approval_dialog_closed(self):
        self._approval_dialog = None

    def _import_component_library(self, file_path: str):
        if self._component_library_import_worker is not None:
            return

        try:
            raw_rows, invalid_skipped = read_component_library_file(file_path)
            clean_components, conflicts, merged_duplicates = detect_library_conflicts(
                raw_rows, self._database_manager
            )
        except Exception as exc:
            QMessageBox.critical(self, "Component Library Read Error", str(exc))
            return

        total_skipped = invalid_skipped + merged_duplicates
        resolved_mappings = []

        if conflicts:
            dlg = ComponentLibraryConflictDialog(conflicts, self)
            if dlg.exec() != ComponentLibraryConflictDialog.DialogCode.Accepted:
                return
            resolved_mappings = dlg.get_resolved_mappings()
            unresolved_count = len(conflicts) - len(resolved_mappings)
            total_skipped += unresolved_count

        final_components = clean_components + resolved_mappings

        if not final_components:
            QMessageBox.information(
                self,
                "Component Library Import",
                f"No valid components to import.\n"
                f"Skipped {total_skipped} empty, invalid, duplicate, or unselected conflicting row(s).",
            )
            return

        self._file_manager.set_component_library_import_enabled(False)
        self.statusBar().showMessage(f"Reading component library: {os.path.basename(file_path)}")
        worker = ComponentLibraryImportWorker(
            file_path,
            self._database_manager,
            self,
            pre_resolved_components=final_components,
            pre_resolved_skipped=total_skipped,
        )
        self._component_library_import_worker = worker
        worker.progress.connect(self._on_component_library_import_progress)
        worker.finished.connect(self._on_component_library_import_finished)
        worker.error.connect(self._on_component_library_import_error)
        worker.warning.connect(self._on_supplier_api_warning)
        worker.start()

    def _on_component_library_import_progress(self, current: int, total: int, message: str):
        self.statusBar().showMessage(f"{message} ({current}/{total})")

    def _finish_component_library_import(self):
        self._file_manager.set_component_library_import_enabled(True)
        self._component_library_import_worker = None

    def _on_component_library_import_finished(self, searched: int, pending: int, skipped: int):
        self._finish_component_library_import()
        self.statusBar().showMessage("Component library import complete.", 5000)
        if self._approval_dialog is not None:
            self._approval_dialog._load_data()
        QMessageBox.information(
            self,
            "Component Library Import Complete",
            f"Searched {searched} component(s).\n"
            f"Added or returned {pending} mapping(s) to Pending Approvals.\n"
            f"Skipped {skipped} empty, invalid, or duplicate row(s).",
        )

    def _on_component_library_import_error(self, message: str):
        self._finish_component_library_import()
        QMessageBox.critical(self, "Component Library Import Error", message)

    def _refresh_mappings(self):
        reply = QMessageBox.question(
            self, "Refresh Mappings",
            "Check all mappings against JLCPCB and DigiKey?\n\n"
            "Only results that differ from the previous automatic search will be sent for review. "
            "User-approved values are never overwritten automatically.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._btn_refresh_mappings.setEnabled(False)
            self._btn_refresh_mappings.setText("⏳ Refreshing Mappings...")
            self._mapping_refresh_worker = MappingRefreshWorker(self._database_manager, self)
            self._mapping_refresh_worker.progress.connect(self._on_mapping_refresh_progress)
            self._mapping_refresh_worker.finished.connect(self._on_mapping_refresh_finished)
            self._mapping_refresh_worker.error.connect(self._on_mapping_refresh_error)
            self._mapping_refresh_worker.warning.connect(self._on_supplier_api_warning)
            self._mapping_refresh_worker.start()

    def _on_mapping_refresh_progress(self, current: int, total: int, message: str):
        self.statusBar().showMessage(f"{message} ({current}/{total})")

    def _on_mapping_refresh_finished(self, updated_count: int):
        self._btn_refresh_mappings.setEnabled(True)
        self._btn_refresh_mappings.setText("🔄 Refresh Mappings")
        self.statusBar().showMessage("Mapping refresh complete.", 5000)
        QMessageBox.information(self, "Refresh Complete", f"Updated {updated_count} mapping(s).")

    def _on_mapping_refresh_error(self, message: str):
        self._btn_refresh_mappings.setEnabled(True)
        self._btn_refresh_mappings.setText("🔄 Refresh Mappings")
        QMessageBox.critical(self, "Refresh Error", f"Failed to refresh mappings: {message}")

    def _on_supplier_api_warning(self, message: str):
        QMessageBox.warning(
            self,
            "Supplier API Warnings",
            "Some supplier lookups failed and were not treated as 'part not found':\n\n"
            f"{message}",
        )

    def _sync_jlc_library(self):
        """Starts syncing the JLC component library to local DB in the background."""
        count = self._database_manager.get_library_count()
        msg = f"Local library has {count:,} components."
        if count > 0:
            msg += "\n\nSync will add/update records from JOP API. Continue?"
        else:
            msg += "\n\nThis will download the full JLCPCB component list to your local database.\nThis may take a few minutes. Continue?"

        reply = QMessageBox.question(
            self, "Sync JLC Library", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._btn_sync_library.setEnabled(False)
        self._btn_sync_library.setText("⏳ Syncing...")

        self._sync_worker = LibrarySyncWorker(
            APP_ID, ACCESS_KEY, SECRET_KEY, self._database_manager, self
        )
        self._sync_worker.progress.connect(self._on_sync_progress)
        self._sync_worker.finished.connect(self._on_sync_finished)
        self._sync_worker.error.connect(self._on_sync_error)
        self._sync_worker.start()

    def _on_sync_progress(self, fetched: int, total: int, message: str):
        self._btn_sync_library.setText(f"⏳ {fetched:,} synced...")
        self.statusBar().showMessage(f"JLC Library Sync: {message}")

    def _on_sync_finished(self, total: int):
        self._btn_sync_library.setEnabled(True)
        self._btn_sync_library.setText("🗄 Sync JLC Library")
        self.statusBar().showMessage(f"JLC Library Sync complete: {total:,} components saved.")
        QMessageBox.information(
            self, "Sync Complete",
            f"JLC Library sync finished.\n{total:,} components are now available for local MPN→LCSC lookup."
        )

    def _on_sync_error(self, message: str):
        self._btn_sync_library.setEnabled(True)
        self._btn_sync_library.setText("🗄 Sync JLC Library")
        QMessageBox.critical(self, "Sync Error", f"JLC Library sync failed:\n{message}")


    def _clear_processed_state(self):
        """Clear state from previous processing runs."""
        self._all_items = []
        self._processed_workspace = None
        self._workspace_aggregation_result = None
        self._processed_project = None
        self._project_aggregation_result = None
        self._search_item_component_keys = []
        self._processed_input_revision = None

    def _apply_approved_internal_mpn_mappings(self, workspace) -> None:
        """Resolve approved internal-code MPNs before component grouping."""
        mappings = {}
        for project in workspace.projects:
            for board in project.board_items:
                for item in board.bom_items:
                    internal_code = item.comment.strip() if item.comment else ""
                    if not internal_code:
                        continue
                    if internal_code not in mappings:
                        mappings[internal_code] = self._database_manager.get_internal_mapping(
                            internal_code
                        )
                    mapping = mappings[internal_code]
                    mapped_mpn = (mapping.get("mpn", "") or "").strip() if mapping else ""
                    if mapping and mapping.get("approved") and mapped_mpn:
                        item.mpn = mapped_mpn

    def _prompt_processing_options(self):
        build_quantity, accepted = QInputDialog.getInt(
            self,
            "Production Quantity / Kart Adedi",
            "How many board sets will be produced?\n"
            "This multiplies the quantities already defined for each BOM/board.",
            1,
            1,
            1_000_000,
        )
        if not accepted:
            return None

        dialog = QMessageBox(self)
        dialog.setWindowTitle("Pricing Mode / Fiyatlandırma")
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setText("Which price should be displayed?")
        dialog.setInformativeText(
            "Single Unit Price uses the first/base supplier price.\n"
            "Project Quantity Pricing selects the price tier for the total required quantity "
            "and also displays the extended total."
        )
        unit_button = dialog.addButton("Single Unit Price", QMessageBox.ButtonRole.AcceptRole)
        project_button = dialog.addButton("Project Quantity Pricing", QMessageBox.ButtonRole.AcceptRole)
        dialog.addButton(QMessageBox.StandardButton.Cancel)
        dialog.exec()

        if dialog.clickedButton() == unit_button:
            return build_quantity, "unit"
        if dialog.clickedButton() == project_button:
            return build_quantity, "project"
        return None

    def _start_processing(
        self,
        force_refresh: bool = False,
        build_quantity: Optional[int] = None,
        pricing_mode: Optional[str] = None,
    ):
        """Parse all BOM files, then start JLCPCB search."""
        if build_quantity is None or pricing_mode is None:
            options = self._prompt_processing_options()
            if options is None:
                return
            build_quantity, pricing_mode = options

        self._build_quantity = build_quantity
        self._pricing_mode = pricing_mode
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
                bf = self._file_manager.get_bom_file_for_board(
                    board.file_path, board.board_name
                )
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

        self._apply_approved_internal_mpn_mappings(workspace)

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
            prod_qty = int(comp.total_quantity * build_quantity)
            surplus = comp.safety_surplus
            purchase_qty = prod_qty + surplus
            search_item.quantity = int(comp.total_quantity)
            search_item.production_quantity = prod_qty
            search_item.safety_surplus = surplus
            search_item.purchase_quantity = purchase_qty
            search_item.pricing_quantity = purchase_qty
            search_item.required_stock = purchase_qty
            self._all_items.append(search_item)
            self._search_item_component_keys.append(comp.component_key)

        if not self._all_items:
            QMessageBox.warning(self, "No Data", "No valid BOM items found to process.")
            return

        self._processed_input_revision = self._input_revision
        self._start_search_worker(force_refresh=force_refresh)

    def _refresh_stock_prices(self):
        """Refresh supplier data for the last processed BOM without reprocessing inputs."""
        if self._search_worker and self._search_worker.isRunning():
            QMessageBox.information(
                self,
                "Search In Progress",
                "Please wait for the current supplier search to finish.",
            )
            return

        if not self._has_processed_bom():
            QMessageBox.information(
                self,
                "Process BOM First",
                "Process the BOM once before refreshing stock and prices.",
            )
            return

        self._start_search_worker(force_refresh=True)

    def _start_search_worker(self, force_refresh: bool):
        """Run supplier enrichment for the already prepared component list."""
        self._partial_banner.hide()
        self._stack.setCurrentIndex(1)
        self._btn_back_setup.setVisible(False)
        self._btn_view_results.setVisible(False)
        self._progress_widget.reset(len(self._all_items))

        self._search_worker = SearchWorker(
            self._all_items,
            APP_ID,
            ACCESS_KEY,
            SECRET_KEY,
            force_refresh=force_refresh,
            pricing_mode=self._pricing_mode,
        )
        self._search_worker.progress.connect(self._on_search_progress)
        self._search_worker.finished_all.connect(self._on_search_finished)
        self._search_worker.error.connect(self._on_search_error)
        self._search_worker.start()

    def _on_search_progress(self, current: int, total: int, mpn: str, status: str):
        self._progress_widget.update_progress(current, total, mpn, status)

    def _on_search_finished(self, items: list[BomItem]):
        search_worker = getattr(self, "_search_worker", None)
        if search_worker and getattr(search_worker, "_is_cancelled", lambda: False)():
            self._progress_widget.set_cancelled()
            self._btn_back_setup.setVisible(True)
            self._btn_view_results.setVisible(False)
            self._validate_ready()
            return

        if getattr(self, "_processed_input_revision", None) != getattr(self, "_input_revision", None):
            self._progress_widget.set_cancelled()
            self._progress_widget._log.appendPlainText(
                "\n⚠️  BOM files or board quantities changed during processing. "
                "The outdated results were discarded; run Process BOM again."
            )
            self._btn_back_setup.setVisible(True)
            self._btn_view_results.setVisible(False)
            self._validate_ready()
            return

        self._all_items = items
        self._partial_banner.hide()
        self._btn_refresh.setEnabled(self._has_processed_bom())
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
                    component_keys=self._search_item_component_keys,
                    build_multipliers=[self._build_quantity],
                    pricing_mode=self._pricing_mode,
                )
                writer.write(output_path)
            elif self._project_aggregation_result and self._search_item_component_keys and self._processed_project:
                from core.project_excel_writer import ProjectExcelWriter
                writer = ProjectExcelWriter(
                    project=self._processed_project,
                    aggregation_result=self._project_aggregation_result,
                    enriched_items=self._all_items,
                    component_keys=self._search_item_component_keys,
                    build_multipliers=[self._build_quantity],
                    pricing_mode=self._pricing_mode,
                )
                writer.write(output_path)
            else:
                writer = ExcelWriter(
                    self._all_items,
                    pricing_mode=self._pricing_mode,
                    build_multipliers=[self._build_quantity],
                )
                writer.write(output_path)
            self._progress_widget._log.appendPlainText(
                f"\n✅  Main Excel saved to: {output_path}"
            )

            # 2. Unavailable Components Report
            unavail_writer = UnavailableReportWriter(self._all_items)
            if unavail_writer.unavailable_items:
                base_dir = os.path.dirname(output_path)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                unavail_path = os.path.join(
                    base_dir, f"Unavailable_Components_{timestamp}.xlsx"
                )
                unavail_writer.write(unavail_path)
                self._last_unavail_path = unavail_path
                self._progress_widget._log.appendPlainText(
                    f"\n✅  Unavailable Report saved to: {unavail_path}"
                )
            else:
                self._last_unavail_path = None
                self._progress_widget._log.appendPlainText(
                    "\n🎉  No unavailable components found! Skipped separate report."
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
            self._partial_banner.show()

    def _populate_results(self):
        """Show canonical project-pricing rows without UI-side recalculation."""
        aggregation = self._workspace_aggregation_result or self._project_aggregation_result
        if not aggregation or not self._search_item_component_keys:
            self._results_model.set_rows([])
            self._showing_label.setText("No components found")
            return
        from services.project_pricing import calculate_project_pricing
        scenario = calculate_project_pricing(
            aggregation, self._all_items, self._search_item_component_keys,
            scenarios=(self._build_quantity,),
        )[0]
        items = dict(zip(self._search_item_component_keys, self._all_items))
        rows = []
        for component in scenario.components:
            item = items[component.component_key]
            quote = component.quote
            category = item.get_ui_category()
            if quote:
                status = "Available"
                supplier = quote.supplier
                unit = f"{quote.currency} {quote.unit_price:.6f}"
                total = f"{quote.currency} {quote.order_price:.6f}"
                stock = item.available_stock_qty if supplier == "JLCPCB" else item.digikey_stock_qty
            else:
                status = "Unavailable" if category in ("not_found",) else "Needs Review"
                supplier, unit, total, stock = "Unavailable" if status == "Unavailable" else "Needs Review", "—", "—", "—"
            searchable = " ".join((item.comment, item.mpn, item.description, item.jlcpcb_part_number, item.digikey_part_number)).casefold()
            rows.append({
                "display": (item.comment, item.mpn, item.description, component.required_quantity,
                            supplier, unit, total, stock if stock is not None else "—", status),
                "search": searchable, "status": status, "supplier": supplier,
                "shortage": any("insufficient stock" in reason for reason in component.supplier_reasons),
                "item": item, "pricing": component,
            })
        self._results_model.set_rows(rows)
        counts = {name: sum(r["status"] == name for r in rows) for name in ("Available", "Needs Review", "Unavailable")}
        self._update_card_value(self._card_total, str(len(rows)))
        self._update_card_value(self._card_found, str(counts["Available"]))
        self._update_card_value(self._card_manual, str(counts["Needs Review"]))
        self._update_card_value(self._card_not_found, str(counts["Unavailable"]))
        jlc = {c: v for c, v in scenario.order_price_totals.items()} if scenario.components else {}
        self._update_card_value(self._card_best_total, self._format_currency_totals(jlc))
        self._apply_result_filters()

    def _apply_result_filters(self):
        self._results_proxy.set_filters(
            self._result_search.text(), self._status_filter.currentText(),
            self._supplier_filter.currentText(), self._only_shortage.isChecked(),
        )
        self._showing_label.setText(
            f"Showing {self._results_proxy.rowCount()} of {self._results_model.rowCount()} components"
        )

    def _reset_result_filters(self):
        self._result_search.clear(); self._status_filter.setCurrentText("All")
        self._supplier_filter.setCurrentText("All"); self._only_shortage.setChecked(False)
        self._apply_result_filters()

    def _show_supplier_details(self, proxy_index):
        row = self._results_proxy.mapToSource(proxy_index).row()
        data = self._results_model.rows[row]
        item, pricing = data["item"], data["pricing"]
        quote = pricing.quote
        def detail(name, part, status, stock, moq, price, source, error):
            selected = quote if quote and quote.supplier == name else None
            status_label = "Pre-order" if status == "preorder" else status
            return (
                f"{name}\nPart #: {part or '—'}\nMatch Status: {status_label or '—'}\n"
                f"Stock: {stock if stock is not None else 'Unknown'}\nMOQ: {moq if moq is not None else '—'}\n"
                f"Unit Price: {selected.currency + ' ' + str(selected.unit_price) if selected else (str(price) if price is not None else '—')}\n"
                f"Price Break: {selected.purchase_quantity if selected else '—'}\n"
                f"Extended Cost: {selected.currency + ' ' + str(selected.order_price) if selected else '—'}\n"
                f"Data Source: {source or '—'}\nUpdated At: —\nError/Warning: {error or '—'}"
            )
        self._supplier_details.setText(
            "Supplier Details\n\n" +
            detail("JLCPCB", item.jlcpcb_part_number, item.jlcpcb_status, item.available_stock_qty,
                   item.jlcpcb_min_order_quantity, item.unit_price, item.jlcpcb_source, item.jlcpcb_error) + "\n\n" +
            detail("DigiKey", item.digikey_part_number, item.digikey_status, item.digikey_stock_qty,
                   item.digikey_min_order_quantity, item.digikey_unit_price, item.digikey_source, item.digikey_error)
        )

    @staticmethod
    def _format_currency_totals(values: dict) -> str:
        if not values:
            return "—"
        return " | ".join(
            f"{currency} {value:.6f}" for currency, value in sorted(values.items())
        )

    def _populate_project_pricing(self):
        """Render pricing service results without recalculating table values."""
        self._pricing_scenario_tabs.clear()
        self._pricing_views.clear()
        aggregation = self._workspace_aggregation_result or self._project_aggregation_result
        if not aggregation or not self._search_item_component_keys:
            self._pricing_scenario_tabs.hide()
            self._pricing_empty_label.show()
            return

        from services.project_pricing import calculate_project_pricing

        scenarios = calculate_project_pricing(
            aggregation, self._all_items, self._search_item_component_keys
        )
        headers = [
            "Component / MPN", "Description", "Quantity per Project",
            "Required Quantity", "Safety Surplus", "Target Quantity",
            "Minimum Order Quantity", "Purchase Quantity",
            "Excess Stock Quantity", "Supplier", "Supplier Part Number",
            "Unit Price", "Price for Quantity", "Order Price",
            "Excess Stock Cost", "Pricing Status / Reason",
        ]
        for scenario in scenarios:
            page = QWidget()
            page_layout = QVBoxLayout(page)
            summary = QLabel(
                f"Production: {scenario.project_quantity}  |  "
                f"Price for Quantity: {self._format_currency_totals(scenario.price_for_quantity_totals)}  |  "
                f"Order Price: {self._format_currency_totals(scenario.order_price_totals)}\n"
                f"Per-project Production: {self._format_currency_totals(scenario.per_project_production_totals)}  |  "
                f"Per-project Order: {self._format_currency_totals(scenario.per_project_order_totals)}  |  "
                f"Excess Stock Cost: {self._format_currency_totals(scenario.excess_stock_cost_totals)}  |  "
                f"Priced: {scenario.priced_count}  |  Unpriced: {scenario.unpriced_count}"
                + ("  |  COST INCOMPLETE" if scenario.cost_incomplete else "")
            )
            summary.setWordWrap(True)
            summary.setObjectName("pricingSummary")
            page_layout.addWidget(summary)

            table = QTableWidget()
            table.setColumnCount(len(headers))
            table.setHorizontalHeaderLabels(headers)
            table.setRowCount(len(scenario.components))
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setAlternatingRowColors(True)
            table.verticalHeader().setVisible(False)
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            table.setSortingEnabled(False)
            for row_index, row in enumerate(scenario.components):
                quote = row.quote
                currency = quote.currency if quote else ""
                values = [
                    row.mpn or row.component_key, row.description,
                    row.quantity_per_project, row.required_quantity,
                    row.safety_surplus, row.target_quantity,
                    quote.minimum_order_quantity if quote else "",
                    quote.purchase_quantity if quote else "",
                    quote.excess_stock_quantity if quote else "",
                    quote.supplier if quote else "Unpriced",
                    quote.part_number if quote else "",
                    f"{currency} {quote.unit_price}" if quote else "",
                    f"{currency} {quote.price_for_quantity}" if quote else "",
                    f"{currency} {quote.order_price}" if quote else "",
                    f"{currency} {quote.excess_stock_cost}" if quote else "",
                    row.status,
                ]
                for column_index, value in enumerate(values):
                    cell = QTableWidgetItem(str(value))
                    if isinstance(value, int):
                        cell.setData(Qt.ItemDataRole.EditRole, value)
                    if quote is None:
                        cell.setBackground(QColor("#4a1a1a"))
                    table.setItem(row_index, column_index, cell)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            table.setSortingEnabled(True)
            page_layout.addWidget(table, stretch=1)
            self._pricing_views[scenario.project_quantity] = (summary, table, scenario)
            self._pricing_scenario_tabs.addTab(page, f"{scenario.project_quantity} Projects")

        self._pricing_empty_label.hide()
        self._pricing_scenario_tabs.show()

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
        self._partial_banner.hide()
        self._stack.setCurrentIndex(0)
