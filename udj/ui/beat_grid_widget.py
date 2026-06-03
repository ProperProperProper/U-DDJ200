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
_BG      = QColor("#06060c")
_SEP     = QColor("#1a1a28")
_CENTRE  = QColor("#ffffff")   # bright white NOW line

# Visible window — how many beats fit across the full width
_WINDOW_BEATS = 16   # tighter window = larger, more readable ticks


class BeatGridWidget(QWidget):

    HEIGHT = 96

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
        # Skip paint when both decks are idle — saves ~30% CPU when nothing is loaded
        if all(d.audio is None for d in self._engine.decks):
            return
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
            beat_len = sr * 60.0 / max(deck.bpm, 1.0)
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

        # Bright NOW line
        qp.setPen(QPen(_CENTRE, 1))
        qp.drawLine(cx, 0, cx, H)

        decks_meta = [
            (self._engine.decks[0], _COL_A, 2,       mid - 2,  "1"),
            (self._engine.decks[1], _COL_B, mid + 2,  H   - 2, "2"),
        ]

        qp.setFont(QFont("Courier New", 8, QFont.Weight.Bold))

        for deck, col, row_top, row_bot, label in decks_meta:
            row_h = row_bot - row_top

            if deck.bpm <= 0 or deck.audio is None:
                qp.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 20), 1))
                qp.drawLine(0, row_top + row_h // 2, W, row_top + row_h // 2)
                qp.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 40), 1))
                qp.drawText(6, row_top + row_h // 2 + 4, f"{label}: no track")
                continue

            sr          = deck.sample_rate
            pos         = deck.position
            first_beat  = deck.first_beat
            # Show native BPM when paused, effective BPM when playing
            eff_bpm     = deck.bpm * deck.rate if deck.playing else deck.bpm
            beat_len    = sr * 60.0 / max(eff_bpm, 1.0)

            px_per_beat = W / _WINDOW_BEATS
            half_w_samp = (W / 2) * beat_len / px_per_beat
            vis_lo = pos - half_w_samp
            vis_hi = pos + half_w_samp

            # Always render using uniform BPM arithmetic — same model as sync engine.
            # Stored beat positions have tiny non-uniformities that cause visual drift
            # even when sync is perfect; uniform rendering makes alignment truthful.
            rel_start  = vis_lo - first_beat
            beat_start = max(0, int(rel_start / beat_len) - 1)
            beat_end   = beat_start + _WINDOW_BEATS + 4
            for i in range(beat_start, beat_end):
                bp = first_beat + i * beat_len
                if bp > vis_hi:
                    break
                px = int(cx + (bp - pos) / beat_len * px_per_beat)
                if 0 <= px < W:
                    _draw_tick(qp, px, row_top, row_h, i % 4 == 0, col)

            # BPM readout (left side)
            role = "MASTER" if deck.sync_master else ("SYNC" if deck.sync_follower else "")
            bpm_txt = f"{label}  {deck.bpm * deck.rate:.1f}"  # always show effective bpm in label
            if role:
                bpm_txt += f"  {role}"
            qp.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 180), 1))
            qp.drawText(6, row_top + row_h - 3, bpm_txt)

        qp.end()


def _draw_tick(qp: QPainter, px: int, row_top: int, row_h: int,
               is_bar: bool, col: QColor):
    """Draw one beat tick mark."""
    if is_bar:
        # Bar-start: full height, very bright with glow
        qp.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 255), 2))
        qp.drawLine(px, row_top, px, row_top + row_h)
        # Wide glow
        qp.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 60), 5))
        qp.drawLine(px, row_top, px, row_top + row_h)
    else:
        # Regular beat: 2/3 height, clearly visible
        tick_h = (row_h * 2) // 3
        ty     = row_top + (row_h - tick_h) // 2
        qp.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 160), 1))
        qp.drawLine(px, ty, px, ty + tick_h)
