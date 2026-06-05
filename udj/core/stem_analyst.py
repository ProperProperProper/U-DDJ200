"""
stem_analyst.py — Claude-powered stem quality analysis.

After demucs separation, captures waveform metrics from all 6 stems and
sends spectrogram images to Claude claude-sonnet-4-6 for analysis. Claude identifies
bleed, gate issues, frequency contamination, and returns concrete parameter
adjustments. Results feed back into the post-processing chain.

Usage:
    from .stem_analyst import analyse_stems_with_claude
    adjustments = analyse_stems_with_claude(raw_stems, orig, sample_rate, track_meta)
"""
from __future__ import annotations

import io
import base64
import json
import numpy as np
from typing import Optional


# ── Metrics extraction ────────────────────────────────────────────────────────

def _rms(a: np.ndarray) -> float:
    return float(np.sqrt(np.mean(a.astype(np.float64) ** 2)) + 1e-9)


def _band_energy(audio: np.ndarray, sr: int, lo: float, hi: float) -> float:
    """Fraction of total energy in frequency band [lo, hi] Hz."""
    from scipy.signal import sosfilt, butter
    nyq = sr / 2
    lo_n = max(lo / nyq, 0.001)
    hi_n = min(hi / nyq, 0.999)
    if lo_n >= hi_n:
        return 0.0
    sos = butter(4, [lo_n, hi_n], "bandpass", output="sos")
    mono = audio[:, 0].astype(np.float64)
    band = sosfilt(sos, mono)
    total_rms = _rms(audio)
    band_rms  = float(np.sqrt(np.mean(band ** 2)) + 1e-9)
    return band_rms / total_rms


def _cross_contamination(source: np.ndarray, target: np.ndarray) -> float:
    """
    Estimate how much of 'source' leaks into 'target'.
    Uses normalised cross-correlation on the RMS envelopes.
    Returns 0.0 (clean) … 1.0 (complete bleed).
    """
    window = 2048
    hop    = 512
    n = min(len(source), len(target))
    src_env = np.array([_rms(source[i:i+window]) for i in range(0, n-window, hop)])
    tgt_env = np.array([_rms(target[i:i+window]) for i in range(0, n-window, hop)])
    if src_env.std() < 1e-9 or tgt_env.std() < 1e-9:
        return 0.0
    corr = float(np.corrcoef(src_env, tgt_env)[0, 1])
    return max(0.0, corr)


def _compute_metrics(stems: dict[str, np.ndarray], orig: np.ndarray,
                     sr: int) -> dict:
    """Compute quality metrics for all stems."""
    stem_names = [s for s in ["drums", "bass", "other", "vocals", "guitar", "piano"]
                  if s in stems]
    metrics = {}
    orig_rms = _rms(orig)

    for name in stem_names:
        a = stems[name]
        rms   = _rms(a)
        level = 20.0 * np.log10(max(rms / orig_rms, 1e-9))   # dB vs original

        metrics[name] = {
            "level_db_vs_orig": round(level, 1),
            "sub_energy":   round(_band_energy(a, sr, 20,   80),  3),
            "bass_energy":  round(_band_energy(a, sr, 80,   300), 3),
            "mid_energy":   round(_band_energy(a, sr, 300,  3000),3),
            "hi_energy":    round(_band_energy(a, sr, 3000, 16000),3),
        }

    # Cross-contamination pairs (most critical for techno)
    pairs = [
        ("bass",   "drums",  "bass→drums bleed"),
        ("drums",  "bass",   "drums→bass bleed"),
        ("drums",  "other",  "drums→synths bleed"),
        ("bass",   "other",  "bass→synths bleed"),
        ("drums",  "vocals", "drums→vocals bleed"),
    ]
    contamination = {}
    for src, tgt, label in pairs:
        if src in stems and tgt in stems:
            contamination[label] = round(_cross_contamination(stems[src], stems[tgt]), 3)

    return {"stems": metrics, "cross_contamination": contamination}


# ── Spectrogram image ─────────────────────────────────────────────────────────

