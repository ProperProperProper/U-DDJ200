"""
IdleLightShow — attract-mode animation when no tracks are loaded.

Animates ALL DDJ-200 LEDs + every visible UI element in a coordinated show:
  Hardware:
    • Pad comet: rainbow chase across both decks' pads (all 3 LED layers)
    • PLAY/CUE flash as comet enters each deck
    • SYNC cross-fade: deck 0 first half, deck 1 second half
    • PFL: slow alternating throb between decks
    • Master row: TRANS FX → PHRASE SYNC → MASTER HP ripple
  UI:
    • Pad comet + trail (rainbow, PadWidget)
    • Knobs: random spin + accent colour flicker
    • Mode tabs (STEMS/HOT CUE/BEAT LOOP/BEAT JUMP): random flash
    • Crossfader: slow ping-pong sweep
    • Channel badges (1 / 2): colour cycle
    • NO TRACK status labels: colour pulse
    • FILTER combo boxes: border colour flash
    • TRANS FX button: pulse in sync with hardware
    • Library toolbar buttons: sequential rainbow flash
Stops and fully restores all state the moment any track is loaded.
"""
from __future__ import annotations

import math
import random
from PyQt6.QtCore import QTimer, QObject
from PyQt6.QtGui  import QColor

_PALETTE = [
    "#ff2255",
    "#ff6600",
    "#ffcc00",
    "#22ff66",
    "#00bbff",
    "#aa44ff",
    "#ff22cc",
]

_NPADS  = 7
_NDECK  = 2
_TOTAL  = _NPADS * _NDECK   # 14 comet positions

_LED_ON  = 0x7F
_LED_OFF = 0x00

_MASTER_PERIOD = 10   # frames per master-button step


