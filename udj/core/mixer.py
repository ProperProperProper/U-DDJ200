"""
Mixer — clean minimal signal path.
Volume faders, constant-power crossfader, soft limiter.
EQ uses simple per-band gain (no IIR in the hot path for now).
"""
from __future__ import annotations

import numpy as np
from scipy.signal import sosfilt, butter


def soft_limit(x: np.ndarray, threshold: float = 0.85) -> np.ndarray:
    """Wider knee (0.15) gives gentler saturation so two-deck sums don't squeal."""
    out  = x.copy()
    over = np.abs(x) > threshold
    if over.any():
        knee = 1.0 - threshold
        sign = np.sign(x[over])
        out[over] = sign * (threshold + knee * np.tanh((np.abs(x[over]) - threshold) / knee))
    return out.astype(np.float32)


class BiquadEQ:
    """
    3-band EQ — 8th-order Linkwitz-Riley crossovers, independent bands.

    Topology: each band is filtered from the RAW input independently.
    - lo  = LR8 LP  at 200 Hz
    - mid = LR8 HP  at 200 Hz → LR8 LP at 3500 Hz  (true bandpass)
    - hi  = LR8 HP  at 3500 Hz

    LR8 = 4 cascaded 2nd-order Butterworth sections.
    Isolation at 1 octave from crossover:  ≈ −48 dB
    (LR4 gave −24 dB;  LR8 halves that in dB = squares in linear)
    At 50 Hz (kick sub-bass) in the mid band: −98.6 dB — inaudible.

    Gain curve — dB-linear taper, asymmetric (matches professional desks):
      v = −1.0  →  true kill (0 linear)
      v = −0.5  →  −13 dB
      v =  0.0  →   0 dB  (unity, knob centre)
      v = +0.5  →  +5 dB
      v = +1.0  →  +10 dB  (Pioneer DJM-900NXS2 max boost)
    """

    LO_FC = 200.0    # Hz  lo/mid crossover
    HI_FC = 3500.0   # Hz  mid/hi  crossover

    def __init__(self, sample_rate: int = 44100):
        self.sr   = sample_rate
        self._low = self._mid = self._high = 0.0

        nyq = sample_rate / 2

        # True LR8 = 2 cascaded 4th-order Butterworth (each −3 dB at fc)
        # LP_LR8 + HP_LR8 = allpass  →  unity sum across full spectrum
        sos_lo4   = butter(4, self.LO_FC / nyq, "low",  output="sos")
        sos_hi4   = butter(4, self.HI_FC / nyq, "high", output="sos")
        sos_lp_m4 = butter(4, self.HI_FC / nyq, "low",  output="sos")
        sos_hp_m4 = butter(4, self.LO_FC / nyq, "high", output="sos")

        self._sos_lo = np.vstack([sos_lo4,   sos_lo4])                       # LR8 LP @200 Hz
        self._sos_hi = np.vstack([sos_hi4,   sos_hi4])                       # LR8 HP @3500 Hz
        self._sos_mi = np.vstack([sos_hp_m4, sos_hp_m4, sos_lp_m4, sos_lp_m4])  # LR8 BP 200–3500 Hz

        def _zi(sos): return np.zeros((sos.shape[0], 2), dtype=np.float64)
        self._zl_lo = _zi(self._sos_lo);  self._zr_lo = _zi(self._sos_lo)
        self._zl_hi = _zi(self._sos_hi);  self._zr_hi = _zi(self._sos_hi)
        self._zl_mi = _zi(self._sos_mi);  self._zr_mi = _zi(self._sos_mi)

    @staticmethod
    def _gain(v: float) -> float:
        """dB-linear taper — feels natural on a professional mixer."""
        if v <= -1.0: return 0.0                          # hard kill
        db = v * (26.0 if v < 0.0 else 10.0)             # −26 dB … 0 … +10 dB
        return float(10.0 ** (db / 20.0))

    @property
    def low(self):  return self._low
    @low.setter
    def low(self,  v): self._low  = max(-1.0, min(1.0, float(v)))
    @property
    def mid(self):  return self._mid
    @mid.setter
    def mid(self,  v): self._mid  = max(-1.0, min(1.0, float(v)))
    @property
    def high(self): return self._high
    @high.setter
    def high(self, v): self._high = max(-1.0, min(1.0, float(v)))

    def process(self, audio: np.ndarray) -> np.ndarray:
        """(N,2) float32 → (N,2) float32"""
        L = audio[:, 0].astype(np.float64)
        R = audio[:, 1].astype(np.float64)

        # Each band filtered independently from the RAW input — no subtraction
        lo_L, self._zl_lo = sosfilt(self._sos_lo, L, zi=self._zl_lo)
        lo_R, self._zr_lo = sosfilt(self._sos_lo, R, zi=self._zr_lo)
        hi_L, self._zl_hi = sosfilt(self._sos_hi, L, zi=self._zl_hi)
        hi_R, self._zr_hi = sosfilt(self._sos_hi, R, zi=self._zr_hi)
        mi_L, self._zl_mi = sosfilt(self._sos_mi, L, zi=self._zl_mi)
        mi_R, self._zr_mi = sosfilt(self._sos_mi, R, zi=self._zr_mi)

        gl = self._gain(self._low)
        gm = self._gain(self._mid)
        gh = self._gain(self._high)

        out_L = lo_L * gl + mi_L * gm + hi_L * gh
        out_R = lo_R * gl + mi_R * gm + hi_R * gh
        return np.stack([out_L, out_R], axis=1).astype(np.float32)


