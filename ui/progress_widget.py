"""Progress widget — shows real-time search progress and log."""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class ProgressWidget(QWidget):
    """Widget displaying JLCPCB search progress with a log view."""

    cancel_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # Title row
        title_row = QHBoxLayout()
        self._title = QLabel("⏳  Processing BOM...")
        self._title.setObjectName("sectionTitle")
        title_row.addWidget(self._title)
        title_row.addStretch()

        self._eta_label = QLabel("")
        self._eta_label.setObjectName("statusLabel")
        title_row.addWidget(self._eta_label)

        layout.addLayout(title_row)

        # Progress bar
        self._progress_bar = QProgressBar()
        self._progress_bar.setMinimum(0)
        self._progress_bar.setMaximum(100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFormat("%v / %m  (%p%)")
        layout.addWidget(self._progress_bar)

        # Status row
        status_row = QHBoxLayout()
        self._status_label = QLabel("Waiting to start...")
        self._status_label.setObjectName("statusLabel")
        status_row.addWidget(self._status_label)
        status_row.addStretch()

        # Stats badges
        self._stats_frame = QFrame()
        stats_layout = QHBoxLayout(self._stats_frame)
        stats_layout.setContentsMargins(0, 0, 0, 0)
        stats_layout.setSpacing(8)

        self._lbl_found = QLabel("Found: 0")
        self._lbl_found.setObjectName("badgeFound")
        stats_layout.addWidget(self._lbl_found)

        self._lbl_not_found = QLabel("Not Found: 0")
        self._lbl_not_found.setObjectName("badgeNotFound")
        stats_layout.addWidget(self._lbl_not_found)

        self._lbl_oos = QLabel("Low Stock / Mismatch: 0")
        self._lbl_oos.setObjectName("badgeOOS")
        stats_layout.addWidget(self._lbl_oos)

        self._lbl_other = QLabel("Other: 0")
        self._lbl_other.setObjectName("badgeOther")
        stats_layout.addWidget(self._lbl_other)

        status_row.addWidget(self._stats_frame)
        layout.addLayout(status_row)

        # Log view
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(2000)
        self._log.setMinimumHeight(150)
        layout.addWidget(self._log)

        # Cancel button
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._btn_cancel = QPushButton("Cancel")
        self._btn_cancel.setObjectName("btnDanger")
        self._btn_cancel.clicked.connect(self._on_cancel)
        btn_row.addWidget(self._btn_cancel)
        layout.addLayout(btn_row)

        # Counters
        self._count_found = 0
        self._count_not_found = 0
        self._count_oos = 0
        self._count_other = 0

    def reset(self, total: int):
        """Reset progress for a new search run."""
        self._progress_bar.setMaximum(total)
        self._progress_bar.setValue(0)
        self._progress_bar.setFormat(f"0 / {total}  (0%)")
        self._status_label.setText("Starting search...")
        self._log.clear()
        self._title.setText("⏳  Processing BOM...")
        self._eta_label.setText("")
        self._btn_cancel.setEnabled(True)

        self._count_found = 0
        self._count_not_found = 0
        self._count_oos = 0
        self._count_other = 0
        self._update_stats()

    def update_progress(self, current: int, total: int, mpn: str, status: str):
        """Update progress bar and status."""
        self._progress_bar.setValue(current)
        self._progress_bar.setFormat(f"{current} / {total}  ({int(current/total*100)}%)")

        self._status_label.setText(f"Searching: {mpn}")

        # Estimate remaining time (200ms per item)
        remaining = total - current
        eta_seconds = remaining * 0.25  # ~250ms per item including overhead
        if eta_seconds > 60:
            self._eta_label.setText(f"~{int(eta_seconds/60)}m {int(eta_seconds%60)}s remaining")
        else:
            self._eta_label.setText(f"~{int(eta_seconds)}s remaining")

        # Log entry
        if not status.startswith("Searching"):
            icon = self._status_icon(status)
            log_status = status if status else "Found"
            self._log.appendPlainText(f"{icon}  [{current}/{total}]  {mpn}  →  {log_status}")

            # Update counters
            if status == "":
                self._count_found += 1
            elif status == "JLCPCB not found":
                self._count_not_found += 1
            elif status in ("Insufficient JLCPCB stock", "No exact JLCPCB match"):
                self._count_oos += 1
            else:
                self._count_other += 1
            self._update_stats()

    def _status_icon(self, status: str) -> str:
        s = status.lower()
        if s == "":
            return "✅"
        elif "not found" in s:
            return "❌"
        elif "insufficient" in s or "match" in s:
            return "⚠️"
        elif "skipped" in s:
            return "⏭️"
        elif "missing" in s:
            return "⬜"
        elif "error" in s:
            return "💥"
        return "ℹ️"

    def _update_stats(self):
        self._lbl_found.setText(f"Found: {self._count_found}")
        self._lbl_not_found.setText(f"Not Found: {self._count_not_found}")
        self._lbl_oos.setText(f"Low Stock/Mismatch: {self._count_oos}")
        self._lbl_other.setText(f"Other: {self._count_other}")

    def set_finished(self, success: bool = True):
        """Mark processing as finished."""
        if success:
            self._title.setText("✅  Processing Complete!")
            self._status_label.setText("All components processed successfully.")
        else:
            self._title.setText("❌  Processing Failed")
        self._eta_label.setText("")
        self._btn_cancel.setEnabled(False)

    def set_cancelled(self):
        """Mark processing as cancelled."""
        self._title.setText("⛔  Processing Cancelled")
        self._status_label.setText("Search was cancelled by user.")
        self._eta_label.setText("")
        self._btn_cancel.setEnabled(False)

    def _on_cancel(self):
        self._btn_cancel.setEnabled(False)
        self._btn_cancel.setText("Cancel")
        self.cancel_requested.emit()