class IdleLightShow(QObject):
    """
    Usage:
        show = IdleLightShow(controller, [dw1, dw2], engine,
                             mixer_widget=mw, library_widget=lw, parent=self)
        show.start_polling()
    """

    def __init__(self, controller, deck_widgets: list, engine,
                 mixer_widget=None, library_widget=None, parent=None):
        super().__init__(parent)
        self._ctrl    = controller
        self._decks   = deck_widgets
        self._engine  = engine
        self._mixer   = mixer_widget
        self._library = library_widget

        self._active = False
        self._frame  = 0
        self._pass   = 0

        # Saved state for restore
        self._saved_knobs:       list = []
        self._saved_mode_styles: list = []
        self._saved_xfader_pos:  float = 0.0
        self._saved_badge_styles: list = []
        self._saved_status_styles: list = []
        self._saved_combo_styles:  list = []
        self._saved_transfx_style: str = ""
        self._saved_lib_styles:    list = []

        self._anim = QTimer(self)
        self._anim.setInterval(80)
        self._anim.timeout.connect(self._tick)

        self._poll = QTimer(self)
        self._poll.setInterval(600)
        self._poll.timeout.connect(self._check)

    def start_polling(self):
        self._poll.start()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def _check(self):
        empty = (not self._engine.deck(0).file_path and
                 not self._engine.deck(1).file_path)
        if empty and not self._active:
            self._start()
        elif not empty and self._active:
            self._stop()

    def _start(self):
        self._active = True
        self._frame  = 0
        self._pass   = 0
        self._save_state()
        self._anim.start()

    def _stop(self):
        self._active = False
        self._anim.stop()
        self._frame = 0
        self._restore_state()
        for dw in self._decks:
            try:
                dw._refresh_pads()
            except Exception:
                pass
        if self._ctrl._connected:
            try:
                self._ctrl._leds_off()
                self._ctrl._init_leds()
            except Exception:
                pass

    # ── save / restore ────────────────────────────────────────────────────────

    def _all_knobs(self):
        if not self._mixer:
            return []
        out = []
        for ch_list in self._mixer._eq_knobs:
            out.extend(ch_list)
        out.extend(self._mixer._fx_knobs)
        return out

    def _lib_btns(self):
        if not self._library:
            return []
        attrs = ['_btn_scan', '_btn_clear', '_btn_clear_cache',
                 '_btn_stems_inspect', '_btn_inspect_all', '_btn_d1', '_btn_d2']
        return [getattr(self._library, a) for a in attrs
                if hasattr(self._library, a)]

    def _save_state(self):
        self._saved_knobs = [(k, k.value(), QColor(k._accent))
                             for k in self._all_knobs()]

        self._saved_mode_styles = []
        for dw in self._decks:
            for mode, btn in dw._mode_btns.items():
                self._saved_mode_styles.append(
                    (btn, btn.styleSheet(), btn.isChecked()))

        if self._mixer:
            self._saved_xfader_pos = self._mixer._xfader.position()

            self._saved_badge_styles = [
                (b, b.styleSheet()) for b in self._mixer._badges]

            self._saved_status_styles = [
                (lbl, lbl.styleSheet())
                for lbl in self._mixer._stems_lbl if lbl]

            self._saved_combo_styles = [
                (cb, cb.styleSheet()) for cb in self._mixer._fx_combos]

            if hasattr(self._mixer, '_trans_fx_btn'):
                self._saved_transfx_style = self._mixer._trans_fx_btn.styleSheet()

        self._saved_lib_styles = [
            (b, b.styleSheet()) for b in self._lib_btns()]

    def _restore_state(self):
        for k, val, accent in self._saved_knobs:
            try:
                k._accent = accent
                k.setValue(val)
                k.update()
            except Exception:
                pass

        for btn, style, checked in self._saved_mode_styles:
            try:
                btn.setStyleSheet(style)
                btn.setChecked(checked)
            except Exception:
                pass

        if self._mixer:
            try:
                self._mixer._xfader.set_position(self._saved_xfader_pos)
            except Exception:
                pass
            for b, style in self._saved_badge_styles:
                try:
                    b.setStyleSheet(style)
                except Exception:
                    pass
            for lbl, style in self._saved_status_styles:
                try:
                    lbl.setStyleSheet(style)
                except Exception:
                    pass
            for cb, style in self._saved_combo_styles:
                try:
                    cb.setStyleSheet(style)
                except Exception:
                    pass
            if hasattr(self._mixer, '_trans_fx_btn') and self._saved_transfx_style:
                try:
                    self._mixer._trans_fx_btn.setStyleSheet(self._saved_transfx_style)
                    self._mixer._trans_fx_btn.setChecked(False)
                except Exception:
                    pass

        for b, style in self._saved_lib_styles:
            try:
                b.setStyleSheet(style)
            except Exception:
                pass

        self._saved_knobs = []
        self._saved_mode_styles = []
        self._saved_badge_styles = []
        self._saved_status_styles = []
        self._saved_combo_styles = []
        self._saved_lib_styles = []

    # ── tick ──────────────────────────────────────────────────────────────────

    def _tick(self):
        f    = self._frame
        head = f % _TOTAL
        if head == 0 and f > 0:
            self._pass += 1

        color  = _PALETTE[self._pass % len(_PALETTE)]
        color2 = _PALETTE[(self._pass + 1) % len(_PALETTE)]

        # ── pad comet (UI) ────────────────────────────────────────────────────
        for di, dw in enumerate(self._decks):
            for pi, pad in enumerate(dw._pad_btns):
                abs_pos = di * _NPADS + pi
                dist    = (head - abs_pos) % _TOTAL
                if dist == 0:
                    pad.set_state("", color, True)
                elif dist == 1:
                    pad.set_state("", color, True)
                elif dist == 2:
                    pad.set_state("", color2, False)
                elif dist == _TOTAL - 1:
                    pad.set_state("", color2, False)
                else:
                    pad.set_state("", "#2a2a3a", False)

        # ── knobs: random spin + accent flash (every 2 frames) ───────────────
        if f % 2 == 0:
            kc = _PALETTE[(f // 2) % len(_PALETTE)]
            for k in self._all_knobs():
                try:
                    k._accent = QColor(kc)
                    k.setValue(random.randint(0, 127))
                    k.update()
                except Exception:
                    pass

        # ── mode tabs: random flash per deck (every 3 frames) ─────────────────
        if f % 3 == 0:
            tc = _PALETTE[(f // 3) % len(_PALETTE)]
            _lit = (
                f"QPushButton{{background:{tc};border:1px solid {tc};"
                f"border-radius:3px;color:#000000;"
                f"font-size:7px;font-weight:bold;letter-spacing:0.5px;padding:0 5px;}}"
            )
            _dim = (
                "QPushButton{background:#181820;border:1px solid #1e1e2a;"
                "border-radius:3px;color:#222230;"
                "font-size:7px;font-weight:bold;letter-spacing:0.5px;padding:0 5px;}"
            )
            for dw in self._decks:
                lit_mode = random.choice(list(dw._mode_btns.keys()))
                for mode, btn in dw._mode_btns.items():
                    btn.setStyleSheet(_lit if mode == lit_mode else _dim)
                    btn.setChecked(mode == lit_mode)

        if self._mixer:
            self._tick_mixer_ui(f, color)
            self._tick_vu(f)

        self._tick_lib_btns(f)

        # ── hardware ──────────────────────────────────────────────────────────
        if self._ctrl._connected:
            self._tick_hardware(f, head)

        self._frame += 1

    def _tick_mixer_ui(self, f: int, color: str):
        # ── crossfader: slow sinusoidal ping-pong ─────────────────────────────
        pos = math.sin(f * 0.06) * 0.9   # −0.9 .. +0.9
        try:
            self._mixer._xfader.set_position(pos)
        except Exception:
            pass

        # ── channel badges: colour cycle ──────────────────────────────────────
        badge_colors = [
            _PALETTE[f       % len(_PALETTE)],
            _PALETTE[(f + 3) % len(_PALETTE)],
        ]
        for i, badge in enumerate(self._mixer._badges):
            c = badge_colors[i % 2]
            try:
                badge.setStyleSheet(
                    f"color:#000000;font-size:11px;font-weight:bold;"
                    f"background:{c};border-radius:3px;letter-spacing:0px;")
            except Exception:
                pass

        # ── NO TRACK status labels: colour pulse ──────────────────────────────
        sc = _PALETTE[(f // 4) % len(_PALETTE)]
        for lbl in self._mixer._stems_lbl:
            if lbl:
                try:
                    lbl.setStyleSheet(
                        f"color:{sc};font-size:7px;background:transparent;"
                        f"letter-spacing:1px;")
                except Exception:
                    pass

        # ── FILTER combo boxes: flashing border ───────────────────────────────
        if f % 4 == 0:
            fc = _PALETTE[(f // 4 + 2) % len(_PALETTE)]
            _combo_style = (
                f"QComboBox{{background:#0d0d14;color:{fc};"
                f"border:1px solid {fc};border-radius:2px;"
                f"font-size:7px;padding:0 4px;letter-spacing:1px;}}"
                f"QComboBox::drop-down{{border:none;width:12px;}}"
                f"QComboBox QAbstractItemView{{background:#111120;"
                f"color:#777788;selection-background-color:#1e1e30;"
                f"font-size:7px;}}"
            )
            for cb in self._mixer._fx_combos:
                try:
                    cb.setStyleSheet(_combo_style)
                except Exception:
                    pass

        # ── TRANS FX button: pulse every 5 frames ─────────────────────────────
        if hasattr(self._mixer, '_trans_fx_btn'):
            pulse_on = (f // 5) % 2 == 0
            tc = _PALETTE[(f // 5) % len(_PALETTE)]
            try:
                btn = self._mixer._trans_fx_btn
                btn.blockSignals(True)
                if pulse_on:
                    btn.setStyleSheet(
                        f"QPushButton{{background:{tc};border:1px solid {tc};"
                        f"border-radius:4px;color:#000000;"
                        f"font-size:8px;font-weight:bold;letter-spacing:1px;"
                        f"padding:0 12px;}}")
                    btn.setChecked(True)
                else:
                    btn.setStyleSheet(
                        "QPushButton{background:#111118;border:1px solid #222230;"
                        "border-radius:4px;color:#252535;"
                        "font-size:8px;font-weight:bold;letter-spacing:1px;"
                        "padding:0 12px;}")
                    btn.setChecked(False)
                btn.blockSignals(False)
            except Exception:
                pass

    def _tick_lib_btns(self, f: int):
        btns = self._lib_btns()
        if not btns:
            return
        # Sequential flash: each button lights up in turn
        if f % 2 == 0:
            lit_idx = (f // 2) % len(btns)
            bc      = _PALETTE[(f // 2) % len(_PALETTE)]
            for i, btn in enumerate(btns):
                if i == lit_idx:
                    try:
                        btn.setStyleSheet(
                            f"QPushButton{{background:{bc};border:1px solid {bc};"
                            f"border-radius:3px;color:#000000;"
                            f"font-size:9px;font-weight:bold;"
                            f"letter-spacing:0.5px;padding:0 10px;}}"
                            f"QPushButton:hover{{background:{bc};}}")
                    except Exception:
                        pass
                else:
                    try:
                        btn.setStyleSheet(
                            "QPushButton{background:#0d0d14;border:1px solid #151520;"
                            "border-radius:3px;color:#1e1e2a;"
                            "font-size:9px;font-weight:bold;"
                            "letter-spacing:0.5px;padding:0 10px;}"
                        )
                    except Exception:
                        pass

    def _tick_vu(self, f: int):
        # Push fake levels into the VU meters so they dance during the show.
        # Channel VUs: alternating sine waves offset between decks.
        # Master VU: combined pulse on the beat of the comet sweep.
        import math
        try:
            # Channel VUs — out-of-phase sine so they move independently
            ch1_level = (math.sin(f * 0.18) * 0.5 + 0.5) ** 1.5
            ch2_level = (math.sin(f * 0.18 + math.pi * 0.7) * 0.5 + 0.5) ** 1.5
            self._mixer._vu_ch1.push(ch1_level)
            self._mixer._vu_ch2.push(ch2_level)
        except Exception:
            pass
        try:
            # Master VU — peaks on every comet-sweep entry (~1.1 s period)
            master_level = (math.sin(f * 0.13) * 0.4 + 0.6) ** 2
            self._mixer._vu_master.push(master_level, master_level * 0.85)
        except Exception:
            pass

    def _tick_hardware(self, f: int, head: int):
        from .ddj200 import (PAD_N, BL_BASE, SP_BASE, CH,
                              NOTE_PLAY, NOTE_CUE, NOTE_SYNC, NOTE_PFL,
                              NOTE_TRANS_FX, NOTE_PHRASE_SYNC, NOTE_MASTER_HP)
        s = self._ctrl._send

        # pad comet (all 3 layers)
        for di in range(_NDECK):
            ch = PAD_N[di]
            for pi in range(_NPADS):
                abs_pos = di * _NPADS + pi
                dist    = (head - abs_pos) % _TOTAL
                vel = _LED_ON if dist <= 1 else _LED_OFF
                s(ch, pi,            vel)
                s(ch, BL_BASE + pi,  vel)
                s(ch, SP_BASE + pi,  vel)

        # PLAY / CUE — flash as comet enters each deck
        for di in range(_NDECK):
            entry = di * _NPADS
            dist  = (head - entry) % _TOTAL
            flash = dist < 3
            s(CH[di], NOTE_PLAY, _LED_ON if flash else _LED_OFF)
            s(CH[di], NOTE_CUE,  _LED_ON if flash else _LED_OFF)

        # SYNC — deck 0 first half, deck 1 second half
        s(CH[0], NOTE_SYNC, _LED_ON if head < _NPADS  else _LED_OFF)
        s(CH[1], NOTE_SYNC, _LED_ON if head >= _NPADS else _LED_OFF)

        # PFL — slow throb alternating decks
        pfl_phase = (f // 12) % 2
        s(CH[0], NOTE_PFL, _LED_ON if pfl_phase == 0 else _LED_OFF)
        s(CH[1], NOTE_PFL, _LED_ON if pfl_phase == 1 else _LED_OFF)

        # Master row ripple
        master_step = (f // _MASTER_PERIOD) % 4
        s(0x96, NOTE_TRANS_FX,    _LED_ON if master_step == 0 else _LED_OFF)
        s(0x96, NOTE_PHRASE_SYNC, _LED_ON if master_step == 1 else _LED_OFF)
        s(0x96, NOTE_MASTER_HP,   _LED_ON if master_step == 2 else _LED_OFF)
