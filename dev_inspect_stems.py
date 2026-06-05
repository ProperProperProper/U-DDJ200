#!/usr/bin/env python3
"""
dev_inspect_stems.py — Dev tool: inspect stem quality and update stems.py

Usage:
    python3 dev_inspect_stems.py <track_path> [<track_path2> ...]

What it does:
1. Loads or separates stems for the given track(s)
2. Captures waveform metrics (frequency band energies, cross-contamination scores)
3. Generates spectrogram images of all stems
4. Sends everything to Claude (claude-sonnet-4-6) for analysis
5. Claude identifies issues specific to techno/dance/house/trance
6. Prints concrete findings + recommended code changes
7. Asks if you want to apply the parameter updates to stems.py automatically

Requires:
    ANTHROPIC_API_KEY env var
    pip install anthropic matplotlib demucs torch torchaudio soundfile scipy numpy
"""

import os
import sys
import io
import json
import base64
import pathlib
import textwrap
import numpy as np

# ── Load stems from cache or separate live ────────────────────────────────────

def load_or_separate(track_path: str, sample_rate: int = 44100) -> tuple[dict, np.ndarray]:
    """Load stems from WAV cache, .npz cache, or run demucs if nothing cached."""
    import soundfile as sf

    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from udj.core.stems import (
        _cache_path, _load_cache, _separate_demucs, _save_cache,
        _split_other_to_guitar_piano,
    )

    stems = None

    # 1. Try WAV cache first (stem_analyzer — fastest)
    try:
        from stem_analyzer import load_cached_stems, is_cached
        if is_cached(track_path):
            stems = load_cached_stems(track_path, sample_rate)
            if stems:
                print(f"  Loaded from WAV cache")
    except Exception as e:
        print(f"  WAV cache unavailable: {e}")

    # 2. Try .npz cache
    if stems is None:
        cache = _cache_path(track_path)
        if cache.exists():
            stems = _load_cache(cache, sample_rate)
            if stems:
                print(f"  Loaded from .npz cache: {cache.name}")

    # 3. Run demucs
    if stems is None:
        print(f"  No cache — running demucs (shifts=4, takes a few minutes)...")
        stems = _separate_demucs(track_path, sample_rate, track_meta=None)
        _save_cache(stems, _cache_path(track_path))

    # Pop metadata flags
    stems.pop("_vocal_present", None)

    # DSP-derive guitar/piano if not present
    if "guitar" not in stems and "other" in stems:
        stems["guitar"], stems["piano"] = _split_other_to_guitar_piano(
            stems["other"], sample_rate)

    # Load original
    orig, orig_sr = sf.read(track_path, dtype="float32", always_2d=True)
    if orig.shape[1] == 1:
        orig = np.repeat(orig, 2, axis=1)
    if orig_sr != sample_rate:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(orig_sr, sample_rate)
        orig = resample_poly(orig, sample_rate // g, orig_sr // g).astype(np.float32)

    n = min(len(orig), min(len(v) for v in stems.values() if isinstance(v, np.ndarray)))
    orig = orig[:n]
    stems = {k: v[:n] for k, v in stems.items() if isinstance(v, np.ndarray)}

    return stems, orig


# ── Metrics ───────────────────────────────────────────────────────────────────

def rms(a: np.ndarray) -> float:
    return float(np.sqrt(np.mean(a.astype(np.float64) ** 2)) + 1e-9)


def band_energy_ratio(audio: np.ndarray, sr: int, lo: float, hi: float) -> float:
    from scipy.signal import sosfilt, butter
    nyq = sr / 2
    lo_n = max(lo / nyq, 0.001)
    hi_n = min(hi / nyq, 0.999)
    if lo_n >= hi_n:
        return 0.0
    sos = butter(4, [lo_n, hi_n], "bandpass", output="sos")
    mono = audio[:, 0].astype(np.float64)
    band_rms = float(np.sqrt(np.mean(sosfilt(sos, mono) ** 2)) + 1e-9)
    return round(band_rms / rms(audio), 4)


def cross_contamination(source: np.ndarray, target: np.ndarray) -> float:
    window, hop = 4096, 1024
    n = min(len(source), len(target))
    src_env = np.array([rms(source[i:i+window]) for i in range(0, n - window, hop)])
    tgt_env = np.array([rms(target[i:i+window]) for i in range(0, n - window, hop)])
    if src_env.std() < 1e-9 or tgt_env.std() < 1e-9:
        return 0.0
    return round(float(max(0.0, np.corrcoef(src_env, tgt_env)[0, 1])), 3)


def compute_metrics(stems: dict, orig: np.ndarray, sr: int, track_name: str) -> dict:
    stem_order = [s for s in ["drums", "bass", "other", "vocals", "guitar", "piano"]
                  if s in stems]
    orig_rms = rms(orig)
    stem_metrics = {}
    for name in stem_order:
        a = stems[name]
        level_db = round(20.0 * np.log10(max(rms(a) / orig_rms, 1e-9)), 1)
        stem_metrics[name] = {
            "level_db_vs_original": level_db,
            "sub_20_80hz":    band_energy_ratio(a, sr, 20,   80),
            "bass_80_300hz":  band_energy_ratio(a, sr, 80,  300),
            "low_mid_300_1k": band_energy_ratio(a, sr, 300, 1000),
            "mid_1k_4k":      band_energy_ratio(a, sr, 1000,4000),
            "hi_4k_16k":      band_energy_ratio(a, sr, 4000,16000),
        }

    contamination = {}
    pairs = [
        ("bass",  "drums",  "bass leaking into drums"),
        ("drums", "bass",   "drums leaking into bass"),
        ("drums", "other",  "drums leaking into synths"),
        ("bass",  "other",  "bass leaking into synths"),
        ("bass",  "vocals", "bass leaking into vocals"),
        ("drums", "vocals", "drums leaking into vocals"),
    ]
    for src, tgt, label in pairs:
        if src in stems and tgt in stems:
            contamination[label] = cross_contamination(stems[src], stems[tgt])

    return {
        "track": track_name,
        "stems": stem_metrics,
        "cross_contamination_0_to_1": contamination,
    }


# ── Spectrograms ──────────────────────────────────────────────────────────────

def make_spectrogram(stems: dict, sr: int, track_name: str) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from scipy.signal import spectrogram as scipy_spec

    order = [s for s in ["drums", "bass", "other", "vocals", "guitar", "piano"]
             if s in stems]
    colors = {"drums": "#ff4466", "bass": "#ff9900", "other": "#44aaff",
              "vocals": "#aa44ff", "guitar": "#44ff88", "piano": "#ff88cc"}

    n = len(order)
    fig, axes = plt.subplots(2, n, figsize=(3.5 * n, 5))
    fig.patch.set_facecolor("#0a0a14")
    fig.suptitle(f"Stems — {track_name}", color="#aaa", fontsize=8)

    for col, name in enumerate(order):
        mono = stems[name][:sr * 90, 0].astype(np.float64)   # first 90s

        # Top: spectrogram 0-8kHz
        f, t, Sxx = scipy_spec(mono, fs=sr, nperseg=2048, noverlap=1536)
        mask = f <= 8000
        Sxx_db = 10 * np.log10(np.maximum(Sxx[mask], 1e-10))
        ax_top = axes[0, col] if n > 1 else axes[0]
        ax_top.pcolormesh(t, f[mask], Sxx_db, shading="gouraud",
                          cmap="magma", vmin=-80, vmax=0)
        ax_top.set_title(name.upper(), color=colors.get(name, "#fff"),
                         fontsize=8, fontweight="bold")
        ax_top.set_facecolor("#0a0a14")
        ax_top.tick_params(colors="#444", labelsize=5)
        if col == 0:
            ax_top.set_ylabel("Hz", color="#555", fontsize=6)

        # Bottom: RMS envelope over time
        hop = sr // 10   # 100ms hop
        env = np.array([rms(stems[name][i:i+hop]) for i in range(0, len(stems[name]) - hop, hop)])
        t_env = np.arange(len(env)) * 0.1
        ax_bot = axes[1, col] if n > 1 else axes[1]
        ax_bot.plot(t_env[:len(t_env[:900])], env[:900],
                    color=colors.get(name, "#fff"), linewidth=0.6)
        ax_bot.set_facecolor("#0a0a14")
        ax_bot.tick_params(colors="#444", labelsize=5)
        ax_bot.set_ylim(0, None)
        if col == 0:
            ax_bot.set_ylabel("RMS", color="#555", fontsize=6)
        ax_bot.set_xlabel("s", color="#555", fontsize=5)

    plt.tight_layout(pad=0.4)
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=110, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ── Claude analysis ───────────────────────────────────────────────────────────

SYSTEM = """You are a senior audio DSP engineer specialising in electronic music stem separation — techno, Detroit techno, house, trance, dance.

You are reviewing demucs htdemucs_ft output for a DJ application. Your job is to:
1. Identify specific problems visible in the spectrograms and metrics
2. Diagnose root causes (bleed, over-processing, gate artifacts, etc.)
3. Recommend exact parameter changes to the Python post-processing code in stems.py

The post-processing parameters you can recommend changes to:

DRUMS:
  hp_hz (currently 30) — HP cutoff to remove rumble, keep kick sub-body
  sub_kick_boost_hz_lo/hi (55/105) — sub-kick boost band
  sub_kick_boost_db (currently 2.0)
  de_mud_hz_lo/hi (180/520) — de-mud notch band
  de_mud_db (currently -5.0)
  hat_air_hz (10000) — hat shelf frequency
  hat_air_db (currently 3.0)
  transient_attack_ms (0.8)
  transient_boost (1.3 + energy×0.45)

BASS:
  lp_hz (currently 1200) — high-cut, keeps 303 acid squeal
  sub_boost_hz (20-60) — sub boost band
  sub_boost_db (currently 3.0)
  mono_below_hz (120)

OTHER/SYNTHS:
  hp_hz (currently 120) — high-pass to strip bass bleed
  hp_order (currently 8) — filter steepness
  presence_hz_lo/hi (2000/6000)
  presence_db (currently 2.0)
  air_hz (12000)
  air_db (currently 1.5)

VOCALS (techno = short samples, stabs):
  hp_hz (currently 100)
  lp_hz (currently 10000)
  gate_threshold_dbfs (currently -24)
  gate_attack_ms (5)
  gate_release_ms (15)
  presence_hz_lo/hi (1500/4000)
  presence_db (currently 3.0)

Be specific. Reference what you see in the spectrogram. Give exact numbers.
Format your response as:

## Findings
[What you see wrong, stem by stem]

## Root Causes
[Why each problem is happening]

## Recommended Code Changes
[Exact parameter values to change in stems.py, with reasoning]"""


def analyse_with_claude(all_metrics: list[dict], all_specs: list[bytes]) -> str:
    import anthropic

    client = anthropic.Anthropic()

    content = []
    for i, (metrics, spec_png) in enumerate(zip(all_metrics, all_specs)):
        content.append({
            "type": "text",
            "text": f"### Track {i+1}: {metrics['track']}\nMetrics:\n```json\n{json.dumps(metrics, indent=2)}\n```"
        })
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.standard_b64encode(spec_png).decode(),
            }
        })

    content.append({
        "type": "text",
        "text": f"Analysed {len(all_metrics)} track(s). Genre: techno/house/dance/trance. Identify issues and give exact parameter recommendations for stems.py."
    })

    print("\n  Sending to Claude for analysis...")
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text


