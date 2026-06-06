"""
BeatGridWidget — Pro-grade dual-deck waveform display.

Layout:
  Two full-width rows (Deck 1 / Deck 2), no side panel eating waveform space.
  Info HUD overlaid on left of waveform with semi-transparent backing.
  3px deck-color accent rail on far left of each row.

Waveform:
  Warm Serato palette  lo=orange  mi=white/pink  hi=blue
  Mirrored symmetric bars.  Glow halo around each bar.
  Tip highlight (brighter top/bottom 2px of each bar).
  Center hairline.

Beat grid:
  Phrase marker  — amber 2px + 6px glow, full height
  Bar marker     — white 1.5px + 3px glow, full height
  Beat marker    — white 1px, half height

Loop region: orange semi-transparent fill + edge lines.
Playhead:    white 2px + 6px cyan glow.

Smooth scroll (Mixxx-style):  deck.rate*sr velocity, pixel-rounded.
"""
from __future__ import annotations

import time
import numpy as np
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore    import Qt, QTimer
from PyQt6.QtGui     import (QPainter, QPen, QColor, QFont, QFontMetrics,
                              QImage)

from ..core          import event_log
from ..core.deck     import WAVEFORM_HOP

# ── Geometry ──────────────────────────────────────────────────────────────────
_ROW_H         = 68     # compact rows
_SEP_H         = 3
_TOTAL_H       = _ROW_H * 2 + _SEP_H
_INFO_W        = 82     # dedicated left info panel — waveform does NOT enter here
_ACCENT_W      = 3      # deck-color accent rail inside info panel right edge
_WINDOW_BEATS  = 20     # many beats → thin crisp bars
_MAX_EXTRAP_MS = 20.0

# ── Colors ────────────────────────────────────────────────────────────────────
_BG          = QColor("#050508")
_SEP_COL     = QColor("#0e0e18")
_CENTER_LINE = QColor(255, 255, 255, 30)
_PLAYHEAD_C  = QColor("#ffffff")
_PH_GLOW     = QColor(0, 220, 255, 55)

_LOOP_FILL   = QColor(255, 130, 0, 40)
_LOOP_EDGE   = QColor(255, 155, 0, 200)

_HUD_BG      = QColor(8, 8, 18, 185)

# Beat grid — blue lines, obvious
_BAR_PEN     = QPen(QColor( 40, 160, 255, 240), 2)   # bar: bright blue
_BAR_GLOW    = QPen(QColor( 40, 160, 255,  50), 5)   # bar glow
_BEAT_PEN    = QPen(QColor( 80, 140, 255, 160), 1)   # beat: dimmer blue
_PHR_PEN     = QPen(QColor(255, 195,  30, 235), 2)   # phrase: amber
_PHR_GLOW    = QPen(QColor(255, 150,   0,  55), 6)

# ── Warm palette coefficients ─────────────────────────────────────────────────
# lo=orange, mi=white/pink, hi=blue  — Serato-style
_LO_R, _LO_G, _LO_B = 4.0,  0.8,  0.0
_MI_R, _MI_G, _MI_B = 0.5,  3.5,  0.7
_HI_R, _HI_G, _HI_B = 0.2,  0.5,  5.0


def _fmt_time(secs: float) -> str:
    secs = max(0.0, secs)
    m    = int(secs) // 60
    s    = secs % 60
    return f"{m}:{s:05.2f}"


