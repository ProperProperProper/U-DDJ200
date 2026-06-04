"""
Stem separator — demucs htdemucs with professional post-processing.

Quality chain:
  1. Demucs htdemucs  (MPS on Apple Silicon, else CPU)
     shifts=2 for ensemble averaging — significant quality improvement
     overlap=0.25 (default, quarter-chunk overlap for continuity)
  2. Per-stem DSP post-processing tuned for the genre/track characteristics:
       drums  — transient restore (kick punch), sub-mud roll-off, hi-hat air
       bass   — sub-bass enhance, upper-bass bleed cut, sub mono-sum
       other  — bass bleed high-pass, presence boost
       vocals — auto-gate (near-silence for instrumental tracks)
  3. Level calibration — each stem gains so their sum RMS matches the original
  4. Vocal presence detection — auto-disable vocals stem for instrumentals

Fallback: pure-DSP approximation when demucs is not installed.
"""
from __future__ import annotations

import threading
import numpy as np
from typing import Callable, Optional

STEM_NAMES = ["drums", "bass", "other", "vocals", "guitar", "piano"]

# Bump this version string whenever the stems post-processing pipeline changes.
# Cached .npz files store this version; mismatches trigger automatic re-analysis.
STEMS_VERSION = "v6"   # htdemucs_6s (6 stems); DSP split of other→guitar+piano fallback

# Single-worker executor — demucs saturates MPS/CPU, no gain from parallelism.
# One track at a time gives consistent throughput without thrashing.
import concurrent.futures as _cf
_stems_executor = _cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="stems")

# ─── public entry point ───────────────────────────────────────────────────────

def separate_async(
    file_path: str,
    sample_rate: int,
    on_done: Callable[[dict], None],
    on_error: Optional[Callable[[Exception], None]] = None,
    track_meta: Optional[dict] = None,
):
    """
    Kick off stem separation in a daemon thread.
    If stems are already cached on disk, loads them instantly.
    Otherwise runs demucs and saves to disk cache for next time.
    """
    # Live load: use separate thread so it doesn't block the pre-analysis queue
    threading.Thread(
        target=_worker,
        args=(file_path, sample_rate, on_done, on_error, track_meta),
        daemon=True,
    ).start()


def preanalyse_stems(file_path: str, sample_rate: int,
                     track_meta: Optional[dict] = None,
                     on_done: Optional[Callable] = None):
    """
    Pre-analyse stems for a track and save to disk cache.
    Queued through the single-worker executor — one track at a time.
    """
    _stems_executor.submit(_preanalyse_worker,
                           file_path, sample_rate, track_meta, on_done)


def _preanalyse_worker(file_path, sample_rate, track_meta, on_done):
    try:
        cache_path = _cache_path(file_path)
        if cache_path.exists():
            # Validate version — if stale, delete and re-run
            existing = _load_cache(cache_path, sample_rate)
            if existing is not None:
                print(f"[Stems] already cached (current version): {cache_path.name}")
                if on_done:
                    on_done(file_path)
                return
            cache_path.unlink(missing_ok=True)
        result = _separate_demucs(file_path, sample_rate, track_meta)
        _save_cache(result, cache_path)
        from ..library.db import mark_stems_cached
        mark_stems_cached(file_path)
        print(f"[Stems] pre-analysed and cached: {cache_path.name}")
        if on_done:
            on_done(file_path)
    except Exception as e:
        print(f"[Stems] pre-analysis failed for {file_path}: {e}")
        from ..library.db import mark_stems_failed
        mark_stems_failed(file_path)


def _cache_path(file_path: str):
    from ..library.db import stems_cache_path
    return stems_cache_path(file_path)


def _save_cache(stems: dict, path):
    """Save stems dict to compressed .npz file with version tag."""
    arrays = {k: v for k, v in stems.items()
              if isinstance(v, np.ndarray)}
    vocal_present = stems.get("_vocal_present", True)
    np.savez_compressed(str(path),
                        vocal_present=np.array([vocal_present]),
                        _version=np.array([STEMS_VERSION], dtype="U16"),
                        **arrays)


