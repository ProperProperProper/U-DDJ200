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

# Beat grid — white lines at varying opacity, Serato-accurate
_BEAT_BAR_PEN    = QPen(QColor(255, 255, 255, 200), 1)   # bar (beat 1): 1px bright white
_BEAT_BAR_GLOW   = QPen(QColor(255, 255, 255,  30), 3)   # narrow soft glow
_BEAT_PEN        = QPen(QColor(255, 255, 255,  80), 1)   # regular beat: dim white
_PHRASE_PEN      = QPen(QColor(255, 210,  40, 240), 2)   # phrase: amber, prominent
_PHRASE_PEN_GLOW = QPen(QColor(255, 180,   0,  50), 6)   # phrase glow

_WINDOW_BEATS = 16


class BeatGridWidget(QWidget):

    HEIGHT = 128   # increased for better waveform visibility

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self._engine = engine
        self.setFixedHeight(self.HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self._grid_log_tick = 0
        # Pre-allocated render buffers — reallocated only on width/height change
        self._wf_buf   = [None, None]   # RGBA8888 uint8 (row_h, W, 4)
        self._wf_w     = [0, 0]
        self._wf_h     = [0, 0]
        # Per-deck pre-allocated intermediate arrays (W-length, reallocated on resize)
        self._arr_x    = [None, None]   # float64 arange(W)
        self._arr_lo   = [None, None]
        self._arr_mi   = [None, None]
        self._arr_hi   = [None, None]
        self._arr_R    = [None, None]
        self._arr_G    = [None, None]
        self._arr_B    = [None, None]
        self._arr_barh = [None, None]
        self._arr_bart = [None, None]
        # Smooth-scroll interpolation
        self._snap_pos  = [0.0, 0.0]
        self._snap_time = [0.0, 0.0]
        self._snap_rate = [1.0, 1.0]

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)

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
            samp_per_px  = beat_len / px_per_beat
            half_w_samp  = cx * samp_per_px
            ph_x         = cx   # playhead always at centre

            win_start = pos - half_w_samp   # may be negative near track start
            vis_lo    = win_start
            vis_hi    = win_start + W * samp_per_px
            dbo    = int(getattr(deck, 'downbeat_offset', 0) or 0)
            beats  = deck.beats

            def _beat_px(samp_pos):
                return int((samp_pos - win_start) / samp_per_px)

            def _draw_beats():
                if beats is not None and len(beats) >= 2:
                    i0 = max(0, int(np.searchsorted(beats, vis_lo)) - 1)
                    for j in range(i0, len(beats)):
                        bp = beats[j]
                        if bp > vis_hi:
                            break
                        px = _beat_px(bp)
                        if 0 <= px < W:
                            _draw_tick(qp, px, row_top, row_h, (j + dbo) % 4 == 0, col)
                else:
                    rel_start  = vis_lo - first_beat
                    beat_start = max(0, int(rel_start / beat_len) - 1)
                    for i in range(beat_start, beat_start + _WINDOW_BEATS + 4):
                        bp = first_beat + i * beat_len
                        if bp > vis_hi:
                            break
                        px = _beat_px(bp)
                        if 0 <= px < W:
                            _draw_tick(qp, px, row_top, row_h, (i + dbo) % 4 == 0, col)

            # ── 1. Beat grid (behind waveform) ───────────────────────────────
            _draw_beats()

            # ── 2. Waveform ──────────────────────────────────────────────────
            wf = deck.waveform
            if wf is not None and len(wf) > 1:
                self._paint_waveform(qp, d_idx, wf, win_start, samp_per_px,
                                     W, row_top, row_h)

            # ── 3. Bar + phrase markers on top of waveform ───────────────────
            if beats is not None and len(beats) >= 2:
                i0 = max(0, int(np.searchsorted(beats, vis_lo)) - 1)
                for j in range(i0, len(beats)):
                    bp = beats[j]
                    if bp > vis_hi:
                        break
                    if (j + dbo) % 4 == 0:
                        px = _beat_px(bp)
                        if 0 <= px < W:
                            qp.setPen(_BEAT_BAR_PEN)
                            qp.drawLine(px, row_top, px, row_top + row_h)

            # Phrase markers — amber, drawn last
            phrases = deck.phrases
            if phrases is not None and len(phrases) > 0:
                i0 = max(0, int(np.searchsorted(phrases, vis_lo)) - 1)
                for pp in phrases[i0:]:
                    if pp > vis_hi:
                        break
                    px = _beat_px(pp)
                    if 0 <= px < W:
                        qp.setPen(_PHRASE_PEN_GLOW)
                        qp.drawLine(px, row_top, px, row_top + row_h)
                        qp.setPen(_PHRASE_PEN)
                        qp.drawLine(px, row_top, px, row_top + row_h)

            # ── Playhead (always at centre) ──────────────────────────────────
            qp.setPen(QPen(_NOW, 1))
            qp.drawLine(cx, row_top, cx, row_top + row_h)

            # Track-start marker — bright line where sample 0 lands
            if win_start < 0:
                start_px = int(-win_start / samp_per_px)
                if 0 <= start_px < W:
                    qp.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 180), 2))
                    qp.drawLine(start_px, row_top, start_px, row_top + row_h)

            # ── 4. BPM / role label ──────────────────────────────
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

    def _paint_waveform(self, qp, d_idx, wf, win_start, samp_per_px,
                        W, row_top, row_h):
        """win_start: sample index at x=0 (may be negative near track start)."""
        # Reallocate only when geometry changes
        if self._wf_buf[d_idx] is None or self._wf_w[d_idx] != W or self._wf_h[d_idx] != row_h:
            self._wf_buf[d_idx]   = np.zeros((row_h, W, 4), dtype=np.uint8)
            self._wf_w[d_idx]     = W
            self._wf_h[d_idx]     = row_h
            self._arr_x[d_idx]    = np.arange(W, dtype=np.float64)
            self._arr_lo[d_idx]   = np.empty(W, np.float32)
            self._arr_mi[d_idx]   = np.empty(W, np.float32)
            self._arr_hi[d_idx]   = np.empty(W, np.float32)
            self._arr_R[d_idx]    = np.empty(W, np.float32)
            self._arr_G[d_idx]    = np.empty(W, np.float32)
            self._arr_B[d_idx]    = np.empty(W, np.float32)
            self._arr_barh[d_idx] = np.empty(W, np.int32)
            self._arr_bart[d_idx] = np.empty(W, np.int32)

        buf   = self._wf_buf[d_idx]
        x_arr = self._arr_x[d_idx]
        lo    = self._arr_lo[d_idx]
        mi    = self._arr_mi[d_idx]
        hi    = self._arr_hi[d_idx]
        R     = self._arr_R[d_idx]
        G     = self._arr_G[d_idx]
        B     = self._arr_B[d_idx]
        bar_h = self._arr_barh[d_idx]
        bar_t = self._arr_bart[d_idx]

        buf[:] = 0

        samp  = win_start + x_arr * samp_per_px
        # Clamp to valid waveform range — before-track region shows sample 0,
        # after-track region shows last sample. Mark pre-track columns as dim.
        before_track = samp < 0
        w_idx = np.clip((samp / WAVEFORM_HOP).astype(np.int64), 0, len(wf) - 1)

        vi = w_idx   # always valid after clip
        lo[:] = wf[vi, 0]
        mi[:] = wf[vi, 1]
        hi[:] = wf[vi, 2]

        # Dim pre-track region so the track-start boundary is obvious
        before_factor = np.where(before_track, 0.18, 1.0).astype(np.float32)

        # Spotlight: brightest at playhead (cx = W//2), fades toward edges
        cx_f   = W / 2.0
        dist   = np.abs(x_arr - cx_f) / cx_f
        bright = np.clip(1.0 - dist * 0.7, 0.3, 1.0) * before_factor
        np.clip(lo * 3.0 * bright, 0, 1, out=R)
        np.clip(mi * 2.0 * bright, 0, 1, out=G)
        np.clip(hi * 2.5 * bright, 0, 1, out=B)

        # Use dominant-band energy for height so bass/mid/hi-only sections
        # show tall bars — vector magnitude divided by √3 made single-band
        # sections appear at only ~48% height.
        amp = np.maximum(np.maximum(lo, mi), hi)
        np.clip((row_h * 0.92 * amp).astype(np.int32), 1, row_h, out=bar_h)
        np.subtract(row_h, bar_h, out=bar_t)
        bar_t >>= 1   # divide by 2

        # Vectorised fill — reuse pre-allocated buf in-place
        y_arr  = np.arange(row_h, dtype=np.int32)
        in_bar = (y_arr[:, None] >= bar_t[None, :]) & \
                 (y_arr[:, None] <  (bar_t + bar_h)[None, :])

        Ru = (R * 255).astype(np.uint8)
        Gu = (G * 255).astype(np.uint8)
        Bu = (B * 255).astype(np.uint8)
        buf[:, :, 0] = np.where(in_bar, Ru[None, :], 0)
        buf[:, :, 1] = np.where(in_bar, Gu[None, :], 0)
        buf[:, :, 2] = np.where(in_bar, Bu[None, :], 0)
        buf[:, :, 3] = np.where(in_bar, 230, 0)

        # Pass numpy buffer directly — no tobytes() copy
        qimg = QImage(buf.data, W, row_h, W * 4, QImage.Format.Format_RGBA8888)
        qp.drawImage(0, row_top, qimg)


def _draw_tick(qp: QPainter, px: int, row_top: int, row_h: int,
               is_bar: bool, col: QColor):
    bot = row_top + row_h
    if is_bar:
        # Soft glow pass behind the bar line
        qp.setPen(_BEAT_BAR_GLOW)
        qp.drawLine(px, row_top, px, bot)
        # Bright white bar line — full height, waveform visible underneath via alpha
        qp.setPen(_BEAT_BAR_PEN)
        qp.drawLine(px, row_top, px, bot)
    else:
        # Regular beat: half-height white hairline, vertically centred
        beat_h = row_h // 2
        ty = row_top + (row_h - beat_h) // 2
        qp.setPen(_BEAT_PEN)
        qp.drawLine(px, ty, px, ty + beat_h)
