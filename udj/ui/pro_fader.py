"""
Professional fader widgets — hand-painted, not QSlider.

VolFader layout (left → right, total 84 px wide):
  ┌─────────────────────────────────────────────┐
  │ dB scale │ groove │       handle area        │
  │  (0..30) │(31..39)│       (31..83)           │
  └─────────────────────────────────────────────┘
  Scale is LEFT of groove so the handle (right side only) never covers it.
  Handle is 46 px wide, right-edge-anchored to groove right.

XFader — horizontal crossfader, deck-coloured fill, double-click = centre.

p² audio taper:  pos=1.0 → 0 dB,  pos=0.707 → −6 dB,  pos=0.5 → −12 dB
Shift + drag     → 10× fine mode
Double-click     → reset to unity / centre
"""
from __future__ import annotations

import math
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore    import Qt, pyqtSignal
from PyQt6.QtGui     import (QPainter, QPen, QColor, QLinearGradient, QFont)


# ── audio taper ───────────────────────────────────────────────────────────────
def _gain_to_pos(g: float) -> float:
    return math.sqrt(max(0.0, min(1.0, g)))

def _pos_to_gain(p: float) -> float:
    return p * p

def _gain_to_db(g: float) -> str:
    if g < 1e-4:
        return "  −∞"
    return f"{20.0 * math.log10(g):+.1f}"


# dB scale marks: (db_value, show_label, tick_colour)
_DB_SCALE = [
    ( 0,   True,  "#cc8800"),
    (-3,   True,  "#888"),
    (-6,   True,  "#888"),
    (-10,  True,  "#666"),
    (-15,  True,  "#555"),
    (-20,  True,  "#555"),
    (-30,  False, "#444"),
    (-40,  True,  "#444"),
]

def _db_to_frac(db: float) -> float:
    """Fader position fraction (0=bottom,1=top) for a given dBFS value."""
    gain = 10.0 ** (db / 20.0)
    return _gain_to_pos(gain)


