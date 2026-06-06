"""
MixerWidget — DDJ-200-style layout, no faders.
Volume is controlled by hardware faders; displayed as dB values.
Three VU meters: CH1 | MASTER | CH2.
"""
from __future__ import annotations

import math
import operator
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                              QDial, QSlider, QPushButton, QFrame, QSizePolicy,
                              QComboBox)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui  import QPainter, QColor

from ..core.mixer  import Mixer, FX_NAMES
from ..core.engine import AudioEngine
from .vu_meter     import VUMeter


def _knob(val: int = 64, size: int = 46) -> QDial:
    k = QDial()
    k.setRange(0, 127)
    k.setValue(val)
    k.setFixedSize(size, size)
    k.setNotchesVisible(True)
    return k


def _lbl(text: str, color: str = "#666", px: int = 8,
         bold: bool = False) -> QLabel:
    l = QLabel(text)
    l.setAlignment(Qt.AlignmentFlag.AlignCenter)
    l.setStyleSheet(
        f"color:{color};font-size:{px}px;"
        f"font-weight:{'bold' if bold else 'normal'};")
    return l


def _sep() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setStyleSheet("color:#1e1e28;")
    f.setFixedHeight(1)
    return f

def _sep_v() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.VLine)
    f.setStyleSheet("color:#1e1e28;")
    f.setFixedWidth(1)
    return f


def _db(gain: float) -> str:
    if gain < 1e-4:
        return "−∞"
    db = 20.0 * math.log10(gain)
    return f"{db:+.1f}"


