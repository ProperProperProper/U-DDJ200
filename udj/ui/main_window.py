"""
MainWindow — top-level U-DJ window.
Layout: [Deck 1] [Mixer] [Deck 2]
        [          Library          ]
"""
from __future__ import annotations


import sys
from PyQt6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                              QSplitter, QStatusBar, QLabel, QPushButton)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont

from ..core.engine import AudioEngine
from ..midi.ddj200 import DDJ200
from ..library.db import init_db
from .deck_widget import DeckWidget
from .mixer_widget import MixerWidget
from .library_widget import LibraryWidget
from .beat_grid_widget import BeatGridWidget
from .dev_log import DevLogWindow


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("U-DJ")
        self.setMinimumSize(1400, 820)
        self.resize(1500, 900)

        # ── Dev log (install first so all engine/MIDI errors are captured) ──
        self._dev_log = DevLogWindow()
        self._dev_log.install()

        # ── Engine & MIDI ──────────────────────────────────
        self.engine = AudioEngine()
        self.controller = DDJ200(self.engine)

        # LED feedback — set AFTER DeckWidget connects its own callbacks
        # so we don't overwrite them here

        # ── UI ────────────────────────────────────────────
        self._build_ui()

        # ── Start engine & connect controller ─────────────
        self.engine.start()
        self.controller.connect()

        # ── Status bar ────────────────────────────────────
        sb = self.statusBar()
        self._midi_status = QLabel("Controller: searching…")
        sb.addPermanentWidget(self._midi_status)

        _sb_btn_base = (
            "QPushButton{background:#141420;border:1px solid #28283a;"
            "border-radius:3px;color:#666680;font-size:8px;font-weight:bold;"
            "letter-spacing:1px;padding:0 8px;}"
            "QPushButton:hover{color:#aaaacc;border-color:#44445a;}"
        )

        log_btn = QPushButton("⬡  DEV LOG")
        log_btn.setCheckable(True)
        log_btn.setFixedHeight(20)
        log_btn.setStyleSheet(
            _sb_btn_base +
            "QPushButton:checked{background:#1a1200;border-color:#ffaa00;"
            "color:#ffaa00;}")
        log_btn.toggled.connect(
            lambda on: self._dev_log.show() if on else self._dev_log.hide())
        sb.addWidget(log_btn)
        self._log_btn = log_btn


        QTimer.singleShot(500, self._check_midi)

        # Analyse only tracks missing BPM/grid/stems — never overwrite good data
        QTimer.singleShot(1500, lambda: self._analyse_library(force_all=False))

    # ──────────────────────────────────────────────────────────
    #  UI layout
    # ──────────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # Top: decks + mixer
        top = QHBoxLayout()
        top.setSpacing(6)

        self.deck1_widget = DeckWidget(
            self.engine.deck(0), deck_label="1", accent="#00c8ff",
            on_load_track=lambda p: self._load_path(0, p),
            vu_peak_fn=lambda: self.engine.vu_peak[0],
            engine=self.engine)
        self.deck2_widget = DeckWidget(
            self.engine.deck(1), deck_label="2", accent="#ff7a01",
            on_load_track=lambda p: self._load_path(1, p),
            vu_peak_fn=lambda: self.engine.vu_peak[1],
            engine=self.engine)

        self.mixer_widget = MixerWidget(
            self.engine.mixer,
            engine=self.engine,
            master_peak_fn=lambda: (self.engine.master_peak[0],
                                    self.engine.master_peak[1]),
            deck_peak_fns=[lambda: self.engine.vu_peak[0],
                           lambda: self.engine.vu_peak[1]])


        top.addWidget(self.deck1_widget, 2)
        top.addWidget(self.mixer_widget, 0)
        top.addWidget(self.deck2_widget, 2)

        # Beat grid monitor — full width strip at top
        self.beat_grid = BeatGridWidget(self.engine)
        root.addWidget(self.beat_grid, 0)

        root.addLayout(top, 3)

        # Bottom: library
        self.library = LibraryWidget(
            on_load_deck1=lambda t: self._load_track(0, t),
            on_load_deck2=lambda t: self._load_track(1, t),
        )
        self.library.setMinimumHeight(200)
        root.addWidget(self.library, 1)

        # Wire the pre-built Clear Log button in the library toolbar
        self.library._btn_clear_log.clicked.connect(self._dev_log._clear)
        self.library._btn_clear_log.setVisible(True)

        # Give the MIDI controller a reference so shift+CFX can scroll/load
        self.controller.library = self.library
        # Give the library a reference to the engine for stem inspection
        self.library.set_engine(self.engine)
        # After folder scan: run stems for all new tracks, then auto-inspect
        self.library.on_folder_added = lambda: self._analyse_library(
            force_all=False,
            on_complete=self.library._run_inspect_all)
        # Allow mixer widget to read controller state (transition FX sync)
        self.engine._controller_ref = self.controller
        # Sync pad mode display when hardware cycles modes
        _deck_widgets = [self.deck1_widget, self.deck2_widget]
        self.controller.on_pad_mode_changed = (
            lambda d, m: _deck_widgets[d]._sig_pad_mode.emit(m))

    # ──────────────────────────────────────────────────────────
    #  Track loading
    # ──────────────────────────────────────────────────────────

    def _load_track(self, deck_idx: int, track: dict):
        from ..library.db import record_play
        self.engine.load_track(deck_idx, track["path"], metadata=track)
        record_play(track["path"])
        self._ensure_grid(deck_idx, track["path"], track)

    def _load_path(self, deck_idx: int, path: str):
        from ..library.db import get_track, record_play
        from ..library.analyzer import analyse_track
        meta = get_track(path)
        if meta and meta.get("bpm"):
            # Load immediately — don't wait for analysis
            self.engine.load_track(deck_idx, path, metadata=meta)
            record_play(path)
            self._ensure_grid(deck_idx, path, meta)
        else:
            # Never seen this track — must analyse before we know BPM/grid
            def _done(m):
                from PyQt6.QtCore import QTimer
                QTimer.singleShot(0, lambda: self.engine.load_track(
                    deck_idx, path, metadata=m))
            analyse_track(path, on_done=_done, force=bool(meta))

    def _ensure_grid(self, deck_idx: int, path: str, meta: dict):
        """
        If first_beat_sec is missing for this track, run analysis in the
        background and patch the live deck as soon as the result arrives.
        The track is already loaded and can be played immediately; the beat
        grid simply improves a few seconds later.
        """
        has_beats   = bool(meta.get("beat_positions_json",   "[]") not in ("[]", "", None))
        has_phrases = bool(meta.get("phrase_positions_json", "[]") not in ("[]", "", None))
        has_cues    = bool(meta.get("cue_points_json",       "[]") not in ("[]", "", None))
        has_fb      = float(meta.get("first_beat_sec", 0) or 0) > 0.0
        has_lufs    = meta.get("loudness_lufs") is not None
        has_camelot = bool(meta.get("camelot"))

        if has_beats and has_phrases and has_cues and has_fb and has_lufs and has_camelot:
            return   # everything already analysed

        from ..library.analyzer import analyse_track

        def _patch(new_meta):
            import json as _json
            import numpy as _np
            deck = self.engine.deck(deck_idx)
            if deck.file_path != path:
                return

            fbs = float(new_meta.get("first_beat_sec", 0) or 0)
            bpm = float(new_meta.get("bpm", 0) or 0)
            if fbs > 0:
                deck.first_beat = fbs * deck.sample_rate
            if bpm > 0 and deck.bpm == 0:
                deck.bpm = bpm

            for attr, key in [("beats",   "beat_positions_json"),
                               ("phrases", "phrase_positions_json")]:
                raw = new_meta.get(key, "[]") or "[]"
                try:
                    sec_list = _json.loads(raw)
                    setattr(deck, attr,
                            _np.array(sec_list, dtype=_np.float64) * deck.sample_rate
                            if sec_list else None)
                except Exception:
                    pass

            # Store loudness/camelot — but do NOT auto-enable gain;
            # that's the user's choice via the NORM button
            gain_db = float(new_meta.get("gain_db", 0) or 0)
            deck.gain_linear  = float(10.0 ** (gain_db / 20.0))
            deck._camelot     = new_meta.get("camelot", "") or ""
            deck.energy       = int(new_meta.get("energy", 0) or 0)

            # Only reset sync state if deck is NOT currently playing
            # (resetting while playing causes a mid-song bar-snap!)
            # Apply auto cue points (only fill empty slots)
            import json as _json2
            cq_raw = new_meta.get("cue_points_json", "[]") or "[]"
            try:
                for cue in _json2.loads(cq_raw):
                    pad = int(cue.get("pad", 0))
                    sec = float(cue.get("sec", 0.0))
                    if 0 <= pad < 8 and deck.hot_cues[pad] is None:
                        deck.hot_cues[pad] = int(sec * deck.sample_rate)
            except Exception:
                pass

            if not deck.playing:
                self.engine._was_playing[deck_idx] = False

            n_b = len(deck.beats)   if deck.beats   is not None else 0
            n_p = len(deck.phrases) if deck.phrases is not None else 0
            print(f"[U-DJ] deck {deck_idx} patched: bpm={deck.bpm:.2f} "
                  f"beats={n_b} phrases={n_p} gain={gain_db:+.1f}dB "
                  f"camelot={deck._camelot}")

        # force=True only when we know something is missing
        analyse_track(path, on_done=_patch, force=True)

    # ──────────────────────────────────────────────────────────
    #  Status
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _is_fully_analysed(meta: dict) -> bool:
        from ..library.db import ANALYSIS_VERSION
        return bool(
            meta.get("bpm") and
            meta.get("beat_positions_json") not in (None, "", "[]") and
            meta.get("phrase_positions_json") not in (None, "", "[]") and
            meta.get("camelot") and
            meta.get("loudness_lufs") is not None and
            meta.get("analysis_version") == ANALYSIS_VERSION
        )

    def _analyse_library(self, force_all: bool = False, on_complete=None):
        from ..library.db import all_tracks, stems_are_cached
        from ..library.analyzer import analyse_track
        from ..core.stems import preanalyse_stems
        from PyQt6.QtCore import QTimer

        tracks = all_tracks()
        if force_all:
            # "Analyse Now" button — queue everything, including already-analysed tracks
            todo_analysis = tracks
            todo_stems    = tracks
        else:
            todo_analysis = [t for t in tracks if not self._is_fully_analysed(t)]
            todo_stems    = [t for t in tracks
                             if not t.get("stems_cached") and not stems_are_cached(t["path"])]

        total_analysis = len(todo_analysis)
        total_stems    = len(todo_stems)
        total          = total_analysis + total_stems

        if not total:
            if on_complete:
                on_complete()
            return

        done = [0]

        QTimer.singleShot(0, lambda: self.library.analysis_progress_start(total))

        def _on_analysis(meta):
            done[0] += 1
            _check_done()
            QTimer.singleShot(0, self.library._model.reload)

        def _on_stems(path):
            done[0] += 1
            _check_done()
            QTimer.singleShot(0, self.library._model.reload)

        def _check_done():
            if done[0] >= total:
                def _finish():
                    self.library._model.reload()
                    self.library.analysis_progress_done()
                    if on_complete:
                        on_complete()
                QTimer.singleShot(0, _finish)
            else:
                QTimer.singleShot(0, self.library._model.reload)

        for t in todo_analysis:
            analyse_track(t["path"], on_done=_on_analysis, force=True)

        for t in todo_stems:
            meta = t
            preanalyse_stems(t["path"], self.engine.sample_rate,
                             track_meta=meta, on_done=_on_stems)

    def _check_midi(self):
        if self.controller._connected:
            self._midi_status.setText("Controller: DDJ-200 ✓")
            self._midi_status.setStyleSheet("color:#00dd88;")
        else:
            self._midi_status.setText("Controller: not found (keyboard/mouse only)")
            self._midi_status.setStyleSheet("color:#ff8800;")

    # ──────────────────────────────────────────────────────────
    #  Cleanup
    # ──────────────────────────────────────────────────────────

    def closeEvent(self, event):
        self.controller.disconnect()
        self.engine.stop()
        super().closeEvent(event)
