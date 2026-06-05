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

STEM_NAMES = ["drums", "bass", "other", "vocals"]

# Bump this version string whenever the stems post-processing pipeline changes.
# Cached .npz files store this version; mismatches trigger automatic re-analysis.
STEMS_VERSION = "v12"  # v12: 4-stem only (Serato model) — removed DSP guitar/piano split

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

    # htdemucs_ft: fine-tuned 4-stem (drums, bass, other, vocals) — fastest quality model.
    # htdemucs_6s is slower (6 output heads) with minimal quality gain for electronic music
    # where "guitar" and "piano" are synths anyway.
    try:
        model = get_model("htdemucs_ft")
        print("[Stems] using htdemucs_ft (fine-tuned 4-stem)")
    except Exception:
        model = get_model("htdemucs")
        print("[Stems] using htdemucs (standard 4-stem)")
    model.eval()

    wav, sr = torchaudio.load(file_path)
    if wav.shape[0] == 1:
        wav = wav.repeat(2, 1)
    wav = wav[:2]

    model_sr = model.samplerate
    if sr != model_sr:
        wav = torchaudio.functional.resample(wav, sr, model_sr)

    # shifts=1, overlap=0.25 (demucs default) — fastest settings that still sound clean.
    # overlap=0.5 was 2× slower with no audible benefit on MPS.
    with torch.no_grad():
        sources = apply_model(
            model,
            wav.unsqueeze(0),
            device=device,
            shifts=4,      # 4-pass phase averaging — maximum quality for drums/bass
            overlap=0.25,
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

    # Replace demucs vocals with MelBand Roformer if available — state-of-art isolation.
    roformer_vocals = _roformer_vocals(file_path, target_sr, n)
    if roformer_vocals is not None:
        raw_stems["vocals"] = roformer_vocals
        print("[Stems] Roformer vocals → replacing demucs vocals")
    else:
        print("[Stems] Roformer unavailable — using demucs vocals")

    # Post-process each stem with techno-tuned DSP chain
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

    # Cross-stem bleed correction — remove drum content from vocals.
    # Hi-hats and cymbals (>4 kHz, fast transients) bleed heavily into the
    # demucs vocals stem. Subtract a controlled fraction of the processed
    # drums high-frequency content to clean the vocals stem.
    if "drums" in result and "vocals" in result:
        result["vocals"] = _subtract_drum_bleed_from_vocals(
            result["vocals"], result["drums"], sr)

    # Level calibration: scale each stem so their RMS sum matches the original
    result = _calibrate_levels(result, orig)

    return result


def _rms(a: np.ndarray) -> float:
    return float(np.sqrt(np.mean(a.astype(np.float64) ** 2)) + 1e-9)


def _process_drums(audio: np.ndarray, sr: int,
                   bpm: float, energy: int) -> np.ndarray:
    """
    Drums — dance/techno tuning:
    - HP 30 Hz: keep kick sub-body (50-80 Hz), strip DC/rumble only
    - Hard LP 8 kHz on bass-bleed band: strip low-bass contamination from demucs
    - Aggressive transient restore: tight attack (0.8ms) for industrial kick snap
    - Hat air +3 dB above 10 kHz: crisp techno hats cut through the mix
    - De-mud −5 dB at 150–600 Hz: wider band (was 180-520 Hz) to kill full mud range
    - Sub-kick boost +4 dB at 55-105 Hz: increased from +2 dB — sub being stolen by bass stem
    - Transient gate (sustain suppress): attenuate sustained drum resonance that bleeds into "other"
    """
    from scipy.signal import sosfilt, butter
    nyq = sr / 2

    audio64 = audio.astype(np.float64)

    # 1. HP 30 Hz — preserve kick sub, remove rumble
    sos_hp = butter(4, 30.0 / nyq, "high", output="sos")
    audio64 = np.stack([sosfilt(sos_hp, audio64[:, 0]),
                        sosfilt(sos_hp, audio64[:, 1])], axis=1)

    # 2. Transient restore — tight attack for techno snap
    kick_boost = 1.3 + (energy / 10.0) * 0.45
    audio64 = _transient_enhance(audio64, sr,
                                 attack_ms=0.8, release_ms=35.0,
                                 boost=kick_boost)

    # 3. Sub-kick boost +4 dB at 55-105 Hz (kick sub-body stolen by bass stem — needs stronger recovery)
    sos_kick_hp = butter(2, 55.0 / nyq,  "high", output="sos")
    sos_kick_lp = butter(2, min(105.0 / nyq, 0.99), "low",  output="sos")
    kick_L = sosfilt(sos_kick_lp, sosfilt(sos_kick_hp, audio64[:, 0]))
    kick_R = sosfilt(sos_kick_lp, sosfilt(sos_kick_hp, audio64[:, 1]))
    kick_gain = 10.0 ** (4.0 / 20.0) - 1.0
    audio64[:, 0] += kick_L * kick_gain
    audio64[:, 1] += kick_R * kick_gain

    # 4. Hat air +3 dB above 10 kHz
    sos_air = butter(1, 10000.0 / nyq, "high", output="sos")
    air = np.stack([sosfilt(sos_air, audio64[:, 0]),
                    sosfilt(sos_air, audio64[:, 1])], axis=1)
    audio64 = audio64 + air * (10.0 ** (3.0 / 20.0) - 1.0)

    # 5. De-mud −5 dB at 150–600 Hz (wider than before — kills boxy resonance across the full mud range)
    sos_mud_hp = butter(2, 150.0 / nyq, "high", output="sos")
    sos_mud_lp = butter(2, 600.0 / nyq, "low",  output="sos")
    mud_L = sosfilt(sos_mud_lp, sosfilt(sos_mud_hp, audio64[:, 0]))
    mud_R = sosfilt(sos_mud_lp, sosfilt(sos_mud_hp, audio64[:, 1]))
    audio64[:, 0] += mud_L * (10.0 ** (-5.0 / 20.0) - 1.0)
    audio64[:, 1] += mud_R * (10.0 ** (-5.0 / 20.0) - 1.0)

    # 6. Transient gate — suppress sustained drum resonance that bleeds into "other".
    #    Invert the transient detector: attenuate the NON-transient (sustained) portion.
    #    Fast env / slow env > 1 → transient → keep; < 1 → sustain/bleed → reduce.
    sustain_suppress = 0.35   # attenuate sustained tail by ~9 dB
    for ch in range(2):
        sig   = audio64[:, ch]
        env_f = _rms_envelope(sig, 1.0,  sr)   # 1ms  fast — captures transients
        env_s = _rms_envelope(sig, 60.0, sr)   # 60ms slow — captures sustained body
        # When fast/slow < 1 (sustain region), blend in attenuation
        ratio = np.where(env_s > 1e-6, env_f / env_s, 1.0)
        # gate_gain: 1.0 during transients, sustain_suppress during sustained tails
        gate_gain = np.clip(ratio, sustain_suppress, 1.0)
        audio64[:, ch] = sig * gate_gain

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
    - High-cut at 900 Hz (tightened from 1200 Hz) — preserves 303 squeal (400-900 Hz)
      while reducing synth mid bleed measured at 300-1200 Hz in T-1000 test tracks
    - Sub-bass mono-sum below 120 Hz (professional mastering practice)
    - Sub boost +3 dB below 60 Hz for Detroit kick-bass punch
    """
    from scipy.signal import sosfilt, butter
    nyq = sr / 2

    audio64 = audio.astype(np.float64)

    # 1. High-cut at 900 Hz — tighter than 1200 Hz; preserves 303 squeal (400-900 Hz)
    #    while reducing upper-mid bleed (300-1200 Hz synth content measured in bass stem)
    sos_lp = butter(6, min(900.0 / nyq, 0.99), "low", output="sos")
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
    Other / synths — dance/techno tuning:
    - Hard HP 150 Hz (10th-order): steeper than 120 Hz 8th-order — drums→synths bleed 0.71-0.91
      is not just spectral, it is rhythmic/transient content; the steeper slope helps but the
      transient suppressor below is the primary fix
    - Transient suppressor (de-transient): subtract alpha * transient_component to kill
      drum hits that leaked through demucs. Targets fast-attack events (< 5ms) that are
      drum transients, not synth attacks.
    - Presence boost +2 dB at 2–6 kHz: synth leads cut through the mix
    - Air boost +1.5 dB above 12 kHz: open up pads and arpeggios
    """
    from scipy.signal import sosfilt, butter
    nyq = sr / 2

    audio64 = audio.astype(np.float64)

    # Hard HP 150 Hz — 10th-order (steeper roll-off than 8th-order at 120 Hz)
    sos_hp = butter(10, min(150.0 / nyq, 0.99), "high", output="sos")
    audio64 = np.stack([sosfilt(sos_hp, audio64[:, 0]),
                        sosfilt(sos_hp, audio64[:, 1])], axis=1)

    # Transient suppressor — remove leaked drum hits from the other stem.
    # Drum bleed is rhythmic/transient (attack < 5ms), not spectral bleed.
    # Detect transient component = fast_env - slow_env; subtract alpha * that component.
    # alpha=0.3: conservative enough not to eat synth attacks (which are also fast).
    alpha = 0.3
    for ch in range(2):
        sig       = audio64[:, ch]
        env_fast  = _rms_envelope(sig, 2.0,  sr)   # 2ms  — captures drum transients
        env_slow  = _rms_envelope(sig, 80.0, sr)   # 80ms — captures synth sustain
        # transient_component: positive where fast > slow (i.e. a sharp hit arrived)
        transient_component = np.maximum(env_fast - env_slow, 0.0)
        # Normalised suppression mask: scale to signal amplitude, then subtract
        total_env = env_fast + 1e-9
        suppress_mask = alpha * (transient_component / total_env)
        suppress_mask = np.clip(suppress_mask, 0.0, 0.8)   # never mute more than 80%
        audio64[:, ch] = sig * (1.0 - suppress_mask)

    # Presence boost +2 dB at 2–6 kHz (synth leads / arp clarity)
    sos_p_hp = butter(2, 2000.0 / nyq, "high", output="sos")
    sos_p_lp = butter(2, min(6000.0 / nyq, 0.99), "low",  output="sos")
    pres_L = sosfilt(sos_p_lp, sosfilt(sos_p_hp, audio64[:, 0]))
    pres_R = sosfilt(sos_p_lp, sosfilt(sos_p_hp, audio64[:, 1]))
    audio64[:, 0] += pres_L * (10.0 ** (2.0 / 20.0) - 1.0)
    audio64[:, 1] += pres_R * (10.0 ** (2.0 / 20.0) - 1.0)

    # Air boost +1.5 dB above 12 kHz (opens pads, arpeggios)
    sos_air = butter(1, min(12000.0 / nyq, 0.99), "high", output="sos")
    air_L = sosfilt(sos_air, audio64[:, 0])
    air_R = sosfilt(sos_air, audio64[:, 1])
    audio64[:, 0] += air_L * (10.0 ** (1.5 / 20.0) - 1.0)
    audio64[:, 1] += air_R * (10.0 ** (1.5 / 20.0) - 1.0)

    return audio64.astype(np.float32)


def _process_vocals(audio: np.ndarray, sr: int) -> np.ndarray:
    """
    Vocals — dance/techno tuning:
    Techno vocals are SHORT SAMPLES, not sustained vocal performances.
    Hard gate at -20 dBFS (more aggressive than -24 dBFS) with fast 3ms/10ms attack/release
    so samples snap cleanly. Tight frequency band — sub measured at 0.518 in T-1000 tests,
    meaning the old 100 Hz HP was insufficient; 180 Hz eliminates the sub/bass contamination.
    1. HP 180 Hz (techno vocal samples/stabs have no useful content below 180 Hz)
    2. LP 10 kHz (techno vocal samples rarely have useful content above)
    3. Presence +3 dB at 1.5–4 kHz (forward, cut through a loud mix)
    4. Gate at −20 dBFS, 3ms attack / 10ms release — more aggressive, tighter snap
    """
    from scipy.signal import sosfilt, butter, lfilter
    nyq = sr / 2
    audio64 = audio.astype(np.float64)

    # 1. HP 180 Hz — eliminates sub/bass contamination in techno vocal stabs
    sos_hp = butter(4, min(180.0 / nyq, 0.99), "high", output="sos")
    audio64 = np.stack([sosfilt(sos_hp, audio64[:, 0]),
                        sosfilt(sos_hp, audio64[:, 1])], axis=1)

    # 2. LP 10 kHz
    sos_lp = butter(2, min(10000.0 / nyq, 0.99), "low", output="sos")
    audio64 = np.stack([sosfilt(sos_lp, audio64[:, 0]),
                        sosfilt(sos_lp, audio64[:, 1])], axis=1)

    # 3. Presence +3 dB at 1.5–4 kHz (cut through a loud techno mix)
    sos_p_hp = butter(2, 1500.0 / nyq, "high", output="sos")
    sos_p_lp = butter(2, min(4000.0 / nyq, 0.99), "low",  output="sos")
    pres_L = sosfilt(sos_p_lp, sosfilt(sos_p_hp, audio64[:, 0]))
    pres_R = sosfilt(sos_p_lp, sosfilt(sos_p_hp, audio64[:, 1]))
    audio64[:, 0] += pres_L * (10.0 ** (3.0 / 20.0) - 1.0)
    audio64[:, 1] += pres_R * (10.0 ** (3.0 / 20.0) - 1.0)

    # 4. Gate at −20 dBFS, 3ms attack / 10ms release — more aggressive for short stabs
    gate_threshold = 10.0 ** (-20.0 / 20.0)
    window = max(1, int(sr * 0.003))   # 3ms RMS window
    rms_env = np.sqrt(np.convolve(audio64[:, 0] ** 2,
                                  np.ones(window) / window, mode="same"))
    atk = 1.0 - np.exp(-1.0 / max(sr * 0.003, 1.0))   # 3ms attack
    rel = 1.0 - np.exp(-1.0 / max(sr * 0.010, 1.0))   # 10ms release
    gate_raw = (rms_env > gate_threshold).astype(np.float64)
    # Asymmetric gate: fast attack, faster-than-old release
    gate = np.zeros_like(gate_raw)
    g = 0.0
    for i, target in enumerate(gate_raw):
        a = atk if target > g else rel
        g = g + a * (target - g)
        gate[i] = g
    gate = np.clip(gate, 0.0, 1.0)

    audio64[:, 0] *= gate
    audio64[:, 1] *= gate

    return audio64.astype(np.float32)


def _roformer_vocals(file_path: str, target_sr: int, n_samples: int) -> "np.ndarray | None":
    """
    Run MelBand Roformer Kim InstVoc Duality V2 to get cleaner vocals than demucs.
    This model is 2024 state-of-the-art for vocal isolation — trained specifically
    for clean extraction of vocals (including short samples/stabs used in techno).
    Falls back to None if audio-separator is not available.
    """
    import tempfile, logging
    import soundfile as sf
    try:
        from audio_separator.separator import Separator
        print("[Stems] running MelBand Roformer for vocals (state-of-art isolation)")
        with tempfile.TemporaryDirectory() as tmp:
            sep = Separator(
                output_dir=tmp,
                output_format="WAV",
                log_level=logging.WARNING,
            )
            # MelBand Roformer Kim InstVoc Duality V2 — best publicly available vocal model
            sep.load_model("MelBand Roformer Kim | InstVoc Duality V2 by Unwa")
            out_files = sep.separate(file_path)
            # Find the vocals file (Duality outputs Vocals + Instrumental)
            vocals_file = next(
                (f for f in out_files if "Vocals" in f or "vocals" in f), None)
            if vocals_file is None:
                print("[Stems] Roformer: no vocals file in output")
                return None
            audio, sr = sf.read(vocals_file, dtype="float32", always_2d=True)
            if audio.shape[1] == 1:
                audio = np.repeat(audio, 2, axis=1)
            if sr != target_sr:
                from math import gcd
                from scipy.signal import resample_poly
                g = gcd(sr, target_sr)
                audio = resample_poly(audio, target_sr // g, sr // g).astype(np.float32)
            # Trim to match demucs length
            audio = audio[:n_samples]
            if len(audio) < n_samples:
                audio = np.pad(audio, ((0, n_samples - len(audio)), (0, 0)))
            return np.ascontiguousarray(audio.astype(np.float32))
    except Exception as e:
        print(f"[Stems] Roformer vocals failed ({e}) — using demucs vocals")
        return None


def _subtract_drum_bleed_from_vocals(vocals: np.ndarray, drums: np.ndarray,
                                      sr: int) -> np.ndarray:
    """
    Remove hi-hat and cymbal bleed from the vocals stem.

    Demucs consistently leaks high-frequency drum content (hi-hats, cymbals,
    snare transients) into vocals. For techno vocals (short samples, stabs)
    this is especially noticeable — the vocal stem sounds like a drum mix.

    Approach: spectral subtraction of the drums stem above 3 kHz.
    - Filter both stems with a high-pass at 3 kHz (the hat/cymbal range)
    - Subtract alpha × drums_hi from vocals_hi
    - Reconstruct: vocals_lo (below 3kHz, untouched) + corrected_hi

    alpha=0.6 — aggressive but avoids over-subtraction artefacts.
    Uses Wiener-style soft floor: never subtracts more than the vocals signal
    itself to avoid phase inversion clicks.
    """
    from scipy.signal import sosfilt, butter
    nyq = sr / 2
    xover = min(3000.0 / nyq, 0.99)

    sos_hp = butter(8, xover, "high", output="sos")
    sos_lp = butter(8, xover, "low",  output="sos")

    voc64  = vocals.astype(np.float64)
    drm64  = drums.astype(np.float64)

    # Split both stems at 3 kHz
    voc_lo = np.stack([sosfilt(sos_lp, voc64[:, 0]),
                       sosfilt(sos_lp, voc64[:, 1])], axis=1)
    voc_hi = np.stack([sosfilt(sos_hp, voc64[:, 0]),
                       sosfilt(sos_hp, voc64[:, 1])], axis=1)
    drm_hi = np.stack([sosfilt(sos_hp, drm64[:, 0]),
                       sosfilt(sos_hp, drm64[:, 1])], axis=1)

    # Spectral subtraction with soft floor (never go below zero energy)
    alpha = 0.6
    corrected_hi = voc_hi - alpha * drm_hi
    # Soft floor: clip to preserve sign but prevent over-subtraction
    corrected_hi = np.where(
        np.abs(corrected_hi) < np.abs(voc_hi) * 0.1,
        voc_hi * 0.1,   # floor at 10% of original to avoid total silence artefacts
        corrected_hi)

    result = (voc_lo + corrected_hi).astype(np.float32)
    print(f"[Stems] drum bleed subtracted from vocals (alpha={alpha}, xover=3kHz)")
    return result


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
