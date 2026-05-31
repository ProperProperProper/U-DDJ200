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
                 parent=None):
        super().__init__(parent)
        self.deck = deck
        self._accent = accent
        self._deck_label = deck_label
        self._on_load_track = on_load_track
        self._vu_peak_fn = vu_peak_fn

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

        row1.addWidget(self.lbl_deck)
        row1.addLayout(title_artist, 1)
        row1.addWidget(self.lbl_key)
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
        badges.addStretch()
        badges.addWidget(self._norm_btn)
        root.addLayout(badges)

        # Stem buttons removed — stems controlled via mixer knobs
        self._stem_btns: dict[str, QPushButton] = {}

        # ── Performance pads — DDJ-200 layout ────────────────
        # Mode selector row
        self._pad_mode = "BEAT LOOP"  # "HOT CUE" | "BEAT LOOP" | "SAMPLER"
        mode_row = QHBoxLayout()
        mode_row.setSpacing(3)
        self._mode_btns: dict[str, QPushButton] = {}
        for mode, color in [("HOT CUE","#ff3333"), ("BEAT LOOP","#00dd88"), ("SAMPLER","#bb88ff")]:
            b = QPushButton(mode)
            b.setCheckable(True)
            b.setChecked(mode == "BEAT LOOP")
            b.setFixedHeight(20)
            b.setStyleSheet(
                f"QPushButton{{background:#111;border:1px solid #2a2a35;"
                f"border-radius:3px;color:#555;font-size:8px;font-weight:bold;padding:0 4px;}}"
                f"QPushButton:checked{{background:{color}22;border:1px solid {color};color:{color};}}")
            b.clicked.connect(lambda _, m=mode: self._set_pad_mode(m))
            mode_row.addWidget(b)
            self._mode_btns[mode] = b
        mode_row.addStretch()
        root.addLayout(mode_row)

        # Pad grid — 2 rows × 4 cols (DDJ-200 layout: row0=pads5-8, row1=pads1-4)
        _HC_COLORS = ["#ff3333","#ff8800","#ffdd00","#00cc44",
                      "#00ccff","#4488ff","#aa44ff","#ff44aa"]
        _BL_LABELS = ["1/4","1/2","1","2","4","8","16","32"]
        _STEM_LABELS= ["DRUMS","BASS","OTHER","VOCALS","","","",""]
        _STEM_COLORS= ["#ff4444","#ff9900","#44aaff","#aa44ff","","","",""]

        self._pad_btns: list[QPushButton] = []
        pads_outer = QVBoxLayout()
        pads_outer.setSpacing(3)

        for row_i in range(2):
            pad_row = QHBoxLayout()
            pad_row.setSpacing(3)
            for col_i in range(4):
                # DDJ-200: top row = pads 4–7, bottom row = pads 0–3
                n = (1 - row_i) * 4 + col_i
                btn = QPushButton()
                btn.setFixedSize(54, 38)
                btn.setCheckable(True)
                btn.setProperty("pad_idx", n)
                # right-click = clear hot cue
                btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                btn.customContextMenuRequested.connect(
                    lambda _, i=n: self._pad_right_click(i))
                btn.pressed.connect(lambda i=n: self._pad_press(i))
                pad_row.addWidget(btn)
                self._pad_btns.append(btn)
            pad_row.addStretch()
            pads_outer.addLayout(pad_row)

        root.addLayout(pads_outer)

        # Store pad color/label info for rendering
        self._hc_colors  = _HC_COLORS
        self._bl_labels  = _BL_LABELS
        self._stem_labels= _STEM_LABELS
        self._stem_colors= _STEM_COLORS

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
        self.lbl_title.setText(d.title or "—")
        self.lbl_artist.setText(d.artist)
        self.lbl_bpm.setText(f"{d.bpm:.2f}" if d.bpm else "---.--")
        camelot = getattr(d, '_camelot', '') or ''
        energy  = getattr(d, 'energy', 0) or 0
        key_str = f"{d.key}  {camelot}" if camelot else d.key
        if energy:
            key_str += f"  E{energy}"
        self.lbl_key.setText(key_str)
        self._refresh_pads()
        self._norm_btn.setChecked(False)
        self._norm_btn.setVisible(False)
        self._update_norm_btn()
        for btn in self._stem_btns.values():
            btn.setVisible(False)
            btn.setChecked(True)

    def _on_stems_ready(self):
        for name, btn in self._stem_btns.items():
            btn.setVisible(True)
            btn.setChecked(name in self.deck.active_stems)
        if self._pad_mode == "SAMPLER":
            self._refresh_pads()

    # ── Pad mode ──────────────────────────────────────────────

    def _set_pad_mode(self, mode: str):
        self._pad_mode = mode
        for m, b in self._mode_btns.items():
            b.setChecked(m == mode)
        self._refresh_pads()

    def _pad_press(self, n: int):
        mode = self._pad_mode
        if mode == "HOT CUE":
            self.deck.hotcue_activate(n)
            self._refresh_pads()
        elif mode == "BEAT LOOP":
            from ..core.deck import BEAT_LOOP_SIZES
            if n < len(BEAT_LOOP_SIZES):
                self.deck.beatloop_toggle(BEAT_LOOP_SIZES[n])
            self._refresh_pads()
        elif mode == "SAMPLER":
            name = ["drums","bass","other","vocals"][n] if n < 4 else None
            if name and self.deck.stems_ready:
                self.deck.toggle_stem(name)
                self._refresh_pads()

    def _pad_right_click(self, n: int):
        if self._pad_mode == "HOT CUE":
            self.deck.hotcue_clear(n)
            self._refresh_pads()

    def _refresh_pads(self):
        """Repaint all pad buttons to reflect current state and mode."""
        mode = self._pad_mode
        from ..core.deck import BEAT_LOOP_SIZES
        for i, btn in enumerate(self._pad_btns):
            if mode == "HOT CUE":
                col  = self._hc_colors[i]
                set_ = self.deck.hot_cues[i] is not None
                lbl  = f"CUE {i+1}" if not set_ else f"● {i+1}"
                if set_:
                    btn.setStyleSheet(
                        f"QPushButton{{background:{col}44;border:2px solid {col};"
                        f"border-radius:4px;color:{col};font-size:9px;font-weight:bold;}}"
                        f"QPushButton:pressed{{background:{col}88;}}")
                else:
                    btn.setStyleSheet(
                        "QPushButton{background:#141418;border:1px solid #2a2a35;"
                        "border-radius:4px;color:#333;font-size:9px;font-weight:bold;}"
                        "QPushButton:pressed{background:#222228;}")
                btn.setText(lbl)
                btn.setChecked(set_)

            elif mode == "BEAT LOOP":
                col   = "#00dd88"
                lbl   = self._bl_labels[i] if i < len(self._bl_labels) else ""
                active = (self.deck.loop_active and
                          abs(self.deck._current_loop_beats -
                              (BEAT_LOOP_SIZES[i] if i < len(BEAT_LOOP_SIZES) else -1)) < 0.01)
                if active:
                    btn.setStyleSheet(
                        f"QPushButton{{background:{col}55;border:2px solid {col};"
                        f"border-radius:4px;color:{col};font-size:10px;font-weight:bold;}}"
                        f"QPushButton:pressed{{background:{col}99;}}")
                else:
                    btn.setStyleSheet(
                        f"QPushButton{{background:#111;border:1px solid #2a2a35;"
                        f"border-radius:4px;color:#446655;font-size:10px;font-weight:bold;}}"
                        f"QPushButton:pressed{{background:#1a2a22;}}")
                btn.setText(lbl)
                btn.setChecked(active)

            elif mode == "SAMPLER":
                if i < 4:
                    name   = ["drums","bass","other","vocals"][i]
                    col    = self._stem_colors[i]
                    lbl    = self._stem_labels[i]
                    active = name in self.deck.active_stems if self.deck.stems_ready else False
                    ready  = self.deck.stems_ready
                    if ready and active:
                        btn.setStyleSheet(
                            f"QPushButton{{background:{col}55;border:2px solid {col};"
                            f"border-radius:4px;color:{col};font-size:8px;font-weight:bold;}}"
                            f"QPushButton:pressed{{background:{col}99;}}")
                    elif ready:
                        btn.setStyleSheet(
                            f"QPushButton{{background:#111;border:1px solid {col}55;"
                            f"border-radius:4px;color:{col}88;font-size:8px;font-weight:bold;}}"
                            f"QPushButton:pressed{{background:#1a1a22;}}")
                    else:
                        btn.setStyleSheet(
                            "QPushButton{background:#0e0e12;border:1px solid #1a1a22;"
                            "border-radius:4px;color:#222;font-size:8px;}")
                    btn.setText(lbl if ready else "…")
                    btn.setChecked(active)
                else:
                    btn.setStyleSheet(
                        "QPushButton{background:#0e0e12;border:1px solid #111;"
                        "border-radius:4px;}")
                    btn.setText("")
                    btn.setChecked(False)

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
        self.badge_loop.setVisible(d.loop_active)
        self.badge_loop_arm.setVisible(d._loop_recording)
        self.badge_master.setVisible(d.sync_master)
        self.badge_follower.setVisible(d.sync_follower)
        if self._pad_mode == "BEAT LOOP":
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