class MixerWidget(QWidget):

    def __init__(self, mixer: Mixer, engine: AudioEngine = None,
                 master_peak_fn=None, deck_peak_fns=None, parent=None):
        super().__init__(parent)
        self.mixer           = mixer
        self.engine          = engine
        self._master_peak_fn = master_peak_fn
        self._deck_peak_fns  = deck_peak_fns or []

        self._stems_lbl:    list = [None, None]  # [ch] → QLabel loading indicator
        self._eq_knobs:     list = [[], []]   # [ch][band_idx 0=hi,1=mid,2=lo] → QDial
        self._fx_knobs:     list = []         # [ch] → QDial (shift+knob = CFX)
        self._pfl_btns:     list = []

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(50)

    def _build_ui(self):
        self.setObjectName("MixerWidget")
        self.setFixedWidth(340)

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(3)

        self._eq_knobs  = [[], []]
        self._fx_knobs  = []
        self._fx_combos = []
        self._pfl_btns  = []
        self._vol_lbl   = [None, None]

        _EQ_BANDS = [
            ("HI",  "high", "#00c8ff"),
            ("MID", "mid",  "#44cc88"),
            ("LOW", "low",  "#ff7733"),
            ("FX",  "fx",   "#bb88ff"),
        ]
        _CH_COLORS = ["#00b8ef", "#ff7a01"]
        _CH_LABELS = ["1", "2"]

        # ── Three-column strip: CH1 | CENTER | CH2 ────────────
        strip = QHBoxLayout()
        strip.setSpacing(4)
        strip.setContentsMargins(0, 0, 0, 0)

        ch_vboxes = []
        for ch in range(2):
            col  = _CH_COLORS[ch]
            dark = "#0a0e12" if ch == 0 else "#0e0c0a"

            # Outer frame widget gives channel a subtle panel background
            panel = QWidget()
            panel.setStyleSheet(f"background:{dark};border-radius:4px;")
            cv = QVBoxLayout(panel)
            cv.setSpacing(1)
            cv.setContentsMargins(4, 4, 4, 4)
            cv.setAlignment(Qt.AlignmentFlag.AlignTop)

            # ── Channel number badge ───────────────────────────
            hdr = QHBoxLayout()
            hdr.setSpacing(4)
            num = QLabel(_CH_LABELS[ch])
            num.setFixedSize(18, 18)
            num.setAlignment(Qt.AlignmentFlag.AlignCenter)
            num.setStyleSheet(
                f"color:{col};font-size:11px;font-weight:bold;"
                f"background:{col}18;border:1px solid {col}55;"
                f"border-radius:3px;")
            hdr.addWidget(num)

            # Status dot — tiny, unobtrusive
            lbl = QLabel("—")
            lbl.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
            lbl.setStyleSheet("color:#2a2a2a;font-size:7px;background:transparent;")
            self._stems_lbl[ch] = lbl
            hdr.addWidget(lbl, 1)

            # FX selector — compact, right-aligned
            combo = QComboBox()
            combo.addItems(FX_NAMES)
            combo.setCurrentText("FILTER")
            combo.setFixedHeight(16)
            combo.setFixedWidth(58)
            combo.setStyleSheet(
                "QComboBox{background:#111;color:#666;border:1px solid #252525;"
                "border-radius:2px;font-size:7px;padding:0 3px;}"
                "QComboBox::drop-down{border:none;}"
                "QComboBox QAbstractItemView{background:#111;color:#888;"
                "selection-background-color:#1e1e1e;font-size:7px;}")
            combo.currentTextChanged.connect(
                lambda name, c=ch: self.mixer.set_fx_type(c, name))
            hdr.addWidget(combo)
            self._fx_combos.append(combo)
            cv.addLayout(hdr)

            cv.addWidget(_sep())

            # ── EQ + FX knobs ──────────────────────────────────
            for el, eb, ec in _EQ_BANDS:
                ek = _knob(val=64)
                if eb == "fx":
                    ek.valueChanged.connect(lambda v, c=ch: self.mixer.set_fx_raw(c, v))
                    self._fx_knobs.append(ek)
                else:
                    ek.valueChanged.connect(
                        lambda v, c=ch, b=eb: self.mixer.set_eq_raw(c, b, v))
                kw = QVBoxLayout()
                kw.setSpacing(0)
                kw.setContentsMargins(0, 3, 0, 0)
                kw.setAlignment(Qt.AlignmentFlag.AlignHCenter)
                kw.addWidget(_lbl(el, ec, 7, True))
                kw.addWidget(ek, alignment=Qt.AlignmentFlag.AlignHCenter)
                cv.addLayout(kw)
                self._eq_knobs[ch].append(ek)

            cv.addWidget(_sep())

            # ── VU meter ──────────────────────────────────────
            cv.addSpacing(10)
            vu = VUMeter(compact=True, stereo=False, col_w=28)
            vu.setFixedHeight(72)
            if ch == 0:
                self._vu_ch1 = vu
            else:
                self._vu_ch2 = vu
            cv.addWidget(vu, alignment=Qt.AlignmentFlag.AlignHCenter)
            cv.addStretch(1)

            # ── CUE button ────────────────────────────────────
            cue = QPushButton("CUE")
            cue.setCheckable(True)
            cue.setFixedHeight(24)
            cue.setStyleSheet(
                f"QPushButton{{background:#111;border:1px solid #252525;"
                f"border-radius:3px;color:#3a3a3a;font-size:9px;"
                f"font-weight:bold;letter-spacing:2px;}}"
                f"QPushButton:checked{{background:{col};"
                f"border:1px solid {col};color:#000;}}")
            cue.toggled.connect(lambda on, c=ch: operator.setitem(self.mixer.pfl, c, on))
            self._pfl_btns.append(cue)
            cv.addWidget(cue)

            # hidden dB label — still updated in _tick for reference
            vl = QLabel()
            vl.hide()
            self._vol_lbl[ch] = vl
            cv.addWidget(vl)

            ch_vboxes.append(panel)

        # ── Center column ─────────────────────────────────────
        cpanel = QWidget()
        cpanel.setStyleSheet("background:#0b0b0b;border-radius:4px;")
        center = QVBoxLayout(cpanel)
        center.setSpacing(3)
        center.setContentsMargins(3, 4, 3, 4)
        center.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._trans_fx_btn = QPushButton("⚡ TRANS FX")
        self._trans_fx_btn.setCheckable(True)
        self._trans_fx_btn.setFixedHeight(20)
        self._trans_fx_btn.setStyleSheet(
            "QPushButton{color:#2a2a2a;background:#0d0d0d;border:1px solid #1c1c1c;"
            "border-radius:3px;font-size:7px;font-weight:bold;letter-spacing:1px;}"
            "QPushButton:checked{color:#fff;background:#ff6600;"
            "border:1px solid #ff8800;}"
            "QPushButton:pressed{background:#110600;}")
        self._trans_fx_btn.toggled.connect(self._on_trans_fx)
        center.addWidget(self._trans_fx_btn)

        center.addWidget(_sep())

        ml = QLabel("MASTER")
        ml.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ml.setStyleSheet(
            "color:#444;font-size:8px;font-weight:bold;letter-spacing:2px;"
            "background:transparent;")
        center.addWidget(ml)

        # hidden — kept so _tick doesn't crash
        self._master_vol_lbl = QLabel()
        self._master_vol_lbl.hide()
        center.addWidget(self._master_vol_lbl)

        center.addStretch(1)

        self._vu_master = VUMeter(compact=False, stereo=True, col_w=22, show_scale=False)
        self._vu_master.setFixedHeight(160)
        center.addWidget(self._vu_master, alignment=Qt.AlignmentFlag.AlignHCenter)

        center.addStretch(1)

        ps = QPushButton("⌛\nPHRASE\nSYNC")
        ps.setCheckable(True)
        ps.setFixedHeight(46)
        ps.setStyleSheet(
            "QPushButton{color:#2a2a2a;background:#0d0d0d;"
            "border:1px solid #1c1c1c;border-radius:3px;"
            "font-size:6px;font-weight:bold;letter-spacing:1px;}"
            "QPushButton:checked{color:#fff;background:#6622cc;"
            "border:1px solid #9944ff;}"
            "QPushButton:pressed{color:#fff;background:#4411aa;"
            "border:1px solid #6622cc;}")
        ps.clicked.connect(self._on_phrase_sync)
        self._phrase_sync_btn = ps
        center.addWidget(ps)

        # Assemble strip — channel panels as widgets
        strip.addWidget(ch_vboxes[0], 5)
        strip.addWidget(cpanel, 3)
        strip.addWidget(ch_vboxes[1], 5)
        root.addLayout(strip)

        root.addWidget(_sep())

        # ── Crossfader ────────────────────────────────────────
        xf_hdr = QHBoxLayout()
        xf_hdr.addWidget(_lbl("A", "#00b8ef", 9, True))
        xf_hdr.addStretch()
        xf_hdr.addWidget(_lbl("CROSSFADER", "#282828", 7))
        xf_hdr.addStretch()
        xf_hdr.addWidget(_lbl("B", "#ff7a01", 9, True))
        root.addLayout(xf_hdr)

        from .pro_fader import XFader
        self._xfader = XFader()
        self._xfader.posChanged.connect(self._on_xf)
        root.addWidget(self._xfader)

    # ── handlers ──────────────────────────────────────────────

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
        # Flash the button for 500 ms
        self._phrase_sync_btn.setChecked(True)
        QTimer.singleShot(500, lambda: self._phrase_sync_btn.setChecked(False))

    def _on_trans_fx(self, active: bool):
        """Transition FX — delegates to controller sweep if available."""
        import threading as _threading
        ctrl = getattr(self.engine, '_controller_ref', None) if self.engine else None
        if ctrl:
            # Sync state so _tick doesn't revert the button
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
            # No controller — simple toggle without sweep
            for ch in range(2):
                if active:
                    self.mixer.set_fx_type(ch, "FILTER")
                    self.mixer.set_fx_raw(ch, 20)
                else:
                    self.mixer.set_fx_raw(ch, 64)

    # ── tick ──────────────────────────────────────────────────

    def _tick(self):
        # Channel vol + master vol readouts
        for ch in range(2):
            if self._vol_lbl[ch]:
                self._vol_lbl[ch].setText(_db(self.mixer.volumes[ch]) + " dB")
        self._master_vol_lbl.setText(_db(self.mixer.master_vol) + " dB")

        # Crossfader
        self._xfader.set_position(self.mixer.crossfader)

        # Sync CUE (PFL) buttons from mixer state — hardware can toggle them
        for ch in range(2):
            if ch < len(self._pfl_btns):
                btn = self._pfl_btns[ch]
                state = self.mixer.pfl[ch]
                if btn.isChecked() != state:
                    btn.blockSignals(True)
                    btn.setChecked(state)
                    btn.blockSignals(False)

        # Flash PHRASE SYNC button when triggered from hardware
        if self.engine and getattr(self.engine, '_phrase_sync_triggered', False):
            self.engine._phrase_sync_triggered = False
            self._phrase_sync_btn.setChecked(True)
            QTimer.singleShot(500, lambda: self._phrase_sync_btn.setChecked(False))

        # Sync Transition FX button from hardware MIDI state
        if self.engine:
            ctrl = getattr(self.engine, '_controller_ref', None)
            if ctrl:
                tfx = getattr(ctrl, '_trans_fx_active', False)
                if self._trans_fx_btn.isChecked() != tfx:
                    self._trans_fx_btn.blockSignals(True)
                    self._trans_fx_btn.setChecked(tfx)
                    self._trans_fx_btn.blockSignals(False)

        # Update stem state pills + status badge
        if self.engine:
            from ..core.deck import STEM_NAMES as _SNAMES
            _STEM_COLORS = {"vocals": "#aa44ff", "drums": "#ff4466",
                            "bass": "#ff9900", "other": "#44aaff"}
            for ch in range(2):
                deck = self.engine.deck(ch)
                has_track = deck.audio is not None
                stems_ready = deck.stems_ready


                lbl = self._stems_lbl[ch]
                if lbl is not None:
                    if not has_track:
                        lbl.setText("NO TRACK")
                        lbl.setStyleSheet("color:#252525;font-size:7px;background:transparent;")
                    elif not stems_ready:
                        lbl.setText("◌ LOADING")
                        lbl.setStyleSheet("color:#664400;font-size:7px;background:transparent;")
                    else:
                        lbl.setText("● STEMS")
                        lbl.setStyleSheet("color:#006633;font-size:7px;background:transparent;")

        # Sync EQ knobs (0=HI, 1=MID, 2=LOW) and FX knob (3) from mixer state
        _EQ_BANDS = ["high", "mid", "low"]
        for ch in range(2):
            eq = self.mixer.eqs[ch]
            for i, band in enumerate(_EQ_BANDS):
                v = getattr(eq, band, 0.0)
                raw = int((v + 1.0) / 2.0 * 127)
                k = self._eq_knobs[ch][i]
                if abs(k.value() - raw) > 1:
                    k.blockSignals(True)
                    k.setValue(raw)
                    k.blockSignals(False)
            depth = self.mixer.fx[ch].depth_value
            raw = int(depth * 127)
            k = self._eq_knobs[ch][3]
            if abs(k.value() - raw) > 1:
                k.blockSignals(True)
                k.setValue(raw)
                k.blockSignals(False)

        # Channel VUs
        self._vu_ch1.push(self.mixer.ch_peak[0])
        self._vu_ch2.push(self.mixer.ch_peak[1])

        # Master VU
        if self._master_peak_fn:
            L, R = self._master_peak_fn()
            self._vu_master.push(L, R)
