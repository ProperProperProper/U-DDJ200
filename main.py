#!/usr/bin/env python3
"""U-DJ — standalone DJ application for Pioneer DDJ-200."""

import sys
import os
from pathlib import Path

# Allow running from project root without installing
sys.path.insert(0, str(Path(__file__).parent))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

from udj.ui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("U-DJ")
    app.setOrganizationName("UDJ")

    # Load stylesheet
    qss_path = Path(__file__).parent / "assets" / "style.qss"
    if qss_path.exists():
        app.setStyleSheet(qss_path.read_text())

    window = MainWindow()
    window.show()

    app.exec()
    os._exit(0)   # force-kill all background threads (demucs, analyzer, stems)


if __name__ == "__main__":
    main()
