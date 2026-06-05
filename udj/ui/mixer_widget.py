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

        _EQ_ROWS = [
            ("HI",  "#00c8ff", "high"),
            ("MID", "#44cc88", "mid"),
            ("LOW", "#ff7733", "low"),
            ("FX",  "#bb88ff", "fx"),
        ]
        self._stem_btns   = [[], []]
        self._eq_knobs    = [[], []]
        self._fx_knobs    = []
        self._fx_combos   = []

        # ── Stems status + Transition FX button ───────────────
        top_ctrl_row = QHBoxLayout()
        top_ctrl_row.addStretch(1)
        for ch in range(2):
            lbl = QLabel("● STEMS READY")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(
                "color:#00ff88;font-size:8px;font-weight:bold;"
                "background:#001a0d;border:1px solid #00ff88;"
                "border-radius:4px;padding:1px 5px;")
            self._stems_lbl[ch] = lbl
            top_ctrl_row.addWidget(lbl)
            if ch == 0:
                top_ctrl_row.addStretch(2)
        top_ctrl_row.addStretch(1)
        root.addLayout(top_ctrl_row)

        # Transition FX button — applies deep filter sweep across both channels
        trans_row = QHBoxLayout()
        trans_row.addStretch(1)
        self._trans_fx_btn = QPushButton("⚡ TRANSITION FX")
        self._trans_fx_btn.setCheckable(True)
        self._trans_fx_btn.setFixedHeight(22)
        self._trans_fx_btn.setStyleSheet(
            "QPushButton{color:#555;background:#0e0e18;border:1px solid #2a2a40;"
            "border-radius:4px;padding:0 10px;font-size:9px;font-weight:bold;}"
            "QPushButton:checked{color:#000;background:#ff6600;"
            "border:1px solid #ff8800;}"
            "QPushButton:pressed{background:#1a1000;}")
        self._trans_fx_btn.toggled.connect(self._on_trans_fx)
        trans_row.addWidget(self._trans_fx_btn)
        trans_row.addStretch(1)
        root.addLayout(trans_row)

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

        # EQ + FX knob rows
        for el, ec, eb in _EQ_ROWS:
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
                ek = _knob(val=64, size=52)
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

        # ── CH 1 / CH 2 channel controls (PFL + vol) ──────────
        ch_row = QHBoxLayout()
        ch_row.setSpacing(6)

        for ch in range(2):
            col = "#00c8ff" if ch == 0 else "#ff7a01"
            label = "CH 1" if ch == 0 else "CH 2"
            ch_col = QVBoxLayout()
            ch_col.setSpacing(3)

            # Channel label — large and bright
            ch_lbl = QLabel(label)
            ch_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            ch_lbl.setStyleSheet(
                f"color:{col};font-size:14px;font-weight:bold;"
                f"background:#0e0e18;border:1px solid {col}44;"
                f"border-radius:4px;padding:2px 8px;")
            ch_col.addWidget(ch_lbl)

            # PFL button
            b = QPushButton("CUE")
            b.setCheckable(True)
            b.setObjectName("PflButton")
            b.setFixedHeight(24)
            b.setStyleSheet(
                f"QPushButton{{background:#111;border:1px solid #333;"
                f"border-radius:4px;color:#555;font-size:10px;font-weight:bold;}}"
                f"QPushButton:checked{{background:{col};border:1px solid {col};"
                f"color:#000;}}")
            b.toggled.connect(lambda on, c=ch: operator.setitem(self.mixer.pfl, c, on))
            self._pfl_btns.append(b)
            ch_col.addWidget(b)

            # Vol readout
            vol_lbl = QLabel("0.0 dB")
            vol_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            vol_lbl.setStyleSheet(
                f"color:{col};font-family:'Courier New';"
                f"font-size:10px;font-weight:bold;")
            ch_col.addWidget(vol_lbl)

            ch_row.addLayout(ch_col, 1)
            if not hasattr(self, '_vol_lbl'):
                self._vol_lbl = [None, None]
            self._vol_lbl[ch] = vol_lbl

            if ch == 0:
                # Master column in the middle
                master_col_layout = QVBoxLayout()
                master_col_layout.setSpacing(3)
                master_lbl = QLabel("MASTER")
                master_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                master_lbl.setStyleSheet(
                    "color:#aaa;font-size:11px;font-weight:bold;"
                    "background:#1a1a1a;border:1px solid #444;"
                    "border-radius:4px;padding:2px 8px;")
                self._master_vol_lbl = QLabel("−∞ dB")
                self._master_vol_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self._master_vol_lbl.setStyleSheet(
                    "color:#ccc;font-family:'Courier New';"
                    "font-size:10px;font-weight:bold;")

                # Phrase Sync button — single, in the middle
                phrase_btn = QPushButton("⌛ PHRASE\nSYNC")
                phrase_btn.setFixedHeight(36)
                phrase_btn.setStyleSheet(
                    "QPushButton{color:#888;background:#0e0e18;border:1px solid #444;"
                    "border-radius:4px;font-size:8px;font-weight:bold;}"
                    "QPushButton:pressed{color:#cc99ff;background:#1a1030;"
                    "border:1px solid #8844cc;}")
                phrase_btn.pressed.connect(self._on_phrase_sync)

                master_col_layout.addWidget(master_lbl)
                master_col_layout.addWidget(self._master_vol_lbl)
                master_col_layout.addWidget(phrase_btn)
                ch_row.addLayout(master_col_layout, 1)

        root.addLayout(ch_row)
        root.addWidget(_sep())

        # ── Three VU meters: CH1 | MASTER | CH2 ───────────────
        vu_section = QHBoxLayout()
        vu_section.setSpacing(6)
        vu_section.setContentsMargins(0, 0, 0, 0)

        VU_H = 110

        ch1_col = QVBoxLayout()
        ch1_col.setSpacing(2)
        self._vu_ch1 = VUMeter(compact=True, stereo=False)
        self._vu_ch1.setFixedHeight(VU_H)
        ch1_col.addWidget(self._vu_ch1)
        vu_section.addLayout(ch1_col, 1)

        master_vu_col = QVBoxLayout()
        master_vu_col.setSpacing(2)
        self._vu_master = VUMeter(compact=False, stereo=True)
        self._vu_master.setFixedHeight(VU_H)
        master_vu_col.addWidget(self._vu_master)
        vu_section.addLayout(master_vu_col, 2)

        ch2_col = QVBoxLayout()
        ch2_col.setSpacing(2)
        self._vu_ch2 = VUMeter(compact=True, stereo=False)
        self._vu_ch2.setFixedHeight(VU_H)
        ch2_col.addWidget(self._vu_ch2)
        vu_section.addLayout(ch2_col, 1)

        root.addLayout(vu_section, 0)
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

    def _on_phrase_sync(self):
        if self.engine:
            # Phrase-sync whichever deck is the follower
            for d in range(2):
                if self.engine.deck(d).sync_follower:
                    self.engine.phrase_sync(d)
                    return
            # No follower set — sync deck 1 to deck 0
            self.engine.phrase_sync(1)

    def _on_trans_fx(self, active: bool):
        """Transition FX — deep filter close across both channels."""
        for ch in range(2):
            if active:
                self.mixer.set_fx_type(ch, "FILTER")
                self.mixer.set_fx_raw(ch, 20)   # close filter
            else:
                self.mixer.set_fx_raw(ch, 64)   # bypass

    # ── tick ──────────────────────────────────────────────────

    def _tick(self):
        # Volume dB readouts
        for ch in range(2):
            self._vol_lbl[ch].setText(_db(self.mixer.volumes[ch]) + " dB")
        self._master_vol_lbl.setText(_db(self.mixer.master_vol) + " dB")

        # Crossfader
        self._xfader.set_position(self.mixer.crossfader)

        # Master vol display in vol row (show as dB)
        mv = self.mixer.master_vol
        mv_db = f"M:{_db(mv)} dB" if mv > 0 else "M:−∞"
        # Sync transition FX button from MIDI state
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
