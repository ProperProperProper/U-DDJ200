"""
AudioEngine — Mixxx-exact sync engine.

Sync algorithm mirrors Mixxx's SyncControl / EngineSync exactly:

  Every audio callback (≈ 43 Hz at 44100 / 1024):
    1. nominal   = master_bpm / follower_file_bpm
    2. bd_error  = master_beat_distance − follower_beat_distance
                   (wrapped to −0.5 … +0.5)
    3. adj       = clamp(bd_error × kSyncAdjustmentMultiplier,
                         ±kSyncAdjustmentCap)
    4. rate      = nominal × (1 + adj)

  Mixxx source constants (synccontrol.cpp):
    kSyncAdjustmentMultiplier = 0.5
    kSyncAdjustmentCap        = 0.02   (2 %)

  beat_distance() uses the stored beat-array (Mixxx: Beats::getBeatDistance).
  Falls back to first_beat + BPM arithmetic when no array is loaded yet.

  On play-start or explicit SYNC press:
    - Snap follower position by bd_error × beat_len (Mixxx: requestSync)
    - This aligns the nearest beat, not necessarily bar-1.
    - Bar-1 alignment is done separately by snap_sync().
"""
from __future__ import annotations

import sounddevice as sd
import numpy as np
from .deck import Deck
from .mixer import Mixer
from . import stems as stems_mod
from . import event_log

NUM_DECKS   = 2
BUFFER_SIZE = 1024

# ── Sync constants ────────────────────────────────────────────────────────────
# Strategy: hard rate lock + continuous position P-controller.
#   - follower.rate = exact nominal every callback  →  zero tempo wobble
#   - one-shot bar-snap on play-start or significant master rate change
#   - continuous smooth nudge: up to _PHASE_CORRECTION per callback toward zero error
#     This corrects imperfect snaps and BPM analysis drift silently (no rate wobble).
_PHASE_GAIN    = 0.5    # P-gain: apply 50 % of error each callback
_PHASE_CAP     = 0.05   # max correction per callback in beats (≈ 23 ms at 130 BPM)
_PHASE_DEAD    = 0.001  # dead-zone: ignore error < 0.1 % of a beat (float noise)
_SNAP_RATE_THR = 0.003  # re-snap when master rate changes > 0.3 %


def _detect_native_sr() -> int:
    try:
        dev = sd.query_devices(sd.default.device[1], "output")
        return int(dev["default_samplerate"])
    except Exception:
        return 44100


SAMPLE_RATE = _detect_native_sr()


# ── Mixxx: Beats::getBeatDistance ─────────────────────────────────────────────

def beat_distance(position: float,
                  beats: "np.ndarray | None",
                  first_beat: float = 0.0,
                  beat_len: float   = 0.0) -> float:
    """
    Fractional position within the current beat (0.0 … 1.0).

    Direct translation of Mixxx's TrackBeats::getBeatDistance():
      lower_bound search → interpolate between surrounding beats.

    Falls back to first_beat + BPM arithmetic when no grid is loaded.
    """
    if beats is not None and len(beats) >= 2:
        idx = int(np.searchsorted(beats, position, side="right")) - 1
        idx = max(0, min(len(beats) - 2, idx))
        lo  = beats[idx]
        hi  = beats[idx + 1]
        span = hi - lo
        if span > 0:
            return float(np.clip((position - lo) / span, 0.0, 1.0))
    if beat_len > 0:
        return ((position - first_beat) % beat_len) / beat_len
    return 0.0


def bar_distance(position: float,
                 beats: "np.ndarray | None",
                 first_beat: float = 0.0,
                 beat_len: float   = 0.0) -> float:
    """Bar phase 0..1 (4 beats).  Beat-index % 4 gives bar position."""
    if beats is not None and len(beats) >= 2:
        idx         = max(0, int(np.searchsorted(beats, position, side="right")) - 1)
        idx         = min(idx, len(beats) - 2)
        beat_in_bar = idx % 4
        # Pass fallback params so degenerate spans don't silently return 0
        bd          = beat_distance(position, beats, first_beat, beat_len)
        return (beat_in_bar + bd) / 4.0
    if beat_len > 0:
        bar_len = beat_len * 4.0
        return ((position - first_beat) % bar_len) / bar_len
    return 0.0


