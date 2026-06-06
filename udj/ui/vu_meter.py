"""
VUMeter — professional segment peak/RMS display.

Two modes:
  full    — stereo L+R with dBFS scale labels, used for master
  compact — single column, no scale labels, sits beside a fader

Segment zones (bottom → top):
  Green   −48 → −18 dBFS
  Yellow  −18 → −9  dBFS
  Orange   −9 → −3  dBFS
  Red      −3 →  0  dBFS
  Clip      0 dBFS+  (latches red, click to reset)

Ballistics:
  Attack  : instantaneous
  Decay   : ~−20 dB/s
  Peak-hold: 1.5 s then gravity fall
"""
from __future__ import annotations
import math
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore    import Qt
from PyQt6.QtGui     import QPainter, QColor, QPen, QFont

# ── segment table ─────────────────────────────────────────────────────────────
# Calibrated like Pioneer DJM: normal program level lands in green.
# Typical dance track peaks at −3 to −6 dBFS → upper green / yellow.
# (db_lo, db_hi, color_off_hex, color_on_hex, glow_hex)
_SEGS: list[tuple[float, float, str, str, str]] = []

def _s(lo, hi, off, on, glow=None):
    _SEGS.append((lo, hi, off, on, glow or on))

# green  −48 → −6   (14 × 3 dB) — program level zone
for _i in range(14): _s(-48+_i*3, -48+(_i+1)*3, "#050e05", "#00e050", "#00ff60")
# yellow  −6 → −3   (3 × 1 dB) — hot but OK
for _i in range(3):  _s(-6+_i, -5+_i, "#0e0d00", "#eecc00", "#ffee00")
# orange  −3 → −1   (2 × 1 dB) — pushing it
for _i in range(2):  _s(-3+_i, -2+_i, "#110600", "#ff8800", "#ffaa00")
# red     −1 →  0   (1 × 1 dB) — danger
_s(-1, 0, "#0e0000", "#ff2200", "#ff5500")
# clip     0 →  3   — pure red flash
_s(0, 3, "#080000", "#ff0000", "#ff4444")

_N = len(_SEGS)   # 24 segments

def _db_to_seg(db: float) -> float:
    """Fractional segment index, 0=bottom, _N=top."""
    for i, (lo, hi, *_) in enumerate(_SEGS):
        if db <= hi:
            return i + (db - lo) / max(1e-9, hi - lo)
    return float(_N)

def _lin_to_db(lin: float) -> float:
    return 20.0 * math.log10(max(lin, 1e-7))


# ── channel ballistics ────────────────────────────────────────────────────────
class _Chan:
    _DECAY     = 0.88   # per tick (50 ms) → ~−20 dB/s
    _HOLD_N    = 30     # ticks of peak hold (1.5 s)
    _PEAK_DEC  = 0.96

    def __init__(self):
        self.level = 0.0
        self.peak  = 0.0
        self._hold = 0

    def push(self, lin: float):
        self.level = lin if lin >= self.level else self.level * self._DECAY
        if lin >= self.peak:
            self.peak, self._hold = lin, self._HOLD_N
        elif self._hold > 0:
            self._hold -= 1
        else:
            self.peak *= self._PEAK_DEC

    def seg_level(self) -> float:
        return _db_to_seg(_lin_to_db(max(self.level, 1e-7)))

    def seg_peak(self) -> int:
        return int(_db_to_seg(_lin_to_db(max(self.peak, 1e-7))))

    def clipped(self) -> bool:
        return self.peak >= 1.0

    def reset(self):
        self.level = self.peak = 0.0
        self._hold = 0


# ── scale labels shown on the full meter ─────────────────────────────────────
_SCALE_MARKS = {0: "0", -3: "-3", -6: "-6", -9: "-9",
                -12: "-12", -18: "-18", -24: "-24", -36: "-36", -48: "-48"}


