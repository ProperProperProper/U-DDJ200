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

        self._stem_knobs:   list = [[], []]   # [ch][blend_idx 0-1] → QDial
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
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(4)

        # 3 blend knobs (one per stem pair) + 3 EQ knobs (HI/MID/LOW) + 1 FX knob
        # Each blend knob: hard left = kill left stem, center = both full,
        #                  hard right = kill right stem
        # Row 1: VOC←→DRM blend | HI EQ
        # Row 2: BSS←→OTH blend | MID EQ
        # Row 3: LEAD←→PAD blend | LOW EQ
        # Row 4: FX only

        _BLEND_ROWS = [
            # (blend_label, left_color, right_color, pair_idx, eq_label, eq_color, eq_band)
            ("VOC ←→ DRM",  "#aa44ff", "#ff4466", 0, "HI",  "#00c8ff", "high"),
            ("BSS ←→ OTH",  "#ff9900", "#44aaff", 1, "MID", "#44cc88", "mid"),
            ("LEAD ←→ PAD", "#44ff88", "#ff88cc", 2, "LOW", "#ff7733", "low"),
        ]
        _EQ_ONLY_ROWS = [
            ("FX", "#bb88ff", "fx"),
        ]

        self._stem_knobs = [[], []]
        self._eq_knobs   = [[], []]   # indices 0..2 = HI/MID/LOW, index 3 = FX depth
        self._fx_knobs   = []
        self._fx_combos  = []

        # Column header
        hdr = QHBoxLayout()
        hdr.addStretch(1)
        hdr.addWidget(_lbl("BLEND", "#555", 7))
        hdr.addWidget(_lbl("EQ", "#555", 7))
        hdr.addStretch(2)
        hdr.addWidget(_lbl("BLEND", "#555", 7))
        hdr.addWidget(_lbl("EQ", "#555", 7))
        hdr.addStretch(1)
        root.addLayout(hdr)

        # Stems status row — one indicator per channel, always visible, changes state
        stems_row = QHBoxLayout()
        stems_row.addStretch(1)
        for ch in range(2):
            lbl = QLabel("● STEMS READY")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(
                "color:#00ff88;font-size:9px;font-weight:bold;"
                "background:#001a0d;border:1px solid #00ff88;"
                "border-radius:4px;padding:2px 6px;")
            self._stems_lbl[ch] = lbl
            stems_row.addWidget(lbl)
            if ch == 0:
                stems_row.addStretch(2)
        stems_row.addStretch(1)
        root.addLayout(stems_row)

        # FX type selectors
        fx_combo_row = QHBoxLayout()
        fx_combo_row.addStretch(1)
        for ch in range(2):
            combo = QComboBox()
            combo.addItems(FX_NAMES)
            combo.setCurrentText("FILTER")
            combo.setFixedWidth(84)
            combo.setStyleSheet(
                "QComboBox{background:#0e0e18;color:#bb88ff;border:1px solid #3a2a4a;"
                "border-radius:3px;font-size:8px;font-weight:bold;padding:1px 3px;}"
                "QComboBox::drop-down{border:none;}"
                "QComboBox QAbstractItemView{background:#0e0e18;color:#bb88ff;"
                "selection-background-color:#2a1a3a;font-size:8px;}")
            combo.currentTextChanged.connect(
                lambda name, c=ch: self.mixer.set_fx_type(c, name))
            fx_combo_row.addWidget(combo)
            self._fx_combos.append(combo)
            if ch == 0:
                fx_combo_row.addStretch(2)
        fx_combo_row.addStretch(1)
        root.addLayout(fx_combo_row)

        # Blend + EQ rows — both use QDial, same style as EQ knobs
        for bl, lc, rc, pair_idx, el, ec, eb in _BLEND_ROWS:
            left_name, right_name = bl.split(" ←→ ")
            row = QHBoxLayout()
            row.setContentsMargins(0, 2, 0, 2)
            row.setSpacing(2)
            row.addStretch(1)

            for ch in range(2):
                # Blend dial
                s_col = QVBoxLayout()
                s_col.setSpacing(1)
                s_col.setContentsMargins(0, 0, 0, 0)
                s_col.setAlignment(Qt.AlignmentFlag.AlignHCenter)
                lbl_w = QLabel(
                    f'<span style="color:{lc};font-weight:bold">{left_name}</span>'
                    f'<span style="color:#888"> ←→ </span>'
                    f'<span style="color:{rc};font-weight:bold">{right_name}</span>')
                lbl_w.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lbl_w.setStyleSheet("font-size:7px;")
                s_col.addWidget(lbl_w)
                sk = _knob(val=64, size=44)
                sk.valueChanged.connect(
                    lambda v, c=ch, p=pair_idx:
                        self.engine and self.engine.deck(c).set_stem_blend(p, v / 127.0))
                s_col.addWidget(sk, alignment=Qt.AlignmentFlag.AlignHCenter)
                row.addLayout(s_col)
                self._stem_knobs[ch].append(sk)

                # EQ dial
                e_col = QVBoxLayout()
                e_col.setSpacing(1)
                e_col.setContentsMargins(0, 0, 0, 0)
                e_col.setAlignment(Qt.AlignmentFlag.AlignHCenter)
                e_col.addWidget(_lbl(el, ec, 8, True))
                ek = _knob(val=64, size=44)
                ek.valueChanged.connect(
                    lambda v, c=ch, b=eb: self.mixer.set_eq_raw(c, b, v))
                e_col.addWidget(ek, alignment=Qt.AlignmentFlag.AlignHCenter)
                row.addLayout(e_col)
                self._eq_knobs[ch].append(ek)

                if ch == 0:
                    row.addStretch(2)

            row.addStretch(1)
            root.addLayout(row)

        # FX-only row
        for eq_idx, (el, ec, eb) in enumerate(_EQ_ONLY_ROWS):
            row = QHBoxLayout()
            row.setContentsMargins(0, 2, 0, 2)
            row.setSpacing(2)
            row.addStretch(1)
            for ch in range(2):
                e_col = QVBoxLayout()
                e_col.setSpacing(1)
                e_col.setContentsMargins(0, 0, 0, 0)
                e_col.setAlignment(Qt.AlignmentFlag.AlignHCenter)
                e_col.addWidget(_lbl(el, ec, 8, True))
                ek = _knob(val=64, size=44)
                if eb == "fx":
                    ek.valueChanged.connect(lambda v, c=ch: self.mixer.set_fx_raw(c, v))
                    self._fx_knobs.append(ek)
                else:
                    ek.valueChanged.connect(
                        lambda v, c=ch, b=eb: self.mixer.set_eq_raw(c, b, v))
                e_col.addWidget(ek, alignment=Qt.AlignmentFlag.AlignHCenter)
                row.addLayout(e_col)
                self._eq_knobs[ch].append(ek)
                if ch == 0:
                    row.addStretch(2)
            row.addStretch(1)
            root.addLayout(row)

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

    def reset_stem_knobs(self, ch: int):
        """Reset all stem blend knobs to center when a track loads."""
        for k in self._stem_knobs[ch]:
            k.blockSignals(True)
            k.setValue(64)
            k.blockSignals(False)

    def _on_xf(self, pos: float):
        self.mixer.crossfader = pos

    # ── tick ──────────────────────────────────────────────────

    def _tick(self):
        # Volume dB readouts
        for ch in range(2):
            self._vol_lbl[ch].setText(_db(self.mixer.volumes[ch]) + " dB")

        # Crossfader
        self._xfader.set_position(self.mixer.crossfader)

        # Stems: tick-sync knobs from deck state — identical pattern to EQ knobs
        if self.engine:
            from ..core.deck import Deck
            for ch in range(2):
                deck = self.engine.deck(ch)
                has_track = deck.audio is not None
                stems_ready = deck.stems_ready

                for pair_idx, (stem_a, stem_b) in enumerate(Deck.STEM_PAIRS):
                    vol_a = deck.stem_volumes.get(stem_a, 1.0)
                    vol_b = deck.stem_volumes.get(stem_b, 1.0)
                    if vol_a < 1.0:
                        raw = int(vol_a * 0.5 * 127)
                    elif vol_b < 1.0:
                        raw = int((1.0 - vol_b * 0.5) * 127)
                    else:
                        raw = 64
                    raw = max(0, min(127, raw))
                    k = self._stem_knobs[ch][pair_idx]
                    if abs(k.value() - raw) > 1:
                        k.blockSignals(True)
                        k.setValue(raw)
                        k.blockSignals(False)

                lbl = self._stems_lbl[ch]
                if lbl is not None:
                    if not has_track:
                        lbl.setText("○ NO TRACK")
                        lbl.setStyleSheet(
                            "color:#444;font-size:9px;font-weight:bold;"
                            "background:#111;border:1px solid #333;"
                            "border-radius:4px;padding:2px 6px;")
                    elif not stems_ready:
                        lbl.setText("◌ STEMS LOADING…")
                        lbl.setStyleSheet(
                            "color:#ffaa00;font-size:9px;font-weight:bold;"
                            "background:#1a1200;border:1px solid #ffaa00;"
                            "border-radius:4px;padding:2px 6px;")
                    else:
                        lbl.setText("● STEMS READY")
                        lbl.setStyleSheet(
                            "color:#00ff88;font-size:9px;font-weight:bold;"
                            "background:#001a0d;border:1px solid #00ff88;"
                            "border-radius:4px;padding:2px 6px;")

        # Sync EQ knobs (indices 0-2) and FX knob (index 3) from mixer state
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
            # FX depth (index 3)
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
