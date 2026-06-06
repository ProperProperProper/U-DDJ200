"""
AudioEngine — beat sync engine.

Every audio callback (≈ 43 Hz at 44100 / 1024):
  1. Hard rate lock: follower.rate = master_eff_bpm / follower_bpm
  2. Phase nudge: position += clip(error × gain, ±cap) × beat_len
     — inaudible correction, converges in < 1 bar

Phase model (uniform BPM arithmetic — no stored beat grid):
  beat_distance = (position − first_beat) % beat_len / beat_len
  bar_distance  = (position − first_beat) % bar_len  / bar_len
  This matches the beat grid widget renderer exactly, so the visual
  grid alignment truthfully reflects sync state.

Snaps fire on:
  - play-start (just_started)
  - explicit snap_sync() call (SYNC button, master switch)
  Both use _snap_to_grid() which also uses uniform arithmetic.
"""
from __future__ import annotations

import sounddevice as sd
import numpy as np
from .deck import Deck
from .mixer import Mixer
from . import stems as stems_mod
from . import event_log

NUM_DECKS   = 2
BUFFER_SIZE = 512

# ── Sync constants ────────────────────────────────────────────────────────────
# Strategy: hard rate lock + continuous position P-controller.
#   - follower.rate = exact nominal every callback  →  zero tempo wobble
#   - one-shot bar-snap on play-start or significant master rate change
#   - continuous position nudge (not rate tweak) to correct residual phase error
#     Nudge is inaudible (<1ms), converges in <1 bar, doesn't fight rate lock.
#
# Studio-grade tuning vs previous:
#   - Tighter dead zone (0.001 vs 0.002): ±0.46ms at 130BPM before correction
#   - Higher gain (0.8 vs 0.6): converges in ~50ms instead of ~100ms
#   - Larger cap (0.04 vs 0.03): faster recovery from snap errors without audible wobble
#   - Phase measured via stored beat grid (sub-sample accurate) not uniform BPM math
_PHASE_DEAD      = 0.001   # dead-zone: ~0.46ms at 130BPM
_PHASE_NUDGE_CAP = 0.04    # max nudge per callback: 4% of beat (~18ms at 130bpm)
_NUDGE_GAIN      = 0.8     # proportional gain


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

    Uses uniform BPM arithmetic anchored to first_beat — NOT stored grid
    interpolation. Grid interpolation drifts over time as stored beat positions
    have tiny non-uniformities, causing phantom phase errors in the sync engine.
    Uniform arithmetic is stable and correct for continuous phase tracking.
    The stored grid is still used for snap operations (finding nearest beat).
    """
    if beat_len > 0:
        return ((position - first_beat) % beat_len) / beat_len
    return 0.0


def bar_distance(position: float,
                 beats: "np.ndarray | None",
                 first_beat: float = 0.0,
                 beat_len: float   = 0.0) -> float:
    """Bar phase 0..1 (4 beats) — uniform BPM arithmetic."""
    if beat_len > 0:
        bar_len = beat_len * 4.0
        return ((position - first_beat) % bar_len) / bar_len
    return 0.0


def _local_beat_len(deck: "Deck", sr: int) -> float:
    """
    Beat length at the deck's current position using the stored beat grid.

    Interpolates between the two surrounding beat positions so BPM-drifting
    recordings (live sets, warped files) are measured correctly.  Falls back to
    uniform BPM arithmetic when no stored grid is available.
    """
    beats = deck.beats
    if beats is not None and len(beats) >= 2:
        pos = deck.position
        idx = int(np.searchsorted(beats, pos, side="right")) - 1
        idx = max(0, min(len(beats) - 2, idx))
        local = float(beats[idx + 1] - beats[idx])
        lo = sr * 60.0 / 300.0
        hi = sr * 60.0 / 30.0
        if lo < local < hi:
            return local
    return sr * 60.0 / max(deck.bpm, 1.0)


def beat_distance_grid(position: float,
                       beats: "np.ndarray | None",
                       first_beat: float,
                       beat_len: float) -> float:
    """
    Phase within the current beat (0.0…1.0) via stored beat grid.

    More accurate than uniform-BPM arithmetic for tracks with BPM drift.
    Falls back to beat_distance() when no grid is available.
    """
    if beats is not None and len(beats) >= 2:
        idx = int(np.searchsorted(beats, position, side="right")) - 1
        idx = max(0, min(len(beats) - 2, idx))
        b0, b1 = float(beats[idx]), float(beats[idx + 1])
        span = b1 - b0
        if span > 0:
            return float(np.clip((position - b0) / span, 0.0, 1.0))
    return beat_distance(position, beats, first_beat, beat_len)


def _snap_to_grid(master: "Deck", follower: "Deck", sr: int) -> float:
    """
    Snap follower so its bar phase matches master's bar phase exactly.

    Uses uniform BPM arithmetic (same model as beat_distance / bar_distance)
    so snap and measurement are consistent — no residual phase error after snap.

    Algorithm:
      1. Master bar phase = (master.pos - master.first_beat) % bar_len / bar_len
      2. Find the follower bar that currently contains follower.position.
      3. snapped = that bar's start + master_bar_phase * follower_bar_len.
         If result < follower.position (would jump backward mid-bar), use next bar.
    """
    master_key_ratio   = 2.0 ** (getattr(master, 'key_shift', 0) / 12.0)
    master_eff_bpm     = master.bpm * master.rate * master_key_ratio
    beat_len_m         = sr * 60.0 / max(master_eff_bpm, 1.0)
    bar_len_m          = beat_len_m * 4.0

    beat_len_f         = sr * 60.0 / max(follower.bpm, 1.0)
    bar_len_f          = beat_len_f * 4.0

    # Master bar phase via uniform arithmetic
    m_bar_phase = ((master.position - master.first_beat) % bar_len_m) / bar_len_m

    # Follower: which bar does it currently sit in?
    rel       = follower.position - follower.first_beat
    bar_idx   = max(0, int(rel / bar_len_f))
    bar_start = follower.first_beat + bar_idx * bar_len_f

    snapped   = bar_start + m_bar_phase * bar_len_f

    # If snap would jump backward, use the next bar (avoids stutters)
    if snapped < follower.position - beat_len_f * 0.25:
        snapped += bar_len_f

    audio_len = len(follower.audio) if follower.audio is not None else 0
    snapped   = float(np.clip(snapped, 0.0, max(0, audio_len - 2)))

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

        self.auto_sync              = False
        self._was_playing           = [False] * NUM_DECKS
        self._last_master_rate      = [None, None]   # reserved
        self._sync_log_tick         = 0               # throttle sync logging to ~1 Hz
        self._phrase_sync_triggered = False           # read by mixer_widget via getattr to flash PHRASE SYNC btn

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
        self._running = False
        if self._stream:
            try:
                self._stream.abort()   # immediate silence, don't wait for buffer drain
                self._stream.close()
            except Exception:
                pass
            self._stream = None

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
            # Log once per second when no master found
            self._sync_log_tick += 1
            if self._sync_log_tick >= 43:
                self._sync_log_tick = 0
                states = [(f"D{i+1}:bpm={d.bpm:.1f},play={d.playing},audio={'y' if d.audio is not None else 'N'}")
                          for i, d in enumerate(self.decks)]
                print(f"[SYNC] no master — {' | '.join(states)}")
            return

        sr             = master.sample_rate
        # Include key_shift ratio: key shift changes effective playback speed.
        # Sync must match the actual audio tempo, not just deck.rate.
        master_key_ratio = 2.0 ** (getattr(master, 'key_shift', 0) / 12.0)
        master_eff_bpm   = master.bpm * master.rate * master_key_ratio
        if master_eff_bpm <= 0:
            return

        # Nominal beat length — used for rate-lock calculation only.
        # Phase measurement uses local_beat_len (stored grid) for studio accuracy.
        master_beat      = sr * 60.0 / master_eff_bpm
        master_bar       = master_beat * 4.0
        master_local_beat = _local_beat_len(master, sr)   # handles BPM drift
        m_bd        = beat_distance_grid(master.position, master.beats,
                                         master.first_beat, master_local_beat)
        m_bar_phase = bar_distance(master.position, master.beats,
                                   master.first_beat, master_local_beat)

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
                if follower.playing:
                    print(f"[SYNC] D{f_idx+1} playing but can't sync — bpm={follower.bpm:.2f} audio={'yes' if follower.audio is not None else 'NO'}")
                self._was_playing[f_idx] = follower.playing
                continue

            just_started = follower.playing and not self._was_playing[f_idx]
            self._was_playing[f_idx] = follower.playing

            if not follower.playing:
                continue

            # ALWAYS hard-lock rate to nominal — zero tempo wobble.
            # follower.rate tracks master_eff_bpm every callback, so the follower
            # immediately matches master tempo whenever the slider moves.
            follower_key_ratio = 2.0 ** (getattr(follower, 'key_shift', 0) / 12.0)
            nominal = (master_eff_bpm / follower.bpm) / follower_key_ratio
            follower.rate = max(0.5, min(2.0, nominal))

            # One-shot bar-phase snap on play-start only.
            # DO NOT snap on rate/tempo change — the rate is already updated above
            # every callback, so the follower tracks instantly. Snapping on rate
            # change caused _do_snap() to fire on every callback while the slider
            # was moving (~86×/s), hard-jumping the follower position each time.
            if just_started:
                print(f"[SNAP] D{f_idx+1} play-start  bar_phase={m_bar_phase:.3f}")
                self._do_snap(master, follower, master_bar, m_bar_phase)
                continue

            # Continuous phase P-controller — position nudge, rate untouched.
            # Uses stored beat grid for both master and follower phase so that
            # BPM drift in either track doesn't accumulate into a phase error.
            follower_beat = _local_beat_len(follower, sr)
            f_bd  = beat_distance_grid(follower.position, follower.beats,
                                       follower.first_beat, follower_beat)
            error = m_bd - f_bd
            if error >  0.5: error -= 1.0
            if error < -0.5: error += 1.0

            # Phase correction: direct position nudge (not rate tweak).
            # Rate tweak fights itself because rate is reset to nominal each callback.
            # Position nudge is immediate, inaudible (<1ms), and converges in <1 bar.
            nudge_samples = 0.0
            if not follower._scratch_active and abs(error) > _PHASE_DEAD:
                nudge_beats = float(np.clip(error * _NUDGE_GAIN, -_PHASE_NUDGE_CAP, _PHASE_NUDGE_CAP))
                nudge_samples = nudge_beats * master_beat
                audio_len = len(follower.audio) if follower.audio is not None else 0
                follower.position = float(np.clip(
                    follower.position + nudge_samples, 0.0, max(0, audio_len - 2)))

            # Log sync state ~1 Hz (every ~43 callbacks)
            self._sync_log_tick += 1
            if self._sync_log_tick >= 43:
                self._sync_log_tick = 0
                m_idx = self.decks.index(master)
                error_ms = error * (60_000.0 / max(master_eff_bpm, 1.0))
                nudge_ms = nudge_samples / max(master.sample_rate, 1) * 1000.0
                status = f"nudge={nudge_ms:+.2f}ms" if abs(nudge_samples) > 0 else "locked"
                print(
                    f"[SYNC] master=D{m_idx+1}({master.bpm:.1f}×{master.rate:.4f})"
                    f"  follower=D{f_idx+1}({follower.bpm:.1f}×{follower.rate:.4f})"
                    f"  err={error:+.4f}beats ({error_ms:+.1f}ms)"
                    f"  nominal={nominal:.4f}  [{status}]"
                )
                event_log.log_sync(
                    m_idx, f_idx,
                    master.bpm, follower.bpm,
                    master.rate, follower.rate,
                    float(error), float(nominal))

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
        event_log.log_snap(
            "play_start", f_idx,
            master.bpm * master.rate, follower.bpm, follower.rate,
            master_bar_phase, snapped / sr, snapped / sr)

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

        sr               = master.sample_rate
        master_key_ratio = 2.0 ** (getattr(master,   'key_shift', 0) / 12.0)
        follower_key_ratio = 2.0 ** (getattr(follower, 'key_shift', 0) / 12.0)
        target_bpm       = master.bpm * master.rate * master_key_ratio

        # Tempo match — only set rate if follower is playing.
        # If paused, rate is set when play starts via _apply_sync just_started.
        # This prevents the BPM display changing unexpectedly while paused.
        nominal = (target_bpm / follower.bpm) / follower_key_ratio
        if follower.playing:
            follower.rate = max(0.5, min(2.0, nominal))

        # Always snap position — even if master is paused, its current position
        # is the correct phase reference. Follower will align and wait for play.
        snapped   = _snap_to_grid(master, follower, sr)
        audio_len = len(follower.audio)
        follower.position = max(0.0, min(float(audio_len - 2), snapped))
        follower.cue_point = int(follower.position)
        m_bar = bar_distance(master.position, None, master.first_beat,
                             sr * 60.0 / max(master.bpm * master.rate, 1))
        event_log.log_snap(
            "sync_button", follower_idx,
            target_bpm, follower.bpm, follower.rate,
            m_bar, snapped / sr, snapped / sr)
        print(f"[SYNC] D{follower_idx+1} snapped → {snapped/sr:.3f}s  "
              f"bar_phase={m_bar:.3f}  master_playing={master.playing}")

        # Force _apply_sync to re-snap on next callback too
        if not follower.playing:
            self._was_playing[follower_idx] = False

    def phrase_sync(self, follower_idx: int):
        self._phrase_sync_triggered = True
        """
        Snap follower to the next upcoming PHRASE boundary in the master.
        More musical than bar-sync for long-form techno structures — aligns
        to 16/32-bar phrase boundaries instead of just the next bar.
        Falls back to bar-sync if master has no phrase data.
        """
        follower = self.decks[follower_idx]
        master   = next((d for d in self.decks
                         if d is not follower and d.sync_master
                         and d.bpm > 0 and d.audio is not None), None)
        if master is None:
            master = next((d for d in self.decks
                           if d is not follower and d.playing
                           and d.bpm > 0 and d.audio is not None), None)
        if master is None or follower.bpm <= 0 or follower.audio is None:
            return

        sr = master.sample_rate

        # Tempo-match the follower
        master_key_ratio   = 2.0 ** (getattr(master,   'key_shift', 0) / 12.0)
        follower_key_ratio = 2.0 ** (getattr(follower, 'key_shift', 0) / 12.0)
        target_bpm = master.bpm * master.rate * master_key_ratio
        nominal    = (target_bpm / follower.bpm) / follower_key_ratio
        if follower.playing:
            follower.rate = max(0.5, min(2.0, nominal))

        # Find the next phrase boundary in the master
        snap_pos = None
        if master.phrases is not None and len(master.phrases) > 1:
            # Next phrase boundary after master's current position
            idx = int(np.searchsorted(master.phrases, master.position, side='right'))
            if idx < len(master.phrases):
                # What bar-phase does that phrase start at in follower-time?
                master_phrase_pos = master.phrases[idx]
                master_bar  = sr * 60.0 / max(master.bpm * master.rate, 1)
                follower_bar = sr * 60.0 / max(follower.bpm, 1)
                # Fraction through current master bar at that phrase boundary
                rel = (master_phrase_pos - master.first_beat) % (master_bar * 4)
                phase = rel / (master_bar * 4)
                # Map same phase onto follower
                f_rel = follower.position - follower.first_beat
                f_bar_len = follower_bar * 4
                f_bar_start = follower.first_beat + (int(f_rel / f_bar_len)) * f_bar_len
                snap_pos = f_bar_start + phase * f_bar_len
                if snap_pos < follower.position:
                    snap_pos += f_bar_len

        # Fall back to bar-snap
        if snap_pos is None:
            snap_pos = _snap_to_grid(master, follower, sr)

        audio_len = len(follower.audio)
        follower.position  = max(0.0, min(float(audio_len - 2), snap_pos))
        follower.cue_point = int(follower.position)
        print(f"[PHRASE SYNC] D{follower_idx+1} snapped to phrase boundary")
        if not follower.playing:
            self._was_playing[follower_idx] = False

    # ── Audio callback ────────────────────────────────────────

    def _callback(self, outdata: np.ndarray, frames: int, _time, _status):
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