# ── VolFader ──────────────────────────────────────────────────────────────────
class VolFader(QWidget):
    gainChanged = pyqtSignal(float)   # linear gain 0..1

    # Layout constants (pixels)
    # Handle is RIGHT-ANCHORED from groove right edge — scale (left) never overlaps.
    # Scale 0..26 | gap 2 | groove 28..34 | handle 32..76  → no overlap.
    _SCALE_W  = 26   # dB scale labels (left side)
    _GROOVE_W = 6    # groove width
    _GROOVE_X = 28   # groove left edge
    _HANDLE_W = 44   # handle width (starts at groove_right - 2)
    _HANDLE_H = 20   # handle height
    _W        = 76   # total widget width
    _MARGIN   = 18   # top and bottom margin for handle-centre travel

    def __init__(self, color: str = "#00c8ff", parent=None):
        super().__init__(parent)
        self._color  = QColor(color)
        self._pos    = 1.0      # 0..1; 1.0 = unity (handle at top)
        self._drag   = False
        self._drag_y = 0        # y at drag start
        self._drag_p = 1.0      # pos at drag start
        self.setFixedWidth(self._W)
        self.setMinimumHeight(120)
        self.setCursor(Qt.CursorShape.SizeVerCursor)
        self.setToolTip("Drag · Shift+drag = fine · Double-click = unity")

    # ── public API ────────────────────────────────────────────
    def gain(self) -> float:
        return _pos_to_gain(self._pos)

    def set_gain(self, g: float, emit: bool = False):
        p = _gain_to_pos(g)
        if abs(p - self._pos) < 1e-4:
            return
        self._pos = p
        self.update()
        if emit:
            self.gainChanged.emit(self.gain())

    # ── geometry ──────────────────────────────────────────────
    def _travel(self) -> int:
        return max(1, self.height() - 2 * self._MARGIN)

    def _handle_cy(self) -> int:
        return self._MARGIN + int((1.0 - self._pos) * self._travel())

    def _y_to_pos(self, y: int) -> float:
        return max(0.0, min(1.0, 1.0 - (y - self._MARGIN) / self._travel()))

    # ── paint ─────────────────────────────────────────────────
    def paintEvent(self, _):
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)

        W, H = self.width(), self.height()
        M    = self._MARGIN
        GX   = self._GROOVE_X
        GW   = self._GROOVE_W
        GH   = H - 2 * M        # groove height
        hcy  = self._handle_cy()
        c    = self._color

        # background
        qp.fillRect(0, 0, W, H, QColor("#111111"))

        # ── dB scale (left of groove, labels right-aligned to groove) ──
        qp.setFont(QFont("Courier New", 7))
        for db, show_lbl, col in _DB_SCALE:
            frac = _db_to_frac(db)
            ty   = M + int((1.0 - frac) * self._travel())
            # tick from scale edge to groove left
            qp.setPen(QPen(QColor(col), 1))
            qp.drawLine(GX - 5, ty, GX - 1, ty)
            if show_lbl:
                lbl = "0" if db == 0 else str(db)
                qp.setPen(QPen(QColor(col), 1))
                qp.drawText(0, ty - 5, self._SCALE_W - 2, 11,
                            Qt.AlignmentFlag.AlignRight |
                            Qt.AlignmentFlag.AlignVCenter, lbl)

        # ── groove rail ───────────────────────────────────────
        qp.setPen(Qt.PenStyle.NoPen)
        qp.setBrush(QColor("#0d0d0d"))
        _rrect(qp, GX, M, GW, GH, 3)

        # ── fill — channel colour, handle centre down to bottom
        fill_top = hcy
        fill_h   = (M + GH) - fill_top
        if fill_h > 2:
            gr = QLinearGradient(0, fill_top, 0, M + GH)
            gr.setColorAt(0.0, QColor(c.red(), c.green(), c.blue(), 210))
            gr.setColorAt(1.0, QColor(c.red(), c.green(), c.blue(), 60))
            qp.setBrush(gr)
            _rrect(qp, GX, fill_top, GW, fill_h, 3)

        # ── unity (0 dB) reference line — full width, amber ──
        unity_y = M   # pos=1.0 (top of travel) is always at y=M
        qp.setPen(QPen(QColor("#cc8800"), 1))
        qp.drawLine(GX - 4, unity_y, W - 2, unity_y)

        # ── handle — right-anchored from groove right edge ────
        HW   = self._HANDLE_W
        HH   = self._HANDLE_H
        hx   = GX + GW - 2      # handle left overlaps groove right by 2px
        hy   = hcy - HH // 2
        gh2  = QLinearGradient(hx, 0, hx + HW, 0)
        gh2.setColorAt(0.0,  QColor("#1e1e1e"))
        gh2.setColorAt(0.25, QColor("#4c4c4c"))
        gh2.setColorAt(0.5,  QColor("#606060"))
        gh2.setColorAt(0.75, QColor("#4c4c4c"))
        gh2.setColorAt(1.0,  QColor("#1e1e1e"))
        qp.setPen(QPen(QColor("#707070"), 1))
        qp.setBrush(gh2)
        _rrect(qp, hx, hy, HW, HH, 4)

        # centre stripe — channel colour (the alignment line on the cap)
        qp.setPen(QPen(c, 2))
        qp.drawLine(hx + 5, hcy, hx + HW - 5, hcy)
        # flanking micro-lines for depth
        qp.setPen(QPen(QColor(c.red(), c.green(), c.blue(), 90), 1))
        qp.drawLine(hx + 5, hcy - 3, hx + HW - 5, hcy - 3)
        qp.drawLine(hx + 5, hcy + 3, hx + HW - 5, hcy + 3)

        # ── dB readout (top of widget) ─────────────────────────
        db_str = _gain_to_db(self.gain())
        qp.setPen(QPen(c, 1))
        qp.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        qp.drawText(0, 4, W, 13, Qt.AlignmentFlag.AlignCenter,
                    f"{db_str} dB")
        qp.end()

    # ── mouse ─────────────────────────────────────────────────
    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._drag   = True
            self._drag_y = ev.pos().y()
            self._pos    = self._y_to_pos(ev.pos().y())
            self._drag_p = self._pos
            self.gainChanged.emit(self.gain())
            self.update()

    def mouseMoveEvent(self, ev):
        if not self._drag:
            return
        fine  = bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if fine:
            dy    = ev.pos().y() - self._drag_y
            delta = -(dy / self._travel()) * 0.08
            self._pos = max(0.0, min(1.0, self._drag_p + delta))
        else:
            # direct tracking — handle follows cursor exactly
            self._pos    = self._y_to_pos(ev.pos().y())
            self._drag_y = ev.pos().y()
            self._drag_p = self._pos
        self.gainChanged.emit(self.gain())
        self.update()

    def mouseReleaseEvent(self, _):
        self._drag = False

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._pos = 1.0
            self.gainChanged.emit(self.gain())
            self.update()


