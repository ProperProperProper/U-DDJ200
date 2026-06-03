"""
stem_analyzer.py — Offline stem precomputation for U-DJ.

Separates audio into vocals/drums/bass/other using demucs htdemucs_ft
(Apple MPS → CUDA → CPU) and caches results as WAV files.

Cache layout:
    udj_cache/<track_hash>/
        vocals.wav
        drums.wav
        bass.wav
        other.wav
        meta.json

First import: slow (demucs, ~90 s/track on MPS).
Second load:  instant — no AI, just WAV reads.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import soundfile as sf

# ── constants ─────────────────────────────────────────────────────────────────

CACHE_ROOT = Path.home() / "udj_cache"
AUDIO_EXTS = {".mp3", ".flac", ".wav", ".aiff", ".aif", ".ogg", ".m4a", ".opus"}
STEM_NAMES = ["vocals", "drums", "bass", "other"]
CACHE_VERSION = "1"   # bump to invalidate all caches

# ── StemTrack ─────────────────────────────────────────────────────────────────


@dataclass
class StemTrack:
    vocals_path: str
    drums_path:  str
    bass_path:   str
    other_path:  str
    sample_rate: int
    duration:    float
    track_hash:  str

    def as_arrays(self) -> dict[str, np.ndarray]:
        """Load all stems into memory as float32 arrays. Shape: (N, 2)."""
        return load_cached_stems(self.vocals_path.replace("vocals.wav", "").rstrip("/\\"))


# ── hash ──────────────────────────────────────────────────────────────────────


def _track_hash(path: str) -> str:
    """Deterministic hash from file path + size + mtime."""
    p = Path(path)
    stat = p.stat()
    key = f"{p.resolve()}|{stat.st_size}|{stat.st_mtime}"
    return hashlib.sha1(key.encode()).hexdigest()[:20]


def _cache_dir(track_hash: str) -> Path:
    return CACHE_ROOT / track_hash


# ── device selection ──────────────────────────────────────────────────────────


def _best_device() -> str:
    try:
        import torch
        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except ImportError:
        pass
    return "cpu"


# ── demucs separation ─────────────────────────────────────────────────────────


def _run_demucs(path: str, cache_dir: Path, sample_rate: int) -> dict[str, np.ndarray]:
    """
    Run demucs htdemucs_ft and return {stem_name: ndarray(N,2) float32}.
    Raises on failure — caller handles fallback.
    """
    import torch
    import torchaudio
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    device = _best_device()
    print(f"[StemAnalyzer] demucs device={device}  file={Path(path).name}")

    try:
        model = get_model("htdemucs_ft")
        print("[StemAnalyzer] model=htdemucs_ft")
    except Exception:
        model = get_model("htdemucs")
        print("[StemAnalyzer] model=htdemucs (ft unavailable)")
    model.eval()

    wav, sr = torchaudio.load(path)
    if wav.shape[0] == 1:
        wav = wav.repeat(2, 1)
    wav = wav[:2]

    if sr != model.samplerate:
        wav = torchaudio.functional.resample(wav, sr, model.samplerate)

    t0 = time.time()
    with torch.no_grad():
        sources = apply_model(
            model, wav.unsqueeze(0),
            device=device, shifts=1, overlap=0.5, progress=True,
        )[0]   # (n_stems, 2, N)
    print(f"[StemAnalyzer] demucs done in {time.time()-t0:.1f}s")

    stems: dict[str, np.ndarray] = {}
    for i, name in enumerate(model.sources):
        stem_wav = sources[i]   # (2, N) tensor
        if model.samplerate != sample_rate:
            stem_wav = torchaudio.functional.resample(
                stem_wav, model.samplerate, sample_rate)
        arr = stem_wav.cpu().numpy().T.astype(np.float32)   # (N, 2)
        # map demucs source names → our canonical names
        mapped = {"other": "other", "drums": "drums",
                  "bass": "bass",   "vocals": "vocals"}.get(name, name)
        stems[mapped] = np.ascontiguousarray(arr)

    return stems


# ── DSP fallback ──────────────────────────────────────────────────────────────


def _run_dsp(path: str, sample_rate: int) -> dict[str, np.ndarray]:
    """Simple DSP stem approximation when demucs is unavailable."""
    from scipy.signal import butter, sosfilt

    audio, sr = sf.read(path, dtype="float32", always_2d=True)
    if audio.shape[1] == 1:
        audio = np.repeat(audio, 2, axis=1)
    if sr != sample_rate:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(sr, sample_rate)
        audio = resample_poly(audio, sample_rate // g, sr // g, axis=0).astype(np.float32)

    def _filt(data, lo=None, hi=None, order=4):
        nyq = sample_rate / 2
        if lo and hi:
            sos = butter(order, [lo / nyq, hi / nyq], btype="band", output="sos")
        elif lo:
            sos = butter(order, lo / nyq, btype="high", output="sos")
        else:
            sos = butter(order, hi / nyq, btype="low", output="sos")
        return sosfilt(sos, data, axis=0).astype(np.float32)

    mid  = ((audio[:, 0] + audio[:, 1]) / 2)[:, None].repeat(2, axis=1)
    side = ((audio[:, 0] - audio[:, 1]) / 2)[:, None].repeat(2, axis=1)

    return {
        "vocals": np.ascontiguousarray(side * 1.5),
        "drums":  np.ascontiguousarray(_filt(audio, lo=200, hi=8000)),
        "bass":   np.ascontiguousarray(_filt(audio, hi=250)),
        "other":  np.ascontiguousarray(_filt(mid, lo=800)),
    }


# ── cache I/O ─────────────────────────────────────────────────────────────────


def _write_cache(stems: dict[str, np.ndarray], cache_dir: Path,
                 sample_rate: int, duration: float, track_hash: str):
    cache_dir.mkdir(parents=True, exist_ok=True)
    for name in STEM_NAMES:
        arr = stems.get(name, np.zeros((int(duration * sample_rate), 2), dtype=np.float32))
        sf.write(str(cache_dir / f"{name}.wav"), arr, sample_rate, subtype="FLOAT")
    meta = {
        "version":     CACHE_VERSION,
        "track_hash":  track_hash,
        "sample_rate": sample_rate,
        "duration":    duration,
        "stems":       STEM_NAMES,
    }
    (cache_dir / "meta.json").write_text(json.dumps(meta, indent=2))


def _read_meta(cache_dir: Path) -> Optional[dict]:
    meta_path = cache_dir / "meta.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text())
        if meta.get("version") != CACHE_VERSION:
            return None   # stale — re-analyse
        for name in STEM_NAMES:
            if not (cache_dir / f"{name}.wav").exists():
                return None
        return meta
    except Exception:
        return None


# ── public API ────────────────────────────────────────────────────────────────


def analyze_track(
    path: str,
    sample_rate: int = 44100,
    on_progress: Optional[Callable[[str], None]] = None,
    force: bool = False,
) -> StemTrack:
    """
    Separate a track into stems and write to cache.

    Blocking — run in a thread for non-UI use.
    Returns a StemTrack with paths to the cached WAV files.
    """
    path = str(Path(path).resolve())
    h = _track_hash(path)
    cdir = _cache_dir(h)

    if not force:
        meta = _read_meta(cdir)
        if meta:
            print(f"[StemAnalyzer] cache hit: {Path(path).name}")
            return StemTrack(
                vocals_path=str(cdir / "vocals.wav"),
                drums_path =str(cdir / "drums.wav"),
                bass_path  =str(cdir / "bass.wav"),
                other_path =str(cdir / "other.wav"),
                sample_rate=meta["sample_rate"],
                duration   =meta["duration"],
                track_hash =h,
            )

    if on_progress:
        on_progress(f"Analysing {Path(path).name}…")

    # Measure duration
    info = sf.info(path)
    duration = info.duration

    print(f"[StemAnalyzer] analysing {Path(path).name}  ({duration:.1f}s)")

    # Try demucs first, DSP fallback on any failure
    try:
        stems = _run_demucs(path, cdir, sample_rate)
        method = "demucs"
    except Exception as e:
        print(f"[StemAnalyzer] demucs failed ({e}) — using DSP fallback")
        stems = _run_dsp(path, sample_rate)
        method = "dsp"

    _write_cache(stems, cdir, sample_rate, duration, h)

    if on_progress:
        on_progress(f"Done ({method}): {Path(path).name}")

    print(f"[StemAnalyzer] cached → {cdir}")
    return StemTrack(
        vocals_path=str(cdir / "vocals.wav"),
        drums_path =str(cdir / "drums.wav"),
        bass_path  =str(cdir / "bass.wav"),
        other_path =str(cdir / "other.wav"),
        sample_rate=sample_rate,
        duration   =duration,
        track_hash =h,
    )


def load_cached_stems(track_path: str, sample_rate: int = 44100) -> Optional[dict[str, np.ndarray]]:
    """
    Load pre-computed stems from WAV cache. NO AI — pure file reads.

    Returns {stem_name: ndarray(N,2) float32} or None if not cached.
    """
    path = str(Path(track_path).resolve())
    h = _track_hash(path)
    cdir = _cache_dir(h)

    meta = _read_meta(cdir)
    if meta is None:
        return None

    result: dict[str, np.ndarray] = {}
    for name in STEM_NAMES:
        wav_path = cdir / f"{name}.wav"
        arr, sr = sf.read(str(wav_path), dtype="float32", always_2d=True)
        if sr != sample_rate:
            from math import gcd
            from scipy.signal import resample_poly
            g = gcd(sr, sample_rate)
            arr = resample_poly(arr, sample_rate // g, sr // g, axis=0).astype(np.float32)
        result[name] = arr

    return result


def is_cached(track_path: str) -> bool:
    """Return True if stems for this track are already cached."""
    try:
        path = str(Path(track_path).resolve())
        h = _track_hash(path)
        return _read_meta(_cache_dir(h)) is not None
    except Exception:
        return False


# ── batch processing ──────────────────────────────────────────────────────────


def batch_analyze(
    folder: str,
    sample_rate: int = 44100,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    on_done: Optional[Callable[[], None]] = None,
    force: bool = False,
) -> list[StemTrack]:
    """
    Scan folder recursively and analyse all audio files into stem cache.

    Processes one track at a time (demucs saturates MPS — no parallelism gain).
    Skips already-cached tracks unless force=True.

    on_progress(done, total, track_name) called after each track.
    on_done() called when all tracks finish.
    Returns list of StemTrack for completed tracks.
    """
    folder_path = Path(folder)
    paths = [
        str(p) for p in folder_path.rglob("*")
        if p.suffix.lower() in AUDIO_EXTS
    ]

    if not force:
        paths = [p for p in paths if not is_cached(p)]

    total = len(paths)
    print(f"[StemAnalyzer] batch: {total} tracks to process in {folder}")

    if total == 0:
        if on_done:
            on_done()
        return []

    results: list[StemTrack] = []
    done = [0]

    def _worker():
        for path in paths:
            try:
                def _prog(msg, p=path):
                    if on_progress:
                        on_progress(done[0], total, Path(p).name)

                st = analyze_track(path, sample_rate=sample_rate,
                                   on_progress=_prog, force=force)
                results.append(st)
            except Exception as e:
                print(f"[StemAnalyzer] failed {Path(path).name}: {e}")
            finally:
                done[0] += 1
                if on_progress:
                    on_progress(done[0], total, Path(path).name)

        if on_done:
            on_done()

    t = threading.Thread(target=_worker, daemon=True, name="stem-batch")
    t.start()
    return results   # returns immediately; results populate as worker runs


def batch_analyze_sync(
    folder: str,
    sample_rate: int = 44100,
    force: bool = False,
) -> list[StemTrack]:
    """Blocking version of batch_analyze — for scripts."""
    done_event = threading.Event()
    results: list[StemTrack] = []

    def _prog(done, total, name):
        pct = int(done / total * 100) if total else 100
        print(f"[{pct:3d}%] {done}/{total}  {name}")

    def _done():
        done_event.set()

    # Run synchronously — submit each track directly
    folder_path = Path(folder)
    paths = [
        str(p) for p in folder_path.rglob("*")
        if p.suffix.lower() in AUDIO_EXTS
    ]
    if not force:
        paths = [p for p in paths if not is_cached(p)]

    total = len(paths)
    print(f"[StemAnalyzer] {total} tracks to process")

    for i, path in enumerate(paths, 1):
        print(f"\n[{i}/{total}] {Path(path).name}")
        try:
            st = analyze_track(path, sample_rate=sample_rate, force=force)
            results.append(st)
            print(f"  → cached: {st.track_hash}")
        except Exception as e:
            print(f"  → FAILED: {e}")

    return results
