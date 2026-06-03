"""
MixerWidget — DDJ-200-style layout, no faders.
Volume is controlled by hardware faders; displayed as dB values.
Three VU meters: CH1 | MASTER | CH2.
"""
from __future__ import annotations

import math
import operator
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                              QDial, QPushButton, QFrame, QSizePolicy,
                              QComboBox)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui  import QPainter, QColor

from ..core.mixer  import Mixer, FX_NAMES
from ..core.engine import AudioEngine
from .vu_meter     import VUMeter


def _knob(val: int = 64, size: int = 52) -> QDial:
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

        self._stem_knobs: list = [[], []]   # [ch][stem_idx] → QDial
        self._eq_knobs:   list = [[], []]   # [ch][band_idx 0=hi,1=mid,2=lo] → QDial
        self._fx_knobs:   list = []         # [ch] → QDial (shift+knob = CFX)
        self._pfl_btns:   list = []

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(50)

    def _build_ui(self):
        self.setObjectName("MixerWidget")
        self.setFixedWidth(280)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(4)

        # ── 4 stem rows: DRUM / BASS / OTHER / VOX ───────────
        # No shift  → stem volume (these knobs)
        # Shift+EQ  → EQ band (handled in MIDI handler)
        # Shift+VOX → Color FX (handled in MIDI handler)
        # HI knob = VOX, MID knob = OTHER, LOW knob = BASS — matches physical EQ layout
        # Drums are controlled via pads only (same as GRV6 Stem ISO)
        _STEM_META = [
            ("VOX",   "#aa44ff", "vocals"),
            ("OTHER", "#44aaff", "other"),
            ("BASS",  "#ff9900", "bass"),
        ]
        self._stem_knobs = [[], []]
        for label, color, stem_name in _STEM_META:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addStretch(1)
            for ch in range(2):
                col = QVBoxLayout()
                col.setSpacing(2)
                col.setContentsMargins(0, 0, 0, 0)
                col.setAlignment(Qt.AlignmentFlag.AlignHCenter)
                col.addWidget(_lbl(label, color, 9, True))
                k = _knob(val=64)    # center = physical knob default = full volume
                k.valueChanged.connect(
                    lambda v, c=ch, sn=stem_name:
                        self.engine and self.engine.deck(c).set_stem_volume(sn, v / 127.0))
                col.addWidget(k, alignment=Qt.AlignmentFlag.AlignHCenter)
                row.addLayout(col)
                self._stem_knobs[ch].append(k)
                if ch == 0:
                    row.addStretch(2)
            row.addStretch(1)
            root.addLayout(row)

        root.addWidget(_sep())

        # ── EQ rows: HI / MID / LOW ───────────────────────────
        # Shift+EQ hardware knobs update these. Center = 0 dB, left = kill.
        _EQ_META = [
            ("HI",  "#00c8ff", "high"),
            ("MID", "#44cc88", "mid"),
            ("LOW", "#ff9900", "low"),
        ]
        self._eq_knobs = [[], []]
        for label, color, band in _EQ_META:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addStretch(1)
            for ch in range(2):
                col = QVBoxLayout()
                col.setSpacing(2)
                col.setContentsMargins(0, 0, 0, 0)
                col.setAlignment(Qt.AlignmentFlag.AlignHCenter)
                col.addWidget(_lbl(label, color, 9, True))
                k = _knob(val=64)   # center = 0 dB
                k.valueChanged.connect(
                    lambda v, c=ch, b=band:
                        self.mixer.set_eq_raw(c, b, v))
                col.addWidget(k, alignment=Qt.AlignmentFlag.AlignHCenter)
                row.addLayout(col)
                self._eq_knobs[ch].append(k)
                if ch == 0:
                    row.addStretch(2)
            row.addStretch(1)
            root.addLayout(row)

        root.addWidget(_sep())

        # ── FX row — shift+knob on hardware sends here ────────
        self._fx_knobs    = []
        self._fx_combos   = []
        fx_row = QHBoxLayout()
        fx_row.setContentsMargins(0, 0, 0, 0)
        fx_row.addStretch(1)
        for ch in range(2):
            col = QVBoxLayout()
            col.setSpacing(2)
            col.setContentsMargins(0, 0, 0, 0)
            col.setAlignment(Qt.AlignmentFlag.AlignHCenter)

            # Effect type selector
            combo = QComboBox()
            combo.addItems(FX_NAMES)
            combo.setCurrentText("FILTER")
            combo.setFixedWidth(90)
            combo.setStyleSheet(
                "QComboBox{background:#0e0e18;color:#bb88ff;border:1px solid #3a2a4a;"
                "border-radius:3px;font-size:9px;font-weight:bold;padding:1px 4px;}"
                "QComboBox::drop-down{border:none;}"
                "QComboBox QAbstractItemView{background:#0e0e18;color:#bb88ff;"
                "selection-background-color:#2a1a3a;font-size:9px;}")
            combo.currentTextChanged.connect(
                lambda name, c=ch: self.mixer.set_fx_type(c, name))
            col.addWidget(combo, alignment=Qt.AlignmentFlag.AlignHCenter)
            self._fx_combos.append(combo)

            col.addWidget(_lbl("FX", "#bb88ff", 9, True))
            k = _knob(val=64)   # centre = bypass
            k.valueChanged.connect(lambda v, c=ch: self.mixer.set_fx_raw(c, v))
            col.addWidget(k, alignment=Qt.AlignmentFlag.AlignHCenter)
            col.addWidget(_lbl("← DRY  WET →", "#444", 7))
            fx_row.addLayout(col)
            self._fx_knobs.append(k)
            if ch == 0:
                fx_row.addStretch(2)
        fx_row.addStretch(1)
        root.addLayout(fx_row)

        root.addWidget(_sep())

        # ── CH labels + PFL ───────────────────────────────────
        pfl_row = QHBoxLayout()
        pfl_row.setSpacing(0)
        pfl_row.addStretch(1)
        pfl_row.addWidget(_lbl("CH 1", "#00c8ff", 9, True))
        for ch in range(2):
            b = QPushButton("PFL")
            b.setCheckable(True)
            b.setObjectName("PflButton")
            b.setFixedSize(40, 22)
            b.toggled.connect(lambda on, c=ch: operator.setitem(self.mixer.pfl, c, on))
            if ch == 0:
                pfl_row.addSpacing(6)
            pfl_row.addWidget(b)
            self._pfl_btns.append(b)
        pfl_row.addSpacing(6)
        pfl_row.addWidget(_lbl("CH 2", "#ff7a01", 9, True))
        pfl_row.addStretch(1)
        root.addLayout(pfl_row)
        root.addWidget(_sep())

        # ── Volume values row (dB readout, no faders) ─────────
        vol_row = QHBoxLayout()
        vol_row.setSpacing(4)
        self._vol_lbl = [QLabel("0.0 dB"), QLabel("0.0 dB")]
        for ch, lbl in enumerate(self._vol_lbl):
            color = "#00c8ff" if ch == 0 else "#ff7a01"
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(
                f"color:{color};font-family:'Courier New';"
                f"font-size:11px;font-weight:bold;")
        vol_row.addWidget(self._vol_lbl[0], 1)
        vol_row.addWidget(_lbl("VOL", "#333", 7), 0)
        vol_row.addWidget(self._vol_lbl[1], 1)
        root.addLayout(vol_row)

        # ── Three VU meters: CH1 | MASTER | CH2 (expanding) ───
        vu_section = QHBoxLayout()
        vu_section.setSpacing(6)
        vu_section.setContentsMargins(0, 0, 0, 0)

        VU_H = 130   # fixed height for the entire VU row

        # CH1 compact VU
        ch1_col = QVBoxLayout()
        ch1_col.setSpacing(2)
        ch1_col.addWidget(_lbl("CH1", "#00c8ff", 7))
        self._vu_ch1 = VUMeter(compact=True, stereo=False)
        self._vu_ch1.setFixedHeight(VU_H - 14)
        ch1_col.addWidget(self._vu_ch1)
        vu_section.addLayout(ch1_col, 1)

        # Master VU — stereo, with dBFS scale
        master_col = QVBoxLayout()
        master_col.setSpacing(2)
        master_col.addWidget(_lbl("MASTER", "#888", 7, True))
        self._vu_master = VUMeter(compact=False, stereo=True)
        self._vu_master.setFixedHeight(VU_H - 14)
        master_col.addWidget(self._vu_master)
        vu_section.addLayout(master_col, 2)

        # CH2 compact VU
        ch2_col = QVBoxLayout()
        ch2_col.setSpacing(2)
        ch2_col.addWidget(_lbl("CH2", "#ff7a01", 7))
        self._vu_ch2 = VUMeter(compact=True, stereo=False)
        self._vu_ch2.setFixedHeight(VU_H - 14)
        ch2_col.addWidget(self._vu_ch2)
        vu_section.addLayout(ch2_col, 1)

        root.addLayout(vu_section, 0)
        root.addWidget(_sep())

        root.addWidget(_sep())

        # ── Crossfader ────────────────────────────────────────
        xf_hdr = QHBoxLayout()
        xf_hdr.addWidget(_lbl("A", "#00c8ff", 10, True))
        xf_hdr.addStretch()
        xf_hdr.addWidget(_lbl("CROSSFADER", "#444", 8))
        xf_hdr.addStretch()
        xf_hdr.addWidget(_lbl("B", "#ff7a01", 10, True))
        root.addLayout(xf_hdr)

        from .pro_fader import XFader
        self._xfader = XFader()
        self._xfader.posChanged.connect(self._on_xf)
        root.addWidget(self._xfader)

    # ── handlers ──────────────────────────────────────────────

    def _on_xf(self, pos: float):
        self.mixer.crossfader = pos

    # ── tick ──────────────────────────────────────────────────

    def _tick(self):
        # Volume dB readouts
        for ch in range(2):
            self._vol_lbl[ch].setText(_db(self.mixer.volumes[ch]) + " dB")

        # Crossfader
        self._xfader.set_position(self.mixer.crossfader)

        # Sync stem knobs from deck state (hardware may have moved them)
        if self.engine:
            _STEM_BANDS = ["vocals", "other", "bass"]
            for ch in range(2):
                deck = self.engine.deck(ch)
                for i, stem in enumerate(_STEM_BANDS):
                    vol = deck.stem_volumes.get(stem, 1.0)
                    # Inverse of sqrt taper: raw = vol² × 0.5 × 127
                    # vol≥1 → show center (64) — hardware knob at ≥center gives unity
                    raw = 64 if vol >= 1.0 else int(vol ** 2 * 63.5)
                    k = self._stem_knobs[ch][i]
                    if abs(k.value() - raw) > 1:
                        k.blockSignals(True)
                        k.setValue(raw)
                        k.blockSignals(False)

        # Sync EQ knobs from mixer state (hardware Shift+EQ may have moved them)
        _EQ_BANDS = ["high", "mid", "low"]
        for ch in range(2):
            eq = self.mixer.eqs[ch]
            for i, band in enumerate(_EQ_BANDS):
                v = getattr(eq, band, 0.0)   # −1.0 … +1.0
                raw = int((v + 1.0) / 2.0 * 127)
                k = self._eq_knobs[ch][i]
                if abs(k.value() - raw) > 1:
                    k.blockSignals(True)
                    k.setValue(raw)
                    k.blockSignals(False)

        # Sync FX knobs from mixer state (CFX hardware knob may have moved them)
        for ch in range(2):
            depth = self.mixer.fx[ch].depth_value   # 0..1, 0.5 = bypass
            raw = int(depth * 127)
            k = self._fx_knobs[ch]
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
