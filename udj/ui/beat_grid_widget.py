"""
BeatGridWidget — scrolling beat-grid monitor for two decks.

Shows a horizontal strip with beat-tick marks scrolling past a centre
line in real time.  Both decks rendered on separate rows so phase
alignment is immediately visible: when bar markers line up vertically
the tracks are in sync.

  Deck 1 (top row):   cyan ticks
  Deck 2 (bottom row): orange ticks

  Tall bright tick  = bar-start (beat 1)
  Short dim tick    = regular beat
  Centre line       = current playhead position
"""
from __future__ import annotations

import numpy as np
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore    import Qt, QTimer
from PyQt6.QtGui     import QPainter, QPen, QColor, QFont

from ..core import event_log


_COL_A   = QColor("#00c8ff")
_COL_B   = QColor("#ff7a01")
_BG      = QColor("#08080e")
_SEP     = QColor("#1a1a28")
_CENTRE  = QColor("#2a2a3a")

# Visible window — how many beats fit across the full width
_WINDOW_BEATS = 32


class BeatGridWidget(QWidget):

    HEIGHT = 64

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self._engine = engine
        self.setFixedHeight(self.HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self._grid_log_tick = 0

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)   # ~30 fps — smooth scrolling

    def _tick(self):
        self.update()
        self._grid_log_tick += 1
        if self._grid_log_tick < 30:   # log once per second (~30 fps)
            return
        self._grid_log_tick = 0
        for d_idx, deck in enumerate(self._engine.decks):
            if deck.bpm <= 0 or deck.audio is None or not deck.playing:
                continue
            sr       = deck.sample_rate
            pos      = deck.position
            beat_len = sr * 60.0 / max(deck.bpm * deck.rate, 1.0)
            if deck.beats is not None and len(deck.beats) >= 2:
                beat_idx = max(0, int(np.searchsorted(deck.beats, pos, side="right")) - 1)
                bar_idx  = beat_idx // 4
                lo = deck.beats[beat_idx]
                hi = deck.beats[min(beat_idx + 1, len(deck.beats) - 1)]
                beat_phase = float((pos - lo) / (hi - lo)) if hi > lo else 0.0
            else:
                beat_idx   = int((pos - deck.first_beat) / beat_len) if beat_len > 0 else 0
                bar_idx    = beat_idx // 4
                beat_phase = ((pos - deck.first_beat) % beat_len) / beat_len if beat_len > 0 else 0.0
            event_log.log_grid(d_idx, beat_idx, bar_idx, beat_phase,
                               pos / sr, deck.bpm * deck.rate)

    def paintEvent(self, _):
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing, False)  # crisp lines

        W, H  = self.width(), self.height()
        mid   = H // 2
        cx    = W // 2

        qp.fillRect(0, 0, W, H, _BG)

        # Row separator
        qp.setPen(QPen(_SEP, 1))
        qp.drawLine(0, mid, W, mid)

        # Centre position marker
        qp.setPen(QPen(_CENTRE, 1))
        qp.drawLine(cx, 0, cx, H)

        # "NOW" label
        qp.setFont(QFont("Courier New", 7))
        qp.setPen(QPen(QColor("#333340"), 1))
        qp.drawText(cx - 12, H - 3, "NOW")

        decks_meta = [
            (self._engine.decks[0], _COL_A, 1,       mid - 3),   # top row
            (self._engine.decks[1], _COL_B, mid + 1,  H   - 3),  # bottom row
        ]

        for deck, col, row_top, row_bot in decks_meta:
            row_h = row_bot - row_top
            if deck.bpm <= 0 or deck.audio is None:
                # No track: draw a dim centre line
                qp.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 25), 1))
                qp.drawLine(0, row_top + row_h // 2, W, row_top + row_h // 2)
                continue

            sr         = deck.sample_rate
            # Use EFFECTIVE BPM (bpm × rate) for pixel mapping.
            # deck.beats is in original sample space; pos advances at deck.rate
            # samples per output sample. Using effective beat_len means both
            # synced decks have the same px/beat and the same scroll speed,
            # so their grids line up visually when phase-locked.
            eff_bpm    = max(deck.bpm * deck.rate, 1.0)
            beat_len   = sr * 60.0 / eff_bpm
            pos        = deck.position
            first_beat = deck.first_beat

            # pixels per beat
            px_per_beat = W / _WINDOW_BEATS
            half_w_samp = (W / 2) * beat_len / px_per_beat

            vis_lo = pos - half_w_samp
            vis_hi = pos + half_w_samp

            if deck.beats is not None and len(deck.beats) >= 2:
                # Use stored beat array — accurate even for variable-tempo tracks.
                # The analyzer already rotated the array by downbeat_offset so that
                # beats[0] is the musical downbeat → i % 4 == 0 marks bar-1.
                lo_i = max(0, int(np.searchsorted(deck.beats, vis_lo)) - 1)
                hi_i = min(len(deck.beats),
                           int(np.searchsorted(deck.beats, vis_hi)) + 2)
                for i in range(lo_i, hi_i):
                    bp     = deck.beats[i]
                    px     = int(cx + (bp - pos) / beat_len * px_per_beat)
                    is_bar = (i % 4 == 0)
                    if 0 <= px < W:
                        _draw_tick(qp, px, row_top, row_h, is_bar, col)
            else:
                # Arithmetic fallback from BPM + first_beat
                rel_start   = vis_lo - first_beat
                beat_start  = max(0, int(rel_start / beat_len) - 1)
                beat_end    = beat_start + _WINDOW_BEATS + 4
                for i in range(beat_start, beat_end):
                    bp  = first_beat + i * beat_len
                    if bp > vis_hi:
                        break
                    px  = int(cx + (bp - pos) / beat_len * px_per_beat)
                    if 0 <= px < W:
                        _draw_tick(qp, px, row_top, row_h, i % 4 == 0, col)

        qp.end()


def _draw_tick(qp: QPainter, px: int, row_top: int, row_h: int,
               is_bar: bool, col: QColor):
    """Draw one beat tick mark."""
    if is_bar:
        # Bar-start: full row height, bright
        qp.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 220), 2))
        qp.drawLine(px, row_top, px, row_top + row_h)
        # Glow cap at top
        qp.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 100), 3))
        qp.drawLine(px, row_top, px, row_top + 4)
    else:
        # Regular beat: half height, dimmer
        tick_h = row_h // 2
        ty     = row_top + (row_h - tick_h) // 2
        qp.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 90), 1))
        qp.drawLine(px, ty, px, ty + tick_h)