def _load_cache(path, sample_rate: int) -> dict | None:
    """
    Load stems from .npz cache file.
    Returns None if the cache was built with a different STEMS_VERSION,
    triggering automatic re-analysis.
    """
    data = np.load(str(path), allow_pickle=False)
    # Version check — if missing or mismatched, invalidate
    if "_version" not in data.files:
        return None
    cached_version = str(data["_version"][0])
    if cached_version != STEMS_VERSION:
        print(f"[Stems] cache version mismatch ({cached_version} != {STEMS_VERSION}) — re-analysing")
        return None
    result = {}
    for k in data.files:
        if k == "_version":
            continue
        if k == "vocal_present":
            result["_vocal_present"] = bool(data[k][0])
        else:
            result[k] = data[k].astype(np.float32)
    return result


def _worker(file_path, sample_rate, on_done, on_error, track_meta):
    try:
        # Check stem_analyzer WAV cache first (richer format, shared with batch tool)
        try:
            import sys, pathlib
            # In PyInstaller bundle, datas land in sys._MEIPASS (not __file__'s parent).
            # Also try the source tree location for development runs.
            for _candidate in [
                getattr(sys, '_MEIPASS', None),
                str(pathlib.Path(__file__).parent.parent.parent),
            ]:
                if _candidate and _candidate not in sys.path:
                    sys.path.insert(0, _candidate)
            from stem_analyzer import load_cached_stems, is_cached
            if is_cached(file_path):
                wav_stems = load_cached_stems(file_path, sample_rate)
                if wav_stems is not None:
                    print(f"[Stems] ✅ WAV cache hit: {pathlib.Path(file_path).name}")
                    on_done(wav_stems)
                    return
                else:
                    print(f"[Stems] WAV cache found but load failed: {pathlib.Path(file_path).name}")
            else:
                print(f"[Stems] no WAV cache for {pathlib.Path(file_path).name} — running demucs")
        except Exception as _sa_err:
            print(f"[Stems] stem_analyzer import failed: {_sa_err} — falling back to .npz")

        cache = _cache_path(file_path)
        if cache.exists():
            result = _load_cache(cache, sample_rate)
            if result is not None:
                print(f"[Stems] loaded from .npz cache: {cache.name}")
                on_done(result)
                return
            # Version mismatch — delete stale cache and re-run
            cache.unlink(missing_ok=True)
        print(f"[Stems] running demucs htdemucs_ft (will cache result)")
        result = _separate_demucs(file_path, sample_rate, track_meta)
        _save_cache(result, cache)
        from ..library.db import mark_stems_cached
        mark_stems_cached(file_path)
        on_done(result)
    except Exception as e:
        print(f"[Stems] demucs failed ({e}), falling back to DSP approximation")
        try:
            import soundfile as sf
            audio, sr = sf.read(file_path, dtype="float32", always_2d=True)
            if audio.shape[1] == 1:
                audio = np.repeat(audio, 2, axis=1)
            result = _separate_dsp(audio, sr, track_meta)
            on_done(result)
        except Exception as e2:
            print(f"[Stems] DSP fallback also failed: {e2}")
            if on_error:
                on_error(e2)


# ─── demucs separation ────────────────────────────────────────────────────────