# ── widget ────────────────────────────────────────────────────────────────────
class VUMeter(QWidget):
    """
    Usage:
        m = VUMeter(stereo=True)   # full master meter
        m = VUMeter(compact=True)  # narrow strip beside a fader

        # called every 50 ms:
        m.push(left_peak, right_peak)
    """

    # geometry
    SEG_GAP  = 1
    MARGIN_T = 4
    MARGIN_B = 4

    def __init__(self, stereo: bool = False, compact: bool = False,
                 label: str = "", col_w: int = None, show_scale: bool = True,
                 parent=None):
        super().__init__(parent)
        self._stereo     = stereo
        self._compact    = compact
        self._label      = label
        self._show_scale = show_scale
        self._L          = _Chan()
        self._R          = _Chan()

        if compact:
            self._col_w   = col_w if col_w is not None else 16
            self._col_gap = 0
            self._scale_w = 0
            cols = 2 if stereo else 1
            w = cols * self._col_w + max(0, cols - 1) * 3 + 4
        else:
            self._col_w   = col_w if col_w is not None else 14
            self._col_gap = 4
            self._scale_w = 32 if show_scale else 0
            cols = 2 if stereo else 1
            w = self._scale_w + cols * self._col_w + max(0, cols-1)*self._col_gap + 6

        self.setFixedWidth(w)
        self.setMinimumHeight(40)
        if not compact:
            self.mousePressEvent = lambda _: (self._L.reset(),
                                              self._R.reset(), self.update())

    def push(self, left: float, right: float = 0.0):
        self._L.push(left)
        if self._stereo:
            self._R.push(right)
        self.update()

    # ── paint ─────────────────────────────────────────────────
    def paintEvent(self, _):
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        W, H = self.width(), self.height()
        MT, MB = self.MARGIN_T, self.MARGIN_B

        label_h = 12 if self._label else 0
        avail   = H - MT - MB - label_h

        # segment geometry
        seg_h   = max(3, (avail - (_N - 1) * self.SEG_GAP) // _N)
        meter_h = _N * seg_h + (_N - 1) * self.SEG_GAP  # actual drawn height
        # top-align the meter block
        top_y   = MT

        # background
        qp.fillRect(0, 0, W, H, QColor("#0e0e0e"))

        # draw scale (full mode only)
        if not self._compact and self._show_scale and self._scale_w > 0:
            self._draw_scale(qp, 0, top_y, self._scale_w, meter_h, seg_h)

        # columns
        col_start = self._scale_w + (2 if not self._compact else 2)
        chans = [(self._L, col_start)]
        if self._stereo:
            chans.append((self._R, col_start + self._col_w + self._col_gap))

        for chan, cx in chans:
            self._draw_col(qp, cx, top_y, meter_h, seg_h, chan)

        # label
        if self._label:
            qp.setPen(QPen(QColor("#555"), 1))
            qp.setFont(QFont("Courier New", 7))
            qp.drawText(0, H - label_h, W, label_h,
                        Qt.AlignmentFlag.AlignCenter, self._label)
        qp.end()

    def _seg_y(self, i: int, top_y: int, meter_h: int, seg_h: int) -> int:
        """Y of segment i (i=0 is bottom, i=N-1 is top)."""
        return top_y + meter_h - (i + 1) * seg_h - i * self.SEG_GAP

    # Pre-build QColor objects once so the hot paint loop does zero allocation
    _SEG_COLS = [(QColor(off), QColor(on), QColor(glow))
                 for _, _, off, on, glow in _SEGS]
    _PK_COLS  = [QColor("#88ffaa")] * max(1, _N - 10) + \
                [QColor("#ffee00")] * 6 + \
                [QColor("#ff4400")] * 3 + \
                [QColor("#ff6666")]

    def _draw_col(self, qp, cx, top_y, meter_h, seg_h, chan: _Chan):
        seg_lv = chan.seg_level()
        seg_pk = chan.seg_peak()
        cw     = self._col_w

        for i in range(_N):
            col_off, col_on, col_glow = self._SEG_COLS[i]
            y  = self._seg_y(i, top_y, meter_h, seg_h)
            sh = max(2, seg_h - 1)

            qp.fillRect(cx, y, cw, sh, col_off)

            if seg_lv >= i + 1:
                frac = 1.0
            elif seg_lv > i:
                frac = seg_lv - i
            else:
                frac = 0.0

            if frac > 0:
                lit = max(2, int(sh * frac))
                ly  = y + sh - lit
                qp.fillRect(cx, ly, cw, lit, col_on)
                # LED lens highlight — top quarter of lit segment is brighter
                hl = max(1, lit // 3)
                qp.fillRect(cx, ly, cw, hl,
                             QColor(col_glow.red(), col_glow.green(),
                                    col_glow.blue(), 90))

            if i == seg_pk and chan.peak > 1e-4:
                pk_idx  = min(i, len(self._PK_COLS) - 1)
                pk_col  = self._PK_COLS[pk_idx]
                dot_h   = max(2, sh - 2)
                qp.fillRect(cx, y + 1, cw, dot_h, pk_col)

    def _draw_scale(self, qp, sx, top_y, sw, meter_h, seg_h):
        qp.setFont(QFont("Courier New", 7))
        for db, lbl in _SCALE_MARKS.items():
            frac = _db_to_seg(float(db))
            i    = min(int(frac), _N - 1)
            y    = self._seg_y(i, top_y, meter_h, seg_h) + seg_h // 2
            col  = "#ff5500" if db >= 0  else \
                   "#ffaa00" if db >= -6 else \
                   "#aaaaaa" if db >= -18 else "#666666"
            qp.setPen(QPen(QColor(col), 1))
            qp.drawLine(sx + sw - 5, y, sx + sw - 1, y)
            qp.drawText(sx, y - 4, sw - 7, 10,
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                        lbl)