# ── Apply changes to stems.py ─────────────────────────────────────────────────

def prompt_apply(analysis: str):
    """Show analysis and offer to apply changes interactively."""
    print("\n" + "═" * 70)
    print(analysis)
    print("═" * 70)

    print("\nDo you want to apply these changes to stems.py?")
    print("  y = yes, apply automatically")
    print("  n = no, I'll do it manually")
    choice = input("  Choice [y/n]: ").strip().lower()

    if choice != "y":
        print("No changes made. Review the analysis above and edit stems.py manually.")
        return

    stems_path = pathlib.Path(__file__).parent / "udj" / "core" / "stems.py"
    print(f"\nParsing recommendations and updating {stems_path}...")

    # Second Claude call: extract exact sed-style replacements
    import anthropic
    client = anthropic.Anthropic()

    stems_src = stems_path.read_text()

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": f"""Based on this analysis:

{analysis}

Here is the current stems.py source:

```python
{stems_src}
```

Apply the recommended parameter changes. Return ONLY the complete updated stems.py source code with the changes applied. No explanation, no markdown fences, just the raw Python source."""
        }]
    )

    new_source = resp.content[0].text.strip()
    # Strip markdown fences if present
    if new_source.startswith("```"):
        lines = new_source.split("\n")
        new_source = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

    # Sanity check — must still be valid Python
    try:
        compile(new_source, "stems.py", "exec")
    except SyntaxError as e:
        print(f"  ERROR: Claude returned invalid Python ({e}). No changes made.")
        backup = stems_path.with_suffix(".py.bak")
        print(f"  Original unchanged. Check the analysis and edit manually.")
        return

    backup = stems_path.with_suffix(".py.bak")
    stems_path.rename(backup)
    stems_path.write_text(new_source)
    print(f"  ✓ stems.py updated (backup at {backup.name})")
    print("  Run: python3 -m PyInstaller udj.spec --clean -y && cp -R dist/U-DJ.app ~/Desktop/U-DJ.app")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    track_paths = sys.argv[1:]
    sr = 44100

    all_metrics = []
    all_specs   = []

    for path in track_paths:
        path = str(pathlib.Path(path).expanduser().resolve())
        name = pathlib.Path(path).stem
        print(f"\n{'─'*60}")
        print(f"Inspecting: {name}")

        stems, orig = load_or_separate(path, sr)
        print(f"  Stems loaded: {list(stems.keys())}")

        print("  Computing metrics...")
        metrics = compute_metrics(stems, orig, sr, name)
        all_metrics.append(metrics)

        print("  Generating spectrograms...")
        spec_png = make_spectrogram(stems, sr, name)
        all_specs.append(spec_png)

        print("  Metrics:")
        for stem_name, m in metrics["stems"].items():
            print(f"    {stem_name:8s}: {m['level_db_vs_original']:+.1f}dB  "
                  f"sub={m['sub_20_80hz']:.3f}  bass={m['bass_80_300hz']:.3f}  "
                  f"mid={m['low_mid_300_1k']:.3f}  hi={m['hi_4k_16k']:.3f}")
        print("  Cross-contamination:")
        for label, score in metrics["cross_contamination_0_to_1"].items():
            flag = " ⚠" if score > 0.4 else ""
            print(f"    {label}: {score:.3f}{flag}")

    analysis = analyse_with_claude(all_metrics, all_specs)
    prompt_apply(analysis)


if __name__ == "__main__":
    main()