def nearest_bar_start(position: float,
                      beats: "np.ndarray | None",
                      first_beat: float = 0.0,
                      beat_len: float   = 0.0) -> float:
    """Sample position of the nearest beat-1 (bar boundary)."""
    if beats is not None and len(beats) >= 4:
        idx    = max(0, min(len(beats) - 1,
                            int(np.searchsorted(beats, position, side="right")) - 1))
        lo_idx = (idx // 4) * 4
        hi_idx = lo_idx + 4
        lo     = beats[lo_idx]
        hi     = (beats[hi_idx] if hi_idx < len(beats)
                  else beats[-1] + (beats[-1] - beats[max(0, len(beats) - 2)]))
        return lo if abs(position - lo) <= abs(position - hi) else hi
    if beat_len > 0:
        bar_len = beat_len * 4.0
        rel     = position - first_beat
        return first_beat + round(rel / bar_len) * bar_len
    return position


def _bar_len_from_beats(beats: np.ndarray, bar1_i: int,
                         beat_len_fallback: float) -> float:
    """
    Return the stored bar length (4-beat span) starting at bar1_i.
    Extrapolates from the previous bar when bar1_i is the last bar in the array.
    """
    n = len(beats)
    end_i = bar1_i + 4
    if end_i < n:
        span = float(beats[end_i] - beats[bar1_i])
        if span > 0:
            return span
    # Near end of track: extrapolate from the preceding bar span
    prev_i = bar1_i - 4
    if prev_i >= 0:
        span = float(beats[bar1_i] - beats[prev_i])
        if span > 0:
            return span
    return beat_len_fallback * 4.0


def _snap_to_grid(master: "Deck", follower: "Deck", sr: int) -> float:
    """
    Snap follower so its bar-1 aligns with master's bar-1.

    Algorithm:
      1. Find master's nearest bar-1 (from stored beats or arithmetic fallback).
      2. Compute m_offset = master.position - master_bar1  (clamped ≥ 0).
      3. Find follower's NEAREST bar-1 using nearest_bar_start() — not just the
         current bar; follower may be closer to the next bar boundary.
      4. Scale offset: scaled = m_offset × (f_bar_len / m_bar_len).
      5. snapped = follower_bar1 + scaled_offset.

    Same-track special case: exact position copy → zero double-beats.
    """
    # ── same track: exact position copy ───────────────────────────
    # Guard: both paths must be non-empty to avoid matching two unloaded decks.
    mfp = master.file_path
    ffp = getattr(follower, 'file_path', '')
    if mfp and ffp and mfp == ffp:
        return float(master.position)

    # ── master: find nearest bar-1 and intra-bar offset ───────────
    beat_len_m = sr * 60.0 / max(master.bpm * master.rate, 1.0)

    if master.beats is not None and len(master.beats) >= 4:
        m_bar1_pos = nearest_bar_start(master.position, master.beats,
                                        master.first_beat, beat_len_m)
        # Resolve m_bar1_pos to a beat index so we can measure bar length
        raw_i    = int(np.searchsorted(master.beats, m_bar1_pos, side="left"))
        m_bar1_i = max(0, min(raw_i, len(master.beats) - 1))
        # Ensure it is truly a bar-boundary index (multiple of 4)
        m_bar1_i = (m_bar1_i // 4) * 4
        m_bar1   = float(master.beats[m_bar1_i])
        m_bar_len = _bar_len_from_beats(master.beats, m_bar1_i, beat_len_m)
    else:
        m_bar1    = nearest_bar_start(master.position, None,
                                       master.first_beat, beat_len_m)
        m_bar_len = beat_len_m * 4.0

    # Clamp: master.position must be ≥ bar-1 (can be negative before first beat)
    m_offset = max(0.0, min(float(master.position - m_bar1), m_bar_len))

    # ── follower: find NEAREST bar-1 (not necessarily current bar) ─
    beat_len_f = sr * 60.0 / max(follower.bpm, 1.0)

    if follower.beats is not None and len(follower.beats) >= 4:
        f_bar1_pos = nearest_bar_start(follower.position, follower.beats,
                                        follower.first_beat, beat_len_f)
        raw_i    = int(np.searchsorted(follower.beats, f_bar1_pos, side="left"))
        f_bar1_i = max(0, min(raw_i, len(follower.beats) - 1))
        f_bar1_i = (f_bar1_i // 4) * 4
        f_bar1   = float(follower.beats[f_bar1_i])
        f_bar_len = _bar_len_from_beats(follower.beats, f_bar1_i, beat_len_f)
    else:
        f_bar1    = nearest_bar_start(follower.position, None,
                                       follower.first_beat, beat_len_f)
        f_bar_len = beat_len_f * 4.0

    # Scale master's intra-bar offset to follower's bar length
    scaled_offset = m_offset * (f_bar_len / m_bar_len) if m_bar_len > 0 else 0.0

    snapped = f_bar1 + scaled_offset
    print(f"[Snap] m_bar1={m_bar1/sr:.3f}s m_offset={m_offset/sr:.3f}s "
          f"f_bar1={f_bar1/sr:.3f}s → snapped={snapped/sr:.3f}s "
          f"(m_bar={m_bar_len/sr:.3f}s f_bar={f_bar_len/sr:.3f}s)")
    return snapped


# ── AudioEngine ───────────────────────────────────────────────────────────────

class AudioEngine:

    def __init__(self):
        self.sample_rate   = SAMPLE_RATE
        self.buffer_size   = BUFFER_SIZE
        self.decks         = [Deck(SAMPLE_RATE, i) for i in range(NUM_DECKS)]
        self.mixer         = Mixer(NUM_DECKS, SAMPLE_RATE)
        self._stream: sd.OutputStream | None = None
        self._running      = False
        self.vu_peak       = [0.0] * NUM_DECKS
        self.master_peak   = [0.0, 0.0]

        self.auto_sync         = False
        self._was_playing      = [False] * NUM_DECKS
        self._last_master_rate = [None, None]   # per-follower: detect master rate jump
        self._sync_log_tick    = 0               # throttle sync logging to ~1 Hz

    # ── stream ────────────────────────────────────────────────

    def start(self, device=None):
        self._stream = sd.OutputStream(
            device     = device,
            samplerate = SAMPLE_RATE,
            blocksize  = BUFFER_SIZE,
            channels   = 2,
            dtype      = "float32",
            callback   = self._callback,
            latency    = "low",
        )
        self._stream.start()
        self._running = True
        print(f"[Engine] started  sr={SAMPLE_RATE}  buf={BUFFER_SIZE}")

    def stop(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self._running = False

    # ── Mixxx-exact sync ──────────────────────────────────────

    def _find_master(self) -> "Deck | None":
        # Explicit sync_master wins; otherwise first playing deck with BPM
        for d in self.decks:
            if d.sync_master and d.playing and d.bpm > 0 and d.audio is not None:
                return d
        if self.auto_sync:
            for d in self.decks:
                if d.playing and d.bpm > 0 and d.audio is not None:
                    return d
        return None

    def _apply_sync(self):
        """
        Hard rate lock — no continuous PLL, zero tempo wobble.

        follower.rate = exact nominal every callback.
        One-shot position snap on: play-start, master rate jump,
        or phase drift > _RESYNC_BEATS (silent correction, no audible wobble).
        """
        master = self._find_master()
        if master is None:
            return

        sr             = master.sample_rate
        master_eff_bpm = master.bpm * master.rate
        if master_eff_bpm <= 0:
            return

        master_beat = sr * 60.0 / master_eff_bpm
        master_bar  = master_beat * 4.0
        m_bd        = beat_distance(master.position, master.beats,
                                    master.first_beat, master_beat)
        m_bar_phase = bar_distance(master.position, master.beats,
                                   master.first_beat, master_bar)

        for f_idx, follower in enumerate(self.decks):
            if follower is master:
                self._was_playing[f_idx]      = follower.playing
                self._last_master_rate[f_idx] = None
                continue

            if not self.auto_sync:
                if not follower.sync_follower or follower.sync_master:
                    self._was_playing[f_idx] = follower.playing
                    continue

            if follower.bpm <= 0 or follower.audio is None:
                self._was_playing[f_idx] = follower.playing
                continue

            just_started = follower.playing and not self._was_playing[f_idx]
            self._was_playing[f_idx] = follower.playing

            if not follower.playing:
                continue

            # ALWAYS set exact nominal — this is the hard lock
            nominal = master_eff_bpm / follower.bpm
            follower.rate = max(0.5, min(2.0, nominal))

            # Detect master rate change
            prev = self._last_master_rate[f_idx]
            rate_jumped = (prev is None or abs(master.rate - prev) > _SNAP_RATE_THR)
            self._last_master_rate[f_idx] = master.rate

            if just_started or rate_jumped:
                # One-shot bar-snap: align beat-1 to master's bar phase
                self._do_snap(master, follower, master_bar, m_bar_phase)
                continue

            # Continuous phase P-controller — position nudge, rate untouched.
            # Fixes imperfect snaps and slow BPM-analysis drift without wobble.
            # At 43 Hz with cap=0.01 beats, max correction = 0.43 beats/s.
            # A 20 ms snap error at 130 BPM (≈ 0.043 beats) corrects in ~100 ms.
            f_bd  = beat_distance(follower.position, follower.beats,
                                  follower.first_beat, master_beat)
            error = m_bd - f_bd
            if error >  0.5: error -= 1.0
            if error < -0.5: error += 1.0

            # Log sync state ~1 Hz (every ~43 callbacks)
            self._sync_log_tick += 1
            if self._sync_log_tick >= 43:
                self._sync_log_tick = 0
                m_idx = self.decks.index(master)
                event_log.log_sync(
                    m_idx, f_idx,
                    master.bpm, follower.bpm,
                    master.rate, follower.rate,
                    float(error), float(nominal))

            # Skip while scratching — user has deliberate position control
            if not follower._scratch_active and abs(error) > _PHASE_DEAD:
                correction = float(np.clip(error * _PHASE_GAIN,
                                           -_PHASE_CAP, _PHASE_CAP))
                audio_len  = len(follower.audio)
                follower.position = max(0.0, min(float(audio_len - 2),
                                                 follower.position + correction * master_beat))

    @staticmethod
    def _do_snap(master: Deck, follower: Deck,
                 master_bar: float, master_bar_phase: float):
        """
        Align follower to master's current bar phase using stored beat grids.

        Uses grid-interpolated positions rather than arithmetic beat_len so that
        the same track on both decks lands at the exact same sample.
        """
        audio_len = len(follower.audio) if follower.audio is not None else 0
        if audio_len < 2:
            return
        sr = master.sample_rate

        snapped = _snap_to_grid(master, follower, sr)

        follower.position = max(0.0, min(float(audio_len - 2), snapped))
        f_idx = getattr(follower, 'deck_id', -1)
        beat_len = sr * 60.0 / max(follower.bpm, 1)
        f_bar1   = nearest_bar_start(follower.position, follower.beats,
                                     follower.first_beat, beat_len)
        event_log.log_snap(
            "play_start", f_idx,
            master.bpm * master.rate, follower.bpm, follower.rate,
            master_bar_phase, f_bar1 / sr, snapped / sr)

    # ── Explicit SYNC button / bar-level snap ─────────────────

    def snap_sync(self, follower_idx: int):
        """
        Called on hardware SYNC button press.

        Does a bar-level snap (beat-1 → beat-1) which is more musically
        accurate than the beat-level snap _apply_sync does on play-start.
        The PLL then maintains alignment.
        """
        follower = self.decks[follower_idx]
        master   = None
        for d in self.decks:
            if d is follower:
                continue
            if d.sync_master and d.bpm > 0 and d.audio is not None:
                master = d; break
        if master is None:
            for d in self.decks:
                if d is follower:
                    continue
                if d.playing and d.bpm > 0 and d.audio is not None:
                    master = d; break
        if master is None or follower.bpm <= 0 or follower.audio is None:
            return

        sr         = master.sample_rate
        target_bpm = master.bpm * master.rate

        # Tempo match
        follower.rate = max(0.5, min(2.0, target_bpm / follower.bpm))

        # Grid-interpolated bar-snap — uses stored beat positions, not arithmetic.
        # For the same track on both decks this gives sample-exact alignment.
        if master.playing:
            snapped   = _snap_to_grid(master, follower, sr)
            audio_len = len(follower.audio)
            follower.position = max(0.0, min(float(audio_len - 2), snapped))
            f_beat_len = sr * 60.0 / max(follower.bpm, 1)
            f_bar1     = nearest_bar_start(follower.position, follower.beats,
                                           follower.first_beat, f_beat_len)
            m_bar      = bar_distance(master.position, master.beats,
                                      master.first_beat, f_beat_len * 4)
            event_log.log_snap(
                "sync_button", follower_idx,
                target_bpm, follower.bpm, follower.rate,
                m_bar, f_bar1 / sr, snapped / sr)

        # Only clear _was_playing when deck is NOT playing (prepares for upcoming
        # PLAY press to fire just_started → snap again at final position).
        # When deck IS already playing, leaving _was_playing=True prevents a
        # spurious double-snap on the very next audio callback.
        if not follower.playing:
            self._was_playing[follower_idx] = False
        print(f"[Engine] snap_sync deck {follower_idx}: "
              f"bpm={target_bpm:.2f}  rate={follower.rate:.4f}")

    # ── Audio callback ────────────────────────────────────────

    def _callback(self, outdata: np.ndarray, frames: int, time, status):
        try:
            self._apply_sync()
            # Feed current BPM to beat-aware effects (Echo, Roll)
            for i, deck in enumerate(self.decks):
                if deck.bpm > 0:
                    fx = self.mixer.fx[i]
                    if hasattr(fx, 'set_bpm'):
                        fx.set_bpm(deck.bpm * deck.rate)
            buffers = [d.read(frames) for d in self.decks]
            for i, b in enumerate(buffers):
                self.vu_peak[i] = float(np.max(np.abs(b)))
            mixed = self.mixer.process(buffers)
            self.master_peak[0] = float(np.max(np.abs(mixed[:, 0])))
            self.master_peak[1] = float(np.max(np.abs(mixed[:, 1])))
            outdata[:] = mixed
        except Exception as e:
            print(f"[Engine] callback error: {e}")
            outdata[:] = 0.0

    # ── Track loading ─────────────────────────────────────────

    def load_track(self, deck_idx: int, path: str, metadata: dict | None = None):
        deck = self.decks[deck_idx]
        deck.load(path, metadata)
        self._was_playing[deck_idx]      = False
        self._last_master_rate[deck_idx] = None   # trigger snap on next play

        def _on_stems_done(stem_dict):
            deck.set_stems(stem_dict)
            print(f"[Engine] deck {deck_idx} stems ready")

        stems_mod.separate_async(path, self.sample_rate, _on_stems_done,
                                 track_meta=metadata)

    def deck(self, n: int) -> Deck:
        return self.decks[n]

    @staticmethod
    def list_devices():
        return sd.query_devices()
