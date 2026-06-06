"""
Deck — single playback channel.
Decode with soundfile, resample with scipy, play back with 4-point
Catmull-Rom (Hermite) interpolation — same as Mixxx cubic quality.
"""
from __future__ import annotations

import json
import threading
from math import gcd
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly, sosfilt, butter
from typing import Optional, Callable
from . import event_log
from .stems import STEM_NAMES
from .mixer import soft_limit

NUM_HOT_CUES    = 8
BEAT_LOOP_SIZES = [0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0]
_LOOP_XFADE     = 64   # samples crossfade at loop boundary

# Quantize / pre-roll constants
_QUANTIZE_WINDOW  = 0.40   # snap only if transient is within 40% of a beat
_PREROLL_SEC      = 0.030  # 30ms pre-roll overlap at cue/loop triggers
_OVERLAP_TRIM_SEC = 0.001  # trim prior region only if overlap > 1ms

WAVEFORM_HOP    = 128  # samples per waveform frame (~2.9 ms at 44100 Hz)
_WF_FFT         = 256  # FFT size for spectral analysis
_WF_CHUNK       = 2048 # frames per processing chunk (keeps memory low)


def _build_waveform(audio: np.ndarray, sr: int) -> np.ndarray:
    """
    Pre-compute 3-band spectral waveform for display.
    Returns float32 array (N, 3): [lo, mid, hi] each normalised 0-1.
    Runs in deck load thread — never blocks audio callback.
    """
    mono      = audio.mean(axis=1).astype(np.float32)
    hop       = WAVEFORM_HOP
    n_frames  = len(mono) // hop
    if n_frames < 2:
        return np.zeros((2, 3), np.float32)

    freq_res = sr / _WF_FFT
    lo_end   = max(1, int(250  / freq_res))   # 0–250 Hz   → bass / kick
    mi_end   = max(lo_end + 1, int(5000 / freq_res))  # 250–5 kHz  → mid / synth
    # high = mi_end .. Nyquist                          → air / hats

    wf = np.zeros((n_frames, 3), np.float32)

    for c0 in range(0, n_frames, _WF_CHUNK):
        c1     = min(c0 + _WF_CHUNK, n_frames)
        cn     = c1 - c0
        chunk  = mono[c0 * hop : c1 * hop].reshape(cn, hop)
        mag    = np.abs(np.fft.rfft(chunk, n=_WF_FFT, axis=1)).astype(np.float32)
        lo_e   = np.sqrt(np.mean(mag[:, 1     : lo_end + 1] ** 2, axis=1) + 1e-10)
        mi_e   = np.sqrt(np.mean(mag[:, lo_end: mi_end]     ** 2, axis=1) + 1e-10)
        hi_e   = np.sqrt(np.mean(mag[:, mi_end:]            ** 2, axis=1) + 1e-10)
        wf[c0:c1] = np.stack([lo_e, mi_e, hi_e], axis=1)

    # Normalise each band by its 99th percentile so all tracks fill the display
    for b in range(3):
        p = np.percentile(wf[:, b], 99)
        if p > 1e-6:
            wf[:, b] /= p
    return np.clip(wf, 0.0, 1.0)


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
        self._solo_stem: Optional[str] = None
        # Smooth stem fade — prevents clicks on toggle.
        # _stem_fade[s]        = current gain (0.0–1.0), updated each audio callback
        # _stem_fade_target[s] = where we're ramping to
        # Linear ramp over _STEM_FADE_SAMPLES samples (~10ms at 44100 Hz).
        self._reset_stem_state()

        self.key_shift: int = 0   # semitones ±12; applied as playback rate multiplier

        # Pre-roll crossfade state — set by _trigger_with_preroll, consumed by read()
        self._xfade_src_pos: float = 0.0   # old playback position to blend from
        self._xfade_len:     int   = 0     # crossfade duration in samples
        self._xfade_done:    int   = 0     # samples completed so far

        self.waveform: Optional[np.ndarray] = None  # (N, 3) float32 lo/mid/hi

        self.on_track_loaded:       Optional[Callable] = None
        self.on_play_state_changed: Optional[Callable] = None
        self.on_hot_cue_changed:    Optional[Callable] = None
        self.on_stems_ready:        Optional[Callable] = None

        # Anti-aliasing filter — precomputed lookup table, safe to call in callback.
        # butter() must NEVER run inside the audio callback; build all buckets at init.
        # Buckets: rate 1.05 → 2.50 in steps of 0.05 (30 entries, <10ms total at init).
        self._aa_table: dict[float, np.ndarray] = {}
        nyq = sample_rate / 2.0
        for _r100 in range(105, 255, 5):   # 1.05 … 2.50
            _r   = _r100 / 100.0
            _cut = min(1.0 / _r, 0.95)    # normalised cutoff
            self._aa_table[_r] = butter(8, _cut, btype='low', output='sos')
        self._aa_bucket: float = 0.0       # last bucket used
        self._aa_zi_L: Optional[np.ndarray] = None
        self._aa_zi_R: Optional[np.ndarray] = None

    def _get_aa_sos(self, rate: float):
        """Return (sos, zi_L, zi_R) for the nearest pre-built bucket.
        No scipy calls here — safe to invoke from the audio callback."""
        # Round to nearest 0.05 bucket, clamped to table range
        bucket = round(min(max(rate, 1.05), 2.50) / 0.05) * 0.05
        bucket = round(bucket, 2)
        sos = self._aa_table.get(bucket) or next(iter(self._aa_table.values()))
        if bucket != self._aa_bucket or self._aa_zi_L is None:
            self._aa_zi_L = np.zeros((sos.shape[0], 2), dtype=np.float64)
            self._aa_zi_R = np.zeros((sos.shape[0], 2), dtype=np.float64)
            self._aa_bucket = bucket
        return sos, self._aa_zi_L, self._aa_zi_R

    def _reset_stem_state(self):
        """Reset all stem playback state — called on init and on each new track load."""
        self.stems: Optional[dict[str, np.ndarray]] = None
        self.stems_ready        = False
        self.active_stems: set[str]        = set(STEM_NAMES)
        self.stem_volumes: dict[str, float] = {s: 1.0 for s in STEM_NAMES}
        self._stem_fade:        dict[str, float] = {s: 1.0 for s in STEM_NAMES}
        self._stem_fade_target: dict[str, float] = {s: 1.0 for s in STEM_NAMES}

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
            self.waveform     = None
            self.audio        = audio
            self.position     = 0.0
            self.playing      = False
            self.cue_point    = -1
            self.hot_cues     = [None] * NUM_HOT_CUES
            self.loop_active  = False
            self._loop_recording = False
            self.file_path    = path
            self.duration     = len(audio) / self.sample_rate
            self._reset_stem_state()
            if metadata:
                self.title  = metadata.get("title",  "")
                self.artist = metadata.get("artist", "")
                self.bpm = float(metadata.get("bpm", 0) or 0)
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
                bp_json = metadata.get("beat_positions_json", "[]") or "[]"
                try:
                    bp_sec = json.loads(bp_json)
                    self.beats = (np.array(bp_sec, dtype=np.float64) * self.sample_rate
                                  if bp_sec else None)
                except Exception:
                    self.beats = None
                ph_json = metadata.get("phrase_positions_json", "[]") or "[]"
                try:
                    ph_sec = json.loads(ph_json)
                    self.phrases = (np.array(ph_sec, dtype=np.float64) * self.sample_rate
                                    if ph_sec else None)
                except Exception:
                    self.phrases = None

                # Load auto-detected cue points (MIK-style energy markers)
                cq_json = metadata.get("cue_points_json", "[]") or "[]"
                try:
                    for cue in json.loads(cq_json):
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

        # Waveform — compute in this thread so paint never has to wait
        self.waveform = _build_waveform(audio, self.sample_rate)

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
        if not self.playing:
            # Snap to grid on play-start (within 40% window)
            snapped = self._q(self.position, "play-start")
            if snapped != int(self.position):
                self.position = float(snapped)
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
        """Snap pos to nearest beat when quantize is on and within ±40% window."""
        if self.quantize and self.bpm > 0:
            snapped = self._nearest_beat(pos)
            beat_len = self.sample_rate * 60.0 / self.bpm
            # Only snap if the transient is within the 40% window of a beat.
            # Transients in the 40–50% zone (near the middle of a beat) are
            # left in place — moving them that far would sound unnatural.
            if abs(snapped - pos) <= beat_len * _QUANTIZE_WINDOW:
                event_log.log_quantize(
                    self.deck_id, event_label,
                    pos / self.sample_rate,
                    snapped / self.sample_rate,
                    self.bpm)
                return snapped
        return int(pos)

    def _trigger_with_preroll(self, new_pos: int) -> int:
        """
        Set up a 30ms pre-roll crossfade and return the actual start position.

        The returned position is 30ms before new_pos so the listener hears the
        audio leading into the cue point.  The outgoing audio (from wherever the
        deck was playing) is simultaneously faded out over the same 30ms window,
        giving a smooth overlap instead of a hard cut.

        If an active loop-out falls inside the overlap region (> 1ms from the
        pre-roll start), it is trimmed back to leave at most 1ms of overlap —
        preventing the loop from doubling audio with the incoming cue.
        """
        preroll   = int(self.sample_rate * _PREROLL_SEC)
        trim_min  = int(self.sample_rate * _OVERLAP_TRIM_SEC)

        self._xfade_src_pos = self.position   # fade OUT from here
        self._xfade_len     = preroll
        self._xfade_done    = 0

        # Extend start backward by 30ms
        actual_start = max(0.0, float(new_pos) - preroll)

        # Trim prior loop if it would overlap the incoming audio by > 1ms
        if (self.loop_active and
                self.loop_out is not None and self.loop_in is not None and
                self.loop_out > actual_start + trim_min):
            self.loop_out = int(actual_start + trim_min)
            if self.loop_out <= self.loop_in:
                self.loop_active = False

        return int(actual_start)

    def cue_default(self, pressed: bool):
        if pressed:
            if not self.playing:
                if self.cue_point < 0:
                    self.cue_point = self._q(self.position)
                else:
                    self.position = float(self._trigger_with_preroll(self.cue_point))
                    self.playing  = True
            else:
                if self.cue_point >= 0:
                    self.position = float(self.cue_point)
                self.playing = False

    def cue_stutter_start(self):
        """rekordbox CDJ cue hold: jump to cue and play while held."""
        if self.cue_point >= 0:
            self.position = float(self._trigger_with_preroll(self.cue_point))
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
            self.position = float(self._trigger_with_preroll(self.cue_point))
        self.playing = True

    # ── Hot cues ──────────────────────────────────────────────

    def hotcue_activate(self, n: int):
        if self.hot_cues[n] is None:
            self.hot_cues[n] = self._q(self.position, f"hotcue_set_{n}")  # set quantised
            if self.on_hot_cue_changed:
                self.on_hot_cue_changed(self.deck_id, n, True)
        else:
            self.position = float(self._trigger_with_preroll(self.hot_cues[n]))
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
        # Compute snap outside the lock (numpy ops, no shared-state writes)
        samps = (int((60.0 / self.bpm) * beats * self.sample_rate)
                 if self.bpm > 0 else int(2.0 * self.sample_rate))
        snap = self._nearest_phrase(self.position, snap_beats=1.5)
        if snap is None:
            snap = self._nearest_beat(self.position)

        # Snap loop_out to stored beat grid for sample-accurate end alignment.
        # Nominal BPM arithmetic accumulates floating-point error over long loops;
        # using the stored grid gives the exact beat position at that bar boundary.
        raw_out = snap + samps
        if self.beats is not None and len(self.beats) >= 2:
            loop_out = self._nearest_beat(float(raw_out))
        else:
            loop_out = raw_out

        with self._lock:
            if self.loop_active and abs(self._current_loop_beats - beats) < 0.001:
                self.loop_active = False
            else:
                self.loop_in             = snap
                self.loop_out            = loop_out
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
        """Toggle stem with smooth 10ms fade to prevent clicks."""
        if name in self.active_stems:
            # Fade out — keep in active_stems until fully silent (handled in read)
            self._stem_fade_target[name] = 0.0
        else:
            # Add immediately and fade in from silence
            self.active_stems.add(name)
            self._stem_fade[name] = 0.0
            self._stem_fade_target[name] = 1.0

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

            # Anti-aliasing: when playing back faster than 1× the signal content
            # is compressed toward Nyquist. Apply a LP filter at Nyquist/rate
            # before Hermite interpolation so aliased images stay below −80 dBFS.
            if rate > 1.05:
                sos, zi_L, zi_R = self._get_aa_sos(rate)
                L_in = audio[:, 0].astype(np.float64)
                R_in = audio[:, 1].astype(np.float64)
                L_aa, self._aa_zi_L = sosfilt(sos, L_in, zi=zi_L)
                R_aa, self._aa_zi_R = sosfilt(sos, R_in, zi=zi_R)
                audio = np.stack([L_aa.astype(np.float32),
                                  R_aa.astype(np.float32)], axis=1)

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

            # Stems: mix with per-stem smooth gain ramp to eliminate toggle clicks.
            # Each stem fades in/out over _STEM_FADE_SAMPLES (~10ms) when toggled.
            _STEM_FADE_SAMPLES = max(1, int(self.sample_rate * 0.010))

            if self.stems_ready and self.stems:
                volumes = dict(self.stem_volumes)           # GIL-safe snapshot
                # Include all stems that are active OR still fading out
                candidates = set(self.active_stems) | {
                    s for s, g in self._stem_fade.items() if g > 0.001}
                out = np.zeros((frames, 2), dtype=np.float32)
                for s in candidates:
                    if s not in self.stems or not isinstance(self.stems[s], np.ndarray):
                        continue
                    vol      = volumes.get(s, 1.0)
                    g_start  = float(self._stem_fade.get(s, 1.0))
                    g_target = float(self._stem_fade_target.get(s, 1.0))

                    if g_start == g_target:
                        gain_curve = np.full(frames, g_start * vol, dtype=np.float32)
                    else:
                        # Equal-power ramp: sqrt gives perceptually smooth fade,
                        # avoiding the audible "step" that a linear ramp produces
                        # on transient-dense material like drums.
                        delta      = g_target - g_start
                        step       = delta / _STEM_FADE_SAMPLES
                        ramp_lin   = g_start + np.arange(frames, dtype=np.float64) * step
                        ramp_lin   = np.clip(ramp_lin,
                                             min(g_start, g_target),
                                             max(g_start, g_target))
                        ramp       = np.sqrt(np.maximum(ramp_lin, 0.0))
                        gain_curve = (ramp * vol).astype(np.float32)
                        # Advance fade state
                        new_g = g_start + step * frames
                        new_g = float(np.clip(new_g,
                                              min(g_start, g_target),
                                              max(g_start, g_target)))
                        self._stem_fade[s] = new_g
                        # Fully faded out — remove from active_stems cleanly
                        if g_target == 0.0 and new_g <= 0.001:
                            self._stem_fade[s] = 0.0
                            self.active_stems.discard(s)

                    out += _hermite(self.stems[s]) * gain_curve[:, np.newaxis]

                out = soft_limit(out)
            else:
                out = _hermite(audio)

            out[~valid] = 0.0

            # Pre-roll crossfade — blend outgoing audio into the new cue start.
            # Runs for _PREROLL_SEC (30ms) after any cue/hotcue/loop trigger.
            # Uses _hermite on the full audio for the fade-out side so that the
            # blend works regardless of whether stems are active on the new side.
            if self._xfade_done < self._xfade_len and self._xfade_len > 0:
                n_blend = min(frames, self._xfade_len - self._xfade_done)
                t0 = self._xfade_done / self._xfade_len
                t1 = (self._xfade_done + n_blend) / self._xfade_len
                # Equal-power (constant-power) crossfade: sin/cos curves preserve
                # total loudness at the midpoint where linear fades dip ~3 dB.
                t_lin    = np.linspace(t0, t1, n_blend, dtype=np.float32)
                fade_in  = np.sin(t_lin * (np.pi / 2)).astype(np.float32)
                fade_out = np.cos(t_lin * (np.pi / 2)).astype(np.float32)

                src = self._xfade_src_pos + np.arange(n_blend, dtype=np.float64) * rate
                src = np.clip(src, 0.0, total - 2.001)
                sq1 = src.astype(np.int64)
                sq0 = np.maximum(sq1 - 1, 0)
                sq2 = np.minimum(sq1 + 1, total - 1)
                sq3 = np.minimum(sq1 + 2, total - 1)
                st  = (src - sq1).astype(np.float32)
                st2 = st * st; st3 = st2 * st
                old_ch = np.empty((n_blend, 2), dtype=np.float32)
                for ch in range(2):
                    y0 = audio[sq0, ch]; y1 = audio[sq1, ch]
                    y2 = audio[sq2, ch]; y3 = audio[sq3, ch]
                    old_ch[:, ch] = 0.5 * (
                        (-y0 + 3*y1 - 3*y2 + y3) * st3 +
                        (2*y0 - 5*y1 + 4*y2 - y3) * st2 +
                        (-y0 + y2) * st + 2*y1)

                out[:n_blend] = (out[:n_blend]    * fade_in[:, np.newaxis] +
                                 old_ch[:n_blend] * fade_out[:, np.newaxis])

                self._xfade_src_pos += float(n_blend) * rate
                self._xfade_done    += n_blend

            # Crossfade the loop restart boundary to eliminate clicks.
            # Blend the first _LOOP_XFADE samples after a wrap with the
            # pre-wrap signal read from loop_out so the discontinuity is
            # smoothed over ~1.5 ms (64 samples @44100).
            if loop_wrap_mask is not None and self.loop_out is not None:
                xf = _LOOP_XFADE
                first_wrap = int(np.argmax(loop_wrap_mask))
                n_blend = min(xf, frames - first_wrap, frames)
                if n_blend > 0:
                    t_lin    = np.linspace(0.0, 1.0, n_blend, dtype=np.float32)
                    fade_in  = np.sin(t_lin * (np.pi / 2)).astype(np.float32)
                    fade_out = np.cos(t_lin * (np.pi / 2)).astype(np.float32)
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
