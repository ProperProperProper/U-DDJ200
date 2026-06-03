"""
stem_example.py — Usage examples for stem_analyzer.py
"""
import sys
import numpy as np
from pathlib import Path
from stem_analyzer import analyze_track, load_cached_stems, batch_analyze_sync, is_cached, StemTrack

SAMPLE_RATE = 44100

# ── Example 1: Process a single track ─────────────────────────────────────────

def example_single(path: str):
    print(f"\n{'='*60}")
    print(f"EXAMPLE 1: Single track")
    print(f"  file: {Path(path).name}")
    print(f"  cached already: {is_cached(path)}")
    print(f"{'='*60}")

    def progress(msg):
        print(f"  → {msg}")

    stem_track: StemTrack = analyze_track(path, sample_rate=SAMPLE_RATE, on_progress=progress)

    print(f"\nResult:")
    print(f"  hash:        {stem_track.track_hash}")
    print(f"  sample_rate: {stem_track.sample_rate} Hz")
    print(f"  duration:    {stem_track.duration:.2f}s")
    print(f"  vocals:      {stem_track.vocals_path}")
    print(f"  drums:       {stem_track.drums_path}")
    print(f"  bass:        {stem_track.bass_path}")
    print(f"  other:       {stem_track.other_path}")


# ── Example 2: Process a folder ────────────────────────────────────────────────

def example_batch(folder: str):
    print(f"\n{'='*60}")
    print(f"EXAMPLE 2: Batch folder")
    print(f"  folder: {folder}")
    print(f"{'='*60}\n")

    results = batch_analyze_sync(folder, sample_rate=SAMPLE_RATE)

    print(f"\nBatch complete: {len(results)} tracks processed")
    for st in results:
        print(f"  [{st.track_hash}]  {st.duration:.1f}s  {Path(st.vocals_path).parent.name}")


# ── Example 3: Load cached stems (no AI) ──────────────────────────────────────

def example_load(path: str):
    print(f"\n{'='*60}")
    print(f"EXAMPLE 3: Load cached stems (no AI)")
    print(f"  file: {Path(path).name}")
    print(f"{'='*60}")

    if not is_cached(path):
        print("  Not cached yet — run example_single first")
        return

    import time
    t0 = time.time()
    stems = load_cached_stems(path, sample_rate=SAMPLE_RATE)
    elapsed = (time.time() - t0) * 1000

    if stems is None:
        print("  Load failed")
        return

    print(f"  Loaded in {elapsed:.1f}ms (no AI)\n")
    for name, arr in stems.items():
        rms = float(np.sqrt(np.mean(arr**2)))
        print(f"  {name:8s}  shape={arr.shape}  dtype={arr.dtype}  rms={rms:.4f}")


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Default to U-DJ test tracks
    DEFAULT_TRACK  = str(Path.home() / "Downloads/WAVES/DJ T-1000 (aka Alan Oldham) - I Told Em I Was from Detroit (Original Mix).wav")
    DEFAULT_FOLDER = str(Path.home() / "Downloads/WAVES")

    track  = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TRACK
    folder = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_FOLDER

    example_single(track)
    example_load(track)
    example_batch(folder)
