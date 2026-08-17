"""JLCPCB BOM Enrichment Tool — Entry Point."""

import sys
import os

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QIcon

from ui.main_window import MainWindow
from core.utils import get_resource_path


def load_stylesheet() -> str:
    """Load the QSS stylesheet from resources."""
    qss_path = get_resource_path(os.path.join("resources", "style.qss"))
    try:
        with open(qss_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        print(f"Warning: Stylesheet not found at {qss_path}")
        return ""


def main():
    # High DPI support
    os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"

    app = QApplication(sys.argv)

    # Apply global font
    font = QFont("Segoe UI", 10)
    app.setFont(font)

    # Apply app icon
    icon_path = get_resource_path(os.path.join("resources", "sirket.ico"))
    app.setWindowIcon(QIcon(icon_path))

    # Apply stylesheet
    stylesheet = load_stylesheet()
    if stylesheet:
        app.setStyleSheet(stylesheet)

    # Create and show main window
    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()