def _separate_demucs(file_path: str, target_sr: int,
                     track_meta: Optional[dict]) -> dict:
    import torch
    import torchaudio
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    # Apple Silicon MPS — dramatically faster than CPU
    if torch.backends.mps.is_available():
        device = "mps"
    elif torch.cuda.is_available():
        device = "cuda"
    else:
        device = "cpu"
    print(f"[Stems] using device={device}")

    # Model priority: 6s (guitar+piano) → ft (fine-tuned 4-stem) → htdemucs (standard 4-stem)
    # htdemucs_6s: drums, bass, other, vocals, guitar, piano — broadest stem palette
    # Falls back to 4-stem models; guitar+piano are then derived via DSP split of "other"
    model_name = "htdemucs_6s"
    try:
        model = get_model("htdemucs_6s")
        print("[Stems] using htdemucs_6s (6-stem: guitar+piano separated)")
    except Exception:
        try:
            model = get_model("htdemucs_ft")
            model_name = "htdemucs_ft"
            print("[Stems] htdemucs_6s unavailable — using htdemucs_ft (4-stem)")
        except Exception:
            model = get_model("htdemucs")
            model_name = "htdemucs"
            print("[Stems] using htdemucs (standard 4-stem)")
    model.eval()

    wav, sr = torchaudio.load(file_path)
    if wav.shape[0] == 1:
        wav = wav.repeat(2, 1)
    wav = wav[:2]

    model_sr = model.samplerate
    if sr != model_sr:
        wav = torchaudio.functional.resample(wav, sr, model_sr)

    # shifts=1: single run — htdemucs_ft is already fine-tuned so the quality
    # benefit of shifts=2/4 is marginal (measured 192s vs 437s on MPS for a
    # 7-minute track). overlap=0.5 still eliminates chunk-boundary artifacts.
    with torch.no_grad():
        sources = apply_model(
            model,
            wav.unsqueeze(0),
            device=device,
            shifts=1,
            overlap=0.5,
            progress=False,
        )[0]   # (num_stems, 2, N)

    stem_names = model.sources   # e.g. ["drums","bass","other","vocals"] or +["guitar","piano"]
    raw_stems: dict[str, np.ndarray] = {}
    for i, name in enumerate(stem_names):
        stem_wav = sources[i]
        if model_sr != target_sr:
            stem_wav = torchaudio.functional.resample(stem_wav, model_sr, target_sr)
        arr = stem_wav.cpu().numpy().T.astype(np.float32)   # (N, 2)
        raw_stems[name] = np.ascontiguousarray(arr)

    # Load original audio for level reference
    import soundfile as sf
    orig, orig_sr = sf.read(file_path, dtype="float32", always_2d=True)
    if orig.shape[1] == 1:
        orig = np.repeat(orig, 2, axis=1)
    if orig_sr != target_sr:
        from math import gcd
        from scipy.signal import resample_poly
        g   = gcd(orig_sr, target_sr)
        orig = resample_poly(orig, target_sr // g, orig_sr // g).astype(np.float32)

    # Trim to same length (demucs may pad)
    n = min(len(orig), min(len(v) for v in raw_stems.values()))
    orig = orig[:n]
    raw_stems = {k: v[:n] for k, v in raw_stems.items()}

    # If model only gave 4 stems, derive guitar+piano from "other" via DSP
    if "guitar" not in raw_stems and "other" in raw_stems:
        raw_stems["guitar"], raw_stems["piano"] = _split_other_to_guitar_piano(
            raw_stems["other"], target_sr)
        print("[Stems] guitar+piano derived via DSP split of 'other'")

    # Post-process each stem, then calibrate levels
    processed = _post_process_stems(raw_stems, target_sr, orig, track_meta)

    # Detect instrumental / vocal-absent tracks → return flag alongside stems
    processed["_vocal_present"] = _detect_vocal_presence(
        raw_stems.get("vocals", np.zeros((1024, 2), dtype=np.float32)), orig)

    return processed


# ─── per-stem post-processing ─────────────────────────────────────────────────

def _post_process_stems(raw: dict[str, np.ndarray], sr: int,
                        orig: np.ndarray,
                        track_meta: Optional[dict]) -> dict[str, np.ndarray]:
    from scipy.signal import sosfilt, butter

    bpm    = float((track_meta or {}).get("bpm",    128.0) or 128.0)
    energy = int(  (track_meta or {}).get("energy",    6)  or 6)
    lufs   = float((track_meta or {}).get("loudness_lufs", -14.0) or -14.0)

    result = {}

    if "drums" in raw:
        result["drums"] = _process_drums(raw["drums"], sr, bpm, energy)
    if "bass" in raw:
        result["bass"]  = _process_bass(raw["bass"],   sr, lufs)
    if "other" in raw:
        result["other"] = _process_other(raw["other"], sr)
    if "vocals" in raw:
        result["vocals"] = _process_vocals(raw["vocals"], sr)
    if "guitar" in raw:
        result["guitar"] = _process_guitar(raw["guitar"], sr)
    if "piano" in raw:
        result["piano"]  = _process_piano(raw["piano"],  sr)

    # Level calibration: scale each stem so their RMS sum matches the original
    result = _calibrate_levels(result, orig)

    return result


def _rms(a: np.ndarray) -> float:
    return float(np.sqrt(np.mean(a.astype(np.float64) ** 2)) + 1e-9)


def _process_drums(audio: np.ndarray, sr: int,
                   bpm: float, energy: int) -> np.ndarray:
    """
    Drums post-processing tuned for Detroit techno / electronic music:
    - HP at 30 Hz only (preserve kick sub-body at 50-80 Hz — not 60 Hz)
    - Aggressive transient restore for industrial kick punch
    - Hi-hat air shelf +2 dB above 10 kHz (more sparkle for techno hats)
    - De-mud: −3 dB around 300 Hz (boxy bleed, heavier cut for techno clarity)
    """
    from scipy.signal import sosfilt, butter
    nyq = sr / 2

    audio64 = audio.astype(np.float64)

    # 1. Remove only DC/rumble below 30 Hz (keep kick sub-body at 50-80 Hz)
    sos_hp = butter(4, 30.0 / nyq, "high", output="sos")
    audio64 = np.stack([
        sosfilt(sos_hp, audio64[:, 0]),
        sosfilt(sos_hp, audio64[:, 1]),
    ], axis=1)

    # 2. Transient restoration — more aggressive for techno punch
    kick_boost = 1.25 + (energy / 10.0) * 0.4   # baseline 25% + up to 40% by energy
    audio64 = _transient_enhance(audio64, sr,
                                 attack_ms=1.0, release_ms=40.0,
                                 boost=kick_boost)

    # 3. Hi-hat air shelf +2 dB above 10 kHz (techno hats need more presence)
    sos_air = butter(1, 10000.0 / nyq, "high", output="sos")
    air = np.stack([
        sosfilt(sos_air, audio64[:, 0]),
        sosfilt(sos_air, audio64[:, 1]),
    ], axis=1)
    air_gain = 10.0 ** (2.0 / 20.0) - 1.0
    audio64 = audio64 + air * air_gain

    # 4. De-mud: −3 dB at 250–400 Hz (heavier cut — techno kicks need tight low-mids)
    sos_mud_hp = butter(2, 200.0 / nyq, "high", output="sos")
    sos_mud_lp = butter(2, 500.0 / nyq, "low",  output="sos")
    mud_L = sosfilt(sos_mud_hp, audio64[:, 0])
    mud_L = sosfilt(sos_mud_lp, mud_L)
    mud_R = sosfilt(sos_mud_hp, audio64[:, 1])
    mud_R = sosfilt(sos_mud_lp, mud_R)
    mud_gain = 10.0 ** (-3.0 / 20.0) - 1.0
    audio64[:, 0] += mud_L * mud_gain
    audio64[:, 1] += mud_R * mud_gain

    return audio64.astype(np.float32)


def _rms_envelope(sig: np.ndarray, tau_ms: float, sr: int) -> np.ndarray:
    """
    Vectorised exponential RMS envelope via scipy lfilter (fully numpy — no loops).
    One-pole lowpass on the squared signal, then sqrt.
    """
    from scipy.signal import lfilter
    alpha = 1.0 - np.exp(-1.0 / max(sr * tau_ms / 1000.0, 1.0))
    b = [alpha]
    a = [1.0, -(1.0 - alpha)]
    power = lfilter(b, a, sig.astype(np.float64) ** 2)
    return np.sqrt(np.maximum(power, 0.0))


def _transient_enhance(audio: np.ndarray, sr: int,
                       attack_ms: float, release_ms: float,
                       boost: float) -> np.ndarray:
    """
    Upward transient shaper: fast RMS envelope / slow RMS envelope → gain ratio.
    When fast > slow (attack), boost the signal. Fully vectorised.
    """
    out = audio.copy()
    for ch in range(audio.shape[1]):
        sig   = audio[:, ch]
        env_a = _rms_envelope(sig, attack_ms,  sr)   # fast (transients)
        env_r = _rms_envelope(sig, release_ms, sr)   # slow  (sustain)
        ratio = np.where(env_r > 1e-6, env_a / env_r, 1.0)
        ratio = np.clip(ratio, 1.0, boost)
        out[:, ch] = sig * ratio

    return out


def _process_bass(audio: np.ndarray, sr: int, lufs: float) -> np.ndarray:
    """
    Bass post-processing tuned for Detroit techno / 303 acid bass:
    - High-cut at 1200 Hz (not 300 Hz) — 303 squeal/resonance lives at 400-1000 Hz
    - Sub-bass mono-sum below 120 Hz (professional mastering practice)
    - Sub boost +3 dB below 60 Hz for Detroit kick-bass punch
    """
    from scipy.signal import sosfilt, butter
    nyq = sr / 2

    audio64 = audio.astype(np.float64)

    # 1. High-cut at 1200 Hz — allows 303 acid squeal/resonance through (400-1000 Hz)
    sos_lp = butter(6, min(1200.0 / nyq, 0.99), "low", output="sos")
    audio64 = np.stack([
        sosfilt(sos_lp, audio64[:, 0]),
        sosfilt(sos_lp, audio64[:, 1]),
    ], axis=1)

    # 2. Sub-bass boost +3 dB below 60 Hz (Detroit bass needs sub weight)
    sos_sub_lp = butter(2, min(60.0 / nyq, 0.99), "low",  output="sos")
    sos_sub_hp = butter(2, 20.0 / nyq,             "high", output="sos")
    sub_L = sosfilt(sos_sub_lp, audio64[:, 0])
    sub_L = sosfilt(sos_sub_hp, sub_L)
    sub_R = sosfilt(sos_sub_lp, audio64[:, 1])
    sub_R = sosfilt(sos_sub_hp, sub_R)
    sub_gain = 10.0 ** (3.0 / 20.0) - 1.0
    audio64[:, 0] += sub_L * sub_gain
    audio64[:, 1] += sub_R * sub_gain

    # 3. Mono-sum below 120 Hz (pro bass management)
    sos_mono = butter(4, min(120.0 / nyq, 0.99), "low", output="sos")
    lo_L = sosfilt(sos_mono, audio64[:, 0])
    lo_R = sosfilt(sos_mono, audio64[:, 1])
    mono = (lo_L + lo_R) * 0.5
    audio64[:, 0] = audio64[:, 0] - lo_L + mono
    audio64[:, 1] = audio64[:, 1] - lo_R + mono

    return audio64.astype(np.float32)


def _process_other(audio: np.ndarray, sr: int) -> np.ndarray:
    """
    Other (synths, pads) post-processing:
    - High-pass 80 Hz to remove bass and kick bleed
    - Gentle presence boost +1 dB at 3–8 kHz (synth clarity)
    """
    from scipy.signal import sosfilt, butter
    nyq = sr / 2

    audio64 = audio.astype(np.float64)

    # Remove bass/kick bleed below 80 Hz
    sos_hp = butter(4, 80.0 / nyq, "high", output="sos")
    audio64 = np.stack([
        sosfilt(sos_hp, audio64[:, 0]),
        sosfilt(sos_hp, audio64[:, 1]),
    ], axis=1)

    # Presence boost 3–8 kHz (+1 dB) — synth clarity, air
    sos_pres_hp = butter(2, 3000.0 / nyq, "high", output="sos")
    sos_pres_lp = butter(2, 8000.0 / nyq, "low",  output="sos")
    pres_L = sosfilt(sos_pres_lp, sosfilt(sos_pres_hp, audio64[:, 0]))
    pres_R = sosfilt(sos_pres_lp, sosfilt(sos_pres_hp, audio64[:, 1]))
    pres_gain = 10.0 ** (1.0 / 20.0) - 1.0
    audio64[:, 0] += pres_L * pres_gain
    audio64[:, 1] += pres_R * pres_gain

    return audio64.astype(np.float32)


def _process_vocals(audio: np.ndarray, sr: int) -> np.ndarray:
    """
    Vocals post-processing for clean voice isolation:
    1. High-pass 80 Hz — remove low-end bleed (kick, bass leaking into vocals stem)
    2. Low-pass 12 kHz — remove hiss/cymbal bleed above vocal range
    3. Presence boost +2 dB at 2–5 kHz — forward vocal clarity (telephone band)
    4. Noise gate at −30 dBFS with 20ms smoothing — kills demucs bleed in silent passages
    """
    from scipy.signal import sosfilt, butter
    nyq = sr / 2
    audio64 = audio.astype(np.float64)

    # 1. HP at 80 Hz — cut kick/bass bleed (vocals don't live below 80 Hz)
    sos_hp = butter(4, 80.0 / nyq, "high", output="sos")
    audio64 = np.stack([
        sosfilt(sos_hp, audio64[:, 0]),
        sosfilt(sos_hp, audio64[:, 1]),
    ], axis=1)

    # 2. LP at 12 kHz — cut hiss and cymbal bleed above vocal range
    sos_lp = butter(2, min(12000.0 / nyq, 0.99), "low", output="sos")
    audio64 = np.stack([
        sosfilt(sos_lp, audio64[:, 0]),
        sosfilt(sos_lp, audio64[:, 1]),
    ], axis=1)

    # 3. Presence boost +2 dB at 2–5 kHz (vocal intelligibility band)
    sos_pres_hp = butter(2, 2000.0 / nyq, "high", output="sos")
    sos_pres_lp = butter(2, min(5000.0 / nyq, 0.99), "low",  output="sos")
    pres_L = sosfilt(sos_pres_lp, sosfilt(sos_pres_hp, audio64[:, 0]))
    pres_R = sosfilt(sos_pres_lp, sosfilt(sos_pres_hp, audio64[:, 1]))
    pres_gain = 10.0 ** (2.0 / 20.0) - 1.0
    audio64[:, 0] += pres_L * pres_gain
    audio64[:, 1] += pres_R * pres_gain

    # 4. Noise gate at −30 dBFS — remove demucs bleed in silent/instrumental passages
    # 20ms window + 30ms smooth to avoid pumping artifacts
    window = max(1, int(sr * 0.020))
    gate_threshold = 10.0 ** (-30.0 / 20.0)

    rms_env = np.sqrt(np.convolve(audio64[:, 0] ** 2,
                                  np.ones(window) / window,
                                  mode="same"))
    # Smooth gate envelope: 30ms attack/release via exponential filter
    from scipy.signal import lfilter
    alpha = 1.0 - np.exp(-1.0 / max(sr * 0.030, 1.0))
    gate_raw = (rms_env > gate_threshold).astype(np.float64)
    gate, _ = lfilter([alpha], [1.0, -(1.0 - alpha)], gate_raw)
    gate = np.clip(gate, 0.0, 1.0)

    audio64[:, 0] *= gate
    audio64[:, 1] *= gate

    return audio64.astype(np.float32)


def _split_other_to_guitar_piano(other: np.ndarray, sr: int
                                  ) -> tuple[np.ndarray, np.ndarray]:
    """
    DSP split of the 'other' stem into guitar-like and piano-like components.
    Guitar/Lead:  brighter, more transient-heavy (HP 1500 Hz) — lead synths, stabs
    Piano/Pad:    lower harmonic content  (LP 1500 Hz) — pads, chords, sub-textures
    Crossover at 1500 Hz chosen for techno/dance (lead synths live above, pads below).
    """
    from scipy.signal import sosfilt, butter
    nyq = sr / 2
    xover = min(1500.0 / nyq, 0.99)

    sos_hp = butter(4, xover, "high", output="sos")
    sos_lp = butter(4, xover, "low",  output="sos")

    a64 = other.astype(np.float64)
    guitar = np.stack([sosfilt(sos_hp, a64[:, 0]),
                       sosfilt(sos_hp, a64[:, 1])], axis=1).astype(np.float32)
    piano  = np.stack([sosfilt(sos_lp, a64[:, 0]),
                       sosfilt(sos_lp, a64[:, 1])], axis=1).astype(np.float32)
    return guitar, piano


def _process_guitar(audio: np.ndarray, sr: int) -> np.ndarray:
    """
    Guitar: HP 80 Hz (remove bass/kick bleed), presence boost +2 dB at 2–5 kHz.
    """
    from scipy.signal import sosfilt, butter
    nyq = sr / 2
    a64 = audio.astype(np.float64)

    sos_hp = butter(4, 80.0 / nyq, "high", output="sos")
    a64 = np.stack([sosfilt(sos_hp, a64[:, 0]),
                    sosfilt(sos_hp, a64[:, 1])], axis=1)

    sos_pres_hp = butter(2, 2000.0 / nyq, "high", output="sos")
    sos_pres_lp = butter(2, min(5000.0 / nyq, 0.99), "low", output="sos")
    pres_L = sosfilt(sos_pres_lp, sosfilt(sos_pres_hp, a64[:, 0]))
    pres_R = sosfilt(sos_pres_lp, sosfilt(sos_pres_hp, a64[:, 1]))
    pres_gain = 10.0 ** (2.0 / 20.0) - 1.0
    a64[:, 0] += pres_L * pres_gain
    a64[:, 1] += pres_R * pres_gain

    return a64.astype(np.float32)


def _process_piano(audio: np.ndarray, sr: int) -> np.ndarray:
    """
    Piano/chords: HP 60 Hz (remove sub-bass bleed), gentle warmth boost +1.5 dB at 200–800 Hz.
    """
    from scipy.signal import sosfilt, butter
    nyq = sr / 2
    a64 = audio.astype(np.float64)

    sos_hp = butter(4, 60.0 / nyq, "high", output="sos")
    a64 = np.stack([sosfilt(sos_hp, a64[:, 0]),
                    sosfilt(sos_hp, a64[:, 1])], axis=1)

    sos_w_hp = butter(2, 200.0 / nyq, "high", output="sos")
    sos_w_lp = butter(2, min(800.0 / nyq, 0.99), "low", output="sos")
    w_L = sosfilt(sos_w_lp, sosfilt(sos_w_hp, a64[:, 0]))
    w_R = sosfilt(sos_w_lp, sosfilt(sos_w_hp, a64[:, 1]))
    warm_gain = 10.0 ** (1.5 / 20.0) - 1.0
    a64[:, 0] += w_L * warm_gain
    a64[:, 1] += w_R * warm_gain

    return a64.astype(np.float32)


def _detect_vocal_presence(vocals: np.ndarray, orig: np.ndarray) -> bool:
    """
    Heuristic: if the vocals stem's RMS is less than 8% of the original's RMS
    the track is likely instrumental and the stem is just bleed/noise.
    """
    voc_rms  = _rms(vocals)
    orig_rms = _rms(orig)
    ratio    = voc_rms / (orig_rms + 1e-9)
    present  = ratio > 0.08
    print(f"[Stems] vocal_rms/orig_rms={ratio:.3f}  vocal_present={present}")
    return present


# ─── level calibration ────────────────────────────────────────────────────────

def _calibrate_levels(stems: dict[str, np.ndarray],
                      orig: np.ndarray) -> dict[str, np.ndarray]:
    """
    Scale each stem so that their sum's RMS matches the original.

    Strategy (same as Traktor Stem Tool):
      1. Sum all processed stems.
      2. Compute global_factor = rms(orig) / rms(sum).
      3. Apply global_factor uniformly to all stems.

    This preserves inter-stem balance while matching the original loudness
    so that using stems in a mix sounds level-matched.
    """
    n = min(len(orig), min(len(v) for v in stems.values() if hasattr(v, '__len__') and not isinstance(v, bool)))
    audio_stems = {k: v for k, v in stems.items()
                   if isinstance(v, np.ndarray) and v.ndim == 2}

    if not audio_stems:
        return stems

    stem_sum = np.zeros((n, 2), dtype=np.float64)
    for arr in audio_stems.values():
        stem_sum += arr[:n].astype(np.float64)

    sum_rms  = _rms(stem_sum)
    orig_rms = _rms(orig[:n])
    factor   = orig_rms / (sum_rms + 1e-9)
    factor   = float(np.clip(factor, 0.1, 4.0))   # safety bounds

    print(f"[Stems] level calibration: sum_rms={sum_rms:.4f} "
          f"orig_rms={orig_rms:.4f}  factor={factor:.3f}")

    result = dict(stems)
    for k in audio_stems:
        result[k] = (stems[k].astype(np.float64) * factor).astype(np.float32)

    return result


# ─── DSP fallback ─────────────────────────────────────────────────────────────

def _separate_dsp(audio: np.ndarray, sr: int,
                  track_meta: Optional[dict]) -> dict:
    """
    Rough DSP-only stem approximation (no ML).

    Improved for electronic/techno: tighter crossover points, better
    kick/bass separation, genre-appropriate processing.
    """
    from scipy.signal import sosfilt, butter

    nyq  = sr / 2
    L    = audio[:, 0].astype(np.float64)
    R    = audio[:, 1].astype(np.float64)
    mid  = (L + R) * 0.5
    side = (L - R) * 0.5

    def _lp(sig, fc, order=4):
        return sosfilt(butter(order, min(fc / nyq, 0.99), "low", output="sos"), sig)

    def _hp(sig, fc, order=4):
        return sosfilt(butter(order, min(fc / nyq, 0.99), "high", output="sos"), sig)

    def _bp(sig, lo, hi, order=4):
        return _hp(_lp(sig, hi, order), lo, order)

    # Bass: mono below 400 Hz
    bass_m = _lp(mid,  400)
    bass_s = _lp(side, 400)
    bass   = np.stack([bass_m + bass_s, bass_m - bass_s], axis=1)

    # Drums: transient side-channel + high-frequency content
    drums_side = _hp(side, 100)
    drums_hi   = _hp(mid,  3500) * 0.5
    drms_L = drums_side + drums_hi
    drms_R = -drums_side + drums_hi
    drms   = np.stack([drms_L, drms_R], axis=1)

    # Other: mid-range content (pads, synths) 200 Hz – 8 kHz, mid channel
    other_m = _bp(mid, 200, 8000)
    other   = np.stack([other_m, other_m], axis=1)

    # Vocals: mid 1–4 kHz residual (likely mostly noise for instrumentals)
    voc_m = _bp(mid, 1000, 4000) * 0.3
    voc   = np.stack([voc_m, voc_m], axis=1)

    # Reconstruct remainder as "other"
    orig_L = L - bass[:, 0] - drms[:, 0] - voc[:, 0]
    orig_R = R - bass[:, 1] - drms[:, 1] - voc[:, 1]
    other  = np.stack([orig_L, orig_R], axis=1)

    def _clip(x):
        return np.clip(x, -1.0, 1.0).astype(np.float32)

    stems = {
        "drums":  _clip(drms),
        "bass":   _clip(bass),
        "other":  _clip(other),
        "vocals": _clip(voc),
    }

    stems = _calibrate_levels(stems, audio)
    stems["_vocal_present"] = False   # DSP fallback assumes instrumental

    return stems