# ── FX base + professional effect suite ──────────────────────────────────────
#
# Six effects tuned for Detroit techno / sound-engineer use:
#
#   FILTER  — resonant 4-pole Moog-style sweep (the classic DJ tool)
#   DELAY   — stereo ping-pong delay with filtered feedback, beat-approximate time
#   REVERB  — Freeverb-inspired plate reverb with room size + damping
#   FLANGER — deep tape-style flanger with feedback and asymmetric LFO
#   PHASER  — 8-stage allpass phaser with slow sweep and resonance
#   DRIVE   — tube/tape saturation with second-harmonic emphasis
#
# Knob convention: centre (depth=0.5) = bypass on all effects.
# Right of centre: increasing wet/depth.
# Left  of centre: varies per effect (see docstrings).

class _Effect:
    NAME = "BYPASS"

    def __init__(self, sr: int = 44100):
        self.sr     = sr
        self._depth = 0.5

    @property
    def depth_value(self) -> float: return self._depth
    @depth_value.setter
    def depth_value(self, v: float):
        self._depth = max(0.0, min(1.0, float(v)))
        self._on_depth_changed()

    def _on_depth_changed(self): pass

    def _amount(self) -> float:
        return abs(self._depth - 0.5) / 0.5   # 0 at centre, 1 at extreme

    def process(self, audio: np.ndarray) -> np.ndarray:
        return audio


# ── helpers ───────────────────────────────────────────────────────────────────

def _resonant_biquad_sos(fc: float, Q: float, sr: int,
                          btype: str = "low") -> np.ndarray:
    """
    Audio EQ Cookbook resonant biquad (Robert Bristow-Johnson).
    Returns single SOS row for sosfilt.
    """
    w0     = 2.0 * np.pi * float(np.clip(fc, 1.0, sr / 2 - 1)) / sr
    cos_w0 = np.cos(w0)
    sin_w0 = np.sin(w0)
    alpha  = sin_w0 / (2.0 * max(Q, 0.1))
    if btype == "low":
        b0 = (1.0 - cos_w0) / 2.0
        b1 = 1.0 - cos_w0
        b2 = (1.0 - cos_w0) / 2.0
    else:                           # high
        b0 = (1.0 + cos_w0) / 2.0
        b1 = -(1.0 + cos_w0)
        b2 = (1.0 + cos_w0) / 2.0
    a0 = 1.0 + alpha
    a1 = -2.0 * cos_w0
    a2 = 1.0 - alpha
    return np.array([[b0/a0, b1/a0, b2/a0, 1.0, a1/a0, a2/a0]])


def _circ_read(buf: np.ndarray, widx: int, delay: int) -> np.ndarray:
    """Read `len(buf[0])` samples from circular buffer `delay` samples behind write head."""
    n    = buf.shape[0]
    ridx = (widx - delay + np.arange(n if n == 1 else 1)) % len(buf)
    return buf[ridx]


def _delay_read(buf: np.ndarray, widx: int, delay: int, n: int) -> np.ndarray:
    """Vectorised circular-buffer read of n samples, delay samples behind widx."""
    size = len(buf)
    ridx = (widx - delay + np.arange(n)) % size
    return buf[ridx]


