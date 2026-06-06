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

    # guitar/piano split removed — app uses 4-stem model only (Serato style)

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


# ── Local DSP analysis ────────────────────────────────────────────────────────

# Thresholds derived from htdemucs_ft expected output on electronic music
_CONTAMINATION_WARN  = 0.35   # bleed correlation above this = problem
_CONTAMINATION_BAD   = 0.55
_LEVEL_SILENT_DB     = -30.0  # stem probably empty / over-gated
_LEVEL_HOT_DB        = -3.0   # stem dominating mix unusually

_STEM_EXPECTED_BANDS = {
    # name: {band: (min_ratio, max_ratio)}  — expected energy distribution
    "drums":  {"sub_20_80hz": (0.02, 0.30), "hi_4k_16k": (0.05, 0.50)},
    "bass":   {"sub_20_80hz": (0.05, 0.60), "hi_4k_16k": (0.0,  0.10)},
    "other":  {"sub_20_80hz": (0.0,  0.08), "mid_1k_4k": (0.05, 0.60)},
    "vocals": {"sub_20_80hz": (0.0,  0.06), "mid_1k_4k": (0.05, 0.70)},
}

_STEMS_PARAMS = {
    "drums":  {"hp_hz": 30, "sub_kick_boost_db": 2.0, "de_mud_db": -5.0,
               "hat_air_db": 3.0, "transient_boost": "1.3 + energy×0.45"},
    "bass":   {"lp_hz": 1200, "sub_boost_db": 3.0, "mono_below_hz": 120},
    "other":  {"hp_hz": 120, "hp_order": 8, "presence_db": 2.0, "air_db": 1.5},
    "vocals": {"hp_hz": 100, "lp_hz": 10000, "gate_threshold_dbfs": -24,
               "presence_db": 3.0},
}


def _flag(score: float, warn: float, bad: float) -> str:
    if score >= bad:  return "  ✖ BAD"
    if score >= warn: return "  ⚠ WARN"
    return "  ✔"


def analyse_stems(all_metrics: list[dict], all_specs: list[bytes]) -> str:
    lines = []

    for metrics in all_metrics:
        track  = metrics["track"]
        stems  = metrics["stems"]
        contam = metrics["cross_contamination_0_to_1"]

        lines.append(f"{'═'*60}")
        lines.append(f"TRACK: {track}")
        lines.append("")

        # ── Per-stem level & frequency analysis ──────────────────────────────
        lines.append("STEM LEVELS & FREQUENCY CONTENT")
        lines.append("─" * 40)
        findings = []
        recommendations = []

        for stem_name, m in stems.items():
            lvl = m["level_db_vs_original"]
            status = ""
            if lvl < _LEVEL_SILENT_DB:
                status = f"  ✖ SILENT ({lvl:+.1f} dB) — possible over-gating or failed separation"
                if stem_name == "vocals":
                    recommendations.append(f"  VOCALS: gate_threshold_dbfs → -30 or lower (currently -24)")
            elif lvl > _LEVEL_HOT_DB:
                status = f"  ⚠ HOT ({lvl:+.1f} dB) — bleed inflating level"
            else:
                status = f"  ✔ {lvl:+.1f} dB"

            lines.append(f"  {stem_name.upper():8s} {status}")

            expected = _STEM_EXPECTED_BANDS.get(stem_name, {})
            for band, (lo, hi) in expected.items():
                val = m.get(band, 0.0)
                if val > hi:
                    issue = f"    {band}: {val:.3f} — TOO HIGH (expected ≤{hi:.2f})"
                    findings.append(f"{stem_name.upper()} {issue}")
                    if stem_name == "bass" and band == "hi_4k_16k":
                        recommendations.append(f"  BASS: lp_hz → 800–1000 (currently 1200) — high bleed in bass stem")
                    elif stem_name == "other" and band == "sub_20_80hz":
                        recommendations.append(f"  OTHER: hp_hz → 140–160 (currently 120) — sub leaking into synths")
                    elif stem_name == "drums" and band == "sub_20_80hz" and val > 0.35:
                        recommendations.append(f"  DRUMS: hp_hz → 40–50 (currently 30) — too much sub in drums")
                elif val < lo and lo > 0.0:
                    issue = f"    {band}: {val:.3f} — LOW (expected ≥{lo:.2f})"
                    findings.append(f"{stem_name.upper()} {issue}")
                    if stem_name == "drums" and band == "hi_4k_16k":
                        recommendations.append(f"  DRUMS: hat_air_db → 4.5–6.0 (currently 3.0) — hats lacking air")
                    elif stem_name == "bass" and band == "sub_20_80hz":
                        recommendations.append(f"  BASS: sub_boost_db → 4.5–6.0 (currently 3.0) — sub weak")

        lines.append("")

        # ── Cross-contamination ───────────────────────────────────────────────
        lines.append("CROSS-CONTAMINATION  (0=clean, 1=identical)")
        lines.append("─" * 40)
        for label, score in contam.items():
            flag = _flag(score, _CONTAMINATION_WARN, _CONTAMINATION_BAD)
            lines.append(f"  {label:<35s} {score:.3f}{flag}")
            if score >= _CONTAMINATION_WARN:
                src, tgt = label.split(" leaking into ")
                if src == "bass" and tgt == "other":
                    recommendations.append(
                        f"  OTHER: hp_order → 10–12 (currently 8) — bass still bleeding into synths (corr={score:.2f})")
                elif src == "drums" and tgt == "bass":
                    recommendations.append(
                        f"  BASS: add de-drum notch 55–80 Hz or raise hp_hz — kick transients leaking (corr={score:.2f})")
                elif src == "drums" and tgt == "other":
                    recommendations.append(
                        f"  OTHER: transient suppressor threshold → lower — drum hits leaking into synths (corr={score:.2f})")
                elif src == "drums" and tgt == "vocals":
                    recommendations.append(
                        f"  VOCALS: gate_threshold_dbfs → -20 (currently -24) — hats leaking (corr={score:.2f})")
                elif src == "bass" and tgt == "vocals":
                    recommendations.append(
                        f"  VOCALS: hp_hz → 150–200 (currently 100) — bass leaking into vocals (corr={score:.2f})")

        lines.append("")

        # ── Summary ───────────────────────────────────────────────────────────
        n_bad  = sum(1 for s in contam.values() if s >= _CONTAMINATION_BAD)
        n_warn = sum(1 for s in contam.values() if _CONTAMINATION_WARN <= s < _CONTAMINATION_BAD)
        n_findings = len(findings)

        lines.append("SUMMARY")
        lines.append("─" * 40)
        if n_bad == 0 and n_warn == 0 and n_findings == 0:
            lines.append("  ✔ Stems look clean — no significant bleed or frequency anomalies detected.")
        else:
            lines.append(f"  {n_bad} critical bleed(s),  {n_warn} warning(s),  {n_findings} frequency anomal(ies)")

        if findings:
            lines.append("")
            lines.append("FREQUENCY ANOMALIES")
            lines.append("─" * 40)
            for f in findings:
                lines.append(f"  {f}")

        if recommendations:
            lines.append("")
            lines.append("RECOMMENDED PARAMETER CHANGES (stems.py)")
            lines.append("─" * 40)
            for r in sorted(set(recommendations)):
                lines.append(r)
        else:
            lines.append("")
            lines.append("No parameter changes needed.")

        lines.append("")

    return "\n".join(lines)


# Keep old name as alias so the worker in library_widget.py needs no change
analyse_with_claude = analyse_stems


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
