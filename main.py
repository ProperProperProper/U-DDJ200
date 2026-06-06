#!/usr/bin/env python3
"""U-DJ — standalone DJ application for Pioneer DDJ-200."""

import sys
import os
import logging
from pathlib import Path

# Allow running from project root without installing
sys.path.insert(0, str(Path(__file__).parent))

# Redirect all stdout/stderr + logging to a rotating log file so we can
# inspect output from the .app bundle without a terminal.
_LOG_PATH = Path.home() / "Library" / "Logs" / "U-DJ" / "udj.log"
_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
_log_file = open(_LOG_PATH, "a", buffering=1)  # line-buffered
sys.stdout = _log_file
sys.stderr = _log_file
logging.basicConfig(stream=_log_file, level=logging.DEBUG,
                    format="%(asctime)s %(levelname)s %(message)s")

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