def _delay_write(buf: np.ndarray, widx: int, data: np.ndarray) -> int:
    n    = len(data)
    size = len(buf)
    widx_arr = (widx + np.arange(n)) % size
    buf[widx_arr] = data
    return int((widx + n) % size)


# ── FILTER ────────────────────────────────────────────────────────────────────

class FilterEffect(_Effect):
    """
    Resonant 4-pole Moog-style sweep.

    Two cascaded resonant biquads (≈ 4-pole, 24 dB/oct) with Q=2.5 for
    pronounced resonance — classic analogue-filter feel.

    Right of centre : LPF sweep 18 kHz → 30 Hz
    Left  of centre : HPF sweep 20 Hz  → 18 kHz
    """
    NAME = "FILTER"
    _Q = 2.5   # resonance — sounds like a real Moog

    def __init__(self, sr: int = 44100):
        super().__init__(sr)
        self._sos  = None
        self._zi   = None   # shape (n_sections*2, 2) — L and R states interleaved

    def _on_depth_changed(self):
        a = self._amount()
        if a < 0.01:
            self._sos = None; return
        if self._depth > 0.5:
            fc    = max(30.0, 18000.0 * (1.0 - a) ** 3.0)
            btype = "low"
        else:
            fc    = min(18000.0, 20.0 + 17980.0 * a ** 2.5)
            btype = "high"
        s         = _resonant_biquad_sos(fc, self._Q, self.sr, btype)
        self._sos = np.vstack([s, s])   # cascade × 2 = 4-pole
        ns        = self._sos.shape[0]
        self._zi  = np.zeros((ns, 2, 2), dtype=np.float64)  # (sections, [L,R], state)

    def process(self, audio: np.ndarray) -> np.ndarray:
        a = self._amount()
        if a < 0.01 or self._sos is None:
            return audio
        wet = a ** 0.5
        out = np.empty_like(audio, dtype=np.float32)
        for ch in range(2):
            sig, self._zi[:, ch, :] = sosfilt(
                self._sos, audio[:, ch].astype(np.float64),
                zi=self._zi[:, ch, :])
            out[:, ch] = sig.astype(np.float32)
        return (audio * (1.0 - wet) + out * wet).astype(np.float32)


# ── DELAY ─────────────────────────────────────────────────────────────────────

