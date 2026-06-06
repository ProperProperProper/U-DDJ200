"""
BeatGridWidget — Serato-style scrolling waveform + beat grid for two decks.

Waveform layer (bottom):
  Spectral colour bars: red=bass, green=mid, blue=high-freq
  Brightness fades toward edges (spotlight on playhead)

Beat grid layer (top):
  Tall bright tick  = bar-start
  Short dim tick    = regular beat
  White NOW line    = playhead

Performance: zero Python loops in paintEvent — fully numpy-vectorised + QImage.
Waveform data pre-computed in deck load thread; paint just indexes into it.
"""
from __future__ import annotations

import time
import numpy as np
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore    import Qt, QTimer
from PyQt6.QtGui     import QPainter, QPen, QColor, QFont, QImage

from ..core          import event_log
from ..core.deck     import WAVEFORM_HOP


_COL_A  = QColor("#00c8ff")
_COL_B  = QColor("#ff7a01")
_BG     = QColor("#06060c")
_SEP    = QColor("#1a1a28")
_NOW    = QColor("#ffffff")

_WINDOW_BEATS = 16


class BeatGridWidget(QWidget):

    HEIGHT = 128   # increased for better waveform visibility

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self._engine = engine
        self.setFixedHeight(self.HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self._grid_log_tick = 0
        # Pre-allocated pixel buffers, resized on first use or width change
        self._wf_buf = [None, None]
        self._wf_buf_w = [0, 0]
        # Smooth-scroll interpolation: snapshot position + wall-clock time each tick
        self._snap_pos  = [0.0, 0.0]   # deck.position at last tick
        self._snap_time = [0.0, 0.0]   # perf_counter at last tick
        self._snap_rate = [1.0, 1.0]   # deck.rate at last tick

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)   # ~60 fps

    def _tick(self):
        if all(d.audio is None for d in self._engine.decks):
            return
        now = time.perf_counter()
        for i, deck in enumerate(self._engine.decks):
            if deck.audio is not None:
                self._snap_pos[i]  = deck.position
                self._snap_time[i] = now
                self._snap_rate[i] = deck.rate
        self.update()
        self._grid_log_tick += 1
        if self._grid_log_tick < 60:
            return
        self._grid_log_tick = 0
        for d_idx, deck in enumerate(self._engine.decks):
            if deck.bpm <= 0 or deck.audio is None or not deck.playing:
                continue
            sr       = deck.sample_rate
            pos      = deck.position
            beat_len = sr * 60.0 / max(deck.bpm, 1.0)
            rel      = pos - deck.first_beat
            beat_idx = int(rel / beat_len) if beat_len > 0 else 0
            bar_idx  = beat_idx // 4
            beat_ph  = (rel % beat_len) / beat_len if beat_len > 0 else 0.0
            event_log.log_grid(d_idx, beat_idx, bar_idx, beat_ph,
                               pos / sr, deck.bpm * deck.rate)

    # ─────────────────────────────────────────────────────────────
    #  Paint
    # ─────────────────────────────────────────────────────────────

    def paintEvent(self, _):
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        W, H = self.width(), self.height()
        mid  = H // 2
        cx   = W // 2

        qp.fillRect(0, 0, W, H, _BG)
        qp.setPen(QPen(_SEP, 1))
        qp.drawLine(0, mid, W, mid)
        qp.setPen(QPen(_NOW, 1))
        qp.drawLine(cx, 0, cx, H)

        now       = time.perf_counter()
        decks_meta = [
            (self._engine.decks[0], _COL_A, 0,   2,       mid - 2,  "1"),
            (self._engine.decks[1], _COL_B, 1,   mid + 2, H   - 2,  "2"),
        ]

        qp.setFont(QFont("Courier New", 8, QFont.Weight.Bold))

        for deck, col, d_idx, row_top, row_bot, label in decks_meta:
            row_h = row_bot - row_top

            if deck.bpm <= 0 or deck.audio is None:
                qp.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 20), 1))
                qp.drawLine(0, row_top + row_h // 2, W, row_top + row_h // 2)
                qp.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 40), 1))
                qp.drawText(6, row_top + row_h // 2 + 4, f"{label}: no track")
                continue

            sr          = deck.sample_rate
            # Interpolate position between audio-callback updates for smooth scroll
            elapsed     = now - self._snap_time[d_idx]
            playing_fwd = deck.playing and not getattr(deck, '_scratch_active', False)
            if playing_fwd:
                pos = self._snap_pos[d_idx] + elapsed * self._snap_rate[d_idx] * sr
            else:
                pos = deck.position
            first_beat  = deck.first_beat
            beat_len    = sr * 60.0 / max(deck.bpm, 1.0)
            eff_bpm     = deck.bpm * deck.rate

            px_per_beat  = W / _WINDOW_BEATS
            half_w_samp  = (W / 2) * beat_len / px_per_beat

            # ── 1. Waveform (vectorised, no loops) ──────────────
            wf = deck.waveform
            if wf is not None and len(wf) > 1:
                self._paint_waveform(qp, d_idx, wf, pos, beat_len,
                                     px_per_beat, cx, W, row_top, row_h)

            # ── 2. Beat grid ticks ───────────────────────────────
            vis_lo     = pos - half_w_samp
            vis_hi     = pos + half_w_samp
            rel_start  = vis_lo - first_beat
            beat_start = max(0, int(rel_start / beat_len) - 1)
            beat_end   = beat_start + _WINDOW_BEATS + 4

            dbo = int(getattr(deck, 'downbeat_offset', 0) or 0)
            for i in range(beat_start, beat_end):
                bp = first_beat + i * beat_len
                if bp > vis_hi:
                    break
                px = int(cx + (bp - pos) / beat_len * px_per_beat)
                if 0 <= px < W:
                    _draw_tick(qp, px, row_top, row_h, (i + dbo) % 4 == 0, col)

            # ── 3. BPM / role label ──────────────────────────────
            role = ("MASTER" if deck.sync_master
                    else ("SYNC" if deck.sync_follower else ""))
            txt  = f"{label}  {eff_bpm:.1f}"
            if role:
                txt += f"  {role}"
            qp.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 200), 1))
            qp.drawText(6, row_top + row_h - 3, txt)

        qp.end()

    # ─────────────────────────────────────────────────────────────
    #  Waveform renderer — zero Python loops
    # ─────────────────────────────────────────────────────────────

    def _paint_waveform(self, qp, d_idx, wf, pos, beat_len,
                        px_per_beat, cx, W, row_top, row_h):
        # Ensure buffer sized correctly (reuse between frames)
        if self._wf_buf[d_idx] is None or self._wf_buf_w[d_idx] != W:
            self._wf_buf[d_idx]   = np.zeros((row_h, W, 4), dtype=np.uint8)
            self._wf_buf_w[d_idx] = W

        buf = self._wf_buf[d_idx]
        buf[:] = 0   # clear previous frame

        # Map every pixel column → waveform frame index
        x_arr  = np.arange(W, dtype=np.float64)
        samp   = pos + (x_arr - cx) * (beat_len / px_per_beat)
        w_idx  = (samp / WAVEFORM_HOP).astype(np.int64)
        valid  = (w_idx >= 0) & (w_idx < len(wf))

        # Gather band energies for valid columns only
        lo = np.zeros(W, np.float32)
        mi = np.zeros(W, np.float32)
        hi = np.zeros(W, np.float32)
        if valid.any():
            vi      = w_idx[valid]
            lo[valid] = wf[vi, 0]
            mi[valid] = wf[vi, 1]
            hi[valid] = wf[vi, 2]

        # Serato-style colour: bass=red, mid=green, high=blue
        # Brightness spotlight: full brightness at centre, 30% at edges
        dist    = np.abs(x_arr - cx) / (W / 2.0)         # 0=centre, 1=edge
        bright  = np.clip(1.0 - dist * 0.7, 0.3, 1.0)

        R = np.clip(lo * 3.0 * bright, 0, 1)
        G = np.clip(mi * 2.0 * bright, 0, 1)
        B = np.clip(hi * 2.5 * bright, 0, 1)

        # Bar height: energy magnitude, centred vertically
        amp    = np.sqrt(lo ** 2 + mi ** 2 + hi ** 2) / 1.732  # normalise √3
        bar_h  = (row_h * 0.88 * amp).astype(np.int32).clip(1, row_h)
        bar_h[~valid] = 0
        bar_t  = ((row_h - bar_h) / 2.0).astype(np.int32)

        # Vectorised fill: in_bar[y, x] True where pixel is inside the bar
        y_arr  = np.arange(row_h, dtype=np.int32)
        in_bar = (y_arr[:, None] >= bar_t[None, :]) & \
                 (y_arr[:, None] <  (bar_t + bar_h)[None, :])   # (row_h, W)

        # RGBA fill (Format_RGBA8888)
        buf[:, :, 0] = np.where(in_bar, (R * 255).astype(np.uint8)[None, :], 0)
        buf[:, :, 1] = np.where(in_bar, (G * 255).astype(np.uint8)[None, :], 0)
        buf[:, :, 2] = np.where(in_bar, (B * 255).astype(np.uint8)[None, :], 0)
        buf[:, :, 3] = np.where(in_bar, 230, 0)

        raw   = buf.tobytes()
        qimg  = QImage(raw, W, row_h, W * 4, QImage.Format.Format_RGBA8888)
        qp.drawImage(0, row_top, qimg)


def _draw_tick(qp: QPainter, px: int, row_top: int, row_h: int,
               is_bar: bool, col: QColor):
    if is_bar:
        qp.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 255), 2))
        qp.drawLine(px, row_top, px, row_top + row_h)
        qp.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 50), 5))
        qp.drawLine(px, row_top, px, row_top + row_h)
    else:
        tick_h = (row_h * 2) // 3
        ty     = row_top + (row_h - tick_h) // 2
        qp.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 160), 1))
        qp.drawLine(px, ty, px, ty + tick_h)
