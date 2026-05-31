"""
WaveformWidget — overview waveform display for a deck.

Symmetric bars drawn above and below the horizontal midline.
Channel-coloured (cyan / orange), played portion full brightness,
unplayed 25 % brightness. White playhead. End-warning zone turns
orange→red in the last 30 s. Background #080808. Fixed height 72 px.
"""
from __future__ import annotations

import numpy as np
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore    import Qt
from PyQt6.QtGui     import QPainter, QColor, QPen


_BG        = QColor("#07070b")
_BORDER    = QColor("#1a1a2a")
_PH_COL    = QColor("#ffffff")
_EMPTY_MID = QColor("#1c1c28")
_EMPTY_BAR = QColor("#14141e")


def _lerp_color(c1: QColor, c2: QColor, t: float) -> QColor:
    r = int(c1.red()   + (c2.red()   - c1.red())   * t)
    g = int(c1.green() + (c2.green() - c1.green()) * t)
    b = int(c1.blue()  + (c2.blue()  - c1.blue())  * t)
    return QColor(r, g, b)


class WaveformWidget(QWidget):
    HEIGHT = 88

    def __init__(self, accent: str = "#00c8ff", parent=None):
        super().__init__(parent)
        self._accent     = QColor(accent)
        self._dim        = QColor(
            self._accent.red()   // 4,
            self._accent.green() // 4,
            self._accent.blue()  // 4,
        )
        self._data: np.ndarray | None = None
        self._pos:      float = 0.0
        self._duration: float = 0.0
        self._phrases:  list  = []

        self.setFixedHeight(self.HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)

    # ── public API ─────────────────────────────────────────────
    def set_waveform(self, data: np.ndarray):
        """Accept a normalised float32 array, one value per pixel column."""
        if data is None or len(data) == 0:
            self._data = None
        else:
            self._data = np.clip(data.astype(np.float32), 0.0, 1.0)
        self.update()

    def set_position(self, pos: float):
        p = float(max(0.0, min(1.0, pos)))
        if abs(p - self._pos) > 1e-4:
            self._pos = p
            self.update()

    def set_duration(self, secs: float):
        self._duration = float(max(0.0, secs))
        self.update()

    def set_phrases(self, phrase_fracs):
        self._phrases = list(phrase_fracs) if phrase_fracs is not None else []
        self.update()

    def clear(self):
        self._data     = None
        self._pos      = 0.0
        self._duration = 0.0
        self._phrases  = []
        self.update()

    # ── painting ───────────────────────────────────────────────
    def paintEvent(self, _):
        qp = QPainter(self)
        W, H = self.width(), self.height()
        mid  = H // 2

        qp.fillRect(0, 0, W, H, _BG)
        # border
        qp.setPen(QPen(_BORDER, 1))
        qp.drawRect(0, 0, W - 1, H - 1)

        data = self._data
        if data is None or len(data) == 0:
            # placeholder: subtle low-amplitude bars to show the widget area
            for x in range(0, W, 3):
                import math
                amp = 0.06 + 0.05 * abs(math.sin(x * 0.07))
                bar_h = max(1, int(amp * (mid - 4)))
                qp.fillRect(x, mid - bar_h, 2, bar_h * 2, _EMPTY_BAR)
            qp.setPen(QPen(_EMPTY_MID, 1))
            qp.drawLine(0, mid, W, mid)
            qp.end()
            return

        n   = len(data)
        pos = self._pos

        # End-warning: compute pixel column where last-30s zone begins
        warn_col = -1
        if self._duration > 0:
            warn_frac = max(0.0, (self._duration - 30.0) / self._duration)
            warn_col  = int(warn_frac * W)

        _col_warn_start = QColor("#ff9900")
        _col_warn_end   = QColor("#ff2200")
        playhead_x = int(pos * (W - 1))

        qp.setPen(Qt.PenStyle.NoPen)
        for x in range(W):
            # map pixel column to data index
            di    = int(x / W * n)
            di    = max(0, min(n - 1, di))
            amp   = float(data[di])

            bar_h = max(1, int(amp * (mid - 2)))

            # choose colour
            played = (x <= playhead_x)
            if warn_col >= 0 and x >= warn_col:
                # end-warning zone: gradient orange → red
                t   = (x - warn_col) / max(1, W - warn_col)
                base = _lerp_color(_col_warn_start, _col_warn_end, t)
            else:
                base = self._accent

            if played:
                col = base
            else:
                col = QColor(base.red() // 4, base.green() // 4, base.blue() // 4)

            qp.setBrush(col)
            # draw bar symmetrically above and below midline
            qp.fillRect(x, mid - bar_h, 1, bar_h * 2, col)

        # Phrase markers — amber, prominent
        _PHRASE_PEN1 = QPen(QColor("#ffdd00"), 2)
        _PHRASE_PEN2 = QPen(QColor("#ffdd00"), 3)
        for frac in self._phrases:
            px = int(frac * (W - 1))
            qp.setPen(_PHRASE_PEN1)
            qp.drawLine(px, 0, px, H)
            qp.setPen(_PHRASE_PEN2)
            qp.drawLine(px, 0, px, 6)
            qp.drawLine(px, H - 6, px, H)

        # playhead
        qp.setPen(QPen(_PH_COL, 2))
        qp.drawLine(playhead_x, 0, playhead_x, H)

        qp.end()