class DelayEffect(_Effect):
    """
    Stereo ping-pong delay with analogue-warm filtered feedback.

    Delay time ≈ 375 ms (quarter-note at 128 BPM).
    Feedback path: one-pole LP at 3.5 kHz — smooths highs on each repeat,
    like tape echo. Successive repeats get progressively darker.

    Right of centre : more feedback + wet (classic echo throw)
    Left  of centre : shorter delay (1/8-note approx.), less feedback — tight slap
    """
    NAME = "DELAY"

    def __init__(self, sr: int = 44100):
        super().__init__(sr)
        size         = int(sr * 0.75) + 64
        self._buf    = np.zeros((size, 2), dtype=np.float64)
        self._widx   = 0
        # Feedback LP state (3.5 kHz one-pole)
        fc_fb         = 3500.0 / (sr / 2)
        self._fb_coef = float(np.exp(-2.0 * np.pi * fc_fb))
        self._fb_z    = np.zeros(2, dtype=np.float64)   # [L, R]

    def process(self, audio: np.ndarray) -> np.ndarray:
        a = self._amount()
        if a < 0.01:
            return audio
        n        = len(audio)
        if self._depth >= 0.5:
            D        = int(self.sr * 0.375)   # 375 ms
            feedback = 0.30 + a * 0.55        # 0.30 → 0.85
            wet      = 0.25 + a * 0.55
        else:
            D        = int(self.sr * 0.1875)  # 187.5 ms (1/8-note)
            feedback = a * 0.55
            wet      = 0.15 + a * 0.35
        D    = max(1, min(D, len(self._buf) - n - 1))
        size = len(self._buf)

        # Ping-pong: L reads from L-buf, R reads from R-buf offset by D/2
        d_L = D
        d_R = max(1, D // 2)

        ridx_L = (self._widx - d_L + np.arange(n)) % size
        ridx_R = (self._widx - d_R + np.arange(n)) % size
        delayed = np.stack([self._buf[ridx_L, 0], self._buf[ridx_R, 1]], axis=1)

        # Filtered feedback
        c = self._fb_coef
        fb_in = delayed * feedback
        fb_out = np.empty_like(fb_in)
        for ch in range(2):
            z = self._fb_z[ch]
            for i in range(n):
                z = c * z + (1.0 - c) * fb_in[i, ch]
                fb_out[i, ch] = z
            self._fb_z[ch] = z

        widx_arr = (self._widx + np.arange(n)) % size
        self._buf[widx_arr] = audio.astype(np.float64) + fb_out
        self._widx = int((self._widx + n) % size)

        return np.clip(audio.astype(np.float64) + delayed * wet,
                       -1.0, 1.0).astype(np.float32)


# ── REVERB ────────────────────────────────────────────────────────────────────

class ReverbEffect(_Effect):
    """
    Plate / hall reverb — Freeverb-inspired algorithm.

    4 feedback comb filters per channel with damping + 2 allpass diffusers.
    Gives a dense, smooth reverb tail — studio plate character.

    Right of centre : longer room size → bigger reverb tail
    Left  of centre : shorter pre-delay + smaller room — tight ambience
    """
    NAME = "REVERB"

    # Freeverb comb delays (samples @44100, scaled at init)
    _COMB_D  = [1557, 1617, 1491, 1422]   # 4-comb (half of Freeverb for efficiency)
    _ALLP_D  = [225,  556]                 # 2 allpass stages

    def __init__(self, sr: int = 44100):
        super().__init__(sr)
        scale  = sr / 44100.0
        self._comb_bufs = [np.zeros(int(d * scale) + 2, dtype=np.float64)
                           for d in self._COMB_D * 2]   # × 2 for stereo
        self._comb_widx = [0] * len(self._comb_bufs)
        self._comb_filt = [0.0] * len(self._comb_bufs)  # damping filter state

        self._allp_bufs = [np.zeros(int(d * scale) + 2, dtype=np.float64)
                           for d in self._ALLP_D * 2]
        self._allp_widx = [0] * len(self._allp_bufs)

    @staticmethod
    def _comb(x: np.ndarray, buf: np.ndarray, widx: int,
              feedback: float, damp: float, filt_z: float) -> tuple:
        """Dampened feedback comb filter — vectorised over buffer."""
        n    = len(x)
        size = len(buf)
        ridx = (widx - size + 1 + np.arange(n)) % size
        delay_out = buf[ridx]

        # Damping: one-pole LP on feedback path (simulates air absorption)
        c     = 1.0 - damp
        new_z = np.empty(n)
        z     = filt_z
        for i in range(n):          # tight loop, small n (~1024)
            z = delay_out[i] * c + z * damp
            new_z[i] = z

        widx_arr = (widx + np.arange(n)) % size
        buf[widx_arr] = x + new_z * feedback
        return delay_out, (widx + n) % size, float(z)

    @staticmethod
    def _allpass(x: np.ndarray, buf: np.ndarray, widx: int,
                  g: float = 0.5) -> tuple:
        """Schroeder allpass — vectorised."""
        n    = len(x)
        size = len(buf)
        ridx = (widx - size + 1 + np.arange(n)) % size
        buf_out = buf[ridx]
        v       = x - g * buf_out
        widx_arr = (widx + np.arange(n)) % size
        buf[widx_arr] = v
        return g * v + buf_out, (widx + n) % size

    def process(self, audio: np.ndarray) -> np.ndarray:
        a = self._amount()
        if a < 0.01:
            return audio

        if self._depth >= 0.5:
            room     = 0.40 + a * 0.55   # 0.40 → 0.95
            damp     = 0.20 + a * 0.60   # 0.20 → 0.80
            wet      = 0.20 + a * 0.60
        else:
            room     = 0.30 + a * 0.40
            damp     = 0.70
            wet      = 0.10 + a * 0.30

        n     = len(audio)
        out_L = np.zeros(n, dtype=np.float64)
        out_R = np.zeros(n, dtype=np.float64)
        dry   = audio.astype(np.float64)

        # 4 comb filters: 0-3 = Left, 4-7 = Right
        nc = len(self._COMB_D)
        for i, ch_idx in enumerate(range(nc)):
            sig = (dry[:, 0] + dry[:, 1]) * 0.5  # mono input
            d_out, self._comb_widx[i], self._comb_filt[i] = self._comb(
                sig, self._comb_bufs[i], self._comb_widx[i],
                room, damp, self._comb_filt[i])
            out_L += d_out

        for i, ch_idx in enumerate(range(nc)):
            sig = (dry[:, 0] + dry[:, 1]) * 0.5
            d_out, self._comb_widx[nc + i], self._comb_filt[nc + i] = self._comb(
                sig, self._comb_bufs[nc + i], self._comb_widx[nc + i],
                room, damp * 1.05, self._comb_filt[nc + i])  # slight damping diff L/R
            out_R += d_out

        out_L /= nc; out_R /= nc

        # 2 allpass stages per channel
        na = len(self._ALLP_D)
        for i in range(na):
            out_L, self._allp_widx[i]      = self._allpass(out_L, self._allp_bufs[i],      self._allp_widx[i])
            out_R, self._allp_widx[na + i] = self._allpass(out_R, self._allp_bufs[na + i], self._allp_widx[na + i])

        reverb = np.stack([out_L, out_R], axis=1)
        return np.clip(dry * (1.0 - wet) + reverb * wet,
                       -1.0, 1.0).astype(np.float32)


# ── FLANGER ───────────────────────────────────────────────────────────────────

class FlangerEffect(_Effect):
    """
    Deep tape-style flanger with feedback.

    Delay: 0.5 ms – 15 ms with sinusoidal LFO.
    Feedback (0.7) gives the characteristic metallic resonance of tape flanging.

    Right of centre : slow LFO (0.15 Hz), deep sweep — spacious, psychedelic
    Left  of centre : fast LFO (2.5 Hz), tight sweep — jet-plane whoosh
    """
    NAME = "FLANGER"

    def __init__(self, sr: int = 44100):
        super().__init__(sr)
        size         = int(sr * 0.020) + 64
        self._buf    = np.zeros((size, 2), dtype=np.float64)
        self._widx   = 0
        self._phase  = 0.0
        self._fb_z   = np.zeros(2, dtype=np.float64)

    def process(self, audio: np.ndarray) -> np.ndarray:
        a = self._amount()
        if a < 0.01:
            return audio
        n    = len(audio)
        size = len(self._buf)

        lfo_rate = (0.15 + a * 0.35) if self._depth >= 0.5 else (0.5 + a * 2.0)
        lfo_val  = np.sin(self._phase)
        self._phase = (self._phase + 2.0 * np.pi * lfo_rate * n / self.sr) % (2.0 * np.pi)

        min_d = max(1, int(self.sr * 0.0005))    # 0.5 ms
        max_d = int(self.sr * 0.012)             # 12 ms
        delay = min_d + int((max_d - min_d) * (1.0 + lfo_val) / 2.0 * a)
        delay = max(1, min(delay, size - n - 2))

        ridx    = (self._widx - delay + np.arange(n)) % size
        delayed = self._buf[ridx].copy()

        # Feedback (0.6 × amount): gives metallic resonance
        feedback = 0.60 * a
        c = 0.85  # one-pole LP on feedback for warmth
        fb_in = delayed * feedback
        for ch in range(2):
            z = self._fb_z[ch]
            for i in range(n):
                z = c * z + (1.0 - c) * fb_in[i, ch]
                fb_in[i, ch] = z
            self._fb_z[ch] = z

        widx_arr = (self._widx + np.arange(n)) % size
        self._buf[widx_arr] = audio.astype(np.float64) + fb_in
        self._widx = int((self._widx + n) % size)

        wet = a ** 0.5
        mix = audio.astype(np.float64) + delayed * wet
        return np.clip(mix, -1.0, 1.0).astype(np.float32)


# ── PHASER ────────────────────────────────────────────────────────────────────

class PhaserEffect(_Effect):
    """
    8-stage all-pass phaser with slow, wide LFO sweep.

    8 cascaded first-order all-pass filters with LFO-modulated cutoff.
    Sweeps 60 Hz – 6 kHz for a rich, musical phase-shift effect.
    Feedback (+0.5) adds resonance and deepens the notches.

    Right of centre : very slow sweep (0.08 Hz), lush and evolving
    Left  of centre : faster sweep (1.5 Hz) — more rhythmic/tremolo feel
    """
    NAME = "PHASER"
    _N_STAGES = 8

    def __init__(self, sr: int = 44100):
        super().__init__(sr)
        self._z      = np.zeros((self._N_STAGES, 2), dtype=np.float64)
        self._phase  = 0.0
        self._fb_z   = np.zeros(2, dtype=np.float64)

    def process(self, audio: np.ndarray) -> np.ndarray:
        from scipy.signal import lfilter
        a = self._amount()
        if a < 0.01:
            return audio
        n = len(audio)

        lfo_rate = 0.08 + a * 0.42 if self._depth >= 0.5 else 0.3 + a * 1.2
        lfo_val  = np.sin(self._phase)
        self._phase = (self._phase + 2.0 * np.pi * lfo_rate * n / self.sr) % (2.0 * np.pi)

        fc_min, fc_max = 60.0, 6000.0
        fc = fc_min * np.exp(np.log(fc_max / fc_min) * (0.5 + lfo_val * a * 0.5))
        fc = float(np.clip(fc, fc_min, self.sr / 2.0 - 10.0))

        t = np.tan(np.pi * fc / self.sr)
        g = (t - 1.0) / (t + 1.0)
        b_ap, a_ap = [g, 1.0], [1.0, g]

        # Add feedback from output of last stage to input of first
        feedback = 0.45 * a
        x = audio.astype(np.float64).copy()
        x[:, 0] += self._fb_z[0] * feedback
        x[:, 1] += self._fb_z[1] * feedback

        for s in range(self._N_STAGES):
            for ch in range(2):
                zi          = np.array([self._z[s, ch]])
                out_ch, zf  = lfilter(b_ap, a_ap, x[:, ch], zi=zi)
                x[:, ch]    = out_ch
                self._z[s, ch] = float(zf[0])

        self._fb_z[0] = float(x[-1, 0])
        self._fb_z[1] = float(x[-1, 1])

        wet = a ** 0.6
        out = audio.astype(np.float64) * (1.0 - wet * 0.5) + x * (wet * 0.5)
        return np.clip(out, -1.0, 1.0).astype(np.float32)


# ── DRIVE ─────────────────────────────────────────────────────────────────────

class DriveEffect(_Effect):
    """
    Tube / tape saturation with second-harmonic emphasis.

    Asymmetric soft-clipping (tanh with even-order harmonics) followed by a
    high-pass at 30 Hz to remove DC, then a gentle presence boost at 3 kHz to
    bring back the air lost to saturation.

    Right of centre : mild to heavy tube grit (pre-gain 1× → 12×)
    Left  of centre : tape saturation — same drive but pre-gain through a
                      gentle low-pass (3 kHz) first, giving a warmer, darker tone
    """
    NAME = "DRIVE"

    def __init__(self, sr: int = 44100):
        super().__init__(sr)
        nyq           = sr / 2.0
        # DC-block HP at 30 Hz (IIR, kept across buffers)
        self._dc_sos  = butter(1, 30.0 / nyq, "high", output="sos")
        self._dc_zi   = np.zeros((self._dc_sos.shape[0], 2, 2), dtype=np.float64)
        # Presence shelf boost at 3 kHz: one-pole HP into blend
        self._pr_sos  = butter(1, 3000.0 / nyq, "high", output="sos")
        self._pr_zi   = np.zeros((self._pr_sos.shape[0], 2, 2), dtype=np.float64)
        # Warm tape LP for left-side mode
        self._tp_sos  = butter(2, 3500.0 / nyq, "low", output="sos")
        self._tp_zi   = np.zeros((self._tp_sos.shape[0], 2, 2), dtype=np.float64)

    def process(self, audio: np.ndarray) -> np.ndarray:
        a = self._amount()
        if a < 0.01:
            return audio

        pre_gain  = 1.0 + a * 11.0    # 1× → 12×
        out_gain  = 1.0 / (1.0 + a * 0.7)   # compensate perceived loudness increase
        wet       = a ** 0.5

        x = audio.astype(np.float64)

        if self._depth < 0.5:
            # Left: warm tape — pre-filter through LP for darker saturation
            for ch in range(2):
                x[:, ch], self._tp_zi[:, ch, :] = sosfilt(
                    self._tp_sos, x[:, ch], zi=self._tp_zi[:, ch, :])

        driven = x * pre_gain
        # Asymmetric soft clip: tanh for negative, softer for positive (tube feel)
        driven = np.where(driven >= 0,
                          np.tanh(driven * 0.8) / 0.8,
                          np.tanh(driven) * 1.05)
        driven *= out_gain

        # DC-block
        for ch in range(2):
            driven[:, ch], self._dc_zi[:, ch, :] = sosfilt(
                self._dc_sos, driven[:, ch], zi=self._dc_zi[:, ch, :])

        # Presence: add subtle high-shelf +2 dB at 3 kHz (restores air)
        pres = np.empty_like(driven)
        for ch in range(2):
            pres[:, ch], self._pr_zi[:, ch, :] = sosfilt(
                self._pr_sos, driven[:, ch], zi=self._pr_zi[:, ch, :])
        driven += pres * 0.25

        return np.clip(x * (1.0 - wet) + driven * wet,
                       -1.0, 1.0).astype(np.float32)


# ── Registry ──────────────────────────────────────────────────────────────────

FX_TYPES: dict[str, type] = {
    cls.NAME: cls
    for cls in [FilterEffect, DelayEffect, ReverbEffect,
                FlangerEffect, PhaserEffect, DriveEffect]
}
FX_NAMES = list(FX_TYPES.keys())


class Mixer:
    def __init__(self, num_channels: int = 2, sample_rate: int = 44100):
        self.num_channels = num_channels
        self.sample_rate  = sample_rate
        self.volumes      = [1.0] * num_channels
        self.crossfader   = 0.0
        self.master_vol   = 0.85
        self.eqs = [BiquadEQ(sample_rate) for _ in range(num_channels)]
        self.fx  = [FilterEffect(sample_rate) for _ in range(num_channels)]
        self.pfl     = [False] * num_channels
        self.ch_peak = [0.0]  * num_channels   # post-fader peak per channel

        # Mono-bass filter: LP below 200 Hz applied to master for mud prevention
        nyq = sample_rate / 2
        sos_mb = butter(4, min(200.0 / nyq, 0.99), "low", output="sos")
        self._sos_mb   = sos_mb
        self._zi_mb_L  = np.zeros((sos_mb.shape[0], 2), dtype=np.float64)
        self._zi_mb_R  = np.zeros((sos_mb.shape[0], 2), dtype=np.float64)

    def _xf_gains(self):
        cf = (self.crossfader + 1.0) / 2.0
        return (float(np.cos(cf * np.pi / 2)),
                float(np.cos((1.0 - cf) * np.pi / 2)))

    def _mono_bass(self, audio: np.ndarray) -> np.ndarray:
        """Sum bass below 200 Hz to mono — prevents two-track low-end mud."""
        L = audio[:, 0].astype(np.float64)
        R = audio[:, 1].astype(np.float64)
        lo_L, self._zi_mb_L = sosfilt(self._sos_mb, L, zi=self._zi_mb_L)
        lo_R, self._zi_mb_R = sosfilt(self._sos_mb, R, zi=self._zi_mb_R)
        mono = (lo_L + lo_R) * 0.5
        out  = audio.copy()
        out[:, 0] = (L - lo_L + mono).astype(np.float32)
        out[:, 1] = (R - lo_R + mono).astype(np.float32)
        return out

    def process(self, channel_buffers: list[np.ndarray]) -> np.ndarray:
        frames = channel_buffers[0].shape[0]
        master = np.zeros((frames, 2), dtype=np.float32)
        xf     = self._xf_gains()
        for i, buf in enumerate(channel_buffers):
            out = self.eqs[i].process(buf)
            out = self.fx[i].process(out)
            out = soft_limit(out)
            ch_out = out * float(self.volumes[i] * xf[i])
            # Post-fader peak for per-channel VU meters
            self.ch_peak[i] = float(np.max(np.abs(ch_out)))
            master += ch_out
        master = self._mono_bass(master)
        return soft_limit(master * self.master_vol)

    def set_volume_raw(self, ch, raw, max_raw=0x3FFF):
        p = raw / max_raw                 # linear position 0..1
        self.volumes[ch] = p * p          # p² audio taper — matches VolFader

    def set_crossfader_raw(self, raw, max_raw=0x3FFF):
        self.crossfader = (raw / max_raw) * 2.0 - 1.0

    def set_eq_raw(self, ch, band, raw):
        setattr(self.eqs[ch], band, (raw / 127.0) * 2.0 - 1.0)

    def set_fx_raw(self, ch, raw):
        self.fx[ch].depth_value = raw / 127.0

    def set_fx_type(self, ch: int, name: str):
        """Replace channel FX with a new effect type, preserving depth value."""
        cls = FX_TYPES.get(name)
        if cls is None:
            return
        old_depth = self.fx[ch].depth_value
        self.fx[ch] = cls(self.sample_rate)
        self.fx[ch].depth_value = old_depth
