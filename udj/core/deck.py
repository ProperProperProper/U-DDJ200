"""
Deck — single playback channel.
Decode with soundfile, resample with scipy, play back with 4-point
Catmull-Rom (Hermite) interpolation — same as Mixxx cubic quality.
"""
from __future__ import annotations

import threading
from math import gcd
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly
from typing import Optional, Callable
from . import event_log

NUM_HOT_CUES    = 8
BEAT_LOOP_SIZES = [0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0]
STEM_NAMES      = ["drums", "bass", "other", "vocals", "guitar", "piano"]
_LOOP_XFADE     = 64   # samples crossfade at loop boundary (prevents click)


class Deck:
    def __init__(self, sample_rate: int = 44100, deck_id: int = 0):
        self.sample_rate = sample_rate
        self.deck_id     = deck_id
        self._lock       = threading.Lock()

        self.audio: Optional[np.ndarray] = None   # (N, 2) float32, contiguous

        self.title     = ""
        self.artist    = ""
        self.bpm       = 0.0
        self.key       = ""
        self._camelot  = ""
        self.energy    = 0
        self.duration  = 0.0
        self.file_path = ""

        self.position   = 0.0
        self.playing    = False
        self.rate       = 1.0
        self._bend      = 0.0
        self.first_beat      = 0.0                 # sample position of first downbeat
        self.downbeat_offset = 0                   # beats trimmed from front to align bar-1
        self.quantize   = True                     # snap hot cues / CUE to beat grid
        self.beats:   Optional[np.ndarray] = None  # all beat positions in playback samples
        self.phrases: Optional[np.ndarray] = None  # phrase-start positions in playback samples
        self.gain_linear:  float = 1.0   # loudness-normalisation gain (linear)
        self.gain_enabled: bool  = False  # OFF by default — user toggles per-deck

        self.cue_point = -1
        self.hot_cues: list[Optional[int]] = [None] * NUM_HOT_CUES

        self.loop_active              = False
        self.loop_in:  Optional[int] = None
        self.loop_out: Optional[int] = None
        self._current_loop_beats      = 0.0
        self._loop_recording          = False

        self._scratch_active = False

        self.sync_follower = False
        self.sync_master   = False

        # Stems — populated asynchronously after track load
        self.stems: Optional[dict[str, np.ndarray]] = None
        self.active_stems: set[str] = set(STEM_NAMES)
        self.stem_volumes: dict[str, float] = {s: 1.0 for s in STEM_NAMES}  # per-stem gain
        self.stems_ready  = False
        self._stems_cache: Optional[np.ndarray] = None
        self._solo_stem:   Optional[str] = None   # name of soloed stem (hold-to-solo)

        self.key_shift: int = 0   # semitones ±12; applied as playback rate multiplier

        self.on_track_loaded:       Optional[Callable] = None
        self.on_play_state_changed: Optional[Callable] = None
        self.on_hot_cue_changed:    Optional[Callable] = None
        self.on_stems_ready:        Optional[Callable] = None

    # ── Loading ───────────────────────────────────────────────

    def load(self, path: str, metadata: dict | None = None):
        threading.Thread(target=self._load, args=(path, metadata),
                         daemon=True).start()

    def _load(self, path: str, metadata: dict | None):
        try:
            audio, sr = sf.read(path, dtype="float32", always_2d=True)
        except Exception as e:
            print(f"[Deck {self.deck_id}] load error: {e}"); return

        if sr != self.sample_rate:
            try:
                g = gcd(sr, self.sample_rate)
                audio = resample_poly(audio, self.sample_rate // g,
                                      sr // g, axis=0).astype(np.float32)
                print(f"[Deck {self.deck_id}] resampled {sr}→{self.sample_rate} Hz")
            except Exception as e:
                print(f"[Deck {self.deck_id}] resample error: {e}"); return

        if audio.shape[1] == 1:
            audio = np.repeat(audio, 2, axis=1)

        audio = np.ascontiguousarray(
            np.clip(audio, -1.0, 1.0).astype(np.float32))

        with self._lock:
            self.audio        = audio
            self.position     = 0.0
            self.playing      = False
            self.cue_point    = -1
            self.hot_cues     = [None] * NUM_HOT_CUES
            self.loop_active  = False
            self._loop_recording = False
            self.file_path    = path
            self.duration     = len(audio) / self.sample_rate
            self.stems        = None
            self.stems_ready  = False
            self._stems_cache = None
            self.active_stems = set(STEM_NAMES)
            self.stem_volumes = {s: 1.0 for s in STEM_NAMES}
            if metadata:
                self.title  = metadata.get("title",  "")
                self.artist = metadata.get("artist", "")
                self.bpm      = float(metadata.get("bpm",  0) or 0)
                self.key      = metadata.get("key",   "")
                self._camelot = metadata.get("camelot", "") or ""
                self.energy   = int(metadata.get("energy", 0) or 0)
                fbs  = float(metadata.get("first_beat_sec", 0) or 0)
                self.first_beat      = fbs * self.sample_rate
                self.downbeat_offset = int(metadata.get("downbeat_offset", 0) or 0)
                # Loudness normalisation gain (Platinum Notes style)
                gain_db = float(metadata.get("gain_db", 0) or 0)
                self.gain_linear = float(10.0 ** (gain_db / 20.0))
                # Load stored beat grid (Mixxx-style: every beat position)
                import json as _json
                bp_json = metadata.get("beat_positions_json", "[]") or "[]"
                try:
                    bp_sec = _json.loads(bp_json)
                    self.beats = (np.array(bp_sec, dtype=np.float64) * self.sample_rate
                                  if bp_sec else None)
                except Exception:
                    self.beats = None
                ph_json = metadata.get("phrase_positions_json", "[]") or "[]"
                try:
                    ph_sec = _json.loads(ph_json)
                    self.phrases = (np.array(ph_sec, dtype=np.float64) * self.sample_rate
                                    if ph_sec else None)
                except Exception:
                    self.phrases = None

                # Load auto-detected cue points (MIK-style energy markers)
                cq_json = metadata.get("cue_points_json", "[]") or "[]"
                try:
                    for cue in _json.loads(cq_json):
                        pad = int(cue.get("pad", 0))
                        sec = float(cue.get("sec", 0.0))
                        if 0 <= pad < NUM_HOT_CUES:
                            self.hot_cues[pad] = int(sec * self.sample_rate)
                except Exception:
                    pass
            else:
                self.first_beat      = 0.0
                self.downbeat_offset = 0
                self.beats           = None
                self.phrases         = None
                self.gain_linear     = 1.0

        n_beats = len(self.beats) if self.beats is not None else 0
        n_cues  = sum(1 for c in self.hot_cues if c is not None)
        print(f"[DECK{self.deck_id+1}] LOADED  '{getattr(self,'title','?')}'  "
              f"bpm={self.bpm:.2f}  key={getattr(self,'key','?')}  "
              f"first_beat={self.first_beat/self.sample_rate:.3f}s  "
              f"beats={n_beats}  cues={n_cues}  dur={self.duration:.1f}s  "
              f"gain={getattr(self,'gain_linear',1.0):.3f}x")
        if self.on_track_loaded:
            self.on_track_loaded(self.deck_id)

    # ── Transport ─────────────────────────────────────────────

    def play_pause(self):
        self.playing = not self.playing
        pos_sec = self.position / max(self.sample_rate, 1)
        print(f"[DECK{self.deck_id+1}] {'▶ PLAY' if self.playing else '⏸ PAUSE'}"
              f"  pos={pos_sec:.3f}s  bpm={self.bpm:.2f}"
              f"  beats={'yes' if self.beats is not None and len(self.beats) else 'NO'}"
              f"  audio={'yes' if self.audio is not None else 'NO'}")
        if self.on_play_state_changed:
            self.on_play_state_changed(self.deck_id, self.playing)

    def play(self):
        self.playing = True
        if self.on_play_state_changed:
            self.on_play_state_changed(self.deck_id, True)

    def pause(self):
        self.playing = False
        if self.on_play_state_changed:
            self.on_play_state_changed(self.deck_id, False)

    def _q(self, pos: float, event_label: str = "quantize") -> int:
        """Return pos snapped to nearest beat when quantize is on."""
        if self.quantize and self.bpm > 0:
            snapped = self._nearest_beat(pos)
            event_log.log_quantize(
                self.deck_id, event_label,
                pos / self.sample_rate,
                snapped / self.sample_rate,
                self.bpm)
            return snapped
        return int(pos)

    def cue_default(self, pressed: bool):
        if pressed:
            if not self.playing:
                if self.cue_point < 0:
                    self.cue_point = self._q(self.position)
                else:
                    self.position = float(self.cue_point)
                    self.playing  = True
            else:
                if self.cue_point >= 0:
                    self.position = float(self.cue_point)
                self.playing = False

    def cue_stutter_start(self):
        """rekordbox CDJ cue hold: jump to cue and play while held."""
        if self.cue_point >= 0:
            self.position = float(self.cue_point)
        self.playing = True
        if self.on_play_state_changed:
            self.on_play_state_changed(self.deck_id, True)

    def cue_stutter_stop(self):
        """rekordbox CDJ cue release: stop and return to cue point."""
        if self.cue_point >= 0:
            self.position = float(self.cue_point)
        self.playing = False
        if self.on_play_state_changed:
            self.on_play_state_changed(self.deck_id, False)

    def cue_gotoandstop(self):
        if self.cue_point >= 0:
            self.position = float(self.cue_point)
        self.playing = False

    def cue_gotoandplay(self):
        if self.cue_point >= 0:
            self.position = float(self.cue_point)
        self.playing = True

    # ── Hot cues ──────────────────────────────────────────────

    def hotcue_activate(self, n: int):
        if self.hot_cues[n] is None:
            self.hot_cues[n] = self._q(self.position, f"hotcue_set_{n}")  # set quantised
            if self.on_hot_cue_changed:
                self.on_hot_cue_changed(self.deck_id, n, True)
        else:
            self.position = float(self.hot_cues[n])
            if not self.playing:
                self.playing = True

    def hotcue_clear(self, n: int):
        self.hot_cues[n] = None
        if self.on_hot_cue_changed:
            self.on_hot_cue_changed(self.deck_id, n, False)

    # ── Beat loops ────────────────────────────────────────────

    def _nearest_phrase(self, position: float,
                        snap_beats: float = 1.5) -> Optional[int]:
        """
        Return the nearest phrase-start position if it's within snap_beats beats.
        Returns None if no phrase boundary is close enough.
        Mirrors djay's smart quantize: wider window near phrase boundaries.
        """
        if self.phrases is None or len(self.phrases) == 0:
            return None
        beat_len = self.sample_rate * 60.0 / self.bpm if self.bpm > 0 else 0.0
        window   = snap_beats * beat_len
        idx      = int(np.searchsorted(self.phrases, position))
        best = None
        best_d = float("inf")
        for i in [idx - 1, idx]:
            if 0 <= i < len(self.phrases):
                d = abs(self.phrases[i] - position)
                if d < window and d < best_d:
                    best_d = d
                    best   = int(self.phrases[i])
        return best

    def _nearest_beat(self, position: float) -> int:
        """Snap to nearest beat using stored grid (Mixxx-style)."""
        if self.beats is not None and len(self.beats) >= 2:
            idx  = int(np.searchsorted(self.beats, position))
            lo   = self.beats[max(0, idx - 1)]
            hi   = self.beats[min(len(self.beats) - 1, idx)]
            return int(lo if abs(position - lo) <= abs(position - hi) else hi)
        if self.bpm <= 0:
            return int(position)
        beat_len = self.sample_rate * 60.0 / self.bpm
        return int(self.first_beat + round((position - self.first_beat) / beat_len) * beat_len)

    def _nearest_bar(self, position: float) -> int:
        """Snap to nearest bar-1 boundary using stored grid (Mixxx-style)."""
        if self.beats is not None and len(self.beats) >= 4:
            idx     = int(np.searchsorted(self.beats, position, side="right")) - 1
            idx     = max(0, min(len(self.beats) - 1, idx))
            lo_idx  = (idx // 4) * 4
            hi_idx  = lo_idx + 4
            lo      = self.beats[lo_idx]
            hi      = self.beats[hi_idx] if hi_idx < len(self.beats) else \
                      lo + (lo - self.beats[max(0, lo_idx - 4)] if lo_idx >= 4
                            else self.sample_rate * 60.0 / max(self.bpm, 1) * 4)
            return int(lo if abs(position - lo) <= abs(position - hi) else hi)
        if self.bpm <= 0:
            return int(position)
        beat_len = self.sample_rate * 60.0 / self.bpm
        bar_len  = beat_len * 4.0
        return int(self.first_beat + round((position - self.first_beat) / bar_len) * bar_len)

    def loop_halve(self):
        """Halve the current loop length."""
        if not self.loop_active or self.loop_in is None or self.loop_out is None:
            return
        mid = self.loop_in + (self.loop_out - self.loop_in) // 2
        self.loop_out = mid
        if self.bpm > 0:
            self._current_loop_beats = (self.loop_out - self.loop_in) / (self.sample_rate * 60.0 / self.bpm)

    def loop_double(self):
        """Double the current loop length."""
        if not self.loop_active or self.loop_in is None or self.loop_out is None:
            return
        span = self.loop_out - self.loop_in
        self.loop_out = min(self.loop_in + span * 2,
                            len(self.audio) - 1 if self.audio is not None else self.loop_out + span)
        if self.bpm > 0:
            self._current_loop_beats = (self.loop_out - self.loop_in) / (self.sample_rate * 60.0 / self.bpm)

    def beat_jump(self, bars: float):
        """Jump forward (positive) or backward (negative) by N bars, quantized to bar grid."""
        if self.bpm <= 0 or self.audio is None:
            return
        bar_samples = self.sample_rate * 60.0 / self.bpm * 4.0
        new_pos = self.position + bars * bar_samples
        total = len(self.audio)
        self.position = float(max(0.0, min(total - 2, new_pos)))

    def beatloop_toggle(self, beats: float):
        if self.loop_active and self._current_loop_beats == beats:
            self.loop_active = False
        else:
            samps = (int((60.0 / self.bpm) * beats * self.sample_rate)
                     if self.bpm > 0 else int(2.0 * self.sample_rate))
            # Smart quantize: prefer phrase boundary if close (djay-style),
            # otherwise snap to nearest beat
            snap = self._nearest_phrase(self.position, snap_beats=1.5)
            if snap is None:
                snap = self._nearest_beat(self.position)
            self.loop_in             = snap
            self.loop_out            = self.loop_in + samps
            self._current_loop_beats = beats
            self.loop_active         = True

    def beat_quantize_loop_start(self):
        """Shift+Sync: arm loop — snaps to nearest phrase boundary, then bar, then beat."""
        with self._lock:
            snap = self._nearest_phrase(self.position, snap_beats=2.0)
            if snap is None:
                snap = self._nearest_bar(self.position)
            self.loop_in     = snap
            self.loop_active = False
            self._loop_recording = True

    def beat_quantize_loop_end(self):
        """Sync (second press): set loop-out snapped to nearest bar, activate."""
        with self._lock:
            if not self._loop_recording:
                return
            if self.bpm > 0:
                beat_len = self.sample_rate * 60.0 / self.bpm
                bar_len  = beat_len * 4.0
                self.loop_out = self._nearest_bar(self.position)
                if self.loop_out <= self.loop_in:
                    self.loop_out = self.loop_in + int(bar_len)
                self._current_loop_beats = (self.loop_out - self.loop_in) / beat_len
            else:
                self.loop_out = int(self.position)
                if self.loop_out <= self.loop_in:
                    self.loop_out = self.loop_in + int(self.sample_rate * 2)
                self._current_loop_beats = 4.0
            self.loop_active     = True
            self._loop_recording = False

    # ── Stems ─────────────────────────────────────────────────

    def set_stems(self, stems: dict):
        """Called from background thread once demucs separation is done."""
        # Extract metadata flags before storing audio arrays
        vocal_present = stems.pop("_vocal_present", True)

        # Auto-disable vocals for instrumental tracks — it's just bleed/noise
        if not vocal_present and "vocals" in self.active_stems:
            self.active_stems.discard("vocals")
            print(f"[Deck {self.deck_id}] instrumental track — vocals stem auto-disabled")

        self.stems       = stems   # atomic assignment, GIL-safe
        self.stems_ready = True    # read() checks this flag
        if self.on_stems_ready:
            self.on_stems_ready(self.deck_id)

    def toggle_stem(self, name: str):
        # GIL-safe set mutation; applied live in read()
        if name in self.active_stems:
            self.active_stems.discard(name)
        else:
            self.active_stems.add(name)

    # Individual stem kill knobs — 4 primary stems, hardware-mapped
    STEM_KNOB_NAMES = ["vocals", "drums", "bass", "other"]

    def set_stem_blend(self, knob_idx: int, raw: float):
        """Individual stem kill knob (replaces crossfader model).
        raw 0.0 = kill, raw 0.5-1.0 = full.
        Maps knob_idx 0-3 to vocals/drums/bass/other.
        """
        if knob_idx >= len(self.STEM_KNOB_NAMES):
            return
        self.set_stem_volume(self.STEM_KNOB_NAMES[knob_idx], raw)

    def set_stem_volume(self, name: str, raw: float):
        """Individual stem kill knob.
        raw 0.0 = kill, raw 0.5 = full, raw 0.5-1.0 = full (no boost).
        Sqrt taper on the left half so the feel matches DJM EQ kill knobs.
        """
        import math
        raw = max(0.0, min(1.0, float(raw)))
        if raw >= 0.5:
            vol = 1.0
        else:
            vol = math.sqrt(raw / 0.5)   # 0→0, 0.5→1, smooth sqrt curve
        self.stem_volumes[name] = vol
        if raw < 0.01:
            self.active_stems.discard(name)
        else:
            self.active_stems.add(name)

    def _rebuild_stems_cache(self):
        """Recompute the precomputed mix of active stems with per-stem volumes."""
        if not self.stems or self.audio is None:
            self._stems_cache = None
            return
        result = None
        for s in self.active_stems:
            if s not in self.stems:
                continue
            vol = self.stem_volumes.get(s, 1.0)
            part = self.stems[s] * vol
            result = part.copy() if result is None else result + part
        if result is None:
            self._stems_cache = np.zeros_like(self.audio)
        else:
            self._stems_cache = np.clip(result, -1.0, 1.0).astype(np.float32)

    # ── Jog ───────────────────────────────────────────────────

    def jog_scratch_start(self): self._scratch_active = True
    def jog_scratch_stop(self):  self._scratch_active = False
    def jog_scratch_tick(self, delta: float):
        self.position = max(0.0, self.position + delta)
    def jog_bend(self, delta: float):
        # Accumulate bends — each jog CC adds to the current bend.
        # Bent rate decays back to 0 over ~10 callbacks (230ms) so the
        # feel persists long enough to register against the sync engine.
        self._bend += delta

    # ── Properties ────────────────────────────────────────────

    @property
    def elapsed_seconds(self) -> float:
        return self.position / self.sample_rate

    @property
    def remaining_seconds(self) -> float:
        if self.audio is None: return 0.0
        return max(0.0, (len(self.audio) - self.position) / self.sample_rate)

    @property
    def play_position(self) -> float:
        if self.audio is None or len(self.audio) == 0: return 0.0
        return self.position / len(self.audio)

    # ── Audio callback — Catmull-Rom (Hermite) interpolation ──

    def read(self, frames: int) -> np.ndarray:
        silence = np.zeros((frames, 2), dtype=np.float32)
        if not self._lock.acquire(blocking=False):
            return silence
        try:
            audio = self.audio
            if audio is None:
                return silence
            if not self.playing and not self._scratch_active:
                return silence

            total = len(audio)
            if total < 4:
                return silence

            # Key shift: multiply rate by 2^(semitones/12) for pitch transposition.
            # Sync engine compensates tempo drift, so only pitch shifts audibly.
            key_ratio  = 2.0 ** (self.key_shift / 12.0) if self.key_shift != 0 else 1.0
            rate       = (self.rate + self._bend) * key_ratio
            self._bend *= 0.75   # decay: bend fades over ~10 callbacks (230ms)

            pos = self.position + np.arange(frames, dtype=np.float64) * rate

            loop_wrap_mask = None
            if (self.loop_active and
                    self.loop_out is not None and self.loop_in is not None):
                span = float(self.loop_out - self.loop_in)
                if span > 0:
                    over = pos >= self.loop_out
                    if over.any():
                        loop_wrap_mask = over.copy()
                    pos[over] = self.loop_in + np.mod(
                        pos[over] - self.loop_in, span)

            valid = (pos >= 0) & (pos < total - 2)
            pos   = np.clip(pos, 0.0, total - 2.001)

            p1 = pos.astype(np.int64)
            p0 = np.maximum(p1 - 1, 0)
            p2 = np.minimum(p1 + 1, total - 1)
            p3 = np.minimum(p1 + 2, total - 1)
            t  = (pos - p1).astype(np.float32)
            t2 = t * t;  t3 = t2 * t

            def _hermite(src):
                out_ch = np.empty((frames, 2), dtype=np.float32)
                for ch in range(2):
                    y0 = src[p0, ch]; y1 = src[p1, ch]
                    y2 = src[p2, ch]; y3 = src[p3, ch]
                    out_ch[:, ch] = 0.5 * (
                        (-y0 + 3*y1 - 3*y2 + y3) * t3 +
                        (2*y0 - 5*y1 + 4*y2 - y3) * t2 +
                        (-y0 + y2) * t + 2*y1)
                return out_ch

            # Stems: mix live per-buffer — O(active_stems × frames), never O(track_length)
            # Snapshot volatile state outside any lock to avoid blocking the audio callback
            if self.stems_ready and self.stems:
                active  = list(self.active_stems)           # GIL-safe snapshot
                volumes = dict(self.stem_volumes)           # GIL-safe snapshot
                out = np.zeros((frames, 2), dtype=np.float32)
                for s in active:
                    if s in self.stems and isinstance(self.stems[s], np.ndarray):
                        vol = volumes.get(s, 1.0)
                        if vol > 0.0:
                            out += _hermite(self.stems[s]) * vol
                # Soft limiter instead of hard clip — same tanh knee as the master bus
                thr = 0.85
                over = np.abs(out) > thr
                if over.any():
                    knee = 1.0 - thr
                    sign = np.sign(out[over])
                    out[over] = sign * (thr + knee * np.tanh(
                        (np.abs(out[over]) - thr) / knee))
                out = out.astype(np.float32)
            else:
                out = _hermite(audio)

            out[~valid] = 0.0

            # Crossfade the loop restart boundary to eliminate clicks.
            # Blend the first _LOOP_XFADE samples after a wrap with the
            # pre-wrap signal read from loop_out so the discontinuity is
            # smoothed over ~1.5 ms (64 samples @44100).
            if loop_wrap_mask is not None and self.loop_out is not None:
                xf = _LOOP_XFADE
                first_wrap = int(np.argmax(loop_wrap_mask))
                n_blend = min(xf, frames - first_wrap, frames)
                if n_blend > 0:
                    fade_in  = np.linspace(0.0, 1.0, n_blend, dtype=np.float32)
                    fade_out = 1.0 - fade_in
                    # Read the outgoing signal directly from loop_out position
                    tail_start = min(self.loop_out, total - 2)
                    tail_idx   = np.clip(np.arange(tail_start,
                                                   tail_start + n_blend,
                                                   dtype=np.int64), 0, total - 1)
                    for ch in range(2):
                        tail = audio[tail_idx, ch]
                        out[first_wrap:first_wrap + n_blend, ch] = (
                            out[first_wrap:first_wrap + n_blend, ch] * fade_in
                            + tail * fade_out)

            if valid[-1]:
                self.position = float(pos[-1]) + rate
            else:
                first_bad = int(np.argmax(~valid))
                if first_bad > 0:
                    self.position = float(pos[first_bad - 1]) + rate
                self.playing = False

            # Loudness normalisation — only applied when gain_enabled is True
            # (toggled by the user via the NORM button in the mixer).
            if self.gain_enabled and self.gain_linear != 1.0:
                out = out * self.gain_linear
                over = np.abs(out) > 0.98
                if over.any():
                    knee = 0.02
                    sign = np.sign(out[over])
                    out[over] = sign * (0.98 + knee * np.tanh(
                        (np.abs(out[over]) - 0.98) / knee))
                out = out.astype(np.float32)

            return out
        finally:
            self._lock.release()