# ── XFader ────────────────────────────────────────────────────────────────────
class XFader(QWidget):
    posChanged = pyqtSignal(float)   # −1 .. 0 .. +1

    _GROOVE_H = 10
    _HANDLE_W = 28
    _HANDLE_H = 52
    _MARGIN   = 18
    _COL_A    = QColor("#00c8ff")
    _COL_B    = QColor("#ff7a01")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pos    = 0.0
        self._drag   = False
        self._drag_x = 0
        self._drag_p = 0.0
        self.setFixedHeight(74)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.setToolTip("Drag · Shift+drag = fine · Double-click = centre")

    # ── public API ────────────────────────────────────────────
    def position(self) -> float:
        return self._pos

    def set_position(self, pos: float, emit: bool = False):
        pos = max(-1.0, min(1.0, pos))
        if abs(pos - self._pos) < 1e-4:
            return
        self._pos = pos
        self.update()
        if emit:
            self.posChanged.emit(pos)

    # ── geometry ──────────────────────────────────────────────
    def _travel(self) -> int:
        return max(1, self.width() - 2 * self._MARGIN)

    def _handle_cx(self) -> int:
        return self._MARGIN + int((self._pos + 1.0) / 2.0 * self._travel())

    def _x_to_pos(self, x: int) -> float:
        return max(-1.0, min(1.0,
                             (x - self._MARGIN) / self._travel() * 2.0 - 1.0))

    # ── paint ─────────────────────────────────────────────────
    def paintEvent(self, _):
        qp  = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)

        W, H  = self.width(), self.height()
        cy    = H // 2
        M     = self._MARGIN
        GH    = self._GROOVE_H
        gy    = cy - GH // 2
        gw    = W - 2 * M
        hcx   = self._handle_cx()
        pos   = self._pos
        t     = (pos + 1.0) / 2.0         # 0=A, 1=B
        ca, cb = self._COL_A, self._COL_B

        qp.fillRect(0, 0, W, H, QColor("#0c0c10"))

        # ── groove shadow / border ────────────────────────────
        qp.setPen(Qt.PenStyle.NoPen)
        qp.setBrush(QColor("#050508"))
        _rrect(qp, M - 1, gy - 1, gw + 2, GH + 2, 5)

        # ── groove background ─────────────────────────────────
        qp.setBrush(QColor("#111116"))
        _rrect(qp, M, gy, gw, GH, 4)

        # ── Deck-A fill (left) — vivid cyan glow ─────────────
        a_w = hcx - M
        if a_w > 2:
            a_strength = 1.0 - t    # brightest at full-A
            ga = QLinearGradient(M, 0, hcx, 0)
            ga.setColorAt(0.0, QColor(ca.red(), ca.green(), ca.blue(),
                                      int(40 + a_strength * 200)))
            ga.setColorAt(0.7, QColor(ca.red(), ca.green(), ca.blue(),
                                      int(20 + a_strength * 160)))
            ga.setColorAt(1.0, QColor(ca.red(), ca.green(), ca.blue(), 15))
            qp.setBrush(ga)
            _rrect(qp, M, gy, a_w, GH, 4)
            # edge glow on A-side
            if a_strength > 0.05:
                eg = QLinearGradient(M, gy, M, gy + GH)
                eg.setColorAt(0.0, QColor(ca.red(), ca.green(), ca.blue(),
                                          int(a_strength * 80)))
                eg.setColorAt(1.0, QColor(0, 0, 0, 0))
                qp.setBrush(eg)
                _rrect(qp, M, gy, a_w, GH // 2, 4)

        # ── Deck-B fill (right) — vivid orange glow ──────────
        b_w = (M + gw) - hcx
        if b_w > 2:
            b_strength = t
            gb = QLinearGradient(hcx, 0, M + gw, 0)
            gb.setColorAt(0.0, QColor(cb.red(), cb.green(), cb.blue(), 15))
            gb.setColorAt(0.3, QColor(cb.red(), cb.green(), cb.blue(),
                                      int(20 + b_strength * 160)))
            gb.setColorAt(1.0, QColor(cb.red(), cb.green(), cb.blue(),
                                      int(40 + b_strength * 200)))
            qp.setBrush(gb)
            _rrect(qp, hcx, gy, b_w, GH, 4)
            if b_strength > 0.05:
                eg = QLinearGradient(hcx, gy, hcx, gy + GH)
                eg.setColorAt(0.0, QColor(cb.red(), cb.green(), cb.blue(),
                                          int(b_strength * 80)))
                eg.setColorAt(1.0, QColor(0, 0, 0, 0))
                qp.setBrush(eg)
                _rrect(qp, hcx, gy, b_w, GH // 2, 4)

        # ── centre detent — glowing notch ─────────────────────
        cx_pos = M + gw // 2
        near_centre = max(0.0, 1.0 - abs(pos) * 8)
        notch_col = QColor(180, 180, 200, int(80 + near_centre * 160))
        qp.setPen(QPen(notch_col, 1))
        qp.drawLine(cx_pos, gy - 6, cx_pos, gy + GH + 5)

        # ── handle — machined aluminium look ──────────────────
        HW, HH = self._HANDLE_W, self._HANDLE_H
        hx  = hcx - HW // 2
        hy  = cy - HH // 2

        # outer shadow
        qp.setPen(Qt.PenStyle.NoPen)
        qp.setBrush(QColor(0, 0, 0, 80))
        _rrect(qp, hx + 1, hy + 2, HW, HH, 6)

        # body gradient — vertical brushed metal
        gv = QLinearGradient(0, hy, 0, hy + HH)
        gv.setColorAt(0.00, QColor("#1a1a1e"))
        gv.setColorAt(0.15, QColor("#3a3a42"))
        gv.setColorAt(0.45, QColor("#686870"))
        gv.setColorAt(0.55, QColor("#686870"))
        gv.setColorAt(0.85, QColor("#3a3a42"))
        gv.setColorAt(1.00, QColor("#1a1a1e"))
        qp.setPen(QPen(QColor("#5a5a62"), 1))
        qp.setBrush(gv)
        _rrect(qp, hx, hy, HW, HH, 5)

        # highlight strip at top of handle
        gh = QLinearGradient(hx, hy, hx + HW, hy)
        gh.setColorAt(0.0, QColor(255, 255, 255, 0))
        gh.setColorAt(0.5, QColor(255, 255, 255, 30))
        gh.setColorAt(1.0, QColor(255, 255, 255, 0))
        qp.setPen(Qt.PenStyle.NoPen)
        qp.setBrush(gh)
        _rrect(qp, hx + 2, hy + 1, HW - 4, HH // 3, 4)

        # colour stripe — blends A→B as handle moves
        r  = int(ca.red()   * (1-t) + cb.red()   * t)
        g_ = int(ca.green() * (1-t) + cb.green() * t)
        b_ = int(ca.blue()  * (1-t) + cb.blue()  * t)
        stripe_col = QColor(r, g_, b_)
        # glow behind stripe
        qp.setPen(Qt.PenStyle.NoPen)
        qp.setBrush(QColor(r, g_, b_, 40))
        _rrect(qp, hx + 3, cy - 4, HW - 6, 8, 3)
        # stripe itself
        qp.setPen(QPen(stripe_col, 2))
        qp.drawLine(hx + 6, cy, hx + HW - 6, cy)
        # micro lines flanking
        qp.setPen(QPen(QColor(r, g_, b_, 70), 1))
        qp.drawLine(hx + 6, cy - 4, hx + HW - 6, cy - 4)
        qp.drawLine(hx + 6, cy + 4, hx + HW - 6, cy + 4)

        # ── readout ───────────────────────────────────────────
        if pos < -0.03:
            txt, col = f"◀ A  {int(abs(pos)*100)}%", ca
        elif pos > 0.03:
            txt, col = f"{int(pos*100)}%  B ▶", cb
        else:
            txt, col = "CENTER", QColor("#666")
        qp.setPen(QPen(col, 1))
        qp.setFont(QFont("Courier New", 8))
        qp.drawText(0, H - 14, W, 12, Qt.AlignmentFlag.AlignCenter, txt)
        qp.end()

    # ── mouse ─────────────────────────────────────────────────
    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            raw = self._x_to_pos(ev.pos().x())
            self._pos    = 0.0 if abs(raw) < 0.01 else raw
            self._drag   = True
            self._drag_x = ev.pos().x()
            self._drag_p = self._pos
            self.posChanged.emit(self._pos)
            self.update()

    def mouseMoveEvent(self, ev):
        if not self._drag:
            return
        fine = bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if fine:
            dx    = ev.pos().x() - self._drag_x
            delta = (dx / self._travel()) * 2.0 * 0.08
            raw   = max(-1.0, min(1.0, self._drag_p + delta))
        else:
            raw = self._x_to_pos(ev.pos().x())
            self._drag_x = ev.pos().x()
            self._drag_p = raw
        self._pos = 0.0 if abs(raw) < 0.008 else raw
        self.posChanged.emit(self._pos)
        self.update()

    def mouseReleaseEvent(self, _):
        self._drag = False

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._pos = 0.0
            self.posChanged.emit(0.0)
            self.update()


# ── helper ─────────────────────────────────────────────────────────────────────
def _rrect(qp: QPainter, x, y, w, h, r):
    from PyQt6.QtCore import QRectF
    qp.drawRoundedRect(QRectF(x, y, w, h), r, r)
