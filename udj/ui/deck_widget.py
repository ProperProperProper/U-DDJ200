"""
DeckWidget — pro-grade deck panel with live waveform display.
"""
from __future__ import annotations

from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                              QPushButton, QSizePolicy)
from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import QDragEnterEvent, QDropEvent

from ..core.deck import Deck, STEM_NAMES


def _fmt_time(secs: float) -> str:
    secs = max(0.0, secs)
    m = int(secs) // 60
    s = secs % 60
    return f"{m}:{s:05.2f}"


def _main_thread(func):
    QTimer.singleShot(0, func)



class DeckWidget(QWidget):
    AUDIO_EXTS = {".mp3", ".flac", ".wav", ".aiff", ".aif",
                  ".ogg", ".m4a", ".opus"}

    def __init__(self, deck: Deck, deck_label: str = "1",
                 accent: str = "#00c8ff",
                 on_load_track=None,
                 vu_peak_fn=None,   # callable → float (current linear peak)
                 engine=None,
                 parent=None):
        super().__init__(parent)
        self.deck = deck
        self._accent = accent
        self._deck_label = deck_label
        self._on_load_track = on_load_track
        self._vu_peak_fn = vu_peak_fn
        self._engine = engine

        self.setAcceptDrops(True)
        self._build_ui()
        self._connect_deck()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(50)   # 20 fps

    def _build_ui(self):
        self.setObjectName("DeckWidget")
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(6)

        # ── Row 1: Deck# · Title · Artist · Key ───────────────
        row1 = QHBoxLayout()
        row1.setSpacing(8)

        self.lbl_deck = QLabel(self._deck_label)
        self.lbl_deck.setStyleSheet(
            f"color:{self._accent};font-size:32px;font-weight:bold;"
            f"min-width:28px;max-width:36px;")
        self.lbl_deck.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        title_artist = QVBoxLayout()
        title_artist.setSpacing(1)
        self.lbl_title = QLabel("Drop a track here")
        self.lbl_title.setStyleSheet(
            "color:#f0f0f0;font-size:14px;font-weight:bold;")
        self.lbl_title.setSizePolicy(QSizePolicy.Policy.Expanding,
                                     QSizePolicy.Policy.Preferred)
        self.lbl_artist = QLabel("")
        self.lbl_artist.setStyleSheet("color:#777;font-size:11px;")
        title_artist.addWidget(self.lbl_title)
        title_artist.addWidget(self.lbl_artist)

        self.lbl_key = QLabel("")
        self.lbl_key.setStyleSheet(
            "color:#33aacc;font-size:16px;font-weight:bold;"
            "min-width:40px;")
        self.lbl_key.setAlignment(Qt.AlignmentFlag.AlignRight |
                                   Qt.AlignmentFlag.AlignVCenter)

        # Key shift controls
        key_shift_col = QVBoxLayout()
        key_shift_col.setSpacing(1)
        key_shift_col.setContentsMargins(0, 0, 0, 0)
        btn_key_up = QPushButton("▲")
        btn_key_dn = QPushButton("▼")
        for b in (btn_key_up, btn_key_dn):
            b.setFixedSize(20, 16)
            b.setStyleSheet(
                "QPushButton{background:#0e0e18;border:1px solid #2a2a40;"
                "color:#33aacc;font-size:9px;border-radius:2px;}"
                "QPushButton:pressed{background:#1a2a44;}")
        btn_key_up.clicked.connect(lambda: self._key_shift(+1))
        btn_key_dn.clicked.connect(lambda: self._key_shift(-1))
        self._btn_key_reset = QPushButton("0")
        self._btn_key_reset.setFixedSize(20, 14)
        self._btn_key_reset.setStyleSheet(
            "QPushButton{background:#0e0e18;border:1px solid #2a2a40;"
            "color:#555;font-size:7px;border-radius:2px;}"
            "QPushButton:pressed{background:#1a1a2a;}")
        self._btn_key_reset.clicked.connect(lambda: self._key_shift(0, reset=True))
        key_shift_col.addWidget(btn_key_up)
        key_shift_col.addWidget(self._btn_key_reset)
        key_shift_col.addWidget(btn_key_dn)

        row1.addWidget(self.lbl_deck)
        row1.addLayout(title_artist, 1)
        row1.addWidget(self.lbl_key)
        row1.addLayout(key_shift_col)
        root.addLayout(row1)

        # ── BPM · Time · CLIP ─────────────────────────────────
        bpm_time_row = QHBoxLayout()
        bpm_time_row.setSpacing(10)

        bpm_col = QVBoxLayout()
        bpm_col.setSpacing(0)
        self.lbl_bpm = QLabel("---.--")
        self.lbl_bpm.setStyleSheet(
            "color:#ff9900;font-family:'Courier New';"
            "font-size:36px;font-weight:bold;")
        self.lbl_bpm_unit = QLabel("BPM")
        self.lbl_bpm_unit.setStyleSheet(
            "color:#664400;font-size:10px;font-weight:bold;")
        bpm_col.addWidget(self.lbl_bpm)
        bpm_col.addWidget(self.lbl_bpm_unit)

        time_col = QHBoxLayout()
        time_col.setSpacing(4)
        self.lbl_time_icon = QLabel("⏸")
        self.lbl_time_icon.setStyleSheet("color:#883300;font-size:13px;")
        self.lbl_time_icon.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self.lbl_time = QLabel("-0:00.00")
        self.lbl_time.setStyleSheet(
            "color:#eeeeee;font-family:'Courier New';"
            "font-size:42px;font-weight:bold;")
        time_col.addWidget(self.lbl_time_icon)
        time_col.addWidget(self.lbl_time)

        self._clip_lbl = QLabel("CLIP")
        self._clip_lbl.setStyleSheet(
            "color:#ff0000;background:#1a0000;border:1px solid #cc0000;"
            "border-radius:3px;padding:2px 6px;font-size:9px;font-weight:bold;")
        self._clip_lbl.setVisible(False)
        self._clip_lbl.mousePressEvent = lambda _: self._clip_lbl.setVisible(False)

        bpm_time_row.addLayout(bpm_col)
        bpm_time_row.addLayout(time_col)
        bpm_time_row.addStretch()
        bpm_time_row.addWidget(self._clip_lbl,
                               alignment=Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(bpm_time_row)

        # ── Rate ──────────────────────────────────────────────
        self.lbl_rate = QLabel("+0.00%")
        self.lbl_rate.setStyleSheet(
            "color:#1a5577;font-family:'Courier New';"
            "font-size:13px;font-weight:bold;")
        root.addWidget(self.lbl_rate)

        # ── Sync / Loop badges ────────────────────────────────
        badges = QHBoxLayout()
        badges.setSpacing(6)

        self.badge_master = QLabel("MASTER")
        self.badge_master.setStyleSheet(
            "color:#ff9900;background:#1a0e00;border:1px solid #cc6600;"
            "border-radius:3px;padding:2px 8px;font-size:10px;font-weight:bold;")
        self.badge_master.setVisible(False)

        self.badge_follower = QLabel("FOLLOWER")
        self.badge_follower.setStyleSheet(
            "color:#00aaff;background:#00101a;border:1px solid #0066aa;"
            "border-radius:3px;padding:2px 8px;font-size:10px;font-weight:bold;")
        self.badge_follower.setVisible(False)

        self.badge_loop = QLabel("⟲ LOOP")
        self.badge_loop.setStyleSheet(
            "color:#00dd88;background:#001a0e;border:1px solid #009955;"
            "border-radius:3px;padding:2px 8px;font-size:10px;font-weight:bold;")
        self.badge_loop.setVisible(False)

        self.badge_loop_arm = QLabel("⏺ LOOP ARM")
        self.badge_loop_arm.setStyleSheet(
            "color:#ff4488;background:#1a0010;border:1px solid #cc0066;"
            "border-radius:3px;padding:2px 8px;font-size:10px;font-weight:bold;")
        self.badge_loop_arm.setVisible(False)

        self.badge_stems = QLabel("⏳ STEMS")
        self.badge_stems.setStyleSheet(
            "color:#ffaa00;background:#1a1000;border:1px solid #aa6600;"
            "border-radius:3px;padding:2px 8px;font-size:10px;font-weight:bold;")
        self.badge_stems.setVisible(False)


        self._norm_btn = QPushButton("NORM")
        self._norm_btn.setCheckable(True)
        self._norm_btn.setChecked(False)
        self._norm_btn.setFixedHeight(20)
        self._norm_btn.setVisible(False)
        self._norm_btn.setStyleSheet(
            "QPushButton{color:#888;background:#111;border:1px solid #333;"
            "border-radius:3px;padding:0 6px;font-size:8px;font-weight:bold;}"
            "QPushButton:checked{color:#00ffaa;background:#003322;"
            "border:1px solid #00aa66;}"
        )
        self._norm_btn.toggled.connect(self._on_norm_toggle)

        badges.addWidget(self.badge_master)
        badges.addWidget(self.badge_follower)
        badges.addWidget(self.badge_loop)
        badges.addWidget(self.badge_loop_arm)
        badges.addWidget(self.badge_stems)
        badges.addStretch()
        badges.addWidget(self._norm_btn)
        root.addLayout(badges)

        # Stem buttons removed — stems controlled via mixer knobs
        self._stem_btns: dict[str, QPushButton] = {}

        # ── Performance pads — 7 visible pads, pad 8 hidden (cycles modes) ──
        # Cycle order: STEMS → HOT CUE → BEAT LOOP → BEAT JUMP → STEMS
        _CYCLE_MODES = ["STEMS", "HOT CUE", "BEAT LOOP", "BEAT JUMP"]
        _MODE_COLORS = {"HOT CUE": "#ff3333", "BEAT LOOP": "#00dd88",
                        "BEAT JUMP": "#ffcc00", "STEMS": "#bb88ff"}
        self._pad_mode    = "STEMS"
        self._cycle_modes = _CYCLE_MODES
        self._mode_colors = _MODE_COLORS

        _HC_COLORS  = ["#ff3333","#ff8800","#ffdd00","#00cc44",
                       "#00ccff","#4488ff","#aa44ff"]
        _BL_LABELS  = ["1/4","1/2","1","2","4","8","16"]
        _BJ_LABELS  = ["-1","−2","−4","−8","+1","+2","+4"]
        _BJ_BARS    = [-1.0,-2.0,-4.0,-8.0, 1.0, 2.0, 4.0]
        _STM_LABELS = ["VOC","DRM","BSS","MLY","VOC►","DRM►","BSS►"]
        _STM_COLORS = ["#aa44ff","#ff4466","#ff9900","#44aaff",
                       "#aa44ff","#ff4466","#ff9900"]

        self._mode_btns: dict[str, QPushButton] = {}
        self._pad_btns:  list[QPushButton] = []   # 7 visible pads for current mode

        self._bj_bars    = _BJ_BARS
        self._pfx_active = [False] * 8
        self._hc_colors  = _HC_COLORS
        self._bl_labels  = _BL_LABELS
        self._bj_labels  = _BJ_LABELS
        self._stm_labels = _STM_LABELS
        self._stm_colors = _STM_COLORS

        PAD_W, PAD_H = 56, 40

        pads_outer = QVBoxLayout()
        pads_outer.setSpacing(3)

        # Mode tabs — lit = active mode
        mode_row = QHBoxLayout()
        mode_row.setSpacing(3)
        for mode in _CYCLE_MODES:
            color = _MODE_COLORS[mode]
            b = QPushButton(mode)
            b.setCheckable(True)
            b.setChecked(mode == "STEMS")
            b.setFixedHeight(20)
            b.setStyleSheet(
                f"QPushButton{{background:#111;border:1px solid #2a2a35;"
                f"border-radius:3px;color:#555;font-size:7px;font-weight:bold;padding:0 4px;}}"
                f"QPushButton:checked{{background:{color}22;border:1px solid {color};color:{color};}}")
            b.clicked.connect(lambda _, m=mode: self._set_pad_mode(m))
            mode_row.addWidget(b)
            self._mode_btns[mode] = b
        mode_row.addStretch()
        pads_outer.addLayout(mode_row)

        # 7 pads: row 0 = pads 0-3, row 1 = pads 4-6 (pad 7 hidden)
        for row_i in range(2):
            pad_row = QHBoxLayout()
            pad_row.setSpacing(3)
            cols = 4 if row_i == 0 else 3   # 4 top, 3 bottom (pad 8 hidden)
            for col_i in range(cols):
                n = row_i * 4 + col_i
                btn = QPushButton()
                btn.setFixedSize(PAD_W, PAD_H)
                btn.setCheckable(True)
                btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                btn.customContextMenuRequested.connect(lambda _, i=n: self._pad_right_click(i))
                btn.pressed.connect(lambda i=n: self._pad_press(i))
                btn.released.connect(lambda i=n: self._pad_release(i))
                pad_row.addWidget(btn)
                self._pad_btns.append(btn)
            pad_row.addStretch()
            pads_outer.addLayout(pad_row)

        root.addLayout(pads_outer)
        root.addStretch()

    def _connect_deck(self):
        self.deck.on_track_loaded = \
            lambda _: _main_thread(self._on_track_loaded)
        self.deck.on_play_state_changed = \
            lambda _, playing: _main_thread(lambda: self._on_play(playing))
        self.deck.on_stems_ready = \
            lambda _: _main_thread(self._on_stems_ready)
        self.deck.on_hot_cue_changed = \
            lambda *_: _main_thread(self._refresh_pads)

    def _on_track_loaded(self):
        d = self.deck
        d.key_shift = 0
        self.badge_stems.setVisible(True)   # show until stems arrive
        self.lbl_title.setText(d.title or "—")
        self.lbl_artist.setText(d.artist)
        self.lbl_bpm.setText(f"{d.bpm:.2f}" if d.bpm else "---.--")
        self._update_key_display()
        self._refresh_pads()
        self._norm_btn.setChecked(False)
        self._norm_btn.setVisible(False)
        self._update_norm_btn()
        for btn in self._stem_btns.values():
            btn.setVisible(False)
            btn.setChecked(True)
        # Show STEMS badge if track has audio but stems not yet ready
        if self.deck.audio is not None and not self.deck.stems_ready:
            self.badge_stems.setVisible(True)

    def _on_stems_ready(self):
        self.badge_stems.setVisible(False)
        for name, btn in self._stem_btns.items():
            btn.setVisible(True)
            btn.setChecked(name in self.deck.active_stems)
        if self._pad_mode == "STEMS":
            self._refresh_pads()

    # ── Pad mode ──────────────────────────────────────────────

    def _cycle_pad_mode(self):
        modes = self._cycle_modes
        cur = next((i for i, m in enumerate(modes) if m == self._pad_mode), 0)
        self._set_pad_mode(modes[(cur + 1) % len(modes)])

    def _set_pad_mode(self, mode: str):
        self._pad_mode = mode
        for m, b in self._mode_btns.items():
            b.setChecked(m == mode)
        self._refresh_pads()

    def _pad_press(self, n: int):
        if n == 7:
            self._cycle_pad_mode(); return
        mode = self._pad_mode
        if mode == "HOT CUE":
            self.deck.hotcue_activate(n); self._refresh_pads()
        elif mode == "BEAT LOOP":
            from ..core.deck import BEAT_LOOP_SIZES
            if n < len(BEAT_LOOP_SIZES):
                self.deck.beatloop_toggle(BEAT_LOOP_SIZES[n])
            self._refresh_pads()
        elif mode == "BEAT JUMP":
            if n < len(self._bj_bars):
                self.deck.beat_jump(self._bj_bars[n])
        elif mode == "STEMS":
            stem_names = ["vocals","drums","bass","other"]
            if n < 4 and self.deck.stems_ready:
                self.deck.toggle_stem(stem_names[n]); self._refresh_pads()
            elif 4 <= n < 7 and self.deck.stems_ready:
                self._stem_solo_start(stem_names[n - 4])

    def _pad_release(self, n: int):
        if self._pad_mode == "STEMS" and 4 <= n < 7:
            stem_names = ["vocals","drums","bass","other"]
            self._stem_solo_end(stem_names[n - 4])

    def _pad_right_click(self, n: int):
        if self._pad_mode == "HOT CUE":
            self.deck.hotcue_clear(n); self._refresh_pads()

    def _pfx_press(self, n: int): pass
    def _pfx_release(self, n: int): pass
    def _hc_press(self, n: int): self._pad_press(n)
    def _hc_right_click(self, n: int): self._pad_right_click(n)
    def _bl_press(self, n: int): self._pad_press(n)
    def _bj_press(self, n: int): self._pad_press(n)
    def _stm_press(self, n: int): self._pad_press(n)
    def _stm_release(self, n: int): self._pad_release(n)

    def _stem_solo_start(self, name: str):
        """Temporarily mute all stems except `name`."""
        self.deck._solo_stem = name
        for s in ["drums","bass","other","vocals"]:
            self.deck.stem_volumes[s] = 1.0 if s == name else 0.0

    def _stem_solo_end(self, name: str):
        """Restore all stems after solo hold."""
        self.deck._solo_stem = None
        for s in ["drums","bass","other","vocals"]:
            self.deck.stem_volumes[s] = 1.0
        self._refresh_pads()

    # PAD_FX_PARAMS: (fx_type, depth) — applied to channel FX while held
    _PAD_FX_PARAMS = [
        ("FILTER", 0.1),   # Filter sweep down
        ("FILTER", 0.9),   # Filter sweep up
        ("DELAY",  0.8),
        ("REVERB", 0.8),
        ("FLANGER",0.8),
        ("PHASER", 0.8),
        ("DRIVE",  0.8),
        ("DELAY",  0.95),  # Echo out (heavy delay)
    ]

    def _apply_pad_fx(self, n: int, on: bool):
        """Gate FX on/off for pad n. Requires mixer reference."""
        # Reach up to find the mixer via the engine (set during connect)
        engine = getattr(self, '_engine', None)
        if engine is None:
            return
        deck_idx = self.deck.deck_id
        if on:
            fx_type, depth = self._PAD_FX_PARAMS[n]
            engine.mixer.set_fx_type(deck_idx, fx_type)
            engine.mixer.set_fx_raw(deck_idx, int(depth * 127))
        else:
            engine.mixer.set_fx_raw(deck_idx, 64)  # bypass (centre)

    def _pad_right_click(self, n: int):
        if self._pad_mode == "HOT CUE":
            self.deck.hotcue_clear(n)
            self._refresh_pads()

    def _refresh_pads(self):
        """Repaint the 7 visible pads for the current mode."""
        mode = self._pad_mode
        from ..core.deck import BEAT_LOOP_SIZES
        for i, btn in enumerate(self._pad_btns):
            if mode == "HOT CUE":
                col  = self._hc_colors[i] if i < len(self._hc_colors) else "#555"
                set_ = self.deck.hot_cues[i] is not None
                lbl  = f"● {i+1}" if set_ else f"CUE {i+1}"
                if set_:
                    btn.setStyleSheet(
                        f"QPushButton{{background:{col}44;border:2px solid {col};"
                        f"border-radius:6px;color:{col};font-size:9px;font-weight:bold;}}"
                        f"QPushButton:pressed{{background:{col}88;}}")
                else:
                    btn.setStyleSheet(
                        "QPushButton{background:#141418;border:1px solid #2a2a35;"
                        "border-radius:6px;color:#333;font-size:9px;font-weight:bold;}"
                        "QPushButton:pressed{background:#222228;}")
                btn.setText(lbl); btn.setChecked(set_)

            elif mode == "BEAT LOOP":
                col   = "#00dd88"
                lbl   = self._bl_labels[i] if i < len(self._bl_labels) else ""
                active = (self.deck.loop_active and i < len(BEAT_LOOP_SIZES) and
                          abs(self.deck._current_loop_beats - BEAT_LOOP_SIZES[i]) < 0.01)
                if active:
                    btn.setStyleSheet(
                        f"QPushButton{{background:{col}55;border:2px solid {col};"
                        f"border-radius:6px;color:{col};font-size:11px;font-weight:bold;}}"
                        f"QPushButton:pressed{{background:{col}99;}}")
                else:
                    btn.setStyleSheet(
                        f"QPushButton{{background:#111;border:1px solid #2a2a35;"
                        f"border-radius:6px;color:#446655;font-size:11px;font-weight:bold;}}"
                        f"QPushButton:pressed{{background:#1a2a22;}}")
                btn.setText(lbl); btn.setChecked(active)

            elif mode == "BEAT JUMP":
                col = "#ffcc00"
                lbl = self._bj_labels[i] if i < len(self._bj_labels) else ""
                btn.setStyleSheet(
                    f"QPushButton{{background:#111;border:1px solid #2a2a35;"
                    f"border-radius:6px;color:{col}99;font-size:11px;font-weight:bold;}}"
                    f"QPushButton:pressed{{background:{col}33;}}")
                btn.setText(lbl); btn.setChecked(False)

            elif mode == "STEMS":
                stem_names = ["vocals","drums","bass","other"]
                col   = self._stm_colors[i] if i < len(self._stm_colors) else "#888"
                lbl   = self._stm_labels[i] if i < len(self._stm_labels) else ""
                ready = self.deck.stems_ready
                if i < 4:
                    name   = stem_names[i]
                    active = name in self.deck.active_stems if ready else False
                    if ready and active:
                        btn.setStyleSheet(
                            f"QPushButton{{background:{col}55;border:2px solid {col};"
                            f"border-radius:6px;color:{col};font-size:10px;font-weight:bold;}}"
                            f"QPushButton:pressed{{background:{col}99;}}")
                    elif ready:
                        btn.setStyleSheet(
                            f"QPushButton{{background:#111;border:1px solid {col}44;"
                            f"border-radius:6px;color:{col}66;font-size:10px;font-weight:bold;}}"
                            f"QPushButton:pressed{{background:#1a1a22;}}")
                    else:
                        btn.setStyleSheet(
                            "QPushButton{background:#0e0e12;border:1px solid #1a1a22;"
                            "border-radius:6px;color:#222;font-size:10px;}")
                    btn.setText(lbl if ready else "…"); btn.setChecked(active)
                else:
                    solo = getattr(self.deck, '_solo_stem', None)
                    name = stem_names[i - 4] if (i - 4) < len(stem_names) else ""
                    active = (solo == name)
                    if ready and active:
                        btn.setStyleSheet(
                            f"QPushButton{{background:{col}88;border:2px solid {col};"
                            f"border-radius:6px;color:#fff;font-size:8px;font-weight:bold;}}"
                            f"QPushButton:pressed{{background:{col}cc;}}")
                    elif ready:
                        btn.setStyleSheet(
                            f"QPushButton{{background:#111;border:1px solid {col}33;"
                            f"border-radius:6px;color:{col}55;font-size:8px;font-weight:bold;}}"
                            f"QPushButton:pressed{{background:#1a1a22;}}")
                    else:
                        btn.setStyleSheet(
                            "QPushButton{background:#0e0e12;border:1px solid #1a1a22;"
                            "border-radius:6px;color:#222;font-size:8px;}")
                    btn.setText(lbl if ready else "…"); btn.setChecked(active)


    def _key_shift(self, delta: int, reset: bool = False):
        if reset:
            self.deck.key_shift = 0
        else:
            self.deck.key_shift = max(-12, min(12, self.deck.key_shift + delta))
        self._update_key_display()

    def _update_key_display(self):
        d = self.deck
        camelot = getattr(d, '_camelot', '') or ''
        energy  = getattr(d, 'energy', 0) or 0
        key_str = f"{d.key}  {camelot}" if camelot else d.key
        if energy:
            key_str += f"  E{energy}"
        shift = d.key_shift
        if shift != 0:
            key_str += f"  {shift:+d}st"
            self.lbl_key.setStyleSheet(
                "color:#ffcc44;font-size:16px;font-weight:bold;min-width:40px;")
            self._btn_key_reset.setStyleSheet(
                "QPushButton{background:#2a1a00;border:1px solid #ffcc44;"
                "color:#ffcc44;font-size:7px;border-radius:2px;}"
                "QPushButton:pressed{background:#3a2a00;}")
        else:
            self.lbl_key.setStyleSheet(
                "color:#33aacc;font-size:16px;font-weight:bold;min-width:40px;")
            self._btn_key_reset.setStyleSheet(
                "QPushButton{background:#0e0e18;border:1px solid #2a2a40;"
                "color:#555;font-size:7px;border-radius:2px;}"
                "QPushButton:pressed{background:#1a1a2a;}")
        self.lbl_key.setText(key_str)

    def _on_norm_toggle(self, on: bool):
        self.deck.gain_enabled = on

    def _update_norm_btn(self):
        g = self.deck.gain_linear
        if g == 1.0:
            self._norm_btn.setVisible(False)
            return
        import math
        db = round(20.0 * math.log10(g), 1)
        self._norm_btn.setText(f"NORM {db:+.1f}dB")
        self._norm_btn.setVisible(True)

    def _toggle_stem(self, name: str):
        self.deck.toggle_stem(name)

    def _on_play(self, playing: bool):
        self.lbl_time_icon.setText("▶" if playing else "⏸")

    def _tick(self):
        d = self.deck
        self.lbl_time.setText(f"-{_fmt_time(d.remaining_seconds)}")
        pct  = (d.rate - 1.0) * 100.0
        sign = "+" if pct >= 0 else ""
        self.lbl_rate.setText(f"{sign}{pct:.2f}%")
        if d.bpm > 0:
            self.lbl_bpm.setText(f"{d.bpm * d.rate:.2f}")
        self._update_key_display()
        self.badge_loop.setVisible(d.loop_active)
        self.badge_loop_arm.setVisible(d._loop_recording)
        self.badge_master.setVisible(d.sync_master)
        self.badge_follower.setVisible(d.sync_follower)
        if self._pad_mode in ("BEAT LOOP", "STEMS", "PAD FX"):
            self._refresh_pads()

        # CLIP indicator
        if self._vu_peak_fn:
            peak = self._vu_peak_fn()
            if peak >= 1.0:
                self._clip_lbl.setVisible(True)
        self._update_norm_btn()

    # ── Drag & Drop ───────────────────────────────────────────

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if any(url.toLocalFile().lower().endswith(e)
                       for e in self.AUDIO_EXTS):
                    event.acceptProposedAction()
                    return
        if event.mimeData().hasText():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        md = event.mimeData()
        path = None
        if md.hasUrls():
            for url in md.urls():
                p = url.toLocalFile()
                if any(p.lower().endswith(e) for e in self.AUDIO_EXTS):
                    path = p; break
        elif md.hasText():
            path = md.text()
        if path and self._on_load_track:
            self._on_load_track(path)
            event.acceptProposedAction()
