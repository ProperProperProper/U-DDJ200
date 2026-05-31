# U-DJ

**Professional DJ application for macOS — built for serious use.**

U-DJ is a full-featured DJ application written in Python/PyQt6 with Pioneer DDJ-200 hardware support, Mixxx-exact beat sync, ML-powered stem separation, and a studio-quality effects chain.
<img width="1468" height="838" alt="Screenshot 2026-05-31 at 8 45 08 PM" src="https://github.com/user-attachments/assets/0aa471fd-5184-4dac-89e0-a85c9006a1c0" />

---

## Features

### Beat Sync Engine
- **Mixxx-exact algorithm** — hard rate lock (zero tempo wobble) + continuous P-controller phase correction
- **Bar-snap** using stored beat-grid positions, not arithmetic approximation — same track on both decks gives sample-exact alignment
- **Downbeat detection** — analyzer rotates beat grid so bar-1 always lands on the musical downbeat
- **Quantize toggle** — Shift+SYNC on DDJ-200

### Analysis
- BPM detection with multi-hypothesis scoring (anti-half/double-tempo)
- Full beat grid stored per track (all positions, not just BPM + first beat)
- **Downbeat offset detection** via sub-bass energy per beat phase
- Key detection (Krumhansl-Schmuckler), Camelot wheel
- Energy level 1–10 (MIK-style)
- EBU R128 loudness, gain normalisation
- Smart cue point detection (INTRO / DROP / BREAK / BUILD / OUTRO)
- Phrase detection (8/16-bar structural boundaries)

### Effects (per channel, selectable via dropdown)
| Effect | Description |
|--------|-------------|
| **FILTER** | Resonant 4-pole Moog-style sweep, 24 dB/oct, Q=2.5 |
| **DELAY** | Stereo ping-pong delay, LP-filtered feedback (tape echo) |
| **REVERB** | Freeverb-inspired plate — comb + allpass diffusion |
| **FLANGER** | Deep tape flanger with feedback, slow/fast LFO |
| **PHASER** | 8-stage allpass, 0.45× feedback, 60 Hz–6 kHz sweep |
| **DRIVE** | Asymmetric tube saturation + presence shelf |

FX activates while Shift is held, cuts to bypass on release.

### Stem Separation
- **Demucs htdemucs** — MPS (Apple Silicon GPU) accelerated, `shifts=2` for quality
- Per-stem post-processing tuned for electronic/techno: transient restore on drums, sub-bass enhance on bass, high-pass on other
- Level calibration so stems sum to original loudness
- Auto-disables vocals stem for instrumental tracks (energy threshold detection)

### Hardware — Pioneer DDJ-200
- Full MIDI mapping (play/pause, cue, sync, jog, EQ, crossfader, tempo)
- LED feedback
- Shift+EQ = EQ bands (soft pickup — no jump on engage)
- Shift+CFX knob = colour FX (cuts to bypass on shift release)
- Shift+SYNC = toggle quantize
- Jog wheel: scratch (touch), pitch bend (no touch), seek (shift+jog)

### Mixer
- 3-band LR8 EQ (Linkwitz-Riley 8th-order, −48 dB isolation at 1 octave)
- Constant-power crossfader (cosine law)
- Mono-bass summing below 200 Hz (prevents low-end mud)
- Soft limiter (tanh knee) on channel outputs and master bus
- 24-segment VU meters with peak hold

### Playback
- 4-point Catmull-Rom (Hermite) interpolation — same quality as Mixxx/Serato
- Loop with 64-sample crossfade at boundary (click-free)
- Hot cues × 8 with quantize snap
- Beat loops: 1/4, 1/2, 1, 2, 4, 8, 16, 32 bars

### Diagnostics
- SQLite event log (`~/.udj/events.db`): sync phase error, snaps, quantize, grid position — all live queryable

---

## Requirements

```
Python 3.9+
PyQt6
sounddevice
soundfile
scipy
numpy
librosa
pyloudnorm
demucs
torch (MPS recommended on Apple Silicon)
rtmidi
mutagen
```

```bash
pip install PyQt6 sounddevice soundfile scipy numpy librosa pyloudnorm demucs torch torchaudio rtmidi mutagen
```

## Run

```bash
python3 main.py
```

## Build (macOS .app)

```bash
pip install pyinstaller
python3 -m PyInstaller udj.spec --clean -y
cp -R dist/U-DJ.app ~/Desktop/U-DJ.app
```

---

## Architecture

```
udj/
  core/
    engine.py      — Sync engine: rate lock, P-controller, bar-snap
    deck.py        — Playback: Hermite interp, loops, stems, hot cues
    mixer.py       — EQ, FX chain, crossfader, limiter
    stems.py       — Demucs ML separation + post-processing
    event_log.py   — SQLite async event logger
  library/
    analyzer.py    — BPM, beats, key, energy, cue point detection
    db.py          — SQLite track library
    scanner.py     — Directory walker
  midi/
    ddj200.py      — Pioneer DDJ-200 full MIDI mapping
  ui/
    main_window.py
    deck_widget.py
    mixer_widget.py
    beat_grid_widget.py   — Scrolling beat grid, both decks
    library_widget.py
    vu_meter.py
    pro_fader.py
```

---

## License

MIT