def _spectrogram_image(stems: dict[str, np.ndarray], sr: int) -> bytes:
    """
    Generate a mel-spectrogram image for all stems side by side.
    Returns PNG bytes suitable for Claude vision.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec
    from scipy.signal import spectrogram as sp_spectrogram

    stem_order = [s for s in ["drums", "bass", "other", "vocals", "guitar", "piano"]
                  if s in stems]
    colours = {
        "drums":  "#ff4466", "bass": "#ff9900", "other": "#44aaff",
        "vocals": "#aa44ff", "guitar": "#44ff88", "piano": "#ff88cc",
    }

    n = len(stem_order)
    fig, axes = plt.subplots(1, n, figsize=(4 * n, 3))
    fig.patch.set_facecolor("#0a0a14")
    if n == 1:
        axes = [axes]

    for ax, name in zip(axes, stem_order):
        mono = stems[name][:, 0].astype(np.float64)
        # Limit to first 60s for speed
        mono = mono[:sr * 60]
        f, t, Sxx = sp_spectrogram(mono, fs=sr, nperseg=2048, noverlap=1536,
                                   scaling="spectrum")
        # Only show 0–8000 Hz (relevant for techno analysis)
        mask = f <= 8000
        Sxx_db = 10 * np.log10(np.maximum(Sxx[mask], 1e-10))
        ax.pcolormesh(t, f[mask], Sxx_db, shading="gouraud",
                      cmap="magma", vmin=-80, vmax=0)
        ax.set_title(name.upper(), color=colours.get(name, "#fff"),
                     fontsize=9, fontweight="bold")
        ax.set_facecolor("#0a0a14")
        ax.tick_params(colors="#555", labelsize=6)
        ax.set_xlabel("time (s)", color="#555", fontsize=6)
        ax.set_ylabel("Hz", color="#555", fontsize=6) if name == stem_order[0] else None

    plt.suptitle("Stem Spectrograms — Techno/Dance Analysis",
                 color="#888", fontsize=8)
    plt.tight_layout(pad=0.5)

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=100, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ── Claude API call ───────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are an expert audio engineer specialising in electronic music stem separation quality analysis — specifically techno, Detroit techno, house, trance, and dance music. You analyse demucs ML stem separation output and provide concrete, actionable post-processing parameter adjustments.

You will receive:
1. A spectrogram image showing all separated stems
2. JSON metrics: per-stem frequency band energies and cross-contamination scores

Your job: identify problems with the separation for this genre and return ONLY a JSON object with parameter adjustments. No prose outside the JSON.

Parameter keys you can adjust:
{
  "drums": {
    "hp_hz": <int 20-80>,              // high-pass freq to strip rumble
    "sub_kick_boost_db": <float 0-6>,  // boost at 60-100Hz for kick body
    "de_mud_hz_lo": <int 150-250>,     // de-mud band low edge
    "de_mud_hz_hi": <int 400-600>,     // de-mud band high edge
    "de_mud_db": <float -2 to -8>,     // de-mud cut amount (negative)
    "hat_air_hz": <int 8000-14000>,    // hat air shelf frequency
    "hat_air_db": <float 0-5>,         // hat air boost amount
    "transient_attack_ms": <float 0.5-3.0>,
    "transient_boost": <float 1.0-2.5>
  },
  "bass": {
    "lp_hz": <int 800-2000>,           // high-cut to strip bleed
    "sub_boost_db": <float 0-5>,       // sub boost <60Hz
    "mono_below_hz": <int 80-160>      // mono-sum crossover
  },
  "other": {
    "hp_order": <int 4-12>,            // high-pass filter order (steepness)
    "hp_hz": <int 80-200>,             // high-pass to strip bass bleed
    "presence_hz_lo": <int 1500-3000>,
    "presence_hz_hi": <int 4000-8000>,
    "presence_db": <float 0-4>,
    "air_hz": <int 10000-16000>,
    "air_db": <float 0-3>
  },
  "vocals": {
    "hp_hz": <int 80-150>,
    "lp_hz": <int 8000-14000>,
    "gate_threshold_dbfs": <float -40 to -18>,  // tighter = more aggressive
    "gate_attack_ms": <float 2-20>,
    "gate_release_ms": <float 5-50>,
    "presence_hz_lo": <int 1000-2500>,
    "presence_hz_hi": <int 3000-6000>,
    "presence_db": <float 0-5>
  }
}

Base decisions on: cross-contamination scores (0=clean, 1=heavy bleed), band energies (are wrong frequencies present?), and visual spectrogram patterns. For techno: drums and bass isolation are the highest priority. Vocals are short samples — gate aggressively. Synths need clean bass-bleed removal."""

