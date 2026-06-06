"""
DevLogWindow — floating developer log panel.

Captures:
  • All Python logging records (DEBUG and above)
  • Unhandled exceptions via sys.excepthook
  • Raw stderr output (print statements, C-extension errors)

Auto-raises on ERROR / EXCEPTION. Close button hides (does not destroy).
"""
from __future__ import annotations

import sys
import logging
import traceback
from datetime import datetime

from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout,
                              QPushButton, QTextEdit, QLabel)
from PyQt6.QtCore    import Qt, QObject, pyqtSignal
from PyQt6.QtGui     import QColor, QTextCharFormat, QTextCursor, QFont


# ── Qt-safe signal bridge (handler runs on any thread) ────────────────────────

class _Bridge(QObject):
    record = pyqtSignal(str, str)   # (level_name, message)


# ── logging.Handler ───────────────────────────────────────────────────────────

class _Handler(logging.Handler):
    def __init__(self, bridge: _Bridge):
        super().__init__()
        self._bridge = bridge

    def emit(self, rec: logging.LogRecord):
        try:
            self._bridge.record.emit(rec.levelname, self.format(rec))
        except Exception:
            pass


# ── stderr proxy ──────────────────────────────────────────────────────────────

class _StderrProxy:
    def __init__(self, bridge: _Bridge, real):
        self._bridge = bridge
        self._real   = real
        self._buf    = ""

    def write(self, s: str):
        self._real.write(s)
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._bridge.record.emit("STDERR", line)

    def flush(self):
        self._real.flush()

    def fileno(self):
        return self._real.fileno()


# ── DevLogWindow ──────────────────────────────────────────────────────────────

_COLORS = {
    "EXCEPTION": "#ff4455",
    "CRITICAL":  "#ff4455",
    "ERROR":     "#ff6644",
    "WARNING":   "#ddaa22",
    "INFO":      "#44aaaa",
    "DEBUG":     "#445566",
    "STDERR":    "#aa7744",
}

_FMT = logging.Formatter(
    "%(name)s — %(filename)s:%(lineno)d\n%(message)s",
    datefmt="%H:%M:%S")


class DevLogWindow(QWidget):
    """Floating dev log.  Call install() once after constructing."""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Tool)
        self.setWindowTitle("U-DJ  ·  Dev Log")
        self.resize(820, 380)
        self.setStyleSheet("background:#090910;")

        self._bridge = _Bridge()
        self._bridge.record.connect(self._append)

        self._build_ui()

    def install(self):
        """Hook into logging, excepthook, and stderr. Call once."""
        # logging
        handler = _Handler(self._bridge)
        handler.setFormatter(_FMT)
        root = logging.getLogger()
        root.addHandler(handler)
        if root.level == logging.NOTSET or root.level > logging.DEBUG:
            root.setLevel(logging.DEBUG)

        # excepthook
        self._prev_hook = sys.excepthook
        sys.excepthook  = self._on_exception

        # stderr
        sys.stderr = _StderrProxy(self._bridge, sys.stderr)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 6)
        root.setSpacing(4)

        # header bar
        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)

        title = QLabel("DEV LOG")
        title.setStyleSheet(
            "color:#333340;font-size:8px;font-weight:bold;"
            "letter-spacing:3px;background:transparent;")
        hdr.addWidget(title)
        hdr.addStretch()

        self._count_lbl = QLabel("0 entries")
        self._count_lbl.setStyleSheet(
            "color:#282838;font-size:8px;background:transparent;letter-spacing:1px;")
        hdr.addWidget(self._count_lbl)

        clear_btn = QPushButton("⊘  Clear")
        clear_btn.setFixedHeight(22)
        clear_btn.setFixedWidth(72)
        clear_btn.setStyleSheet(
            "QPushButton{background:#0e0e18;border:1px solid #1e1e2c;"
            "border-radius:3px;color:#444456;font-size:8px;letter-spacing:1px;}"
            "QPushButton:hover{color:#aaaacc;border-color:#3a3a50;}"
            "QPushButton:pressed{background:#1a1a28;}")
        clear_btn.clicked.connect(self._clear)
        hdr.addWidget(clear_btn)
        root.addLayout(hdr)

        # log area
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        font = QFont("Menlo", 11)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self._log.setFont(font)
        self._log.setStyleSheet(
            "QTextEdit{background:#07070c;color:#445566;"
            "border:1px solid #141422;border-radius:3px;"
            "padding:4px;selection-background-color:#1e1e30;}")
        root.addWidget(self._log)

        self._entry_count = 0

    # ── slots ─────────────────────────────────────────────────────────────────

    def _append(self, level: str, msg: str):
        ts  = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        col = _COLORS.get(level, "#556677")

        cur = self._log.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)

        # level badge + timestamp
        badge_fmt = QTextCharFormat()
        badge_fmt.setForeground(QColor(col))
        cur.insertText(f"[{ts}]  {level:<9s}", badge_fmt)

        # message body (slightly dimmer)
        body_col = QColor(col)
        body_col.setAlpha(180)
        body_fmt = QTextCharFormat()
        body_fmt.setForeground(body_col)
        cur.insertText(f"{msg}\n\n", body_fmt)

        self._log.setTextCursor(cur)
        self._log.ensureCursorVisible()

        self._entry_count += 1
        self._count_lbl.setText(f"{self._entry_count} entries")

        # auto-raise on serious issues
        if level in ("EXCEPTION", "ERROR", "CRITICAL"):
            self.show()
            self.raise_()
            self.activateWindow()

    def _clear(self):
        self._log.clear()
        self._entry_count = 0
        self._count_lbl.setText("0 entries")

    def log(self, level: str, msg: str):
        """Direct-inject a message (no logging module needed)."""
        self._bridge.record.emit(level, msg)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def _on_exception(self, exc_type, exc_val, exc_tb):
        msg = "".join(traceback.format_exception(exc_type, exc_val, exc_tb))
        self._bridge.record.emit("EXCEPTION", msg.rstrip())
        self._prev_hook(exc_type, exc_val, exc_tb)

    def closeEvent(self, ev):
        ev.ignore()
        self.hide()
