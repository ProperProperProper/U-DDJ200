"""
MixerWidget — DDJ-200-style layout.
Custom hand-painted Knob replaces QDial for a professional look.
"""
from __future__ import annotations

import math
import operator
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                              QPushButton, QFrame, QComboBox)
from PyQt6.QtCore import Qt, QTimer, QPointF, pyqtSignal
from PyQt6.QtGui  import (QPainter, QPen, QColor, QFont,
                           QRadialGradient, QLinearGradient)

from ..core.mixer  import Mixer, FX_NAMES
from ..core.engine import AudioEngine
from .vu_meter     import VUMeter


# ── Custom rotary knob ────────────────────────────────────────────────────────

class Knob(QWidget):
    """Hand-painted rotary knob. Compatible with QDial interface."""
    valueChanged = pyqtSignal(int)   # 0..127

    _START =  -135  # degrees CCW from top (fully counter-clockwise)
    _SWEEP =   270  # total arc

    def __init__(self, val: int = 64, size: int = 50,
                 accent: str = "#ffffff", parent=None):
        super().__init__(parent)
        self._val    = val
        self._accent = QColor(accent)
        self._drag   = False
        self._dy_ref = 0
        self._v_ref  = val
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.SizeVerCursor)
        self.setToolTip("Drag · Shift = fine · Double-click = centre")

    # ── public API (QDial-compatible) ─────────────────────────────────────────

    def value(self) -> int:
        return self._val

    def setValue(self, v: int, emit: bool = False):
        v = max(0, min(127, int(v)))
        if v == self._val:
            return
        self._val = v
        self.update()
        if emit and not self.signalsBlocked():
            self.valueChanged.emit(v)

    # ── paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, _):  # noqa: C901
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)

        S    = self.width()
        cx   = cy = S / 2.0
        frac = self._val / 127.0
        ac   = self._accent

        # Radii (from outside in)
        R_out  = S / 2.0 - 1.5   # metal ring outer
        R_ring = R_out - 4.5     # metal ring inner / tick track outer
        R_tck  = R_ring - 7.0    # tick track inner
        R_body = R_tck - 2.0     # main knob face

        # ── drop shadow ───────────────────────────────────────────────────────
        qp.setPen(Qt.PenStyle.NoPen)
        for i in range(5):
            qp.setBrush(QColor(0, 0, 0, 50 - i * 8))
            d = 0.8 + i * 0.7
            qp.drawEllipse(QPointF(cx + d * 0.5, cy + d),
                           R_body + 3 + i, R_body + 3 + i)

        # ── metal ring — linear gradient faking directional light ─────────────
        # Top-left = bright silver, bottom-right = dark gunmetal
        ring_grad = QLinearGradient(cx - R_out, cy - R_out,
                                    cx + R_out, cy + R_out)
        ring_grad.setColorAt(0.00, QColor("#d8d8e4"))   # bright silver
        ring_grad.setColorAt(0.30, QColor("#a0a0b4"))
        ring_grad.setColorAt(0.55, QColor("#383844"))   # dark gunmetal
        ring_grad.setColorAt(0.80, QColor("#505060"))
        ring_grad.setColorAt(1.00, QColor("#888898"))   # slight rim bounce
        qp.setPen(Qt.PenStyle.NoPen)
        qp.setBrush(ring_grad)
        qp.drawEllipse(QPointF(cx, cy), R_out, R_out)

        # ── tick track (dark groove inside the ring) ──────────────────────────
        qp.setBrush(QColor("#080810"))
        qp.setPen(QPen(QColor("#040408"), 1))
        qp.drawEllipse(QPointF(cx, cy), R_ring, R_ring)

        # ── tick marks (arc segments — high contrast, clearly visible) ────────
        N = 11
        for i in range(N):
            tf    = i / (N - 1)
            angle = math.radians(self._START + tf * self._SWEEP)
            s_, c_ = math.sin(angle), math.cos(angle)
            ox = cx + R_ring * s_;  oy = cy - R_ring * c_
            ix = cx + R_tck  * s_;  iy = cy - R_tck  * c_
            if tf <= frac:
                col = QColor(ac.red(), ac.green(), ac.blue(),
                             int(170 + tf * 85))
                pw  = 2.5
            else:
                col = QColor("#1e1e2e")
                pw  = 1.8
            qp.setPen(QPen(col, pw, Qt.PenStyle.SolidLine,
                           Qt.PenCapStyle.RoundCap))
            qp.drawLine(QPointF(ox, oy), QPointF(ix, iy))

        # ── shadow groove between tick track and body ─────────────────────────
        qp.setPen(Qt.PenStyle.NoPen)
        qp.setBrush(QColor("#020205"))
        qp.drawEllipse(QPointF(cx, cy), R_tck, R_tck)

        # ── knob face — strong directional lighting ───────────────────────────
        face = QRadialGradient(cx - R_body * 0.35, cy - R_body * 0.40,
                               R_body * 1.45)
        face.setColorAt(0.00, QColor("#52525e"))   # lit top-left
        face.setColorAt(0.25, QColor("#303038"))
        face.setColorAt(0.60, QColor("#1a1a20"))
        face.setColorAt(1.00, QColor("#0c0c10"))   # deep shadow
        qp.setPen(QPen(QColor("#1c1c24"), 1))
        qp.setBrush(face)
        qp.drawEllipse(QPointF(cx, cy), R_body, R_body)

        # accent rim catch-light (bottom — colour bounced from below)
        rim = QRadialGradient(cx, cy + R_body * 0.80, R_body * 0.50)
        rim.setColorAt(0.0, QColor(ac.red(), ac.green(), ac.blue(), 28))
        rim.setColorAt(1.0, QColor(0, 0, 0, 0))
        qp.setPen(Qt.PenStyle.NoPen)
        qp.setBrush(rim)
        qp.drawEllipse(QPointF(cx, cy), R_body, R_body)

        # broad specular highlight (top-left dome)
        sp1 = QRadialGradient(cx - R_body * 0.30, cy - R_body * 0.36,
                              R_body * 0.58)
        sp1.setColorAt(0.0, QColor(255, 255, 255, 65))
        sp1.setColorAt(0.5, QColor(255, 255, 255, 18))
        sp1.setColorAt(1.0, QColor(255, 255, 255, 0))
        qp.setBrush(sp1)
        qp.drawEllipse(QPointF(cx, cy), R_body, R_body)

        # sharp specular hot-spot
        sp2 = QRadialGradient(cx - R_body * 0.33, cy - R_body * 0.40,
                              R_body * 0.13)
        sp2.setColorAt(0.0, QColor(255, 255, 255, 220))
        sp2.setColorAt(0.5, QColor(255, 255, 255, 50))
        sp2.setColorAt(1.0, QColor(255, 255, 255, 0))
        qp.setBrush(sp2)
        qp.drawEllipse(QPointF(cx, cy), R_body, R_body)

        # inner bevel stroke
        qp.setBrush(Qt.BrushStyle.NoBrush)
        qp.setPen(QPen(QColor(255, 255, 255, 20), 1.5))
        qp.drawEllipse(QPointF(cx, cy), R_body - 0.75, R_body - 0.75)

        # ── indicator — groove shadow + 3-pass glow + crisp line ─────────────
        ind     = math.radians(self._START + frac * self._SWEEP)
        si, ci_ = math.sin(ind), math.cos(ind)
        x_far   = cx + (R_body - 4.0) * si
        y_far   = cy - (R_body - 4.0) * ci_
        x_near  = cx + (R_body * 0.22) * si
        y_near  = cy - (R_body * 0.22) * ci_

        # groove shadow
        qp.setPen(QPen(QColor(0, 0, 0, 180), 4,
                       Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        qp.drawLine(QPointF(x_near, y_near), QPointF(x_far, y_far))
        # outer glow
        qp.setPen(QPen(QColor(ac.red(), ac.green(), ac.blue(), 40), 9,
                       Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        qp.drawLine(QPointF(x_near, y_near), QPointF(x_far, y_far))
        # mid glow
        qp.setPen(QPen(QColor(ac.red(), ac.green(), ac.blue(), 100), 4,
                       Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        qp.drawLine(QPointF(x_near, y_near), QPointF(x_far, y_far))
        # bright core
        qp.setPen(QPen(ac, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        qp.drawLine(QPointF(x_near, y_near), QPointF(x_far, y_far))

        # centre jewel
        piv = QRadialGradient(cx - 0.6, cy - 0.7, R_body * 0.20)
        piv.setColorAt(0.0, QColor(ac.red(), ac.green(), ac.blue(), 230))
        piv.setColorAt(0.6, QColor(ac.red(), ac.green(), ac.blue(), 100))
        piv.setColorAt(1.0, QColor(ac.red(), ac.green(), ac.blue(), 0))
        qp.setPen(Qt.PenStyle.NoPen)
        qp.setBrush(piv)
        qp.drawEllipse(QPointF(cx, cy), 5.0, 5.0)
        qp.setBrush(QColor(255, 255, 255, 210))
        qp.drawEllipse(QPointF(cx - 0.6, cy - 0.7), 1.4, 1.4)

        qp.end()

    # ── mouse ─────────────────────────────────────────────────────────────────

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._drag   = True
            self._dy_ref = ev.pos().y()
            self._v_ref  = self._val

    def mouseMoveEvent(self, ev):
        if not self._drag:
            return
        fine  = bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        dy    = self._dy_ref - ev.pos().y()
        scale = 0.28 if fine else 1.1
        new_v = max(0, min(127, int(self._v_ref + dy * scale)))
        if new_v != self._val:
            self._val = new_v
            self.update()
            if not self.signalsBlocked():
                self.valueChanged.emit(self._val)

    def mouseReleaseEvent(self, _):
        self._drag = False

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._val = 64
            self.update()
            if not self.signalsBlocked():
                self.valueChanged.emit(64)


# ── helpers ───────────────────────────────────────────────────────────────────

def _knob(val: int = 64, size: int = 56, accent: str = "#aaaabc") -> Knob:
    return Knob(val=val, size=size, accent=accent)


def _lbl(text: str, color: str = "#5a5a70", px: int = 8,
         bold: bool = False, spacing: str = "1px") -> QLabel:
    l = QLabel(text)
    l.setAlignment(Qt.AlignmentFlag.AlignCenter)
    l.setStyleSheet(
        f"color:{color};font-size:{px}px;"
        f"font-weight:{'bold' if bold else 'normal'};"
        f"letter-spacing:{spacing};background:transparent;")
    return l


def _sep() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setStyleSheet("color:#1a1a24;background:#1a1a24;")
    f.setFixedHeight(1)
    return f


import math as _math


def _db(gain: float) -> str:
    if gain < 1e-4:
        return "−∞"
    db = 20.0 * _math.log10(gain)
    return f"{db:+.1f}"


# ── MixerWidget ───────────────────────────────────────────────────────────────

class MixerWidget(QWidget):

    def __init__(self, mixer: Mixer, engine: AudioEngine = None,
                 master_peak_fn=None, deck_peak_fns=None, parent=None):
        super().__init__(parent)
        self.mixer           = mixer
        self.engine          = engine
        self._master_peak_fn = master_peak_fn
        self._deck_peak_fns  = deck_peak_fns or []

        self._stems_lbl: list = [None, None]
        self._badges:    list = []
        self._eq_knobs:  list = [[], []]
        self._fx_knobs:  list = []
        self._pfl_btns:  list = []

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(50)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self.setObjectName("MixerWidget")
        self.setFixedWidth(400)
        self.setStyleSheet("background:#090910;")

        root = QVBoxLayout(self)
        root.setContentsMargins(5, 5, 5, 5)
        root.setSpacing(4)

        self._eq_knobs  = [[], []]
        self._fx_knobs  = []
        self._fx_combos = []
        self._pfl_btns  = []
        self._vol_lbl   = [None, None]

        # Professional palette
        # Ch1 = steel blue, Ch2 = warm amber — enough contrast, not garish
        _CH_ACCENT  = ["#3ab8d8", "#d89840"]
        _CH_LABELS  = ["1", "2"]
        _CH_DARK    = ["#0c0c14", "#100e0a"]

        # EQ band definitions — neutral labels, accent color comes from channel
        _EQ_BANDS = [
            ("HI",  "high"),
            ("MID", "mid"),
            ("LOW", "low"),
            ("FX",  "fx"),
        ]

        # ── Three-column strip: CH1 | CENTER | CH2 ────────────────────────────
        strip = QHBoxLayout()
        strip.setSpacing(5)
        strip.setContentsMargins(0, 0, 0, 0)

        for ch in range(2):
            accent = _CH_ACCENT[ch]
            dark   = _CH_DARK[ch]

            panel = QWidget()
            panel.setStyleSheet(
                f"QWidget#ch{ch}"
                f"{{background:{dark};"
                f"border:1px solid #1c1c26;border-radius:5px;}}")
            panel.setObjectName(f"ch{ch}")

            cv = QVBoxLayout(panel)
            cv.setSpacing(0)
            cv.setContentsMargins(6, 6, 6, 6)
            cv.setAlignment(Qt.AlignmentFlag.AlignTop)

            # ── header: row1 = badge | stretch | combo  /  row2 = status ─────────
            hdr_wrap = QVBoxLayout()
            hdr_wrap.setSpacing(2)
            hdr_wrap.setContentsMargins(0, 0, 0, 4)

            hdr = QHBoxLayout()
            hdr.setSpacing(4)

            badge = QLabel(_CH_LABELS[ch])
            badge.setFixedSize(20, 20)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setStyleSheet(
                f"color:#ffffff;font-size:11px;font-weight:bold;"
                f"background:{accent};border-radius:3px;"
                f"letter-spacing:0px;")
            hdr.addWidget(badge)
            self._badges.append(badge)

            hdr.addStretch()

            combo = QComboBox()
            combo.addItems(FX_NAMES)
            combo.setCurrentText("FILTER")
            combo.setFixedHeight(18)
            combo.setFixedWidth(56)
            combo.setStyleSheet(
                "QComboBox{background:#0d0d14;color:#555568;"
                "border:1px solid #202030;border-radius:2px;"
                "font-size:7px;padding:0 3px;}"
                "QComboBox::drop-down{border:none;width:10px;}"
                "QComboBox QAbstractItemView{background:#111120;"
                "color:#777788;selection-background-color:#1e1e30;"
                "font-size:7px;}")
            combo.currentTextChanged.connect(
                lambda name, c=ch: self.mixer.set_fx_type(c, name))
            hdr.addWidget(combo)
            self._fx_combos.append(combo)

            hdr_wrap.addLayout(hdr)

            status = QLabel("NO TRACK")
            status.setAlignment(Qt.AlignmentFlag.AlignLeft)
            status.setStyleSheet(
                "color:#252535;font-size:7px;background:transparent;"
                "letter-spacing:0.5px;")
            self._stems_lbl[ch] = status
            hdr_wrap.addWidget(status)

            cv.addLayout(hdr_wrap)
            cv.addWidget(_sep())
            cv.addSpacing(2)

            # ── EQ + FX knobs ──────────────────────────────────────────────────
            for el, eb in _EQ_BANDS:
                ek = _knob(val=64, size=50, accent=accent)
                if eb == "fx":
                    ek.valueChanged.connect(
                        lambda v, c=ch: self.mixer.set_fx_raw(c, v))
                    self._fx_knobs.append(ek)
                else:
                    ek.valueChanged.connect(
                        lambda v, c=ch, b=eb: self.mixer.set_eq_raw(c, b, v))

                kw = QVBoxLayout()
                kw.setSpacing(2)
                kw.setContentsMargins(0, 2, 0, 6)
                kw.setAlignment(Qt.AlignmentFlag.AlignHCenter)
                kw.addWidget(_lbl(el, "#565668", 7, True, "2px"))
                kw.addWidget(ek, alignment=Qt.AlignmentFlag.AlignHCenter)
                cv.addLayout(kw)
                self._eq_knobs[ch].append(ek)

            cv.addSpacing(4)
            cv.addWidget(_sep())
            cv.addSpacing(6)

            # ── channel VU ────────────────────────────────────────────────────
            vu = VUMeter(compact=True, stereo=False, col_w=28)
            vu.setFixedHeight(68)
            if ch == 0:
                self._vu_ch1 = vu
            else:
                self._vu_ch2 = vu
            cv.addWidget(vu, alignment=Qt.AlignmentFlag.AlignHCenter)
            cv.addStretch(1)

            # ── CUE button ────────────────────────────────────────────────────
            cue = QPushButton("C U E")
            cue.setCheckable(True)
            cue.setFixedHeight(26)
            cue.setStyleSheet(
                f"QPushButton{{background:#0d0d14;border:1px solid #1e1e2c;"
                f"border-radius:3px;color:#2a2a3a;font-size:8px;"
                f"font-weight:bold;letter-spacing:3px;}}"
                f"QPushButton:hover{{border:1px solid #2e2e3e;color:#3a3a4e;}}"
                f"QPushButton:checked{{background:#c8900a;"
                f"border:1px solid #e0a820;color:#ffffff;}}")
            cue.toggled.connect(
                lambda on, c=ch: operator.setitem(self.mixer.pfl, c, on))
            self._pfl_btns.append(cue)
            cv.addWidget(cue)

            vl = QLabel()
            vl.hide()
            self._vol_lbl[ch] = vl
            cv.addWidget(vl)

            strip.addWidget(panel, 5)

            if ch == 0:
                # ── Center column ──────────────────────────────────────────────
                cpanel = QWidget()
                cpanel.setStyleSheet(
                    "QWidget#center{background:#0c0c12;"
                    "border:1px solid #181824;border-radius:5px;}")
                cpanel.setObjectName("center")
                center = QVBoxLayout(cpanel)
                center.setSpacing(4)
                center.setContentsMargins(4, 6, 4, 6)
                center.setAlignment(Qt.AlignmentFlag.AlignTop)

                tfx = QPushButton("⚡  TRANS FX")
                tfx.setCheckable(True)
                tfx.setFixedHeight(22)
                tfx.setStyleSheet(
                    "QPushButton{color:#282838;background:#0c0c12;"
                    "border:1px solid #181828;border-radius:3px;"
                    "font-size:7px;font-weight:bold;letter-spacing:1px;}"
                    "QPushButton:hover{color:#383848;border:1px solid #242436;}"
                    "QPushButton:checked{color:#fff;background:#cc5500;"
                    "border:1px solid #ee7722;}"
                    "QPushButton:pressed{background:#441c00;}")
                tfx.toggled.connect(self._on_trans_fx)
                self._trans_fx_btn = tfx
                center.addWidget(tfx)

                center.addWidget(_sep())

                ml = QLabel("MASTER")
                ml.setAlignment(Qt.AlignmentFlag.AlignCenter)
                ml.setStyleSheet(
                    "color:#303040;font-size:7px;font-weight:bold;"
                    "letter-spacing:3px;background:transparent;")
                center.addWidget(ml)

                self._master_vol_lbl = QLabel()
                self._master_vol_lbl.hide()
                center.addWidget(self._master_vol_lbl)

                center.addStretch(1)

                self._vu_master = VUMeter(
                    compact=False, stereo=True, col_w=20, show_scale=False)
                self._vu_master.setFixedHeight(155)
                center.addWidget(
                    self._vu_master, alignment=Qt.AlignmentFlag.AlignHCenter)

                center.addStretch(1)

                ps = QPushButton("⌛\nPHRASE\nSYNC")
                ps.setCheckable(True)
                ps.setFixedHeight(50)
                ps.setStyleSheet(
                    "QPushButton{color:#222234;background:#0c0c12;"
                    "border:1px solid #181828;border-radius:3px;"
                    "font-size:6px;font-weight:bold;letter-spacing:1px;}"
                    "QPushButton:hover{color:#2e2e44;border:1px solid #222238;}"
                    "QPushButton:checked{color:#fff;background:#5520bb;"
                    "border:1px solid #8844ee;}"
                    "QPushButton:pressed{color:#fff;background:#3a1580;"
                    "border:1px solid #5520bb;}")
                ps.clicked.connect(self._on_phrase_sync)
                self._phrase_sync_btn = ps
                center.addWidget(ps)

                strip.addWidget(cpanel, 3)

        root.addLayout(strip)
        root.addWidget(_sep())

        # ── crossfader ────────────────────────────────────────────────────────
        xf_hdr = QHBoxLayout()
        xf_hdr.setContentsMargins(4, 0, 4, 0)
        xf_hdr.addWidget(_lbl("A", _CH_ACCENT[0], 9, True))
        xf_hdr.addStretch()
        xf_hdr.addWidget(_lbl("CROSSFADER", "#252535", 7, False, "2px"))
        xf_hdr.addStretch()
        xf_hdr.addWidget(_lbl("B", _CH_ACCENT[1], 9, True))
        root.addLayout(xf_hdr)

        from .pro_fader import XFader
        self._xfader = XFader()
        self._xfader.posChanged.connect(self._on_xf)
        root.addWidget(self._xfader)

    # ── handlers ──────────────────────────────────────────────────────────────

    def _on_xf(self, pos: float):
        self.mixer.crossfader = pos

    def _on_phrase_sync(self):
        if self.engine:
            for d in range(2):
                if self.engine.deck(d).sync_follower:
                    self.engine.phrase_sync(d)
                    break
            else:
                self.engine.phrase_sync(1)
        self._phrase_sync_btn.setChecked(True)
        QTimer.singleShot(500, lambda: self._phrase_sync_btn.setChecked(False))

    def _on_trans_fx(self, active: bool):
        import threading as _threading
        ctrl = getattr(self.engine, '_controller_ref', None) if self.engine else None
        if ctrl:
            ctrl._trans_fx_active = active
            if active:
                for ch in range(2):
                    self.mixer.set_fx_type(ch, "FILTER")
                    if ch < len(self._fx_combos):
                        self._fx_combos[ch].blockSignals(True)
                        self._fx_combos[ch].setCurrentText("FILTER")
                        self._fx_combos[ch].blockSignals(False)
                _threading.Thread(target=ctrl._run_trans_fx_sweep,
                                  daemon=True).start()
            else:
                for ch in range(2):
                    self.mixer.set_fx_raw(ch, 64)
        else:
            for ch in range(2):
                if active:
                    self.mixer.set_fx_type(ch, "FILTER")
                    self.mixer.set_fx_raw(ch, 20)
                else:
                    self.mixer.set_fx_raw(ch, 64)

    # ── tick ──────────────────────────────────────────────────────────────────

    def _tick(self):
        for ch in range(2):
            if self._vol_lbl[ch]:
                self._vol_lbl[ch].setText(_db(self.mixer.volumes[ch]) + " dB")
        self._master_vol_lbl.setText(_db(self.mixer.master_vol) + " dB")

        self._xfader.set_position(self.mixer.crossfader)

        for ch in range(2):
            if ch < len(self._pfl_btns):
                btn   = self._pfl_btns[ch]
                state = self.mixer.pfl[ch]
                if btn.isChecked() != state:
                    btn.blockSignals(True)
                    btn.setChecked(state)
                    btn.blockSignals(False)

        if self.engine and getattr(self.engine, '_phrase_sync_triggered', False):
            self.engine._phrase_sync_triggered = False
            self._phrase_sync_btn.setChecked(True)
            QTimer.singleShot(500, lambda: self._phrase_sync_btn.setChecked(False))

        if self.engine:
            ctrl = getattr(self.engine, '_controller_ref', None)
            if ctrl:
                tfx = getattr(ctrl, '_trans_fx_active', False)
                if self._trans_fx_btn.isChecked() != tfx:
                    self._trans_fx_btn.blockSignals(True)
                    self._trans_fx_btn.setChecked(tfx)
                    self._trans_fx_btn.blockSignals(False)

        if self.engine:
            for ch in range(2):
                deck       = self.engine.deck(ch)
                has_track  = deck.audio is not None
                stems_ready = deck.stems_ready
                lbl = self._stems_lbl[ch]
                if lbl is not None:
                    if not has_track:
                        lbl.setText("NO TRACK")
                        lbl.setStyleSheet(
                            "color:#252535;font-size:7px;background:transparent;"
                            "letter-spacing:1px;")
                    elif not stems_ready:
                        lbl.setText("◌  LOADING")
                        lbl.setStyleSheet(
                            "color:#6b4a00;font-size:7px;background:transparent;"
                            "letter-spacing:1px;")
                    else:
                        lbl.setText("●  STEMS")
                        lbl.setStyleSheet(
                            "color:#1a6640;font-size:7px;background:transparent;"
                            "letter-spacing:1px;")

        _EQ_BANDS = ["high", "mid", "low"]
        for ch in range(2):
            eq = self.mixer.eqs[ch]
            for i, band in enumerate(_EQ_BANDS):
                v   = getattr(eq, band, 0.0)
                raw = int((v + 1.0) / 2.0 * 127)
                k   = self._eq_knobs[ch][i]
                if abs(k.value() - raw) > 1:
                    k.blockSignals(True)
                    k.setValue(raw)
                    k.blockSignals(False)
            depth = self.mixer.fx[ch].depth_value
            raw   = int(depth * 127)
            k     = self._eq_knobs[ch][3]
            if abs(k.value() - raw) > 1:
                k.blockSignals(True)
                k.setValue(raw)
                k.blockSignals(False)

        self._vu_ch1.push(self.mixer.ch_peak[0])
        self._vu_ch2.push(self.mixer.ch_peak[1])

        if self._master_peak_fn:
            L, R = self._master_peak_fn()
            self._vu_master.push(L, R)
