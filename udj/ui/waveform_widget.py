"""
WaveformWidget — full-track spectral overview for a deck.

Displays the entire track as a scrolling-free minimap:
  - Spectral colour bars matching the scrolling beat grid (bass=red, mid=green, hi=blue)
  - Playhead line (white)
  - Phrase markers (amber)
  - End-warning zone (orange→red, last 30 s)
Fixed height 52 px. Zero per-frame allocations after first load.
"""
from __future__ import annotations

import numpy as np
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore    import Qt
from PyQt6.QtGui     import QPainter, QColor, QPen, QImage

_BG        = QColor("#07070b")
_BORDER    = QColor("#1a1a2a")
_PH_COL    = QColor("#ffffff")
_PHRASE_A  = QColor(255, 210,  40, 220)   # amber phrase marker
_WARN_LO   = QColor("#ff9900")
_WARN_HI   = QColor("#ff2200")


class WaveformWidget(QWidget):
    HEIGHT = 52

    def __init__(self, accent: str = "#00c8ff", parent=None):
        super().__init__(parent)
        self._accent      = QColor(accent)
        self._duration    = 0.0
        self._pos         = 0.0
        self._phrases: list[float] = []   # fractional positions 0-1

        # Pre-rendered RGBA strip — rebuilt only when waveform data changes
        self._strip:     np.ndarray | None = None   # (H, N, 4) uint8
        self._strip_img: QImage     | None = None   # QImage view into _strip

        self.setFixedHeight(self.HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)

    # ── public API ─────────────────────────────────────────────

    def set_waveform(self, data: np.ndarray | None):
        """
        Accept deck.waveform — float32 (N, 3): [lo, mid, hi] 0-1 each.
        Pre-renders a full-width RGBA strip; paint just blits + overlays.
        """
        if data is None or len(data) == 0:
            self._strip     = None
            self._strip_img = None
            self.update()
            return
        self._build_strip(data)
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
        self._phrases = list(phrase_fracs) if phrase_fracs else []
        self.update()

    def clear(self):
        self._strip     = None
        self._strip_img = None
        self._pos       = 0.0
        self._duration  = 0.0
        self._phrases   = []
        self.update()

    # ── internal ───────────────────────────────────────────────

    def _build_strip(self, wf: np.ndarray):
        """Pre-render waveform into a fixed-width RGBA strip."""
        W = max(self.width(), 1)
        H = self.HEIGHT - 2   # leave 1px top/bottom for border

        n = len(wf)
        # Map each pixel column → waveform frame
        idx = (np.arange(W, dtype=np.float64) / W * n).astype(np.int64)
        idx = np.clip(idx, 0, n - 1)

        lo = wf[idx, 0]
        mi = wf[idx, 1]
        hi = wf[idx, 2]

        # Dominant-band amplitude for bar height
        amp   = np.maximum(np.maximum(lo, mi), hi)
        bar_h = np.clip((H * 0.92 * amp).astype(np.int32), 1, H)

        # Spectral colour
        R = np.clip(lo * 3.0, 0, 1)
        G = np.clip(mi * 2.0, 0, 1)
        B = np.clip(hi * 2.5, 0, 1)

        # End-warning zone: last 30 s fades orange→red
        warn_frac = (self._duration - 30.0) / self._duration if self._duration > 0 else 1.0
        warn_frac = max(0.0, warn_frac)
        x_frac    = np.arange(W, dtype=np.float32) / max(W - 1, 1)
        in_warn   = x_frac >= warn_frac
        if in_warn.any():
            t      = np.where(in_warn, (x_frac - warn_frac) / max(1.0 - warn_frac, 1e-6), 0.0)
            R_warn = np.clip(1.0,         0, 1)
            G_warn = np.clip(0.6 - t*0.6, 0, 1)
            B_warn = np.zeros(W)
            R = np.where(in_warn, R_warn, R)
            G = np.where(in_warn, G_warn, G)
            B = np.where(in_warn, B_warn, B)

        strip = np.zeros((H, W, 4), dtype=np.uint8)
        y_arr = np.arange(H, dtype=np.int32)
        bar_t = ((H - bar_h) // 2)
        in_bar = (y_arr[:, None] >= bar_t[None, :]) & \
                 (y_arr[:, None] <  (bar_t + bar_h)[None, :])

        Ru = (R * 255).astype(np.uint8)
        Gu = (G * 255).astype(np.uint8)
        Bu = (B * 255).astype(np.uint8)
        strip[:, :, 0] = np.where(in_bar, Ru[None, :], 0)
        strip[:, :, 1] = np.where(in_bar, Gu[None, :], 0)
        strip[:, :, 2] = np.where(in_bar, Bu[None, :], 0)
        strip[:, :, 3] = np.where(in_bar, 230, 0)

        self._strip     = strip
        self._strip_img = QImage(strip.data, W, H, W * 4,
                                 QImage.Format.Format_RGBA8888)

    def resizeEvent(self, _):
        # Rebuild strip when widget is resized
        pass   # strip rebuilt lazily on next set_waveform call

    # ── painting ───────────────────────────────────────────────

    def paintEvent(self, _):
        qp = QPainter(self)
        W, H = self.width(), self.height()

        qp.fillRect(0, 0, W, H, _BG)

        if self._strip_img is not None:
            # Stretch pre-rendered strip to current widget width
            qp.drawImage(qp.viewport(), self._strip_img)

        # Phrase markers — amber, full height
        _PP = QPen(_PHRASE_A, 1)
        for frac in self._phrases:
            px = int(frac * (W - 1))
            qp.setPen(_PP)
            qp.drawLine(px, 0, px, H)

        # Playhead — bright white
        ph = int(self._pos * (W - 1))
        qp.setPen(QPen(_PH_COL, 2))
        qp.drawLine(ph, 0, ph, H)

        # Border
        qp.setPen(QPen(_BORDER, 1))
        qp.drawRect(0, 0, W - 1, H - 1)

        qp.end()