class BeatGridWidget(QWidget):

    HEIGHT = _TOTAL_H

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self._engine = engine
        self.setFixedHeight(self.HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)

        # Per-deck render state — reallocated when width changes
        self._wf_cache = [None, None]   # (buf, x_a, y_a, lo, mi, hi, R, G, B, bh, bt, w)

        # Interpolation snapshots
        self._snap_pos  = [0.0, 0.0]
        self._snap_time = [0.0, 0.0]

        self._log_tick = 0

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)   # ~60 fps

    # ── Tick ──────────────────────────────────────────────────────────────────

    def _tick(self):
        now = time.perf_counter()
        any_loaded = False
        for i, deck in enumerate(self._engine.decks):
            if deck.audio is None:
                continue
            any_loaded = True
            self._snap_pos[i]  = deck.position
            self._snap_time[i] = now
        if not any_loaded:
            return
        self.update()

        self._log_tick += 1
        if self._log_tick < 60:
            return
        self._log_tick = 0
        for d_idx, deck in enumerate(self._engine.decks):
            if deck.bpm <= 0 or deck.audio is None or not deck.playing:
                continue
            sr       = deck.sample_rate
            beat_len = sr * 60.0 / max(deck.bpm, 1.0)
            rel      = deck.position - deck.first_beat
            beat_idx = int(rel / beat_len) if beat_len > 0 else 0
            bar_idx  = beat_idx // 4
            beat_ph  = (rel % beat_len) / beat_len if beat_len > 0 else 0.0
            event_log.log_grid(d_idx, beat_idx, bar_idx, beat_ph,
                               deck.position / sr, deck.bpm * deck.rate)

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, _):
        qp  = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        W   = self.width()
        now = time.perf_counter()

        qp.fillRect(0, 0, W, self.HEIGHT, _BG)
        qp.fillRect(0, _ROW_H, W, _SEP_H, _SEP_COL)

        rows = [
            (self._engine.decks[0], QColor("#00c8ff"), 0, 0),
            (self._engine.decks[1], QColor("#ff7a01"), 1, _ROW_H + _SEP_H),
        ]
        for deck, col, d_idx, row_top in rows:
            self._paint_row(qp, deck, col, d_idx, row_top, W, _ROW_H, now)

        qp.end()

    # ── Row ───────────────────────────────────────────────────────────────────

    def _paint_row(self, qp: QPainter, deck, col: QColor,
                   d_idx: int, row_top: int, W: int, row_h: int, now: float):

        # Info panel — completely separate, waveform never touches this area
        self._paint_info(qp, deck, col, d_idx, row_top, row_h)

        wx  = _INFO_W            # waveform starts after info panel
        ww  = W - _INFO_W        # waveform width

        if deck.bpm <= 0 or deck.audio is None:
            qp.fillRect(wx, row_top, ww, row_h, _BG)
            mid = row_top + row_h // 2
            qp.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 18), 1))
            qp.drawLine(wx, mid, wx + ww, mid)
            return

        sr       = deck.sample_rate
        beat_len = sr * 60.0 / max(deck.bpm, 1.0)
        cx       = wx + ww // 2   # playhead x

        samp_per_px = beat_len * _WINDOW_BEATS / ww

        # Mixxx-style smooth interpolation
        scratch = getattr(deck, '_scratch_active', False)
        if deck.playing and not scratch:
            elapsed = min(now - self._snap_time[d_idx], _MAX_EXTRAP_MS * 0.001)
            raw_pos = self._snap_pos[d_idx] + elapsed * deck.rate * sr
        else:
            raw_pos = self._snap_pos[d_idx]
        pos       = round(raw_pos / samp_per_px) * samp_per_px
        pos       = max(0.0, pos)
        win_start = pos - (ww / 2) * samp_per_px
        win_end   = win_start + ww * samp_per_px

        def _px(sp):
            return wx + int((sp - win_start) / samp_per_px)

        # 1. Waveform image
        wf = deck.waveform
        if wf is not None and len(wf) > 1:
            self._render_waveform(qp, d_idx, wf, win_start, samp_per_px,
                                  wx, row_top, ww, row_h)
        else:
            qp.fillRect(wx, row_top, ww, row_h, _BG)

        # 2. Center hairline
        mid_y = row_top + row_h // 2
        qp.setPen(QPen(_CENTER_LINE, 1))
        qp.drawLine(wx, mid_y, wx + ww, mid_y)

        # 3. Loop region
        if deck.loop_active and deck.loop_in is not None:
            lx1 = _px(deck.loop_in)
            lx2 = _px(deck.loop_out)
            lx1c = max(wx, lx1);  lx2c = min(wx + ww, lx2)
            if lx2c > lx1c:
                qp.fillRect(lx1c, row_top, lx2c - lx1c, row_h, _LOOP_FILL)
                qp.setPen(QPen(_LOOP_EDGE, 2))
                if wx <= lx1 <= wx + ww:
                    qp.drawLine(lx1, row_top, lx1, row_top + row_h)
                if wx <= lx2 <= wx + ww:
                    qp.drawLine(lx2, row_top, lx2, row_top + row_h)

        # 4. Beat + bar grid
        dbo    = int(getattr(deck, 'downbeat_offset', 0) or 0)
        beats  = deck.beats
        if beats is not None and len(beats) >= 2:
            i0 = max(0, int(np.searchsorted(beats, win_start)) - 1)
            for j in range(i0, len(beats)):
                bp = beats[j]
                if bp > win_end:
                    break
                px = _px(bp)
                if wx <= px < wx + ww:
                    _draw_beat_tick(qp, px, row_top, row_h,
                                    (j + dbo) % 4 == 0)
        else:
            rel = win_start - deck.first_beat
            i0  = max(0, int(rel / beat_len) - 1)
            for i in range(i0, i0 + _WINDOW_BEATS + 4):
                bp = deck.first_beat + i * beat_len
                if bp > win_end:
                    break
                px = _px(bp)
                if wx <= px < wx + ww:
                    _draw_beat_tick(qp, px, row_top, row_h,
                                    (i + dbo) % 4 == 0)

        # 5. Phrase markers
        phrases = deck.phrases
        if phrases is not None and len(phrases) > 0:
            i0 = max(0, int(np.searchsorted(phrases, win_start)) - 1)
            for pp in phrases[i0:]:
                if pp > win_end:
                    break
                px = _px(pp)
                if wx <= px < wx + ww:
                    qp.setPen(_PHR_GLOW)
                    qp.drawLine(px, row_top, px, row_top + row_h)
                    qp.setPen(_PHR_PEN)
                    qp.drawLine(px, row_top, px, row_top + row_h)

        # 6. Hot cue markers
        for cue_idx, cue_pos in enumerate(deck.hot_cues):
            if cue_pos is None:
                continue
            px = _px(cue_pos)
            if wx <= px < wx + ww:
                cue_col = _CUE_COLORS[cue_idx % len(_CUE_COLORS)]
                qp.setPen(QPen(cue_col, 2))
                qp.drawLine(px, row_top, px, row_top + 14)
                qp.fillRect(px - 4, row_top, 9, 8, cue_col)

        # 7. Playhead glow then sharp line
        qp.setPen(QPen(_PH_GLOW, 6))
        qp.drawLine(cx, row_top, cx, row_top + row_h)
        qp.setPen(QPen(_PLAYHEAD_C, 2))
        qp.drawLine(cx, row_top, cx, row_top + row_h)


    # ── Waveform numpy renderer ───────────────────────────────────────────────

    def _render_waveform(self, qp, d_idx, wf, win_start, samp_per_px,
                         wx, row_top, ww, row_h):
        c = self._wf_cache[d_idx]
        if c is None or c[11] != ww or c[0].shape[0] != row_h:
            buf = np.zeros((row_h, ww, 4), dtype=np.uint8)
            x_a = np.arange(ww, dtype=np.float64)
            y_a = np.arange(row_h, dtype=np.int32)
            lo  = np.zeros(ww, dtype=np.float64)
            mi  = np.zeros(ww, dtype=np.float64)
            hi  = np.zeros(ww, dtype=np.float64)
            R   = np.zeros(ww, dtype=np.float64)
            G   = np.zeros(ww, dtype=np.float64)
            B   = np.zeros(ww, dtype=np.float64)
            bh  = np.zeros(ww, dtype=np.int32)
            bt  = np.zeros(ww, dtype=np.int32)
            self._wf_cache[d_idx] = c = [buf,x_a,y_a,lo,mi,hi,R,G,B,bh,bt,ww]

        buf,x_a,y_a,lo,mi,hi,R,G,B,bh,bt,_ = c

        buf[:] = 0

        samp      = win_start + x_a * samp_per_px
        wf_end    = float(len(wf) * WAVEFORM_HOP)
        in_track  = (samp >= 0) & (samp < wf_end)

        # Pre-track tint
        buf[:, ~in_track, :3] = (6, 6, 14)
        buf[:, ~in_track, 3]  = 255

        lo[:] = 0.0; mi[:] = 0.0; hi[:] = 0.0
        if in_track.any():
            # Linear interpolation between wf frames — no blocky flat-top bars
            wf_f = samp[in_track] / WAVEFORM_HOP
            wi0  = np.clip(wf_f.astype(np.int64), 0, len(wf) - 1)
            wi1  = np.clip(wi0 + 1,                0, len(wf) - 1)
            frac = (wf_f - wi0)[:, None]
            interp = wf[wi0] + frac * (wf[wi1] - wf[wi0])
            lo[in_track] = interp[:, 0]
            mi[in_track] = interp[:, 1]
            hi[in_track] = interp[:, 2]

        # Same palette as deck WaveformWidget: lo=red→orange  mi=green  hi=blue
        np.clip(lo * 3.0, 0, 1, out=R)
        np.clip(mi * 2.0, 0, 1, out=G)
        np.clip(hi * 2.5, 0, 1, out=B)

        # Bar height from amplitude — full range
        amp = np.maximum(np.maximum(lo, mi), hi)
        np.clip((row_h * amp).astype(np.int32), 0, row_h, out=bh)
        bh[~in_track] = 0
        np.subtract(row_h, bh, out=bt); bt >>= 1   # symmetric centre

        Ru = (R * 255).astype(np.uint8)
        Gu = (G * 255).astype(np.uint8)
        Bu = (B * 255).astype(np.uint8)

        in_bar = ((y_a[:, None] >= bt[None, :]) &
                  (y_a[:, None] <  (bt + bh)[None, :]))

        buf[:, :, 0] = np.where(in_bar, Ru[None, :], buf[:, :, 0])
        buf[:, :, 1] = np.where(in_bar, Gu[None, :], buf[:, :, 1])
        buf[:, :, 2] = np.where(in_bar, Bu[None, :], buf[:, :, 2])
        buf[:, :, 3] = np.where(in_bar, 255,         buf[:, :, 3])

        qp.drawImage(wx, row_top,
                     QImage(buf.data, ww, row_h, ww * 4,
                            QImage.Format.Format_RGBA8888))

    # ── Info panel (left, separate from waveform) ─────────────────────────────

    def _paint_info(self, qp: QPainter, deck, col: QColor,
                    d_idx: int, row_top: int, row_h: int):
        # Solid background — waveform never draws here
        qp.fillRect(0, row_top, _INFO_W, row_h, QColor("#0a0a12"))

        # Right-edge accent rail in deck color
        qp.fillRect(_INFO_W - _ACCENT_W, row_top, _ACCENT_W, row_h, col)

        acc = QPen(QColor(col.red(), col.green(), col.blue(), 230), 1)
        dim = QPen(QColor(110, 115, 130, 200), 1)
        x   = 6

        # "DECK N" label
        qp.setPen(acc)
        qp.setFont(QFont("Menlo", 7, QFont.Weight.Bold))
        qp.drawText(x, row_top + 12, f"DECK {d_idx + 1}")

        if deck.audio is None:
            qp.setPen(dim)
            qp.setFont(QFont("Menlo", 7))
            qp.drawText(x, row_top + 30, "no track")
            return

        # Key
        key = deck.key or ''
        if key:
            qp.setPen(QPen(QColor(200, 220, 255, 230), 1))
            qp.setFont(QFont("Menlo", 11, QFont.Weight.Bold))
            qp.drawText(x, row_top + 30, key)

        # Bars remaining
        sr = deck.sample_rate
        if deck.bpm > 0 and sr > 0:
            beat_len  = sr * 60.0 / deck.bpm
            remain_s  = max(0.0, deck.duration - self._snap_pos[d_idx] / sr)
            bars_rem  = remain_s * sr / max(beat_len, 1) / 4.0
            qp.setPen(acc)
            qp.setFont(QFont("Menlo", 9, QFont.Weight.Bold))
            qp.drawText(x, row_top + 46, f"{bars_rem:.1f}")
            qp.setPen(dim)
            qp.setFont(QFont("Menlo", 7))
            qp.drawText(x, row_top + 58, "Bars")


# ── Beat tick helper ──────────────────────────────────────────────────────────

_CUE_COLORS = [
    QColor("#ff4444"), QColor("#ff9900"), QColor("#44ff44"),
    QColor("#44aaff"), QColor("#cc44ff"), QColor("#ff44cc"),
    QColor("#ffffff"), QColor("#ffff44"),
]


def _draw_beat_tick(qp: QPainter, px: int, row_top: int,
                    row_h: int, is_bar: bool):
    bot = row_top + row_h
    if is_bar:
        qp.setPen(_BAR_GLOW)
        qp.drawLine(px, row_top, px, bot)
        qp.setPen(_BAR_PEN)
        qp.drawLine(px, row_top, px, bot)
    else:
        h  = row_h // 2
        ty = row_top + (row_h - h) // 2
        qp.setPen(_BEAT_PEN)
        qp.drawLine(px, ty, px, ty + h)