_USER_PROMPT = """Analyse these stem spectrograms and metrics from a techno/dance track separation.

Metrics:
{metrics_json}

Return ONLY a JSON object with the parameter adjustments needed. Focus on what is actually wrong — do not change values that are already correct."""


def analyse_stems_with_claude(
    raw_stems: dict[str, np.ndarray],
    orig: np.ndarray,
    sr: int,
    track_meta: Optional[dict] = None,
) -> Optional[dict]:
    """
    Send stem waveform data to Claude for analysis.
    Returns a dict of post-processing parameter adjustments, or None on failure.
    """
    try:
        import anthropic
    except ImportError:
        print("[StemAnalyst] anthropic SDK not installed — skipping Claude analysis")
        return None

    try:
        metrics = _compute_metrics(raw_stems, orig, sr)
        print(f"[StemAnalyst] metrics: {json.dumps(metrics, indent=2)}")

        spec_png  = _spectrogram_image(raw_stems, sr)
        spec_b64  = base64.standard_b64encode(spec_png).decode()

        client = anthropic.Anthropic()   # uses ANTHROPIC_API_KEY env var
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": spec_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": _USER_PROMPT.format(
                            metrics_json=json.dumps(metrics, indent=2)
                        ),
                    },
                ],
            }],
        )

        raw_text = response.content[0].text.strip()
        print(f"[StemAnalyst] Claude response:\n{raw_text}")

        # Extract JSON from response (may be wrapped in ```json ... ```)
        if "```" in raw_text:
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        adjustments = json.loads(raw_text.strip())
        print(f"[StemAnalyst] adjustments: {adjustments}")
        return adjustments

    except Exception as e:
        print(f"[StemAnalyst] Claude analysis failed: {e}")
        return None


# ── Apply adjustments to post-processing ─────────────────────────────────────

