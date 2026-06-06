"""
Track analyzer — BPM, key, duration.
BPM uses a multi-hypothesis scorer to avoid the half/double-tempo error
that librosa's beat_track commonly produces on electronic music.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Optional
from concurrent.futures import ThreadPoolExecutor
from math import gcd

import numpy as np
import librosa
import soundfile as sf
from mutagen import File as MutagenFile

from .db import upsert_track, get_track, AUDIO_EXTS, ANALYSIS_VERSION
from scipy.signal import resample_poly

import os as _os
_WORKERS = max(1, int(_os.cpu_count() * 0.8))   # 80% of cores
_executor = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="analyzer")


def _read_tags(path: str) -> dict:
    meta = {"title": Path(path).stem, "artist": "", "album": "",
            "bitrate": 0, "duration": 0.0, "bpm": 0.0, "key": ""}
    try:
        f = MutagenFile(path, easy=True)
        if f:
            meta["title"]  = str(f.get("title",  [meta["title"]])[0])
            meta["artist"] = str(f.get("artist", [""])[0])
            meta["album"]  = str(f.get("album",  [""])[0])
            if hasattr(f, "info"):
                meta["duration"] = getattr(f.info, "length", 0.0)
                meta["bitrate"]  = getattr(f.info, "bitrate", 0)
    except Exception:
        pass
    return meta


def _detect_beats(y: np.ndarray, sr: int) -> tuple[float, float, list]:
    """
    Mixxx-style beat detection.  Returns (bpm, first_beat_sec, beat_times_sec).

    Approach mirrors Mixxx's AnalyzerBeats:
    1. Spectral-flux onset envelope (weighted toward low frequencies where
       kick/snare live) — more accurate for EDM than a flat onset envelope.
    2. Autocorrelation + dynamic-programming beat tracking (librosa.beat.beat_track
       uses the same algorithm as Mixxx's BeatUtils).
    3. BPM refined from median inter-beat interval on the final beat sequence,
       with outlier rejection.
    4. Tempo candidates checked at ×0.5, ×1, ×2 against an onset-energy scorer
       to avoid the half/double-tempo error.
    5. All beat positions stored (not just BPM + first beat) so the engine can
       call beat_distance(position, beats) — exactly as Mixxx does — instead of
       computing (position − first_beat) % beat_len which accumulates float error.
    """
    import json
    hop = 512

    # Spectral-flux onset envelope — Mixxx uses a similar low-freq-weighted approach
    onset_env = librosa.onset.onset_strength(
        y=y, sr=sr, hop_length=hop,
        aggregate=np.median,      # robust to noise
        fmax=8000,                # focus on kick/snare frequencies
    )

    # Autocorrelation BPM — biased toward 130 BPM so half-tempo (65 BPM) loses
    bpm_ac = float(np.median(librosa.beat.tempo(
        onset_envelope=onset_env, sr=sr, hop_length=hop,
        start_bpm=130, aggregate=None)))

    # Candidates × 0.5 / 1 / 2 — only keep those in the DJ range 110–185 BPM.
    # Filtering here eliminates 69.8 BPM (half of 139.6) before scoring starts.
    candidates = {round(bpm_ac * m, 2) for m in (0.5, 1.0, 2.0)
                  if 110.0 <= bpm_ac * m <= 185.0}
    if not candidates:
        # Fallback: expand range if nothing lands in 110–185
        candidates = {round(bpm_ac * m, 2) for m in (0.5, 1.0, 2.0)
                      if 60.0 <= bpm_ac * m <= 220.0} or {round(bpm_ac, 2)}

    def _score(bpm: float) -> float:
        period = sr * 60.0 / (bpm * hop)
        if period < 2: return 0.0
        idxs = np.arange(0, len(onset_env), period).astype(int)
        idxs = idxs[idxs < len(onset_env)]
        return float(onset_env[idxs].mean()) if len(idxs) else 0.0

    scored   = sorted(((b, _score(b)) for b in candidates),
                      key=lambda x: x[1], reverse=True)
    best_bpm = scored[0][0]

    # Re-run beat tracker locked to best_bpm so positions match the winning tempo
    _, beat_frames = librosa.beat.beat_track(
        onset_envelope=onset_env, sr=sr, hop_length=hop,
        bpm=best_bpm, units="frames", trim=True,
    )
    beat_times = librosa.frames_to_time(beat_frames, sr=sr, hop_length=hop)

    # Validate grid consistency: if median IBI is badly off, build a uniform grid.
    # This catches tracks where librosa still returns inconsistent spacings.
    if len(beat_times) > 4:
        ibi_arr    = np.diff(beat_times)
        ibi_target = 60.0 / best_bpm
        ibi_med    = float(np.median(ibi_arr))
        bad_ratio  = abs(ibi_med / ibi_target - 1.0)
        if bad_ratio > 0.15:
            # Grid is too inconsistent — build uniform grid from first onset + BPM
            print(f"[Analyzer] inconsistent grid (ibi={ibi_med:.4f}s vs {ibi_target:.4f}s)"
                  f" — using uniform grid at {best_bpm:.2f} BPM")
            duration   = (len(beat_times) * ibi_target)
            first_beat = float(beat_times[0])
            beat_times = np.arange(first_beat, first_beat + duration, ibi_target)

    first_beat_sec  = float(beat_times[0]) if len(beat_times) > 0 else 0.0
    beat_times_list = [round(float(t), 6) for t in beat_times]

    return best_bpm, first_beat_sec, beat_times_list


def _detect_downbeat_offset(y: np.ndarray, sr: int,
                             beat_times: list, bpm: float,
                             hop: int = 512) -> int:
    """
    Find the downbeat offset (0–3): which beat in each group of 4 is beat 1.

    Two-pass approach — handles 4-on-the-floor techno/house correctly:

    Pass 1 — kick energy (sub-bass 20–200 Hz).
      Works for breakbeats, hip-hop, anything with non-uniform kick pattern.
      Fails on 4-on-the-floor (kick on every beat = flat energy across phases).

    Pass 2 — harmonic novelty (chroma flux).
      Bar boundaries in any genre tend to have melodic/harmonic changes.
      Averaged chroma flux at each of the 4 phases identifies which phase
      sees the most harmonic change = bar-1. Works on 4-on-the-floor.

    The pass with higher confidence (margin) wins. If neither is confident
    (margin < 12%), fallback to offset 0.
    """
    if not beat_times or len(beat_times) < 8 or bpm <= 0:
        return 0

    beats = np.array(beat_times)
    n     = len(beats)
    beat_sec = 60.0 / bpm

    def _beat_frames(t):
        f0 = int(np.clip(librosa.time_to_frames(t, sr=sr, hop_length=hop),
                         0, 1_000_000))
        f1 = int(np.clip(librosa.time_to_frames(t + beat_sec * 0.9, sr=sr, hop_length=hop),
                         f0 + 1, 1_000_000))
        return f0, f1

    # ── Pass 1: kick (sub-bass) energy ───────────────────────────
    mel = librosa.feature.melspectrogram(
        y=y, sr=sr, hop_length=hop, n_mels=128, fmin=20, fmax=200, power=2.0)
    low_env = np.mean(mel, axis=0)

    kick_energy = np.zeros(n)
    for i, t in enumerate(beats):
        f0, f1 = _beat_frames(t)
        f1 = min(f1, len(low_env))
        if f1 > f0:
            kick_energy[i] = float(low_env[f0:f1].mean())

    phase_kick = np.zeros(4)
    for i in range(n):
        phase_kick[i % 4] += kick_energy[i]

    w_kick = int(np.argmax(phase_kick))
    m_kick = ((phase_kick[w_kick] - np.sort(phase_kick)[-2])
              / (phase_kick[w_kick] + 1e-9))

    # ── Pass 2: harmonic novelty (chroma flux) ───────────────────
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop,
                                         bins_per_octave=24)
    # Chroma flux: L1 difference between adjacent frames
    chroma_flux = np.sum(np.abs(np.diff(chroma, axis=1)), axis=0)  # (n_frames-1,)

    novelty = np.zeros(n)
    for i, t in enumerate(beats):
        f0, f1 = _beat_frames(t)
        f1 = min(f1, len(chroma_flux))
        if f1 > f0:
            novelty[i] = float(chroma_flux[f0:f1].mean())

    phase_nov = np.zeros(4)
    for i in range(n):
        phase_nov[i % 4] += novelty[i]

    w_nov = int(np.argmax(phase_nov))
    m_nov = ((phase_nov[w_nov] - np.sort(phase_nov)[-2])
             / (phase_nov[w_nov] + 1e-9))

    # ── Pick winner ───────────────────────────────────────────────
    CONF = 0.12   # minimum margin to trust a result
    if m_kick >= m_nov and m_kick >= CONF:
        return w_kick
    if m_nov >= m_kick and m_nov >= CONF:
        return w_nov
    return 0   # neither confident — trust beat tracker's first beat


# ── Mixed In Key — Camelot wheel ────────────────────────────────────────────
# Maps (key_index 0=C, mode) → Camelot code
# Major keys: A-wheel (right side);  Minor keys: B-wheel (left side)
_CAMELOT = {
    # major
    (0,  1): "8B",  (1,  1): "3B",  (2,  1): "10B", (3,  1): "5B",
    (4,  1): "12B", (5,  1): "7B",  (6,  1): "2B",  (7,  1): "9B",
    (8,  1): "4B",  (9,  1): "11B", (10, 1): "6B",  (11, 1): "1B",
    # minor
    (0,  0): "5A",  (1,  0): "12A", (2,  0): "7A",  (3,  0): "2A",
    (4,  0): "9A",  (5,  0): "4A",  (6,  0): "11A", (7,  0): "6A",
    (8,  0): "1A",  (9,  0): "8A",  (10, 0): "3A",  (11, 0): "10A",
}
_NOTE_NAMES = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]


def _detect_key_and_camelot(y: np.ndarray, sr: int) -> tuple[str, str]:
    """
    Mixed In Key-style key detection using chroma + mode (major/minor) detection.

    Returns (key_name, camelot_code) e.g. ("Am", "8A") or ("C", "8B").

    Mixed In Key uses a proprietary algorithm but the core is the same:
    chroma energy profile compared to Krumhansl-Schmuckler major/minor templates.
    """
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, bins_per_octave=36)
    chroma_mean = np.mean(chroma, axis=1)   # shape (12,)

    # Krumhansl-Schmuckler key profiles
    major_profile = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
                               2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
    minor_profile = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
                               2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

    best_key, best_mode, best_corr = 0, 1, -np.inf
    for k in range(12):
        roll_major = np.roll(major_profile, k)
        roll_minor = np.roll(minor_profile, k)
        corr_major = float(np.corrcoef(chroma_mean, roll_major)[0, 1])
        corr_minor = float(np.corrcoef(chroma_mean, roll_minor)[0, 1])
        if corr_major > best_corr:
            best_key, best_mode, best_corr = k, 1, corr_major
        if corr_minor > best_corr:
            best_key, best_mode, best_corr = k, 0, corr_minor

    note  = _NOTE_NAMES[best_key]
    name  = note if best_mode else note + "m"
    camelot = _CAMELOT.get((best_key, best_mode), "?")
    return name, camelot


def _detect_energy(y: np.ndarray, sr: int, lufs: float) -> int:
    """
    Mixed In Key energy level 1–10.

    Mixed In Key computes energy from:
    - Integrated loudness (base)
    - High-frequency content (brightness/aggression)
    - Dynamic range (compressed = high energy feel)

    We replicate this heuristic: louder + brighter + more compressed = higher energy.
    """
    # Spectral centroid (brightness)
    centroid = float(np.mean(librosa.feature.spectral_centroid(y=y, sr=sr)))
    # Normalise centroid to 0..1 range (typical: 500–5000 Hz for EDM)
    brightness = float(np.clip((centroid - 500) / 4500, 0.0, 1.0))

    # Dynamic range estimate: RMS vs peak
    rms  = float(np.sqrt(np.mean(y ** 2)))
    peak = float(np.max(np.abs(y)) + 1e-6)
    crest_db = 20 * np.log10(peak / (rms + 1e-6))
    # Low crest = heavily compressed = energetic feel
    # Typical range 6–20 dB; invert so low crest → high energy score
    compression = float(np.clip(1.0 - (crest_db - 6) / 14, 0.0, 1.0))

    # Loudness contribution: −20 LUFS is "1", −6 LUFS is "10"
    lufs_clamped = float(np.clip(lufs, -20, -6))
    loudness_score = (lufs_clamped - (-20)) / 14   # 0..1

    # Weighted combination (matches Mixed In Key's perceptual weighting)
    raw = loudness_score * 0.5 + brightness * 0.3 + compression * 0.2
    return int(np.clip(round(raw * 9) + 1, 1, 10))


_TARGET_LUFS = -14.0   # EBU R128 streaming standard (same as Platinum Notes target)
_MAX_GAIN_DB =  12.0   # never boost more than +12 dB (avoids clipping quiet tracks)
_MIN_GAIN_DB = -12.0   # never cut more than −12 dB


def _measure_loudness(raw: np.ndarray, sr: int) -> tuple[float, float]:
    """
    Measure integrated loudness (EBU R128) and compute the gain needed to
    reach _TARGET_LUFS.

    Returns (loudness_lufs, gain_db).

    Platinum Notes uses the same method:
      1. Measure integrated loudness of the full track with a K-weighting filter
         (BS.1770 algorithm).
      2. Compute gain = target − measured.
      3. Clamp to safe limits (no extreme boosts that would cause clipping).

    We use pyloudnorm which is a pure-Python BS.1770-4 implementation.
    The gain is stored in the DB and applied at playback time as a simple
    linear scaler — no re-encoding, no file modification.
    """
    try:
        import pyloudnorm as pyln
        # pyloudnorm needs float64, shape (N,) or (N,2)
        audio64 = raw.astype(np.float64)
        meter   = pyln.Meter(sr)
        lufs    = meter.integrated_loudness(audio64)

        if not np.isfinite(lufs) or lufs < -70.0:
            # Too quiet or silence — don't apply gain
            return lufs, 0.0

        gain_db = float(np.clip(_TARGET_LUFS - lufs, _MIN_GAIN_DB, _MAX_GAIN_DB))
        return float(lufs), gain_db

    except Exception as e:
        print(f"[Analyzer] loudness error: {e}")
        return 0.0, 0.0


def _detect_cue_points(y: np.ndarray, sr: int, beats_sec: list,
                       phrase_sec: list, bpm: float, hop: int = 512) -> list:
    """
    Smart cue point detection — Mixed In Key 11 Pro style.

    Uses per-bar energy (RMS), spectral novelty, and spectral centroid to
    classify every phrase boundary as one of:
      INTRO  — first musical entry point (after silence/count-in)
      DROP   — biggest energy rise (main drop, chorus hit)
      BREAK  — significant energy fall (breakdown before build)
      BUILD  — gradual energy increase leading to a drop
      OUTRO  — track winds down

    Returns up to 8 cue dicts: [{sec, label, pad_idx}, ...]
    All times are snapped to the nearest bar boundary on the beat grid.
    Minimum separation: 8 bars between cues.
    """
    import json

    if not beats_sec or bpm <= 0:
        return []

    beats_arr = np.array(beats_sec)
    n_beats   = len(beats_arr)
    n_bars    = n_beats // 4
    if n_bars < 4:
        return []

    bar_sec = beats_arr[::4]   # time of every bar-1
    n_bars  = len(bar_sec)

    # ── Features per bar ──────────────────────────────────────
    rms    = librosa.feature.rms(y=y, hop_length=hop)[0]
    onset  = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    spec_c = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=hop)[0]
    n_rms  = len(rms)

    def _bar_feat(arr):
        out = np.zeros(n_bars)
        for i, t in enumerate(bar_sec):
            f0 = int(librosa.time_to_frames(t, sr=sr, hop_length=hop))
            f1 = int(librosa.time_to_frames(
                bar_sec[i + 1] if i + 1 < n_bars else t + 60.0 / bpm * 4,
                sr=sr, hop_length=hop))
            f0, f1 = max(0, f0), min(n_rms, f1)
            if f0 < f1:
                out[i] = float(np.mean(arr[f0:f1]))
        return out

    e_rms  = _bar_feat(rms)
    e_ons  = _bar_feat(onset)
    e_spec = _bar_feat(spec_c)

    # Smooth with 4-bar window
    def _sm(a, w=4):
        return np.convolve(a, np.ones(w) / w, mode='same')

    s_rms  = _sm(e_rms)
    s_ons  = _sm(e_ons)
    s_spec = _sm(e_spec)

    # Normalise 0..1
    def _norm(a):
        mn, mx = a.min(), a.max()
        return (a - mn) / max(mx - mn, 1e-6)

    n_rms_  = _norm(s_rms)
    n_ons_  = _norm(s_ons)
    n_spec_ = _norm(s_spec)

    # Combined novelty score — weighted sum
    novelty = 0.5 * n_rms_ + 0.3 * n_ons_ + 0.2 * n_spec_

    # Energy delta per bar: positive = rising, negative = falling
    delta = np.diff(s_rms, prepend=s_rms[0])

    # ── Snap phrase times to nearest bar-start ─────────────────
    phrase_snapped = []
    for pt in phrase_sec:
        dists = np.abs(bar_sec - pt)
        phrase_snapped.append(int(np.argmin(dists)))
    phrase_snapped = sorted(set(phrase_snapped))

    # ── Classify each phrase boundary ─────────────────────────
    min_gap_bars = 8          # no two cues within 8 bars
    e_max        = s_rms.max()
    e_mean       = s_rms.mean()
    e_std        = s_rms.std()

    candidates   = []         # (bar_idx, label, score)

    for bi in phrase_snapped:
        if bi >= n_bars:
            continue

        e_now    = s_rms[bi]
        d_now    = delta[bi]
        n_now    = novelty[bi]
        frac_pos = bi / max(n_bars - 1, 1)   # 0=start, 1=end

        # ── Classification rules (MIK-style heuristics) ────────
        if bi == phrase_snapped[0] or (frac_pos < 0.15 and e_now < e_mean):
            label = "INTRO"
            score = 1.0 - frac_pos   # prefer earliest
        elif frac_pos > 0.82 and e_now < e_mean:
            label = "OUTRO"
            score = frac_pos
        elif d_now > e_std * 0.6 and e_now > e_mean:
            # Big positive jump into a high-energy section = DROP
            label = "DROP"
            score = d_now / (e_std + 1e-6) + n_now
        elif d_now < -e_std * 0.5 and e_now < e_mean + e_std:
            # Significant energy fall = BREAK
            label = "BREAK"
            score = (-d_now) / (e_std + 1e-6) + n_now
        elif d_now > e_std * 0.3 and e_now < e_mean:
            # Rising from below mean = BUILD
            label = "BUILD"
            score = n_now
        else:
            # Generic energy point
            label = "CUE"
            score = n_now * 0.5

        candidates.append((bi, label, score))

    # ── Greedy selection — highest-score, minimum spacing ──────
    selected = []
    for bi, label, score in sorted(candidates, key=lambda x: -x[2]):
        too_close = any(abs(bi - s[0]) < min_gap_bars for s in selected)
        if not too_close:
            selected.append((bi, label, score))
        if len(selected) >= 8:
            break

    # Sort by time, assign pad indices
    selected.sort(key=lambda x: x[0])
    cues = []
    for pad_idx, (bi, label, _) in enumerate(selected):
        t = float(bar_sec[bi])
        cues.append({"sec": round(t, 4), "label": label, "pad": pad_idx})

    return cues


def _detect_phrases(y: np.ndarray, sr: int, beats_sec: list, hop: int = 512) -> list:
    """
    Algoriddim-style phrase detection.

    Computes RMS energy and spectral centroid per bar, then identifies
    structurally significant boundaries — mimicking how djay v5.5+ uses
    energy-flux analysis to find intro/verse/chorus/drop/outro boundaries.

    Returns sorted list of phrase start times (seconds).
    Every 8-bar boundary is a candidate; energy/spectral jumps promote
    4-bar boundaries too.  This matches djay's default phrase grid.
    """
    if not beats_sec or len(beats_sec) < 8:
        return [beats_sec[0]] if beats_sec else []

    beats_arr = np.array(beats_sec)
    n_beats   = len(beats_arr)
    n_bars    = n_beats // 4

    beat_frames = np.clip(
        np.round(librosa.time_to_frames(beats_arr, sr=sr, hop_length=hop)).astype(int),
        0, None)

    rms   = librosa.feature.rms(y=y, hop_length=hop)[0]
    spec  = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=hop)[0]
    n_rms = len(rms)

    # Per-bar energy and spectral centroid
    bar_rms = np.zeros(n_bars)
    bar_cen = np.zeros(n_bars)
    for i in range(n_bars):
        b  = i * 4
        fs = beat_frames[b:b + 4]
        fs = fs[fs < n_rms]
        if len(fs):
            bar_rms[i] = float(np.mean(rms[fs]))
            bar_cen[i] = float(np.mean(spec[fs]))

    # Smoothed energy for drop / build detection (4-bar window)
    def _smooth4(arr):
        return np.array([np.mean(arr[max(0, i-2):i+2]) for i in range(len(arr))])

    smooth_rms = _smooth4(bar_rms)
    smooth_cen = _smooth4(bar_cen)

    phrases = [float(beats_arr[0])]   # track always starts with a phrase

    for bar_i in range(4, n_bars):
        beat_i = bar_i * 4
        if beat_i >= n_beats:
            break

        at_8  = (bar_i % 8  == 0)
        at_16 = (bar_i % 16 == 0)
        at_4  = (bar_i % 4  == 0)

        if not at_4:
            continue

        # Energy jump: compare 4 bars before vs 4 bars after
        e_before = smooth_rms[max(0, bar_i - 2)]
        e_after  = smooth_rms[min(n_bars - 1, bar_i + 1)]
        e_change = abs(e_after - e_before) / (e_before + 1e-6)

        # Spectral jump (timbral change — chorus vs verse)
        c_before = smooth_cen[max(0, bar_i - 2)]
        c_after  = smooth_cen[min(n_bars - 1, bar_i + 1)]
        c_change = abs(c_after - c_before) / (c_before + 1e-6)

        is_phrase = (
            at_16 or                          # always at 16 bars
            at_8 or                           # always at 8 bars
            (at_4 and e_change > 0.25) or     # significant energy jump
            (at_4 and c_change > 0.35)        # significant timbral change (drop/build)
        )

        if is_phrase:
            phrases.append(float(beats_arr[beat_i]))

    return sorted(set(round(t, 4) for t in phrases))


def _detect_bpm(y: np.ndarray, sr: int) -> float:
    """
    Robust BPM detection for DJ music (house/techno/drum&bass etc.).

    Strategy:
    1. Compute onset strength envelope.
    2. Get autocorrelation-based tempo estimate (librosa.beat.tempo).
    3. Build candidate set: base tempo plus halvings/doublings that fall
       in the 80–185 BPM window (covers virtually all DJ genres).
    4. Score each candidate by how strongly the onset envelope peaks at
       that period — the candidate with the highest peak correlation wins.
    """
    hop = 512
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)

    # Base estimate from autocorrelation
    ac = librosa.beat.tempo(onset_envelope=onset_env, sr=sr,
                            hop_length=hop, aggregate=None)
    base = float(np.median(ac))

    # Generate candidates: base × {0.25, 0.5, 1, 2, 4}
    candidates = set()
    for mult in (0.25, 0.5, 1.0, 2.0, 4.0):
        c = round(base * mult, 2)
        if 60.0 <= c <= 220.0:
            candidates.add(c)

    # Force into DJ range if all candidates are too low
    if candidates and max(candidates) < 80:
        candidates = {round(c * 2, 2) for c in candidates if c * 2 <= 220}

    def _score(bpm: float) -> float:
        period = sr * 60.0 / (bpm * hop)   # period in hop frames
        if period < 2:
            return 0.0
        idxs = np.arange(0, len(onset_env), period).astype(int)
        idxs = idxs[idxs < len(onset_env)]
        return float(onset_env[idxs].mean()) if len(idxs) else 0.0

    scored = sorted(((b, _score(b)) for b in candidates),
                    key=lambda x: x[1], reverse=True)

    # Prefer candidates in the core DJ range 80–185 BPM
    dj_range = [(b, s) for b, s in scored if 80 <= b <= 185]
    best = dj_range[0][0] if dj_range else (scored[0][0] if scored else base)

    return best


def _analyse(path: str) -> dict:
    meta = _read_tags(path)
    try:
        raw, native_sr = sf.read(path, dtype="float32", always_2d=True)
        mono = raw[:, 0]
        meta["duration"] = float(len(raw) / native_sr)

        # Loudness at native SR (BS.1770 requires the original sample rate)
        lufs, gain_db = _measure_loudness(raw, native_sr)
        meta["loudness_lufs"] = round(lufs, 2)
        meta["gain_db"]       = round(gain_db, 2)
        print(f"[Analyzer] Loudness={lufs:.1f} LUFS  gain={gain_db:+.1f} dB  "
              f"{Path(path).name[:40]}")

        # Downsample to 22050 Hz for analysis (scipy, no resampy needed)
        target_sr = 22050
        if native_sr != target_sr:
            g  = gcd(native_sr, target_sr)
            mono = resample_poly(mono, target_sr // g,
                                 native_sr // g).astype(np.float32)
        sr = target_sr
        y  = mono

        import json
        bpm, first_beat_sec, beat_list = _detect_beats(y, sr)

        # Downbeat offset: rotate beat grid so index%4==0 lands on beat 1
        db_offset = _detect_downbeat_offset(y, sr, beat_list, bpm)
        if db_offset > 0 and len(beat_list) > db_offset:
            beat_list      = beat_list[db_offset:]
            first_beat_sec = beat_list[0]

        phrase_list = _detect_phrases(y, sr, beat_list)
        cue_list    = _detect_cue_points(y, sr, beat_list, phrase_list, bpm)
        meta["bpm"]                   = round(bpm, 2)
        meta["first_beat_sec"]        = round(first_beat_sec, 6)
        meta["beat_positions_json"]   = json.dumps(beat_list)
        meta["phrase_positions_json"] = json.dumps(phrase_list)
        meta["cue_points_json"]       = json.dumps(cue_list)
        meta["downbeat_offset"]       = db_offset
        meta["analysis_version"]      = ANALYSIS_VERSION
        print(f"[Analyzer] BPM={bpm:.2f}  first_beat={first_beat_sec:.3f}s  "
              f"beats={len(beat_list)}  phrases={len(phrase_list)}  "
              f"cues={len(cue_list)}  downbeat_offset={db_offset}"
              f"  {Path(path).name[:40]}")

        # Key + Camelot wheel (Mixed In Key-style)
        key_name, camelot = _detect_key_and_camelot(y, sr)
        meta["key"]     = key_name
        meta["camelot"] = camelot

        # Energy level 1–10 (Mixed In Key-style)
        lufs_for_energy = meta.get("loudness_lufs", -14.0) or -14.0
        meta["energy"] = _detect_energy(y, sr, lufs_for_energy)
        print(f"[Analyzer] Key={key_name} ({camelot})  Energy={meta['energy']}/10")

    except Exception as e:
        print(f"[Analyzer] error on {Path(path).name}: {e}")
    return meta


def analyse_track(path: str,
                  on_done: Optional[Callable[[dict], None]] = None,
                  force: bool = False):
    def _work():
        existing = get_track(path)
        if existing and not force and existing.get("bpm"):
            if on_done:
                on_done(existing)
            return
        meta = _analyse(path)
        meta["path"] = path
        upsert_track(meta)
        if on_done:
            on_done(meta)
    _executor.submit(_work)


def analyse_many(paths: list[str],
                 on_each: Optional[Callable[[dict], None]] = None,
                 on_all_done: Optional[Callable[[], None]] = None):
    total = len(paths)
    done_count = [0]
    lock = threading.Lock()

    def _each(meta):
        with lock:
            done_count[0] += 1
            if on_each:
                on_each(meta)
            if done_count[0] == total and on_all_done:
                on_all_done()

    for p in paths:
        analyse_track(p, on_done=_each)