def apply_adjustments(raw_stems: dict[str, np.ndarray], sr: int,
                      adj: dict) -> dict[str, np.ndarray]:
    """
    Apply Claude-recommended parameter adjustments to the raw stems.
    Returns processed stems dict.
    """
    from scipy.signal import sosfilt, butter
    result = {}

    def _hp(a, hz, order=4):
        nyq = sr / 2
        sos = butter(order, min(hz / nyq, 0.99), "high", output="sos")
        return np.stack([sosfilt(sos, a[:, 0].astype(np.float64)),
                         sosfilt(sos, a[:, 1].astype(np.float64))], axis=1)

    def _lp(a, hz, order=4):
        nyq = sr / 2
        sos = butter(order, min(hz / nyq, 0.99), "low", output="sos")
        return np.stack([sosfilt(sos, a[:, 0].astype(np.float64)),
                         sosfilt(sos, a[:, 1].astype(np.float64))], axis=1)

    def _boost_band(a, lo_hz, hi_hz, db, order=2):
        nyq = sr / 2
        lo_n = min(lo_hz / nyq, 0.99)
        hi_n = min(hi_hz / nyq, 0.99)
        if lo_n >= hi_n:
            return a
        sos_hp = butter(order, lo_n, "high", output="sos")
        sos_lp = butter(order, hi_n, "low",  output="sos")
        a64 = a.astype(np.float64)
        band_L = sosfilt(sos_lp, sosfilt(sos_hp, a64[:, 0]))
        band_R = sosfilt(sos_lp, sosfilt(sos_hp, a64[:, 1]))
        gain = 10.0 ** (db / 20.0) - 1.0
        out = a64.copy()
        out[:, 0] += band_L * gain
        out[:, 1] += band_R * gain
        return out.astype(np.float32)

    # ── DRUMS ──
    if "drums" in raw_stems:
        d = adj.get("drums", {})
        a = raw_stems["drums"].astype(np.float32)
        a = _hp(a, d.get("hp_hz", 30)).astype(np.float32)
        sub_db = d.get("sub_kick_boost_db", 2.0)
        if sub_db > 0:
            a = _boost_band(a, 55, 105, sub_db)
        mud_lo = d.get("de_mud_hz_lo", 180)
        mud_hi = d.get("de_mud_hz_hi", 520)
        mud_db = d.get("de_mud_db", -5.0)
        a = _boost_band(a, mud_lo, mud_hi, mud_db)
        hat_hz = d.get("hat_air_hz", 10000)
        hat_db = d.get("hat_air_db", 3.0)
        a = _boost_band(a, hat_hz, 20000, hat_db)
        result["drums"] = a

    # ── BASS ──
    if "bass" in raw_stems:
        d = adj.get("bass", {})
        a = raw_stems["bass"].astype(np.float32)
        a = _lp(a, d.get("lp_hz", 1200), order=6).astype(np.float32)
        sub_db = d.get("sub_boost_db", 3.0)
        if sub_db > 0:
            a = _boost_band(a, 20, 60, sub_db)
        # Mono below crossover
        mono_hz = d.get("mono_below_hz", 120)
        nyq = sr / 2
        sos_mono = butter(4, min(mono_hz / nyq, 0.99), "low", output="sos")
        a64 = a.astype(np.float64)
        lo_L = sosfilt(sos_mono, a64[:, 0])
        lo_R = sosfilt(sos_mono, a64[:, 1])
        mono = (lo_L + lo_R) * 0.5
        a64[:, 0] = a64[:, 0] - lo_L + mono
        a64[:, 1] = a64[:, 1] - lo_R + mono
        result["bass"] = a64.astype(np.float32)

    # ── OTHER / SYNTHS ──
    if "other" in raw_stems:
        d = adj.get("other", {})
        a = raw_stems["other"].astype(np.float32)
        a = _hp(a, d.get("hp_hz", 120),
                order=d.get("hp_order", 8)).astype(np.float32)
        pres_lo = d.get("presence_hz_lo", 2000)
        pres_hi = d.get("presence_hz_hi", 6000)
        pres_db = d.get("presence_db", 2.0)
        a = _boost_band(a, pres_lo, pres_hi, pres_db)
        air_hz = d.get("air_hz", 12000)
        air_db = d.get("air_db", 1.5)
        a = _boost_band(a, air_hz, 20000, air_db)
        result["other"] = a

    # ── VOCALS ──
    if "vocals" in raw_stems:
        d  = adj.get("vocals", {})
        a  = raw_stems["vocals"].astype(np.float32)
        a  = _hp(a, d.get("hp_hz", 100)).astype(np.float32)
        a  = _lp(a, d.get("lp_hz", 10000)).astype(np.float32)
        pres_lo = d.get("presence_hz_lo", 1500)
        pres_hi = d.get("presence_hz_hi", 4000)
        pres_db = d.get("presence_db", 3.0)
        a = _boost_band(a, pres_lo, pres_hi, pres_db)
        # Gate
        gate_thr = d.get("gate_threshold_dbfs", -24.0)
        gate_atk = d.get("gate_attack_ms",  5.0)
        gate_rel = d.get("gate_release_ms", 15.0)
        threshold_linear = 10.0 ** (gate_thr / 20.0)
        window = max(1, int(sr * gate_atk / 1000.0))
        a64 = a.astype(np.float64)
        rms_env = np.sqrt(np.convolve(a64[:, 0] ** 2,
                                      np.ones(window) / window, mode="same"))
        atk_c = 1.0 - np.exp(-1.0 / max(sr * gate_atk  / 1000.0, 1.0))
        rel_c = 1.0 - np.exp(-1.0 / max(sr * gate_rel  / 1000.0, 1.0))
        gate_raw = (rms_env > threshold_linear).astype(np.float64)
        gate = np.zeros_like(gate_raw)
        g = 0.0
        for i, target in enumerate(gate_raw):
            c = atk_c if target > g else rel_c
            g = g + c * (target - g)
            gate[i] = g
        gate = np.clip(gate, 0.0, 1.0)
        a64[:, 0] *= gate
        a64[:, 1] *= gate
        result["vocals"] = a64.astype(np.float32)

    # Pass through stems not adjusted
    for name, arr in raw_stems.items():
        if name not in result:
            result[name] = arr

    return result
