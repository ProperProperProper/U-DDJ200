# U-DJ

Professional DJ app for macOS. Built for **Detroit techno, house, trance and dance music**. Pioneer DDJ-200 controller. AI stem separation. Studio-grade beat sync with transient quantization.

<img width="1467" height="830" alt="U-DJ Screenshot" src="https://github.com/user-attachments/assets/6014058a-73c6-4ea5-8669-2a57d2bf9b49" />

---

## Setup

1. Connect DDJ-200 via USB **before** launching
2. Open `U-DJ.app` from your Desktop
3. Click **+ Add Folder** → select your music folder
4. Tracks analyse automatically in the background
5. Drag a track onto each deck or double-click to load
6. Press PLAY

---

## DDJ-200 — Full Control Reference

### Transport

| Control | Action |
|---------|--------|
| **PLAY/PAUSE** | Play / pause |
| **CUE** | Set cue / return to cue / stutter play (hold) |
| **Shift + CUE** | Jump to cue and stop |
| **SYNC** | This deck becomes follower, other deck becomes master — snaps bar phase immediately |
| **Shift + SYNC** | Set loop IN point |
| **SYNC (2nd press while follower)** | Re-snap to master's current bar phase |

### Tempo Slider

Adjusts playback rate ±16%. Moving the slider while sync is active breaks sync for that deck.

### Jog Wheel

| Action | Result |
|--------|--------|
| Touch surface | Scratch — suspends sync momentarily |
| Rim only | Pitch bend (accumulates, decays 230ms) |
| Shift + Rim | Key shift ±1 semitone per 5 ticks |

### EQ / FX Knobs

| Knob | Action |
|------|--------|
| **HI** | EQ High — kill to +10 dB (soft-pickup) |
| **MID** | EQ Mid — kill to +10 dB |
| **LOW** | EQ Low — kill to +10 dB |
| **CFX Super Knob** | Color FX depth — centre = bypass |

### Crossfader & Channel Fader

- **Channel fader** — per-deck volume (p² taper)
- **Crossfader** — constant-power cosine-law A→B blend
- **PFL button** — headphone cue for that deck
- **Shift + PFL** — Key Sync: auto-match key to master via Camelot wheel (≤6 semitones)

---

## Performance Pads

### Layout

```
Top row:    [ VOC ] [ BASS ] [ DRM ] [ SYNTH ]   ← mode function (see below)
Bottom row: [ VOC ] [ BASS ] [ DRM ] [ ●8● ]
             ECH     ECH     ECH    (mode cycle)
```

**Pad 8** (bottom-right, hidden on screen) cycles pad modes:

`STEMS → HOT CUE → BEAT LOOP → BEAT JUMP → STEMS`

---

### STEMS mode (default)

**Top row — stem mute toggles:**

| Pad | Stem | LED dark | LED lit |
|-----|------|----------|---------|
| 1 | VOC (Vocals) | Playing | **Muted** |
| 2 | BASS | Playing | **Muted** |
| 3 | DRM (Drums) | Playing | **Muted** |
| 4 | SYNTH (Other) | Playing | **Muted** |

Press to mute → pad **lights up**. Press again → stem returns, pad goes **dark**. Smooth 10ms crossfade on every cut — no clicks.

**Bottom row — echo effects:**

| Pad | Effect |
|-----|--------|
| 5 | VOC ECHO — apply echo to vocal stem |
| 6 | BSS ECHO — apply echo to bass |
| 7 | DRM ECHO — apply echo to drums |

- Press to toggle echo ON/OFF (lit = active, dark = off)
- **Shift + echo pad** — cycle FX type: `ECHO → DELAY → REVERB → FILTER`
- Only one echo active at a time

Stems must show **● STEMS READY** in the mixer before pads are active. Shows **◌ STEMS LOADING…** while separating (~90s first time, instant from cache).

---

### HOT CUE mode

| Action | Result |
|--------|--------|
| Pad (empty) | Set cue at current position (beat-snapped, 40% window) |
| Pad (set, lit) | Jump to cue with 30ms pre-roll crossfade |
| Right-click pad | Delete cue |

Up to 8 auto cue points (INTRO / DROP / BUILD / BREAK / OUTRO) loaded automatically.

---

### BEAT LOOP mode

Pads left→right: `1/4  1/2  1  2  4  8  16` bars

| Action | Result |
|--------|--------|
| Pad | Activate loop at that size (lit = active) |
| Same pad | Exit loop |

Loop IN is snapped to the nearest beat/phrase boundary. Loop OUT is snapped to the stored beat grid (sample-accurate end alignment).

Manual loop: **Shift + SYNC** → set loop IN, **SYNC** → set loop OUT.

---

### BEAT JUMP mode

| Pads 1–4 | Pads 5–7 |
|----------|----------|
| −1 bar | +1 bar |
| −2 bars | +2 bars |
| −4 bars | +4 bars |

---

## Stems — How It Works

U-DJ uses **htdemucs_ft** (demucs ML model, Apple Silicon MPS accelerated) to separate tracks into 4 stems. Post-processing is tuned specifically for **Detroit techno / electronic music**:

| Stem | What it contains | Post-processing |
|------|-----------------|-----------------|
| **VOC** | Vocal samples, stabs | HP 100Hz, tight gate -20dBFS, presence +3dB 1.5-4kHz |
| **BASS** | Bassline, 303 acid | LP 1200Hz (keeps acid squeal), sub +3dB, mono <120Hz |
| **DRM** | Kick, snare, hats | HP 30Hz (preserves kick body), transient boost, hat air +3dB >10kHz |
| **SYNTH** | Synths, pads, leads | 8th-order HP 120Hz (kills bass bleed), presence +2dB |

Post-separation: **Wiener soft-mask refinement** (STFT 4096/hop 1024) sharpens TF-bin separation. Adaptive vocal gate at 30th percentile. Drum bleed from hats (moderate correlation only) removed via mild shelf cut. High drums→synths / drums→vocals correlation on dance tracks is rhythmic co-occurrence — no DSP applied.

**First separation:** ~90 seconds on Apple Silicon M-series  
**Subsequent loads:** instant from cache (`~/udj_cache/`)

---

## Quantize & Beat Grid

U-DJ uses a **40% quantize window** (rekordbox/Serato standard). When you trigger a cue, hot cue, or loop:

- The hit is snapped to the nearest beat **only if it lands within ±40%** of a beat interval. Hits in the 40–50% zone (near the middle of two beats) stay where they are — forced snapping that far would sound unnatural.
- Playback extends **30ms before** the target point. The outgoing audio fades out over the same 30ms window while the incoming audio fades in — a smooth overlap instead of a hard cut.
- If an active loop's out-point falls inside that 30ms pre-roll window, it is **trimmed** back to leave at most 1ms of overlap.
- **Beat loop out-points** are snapped to the stored beat grid (not nominal BPM math), giving sample-accurate loop lengths regardless of BPM drift.
- Pressing **PLAY** on a paused deck snaps position to the nearest beat (same 40% window).

---

## Sync

Press **SYNC** on the incoming deck → it locks to the playing deck (follower) and snaps bar phase immediately.

**Studio-grade phase engine:**
- Phase is measured against the stored beat grid (actual inter-beat intervals), not uniform BPM arithmetic — eliminates phantom drift on tracks with natural BPM variation.
- Dead zone: ±0.46ms at 130 BPM. Errors inside this window are ignored to avoid micro-jitter.
- Convergence: phase errors correct in ~50ms (P-gain 0.8, cap 4% per beat).
- Rate lock is hard nominal (zero tempo wobble); only position is nudged for phase correction.

The beat grid strip at top shows both decks — when bar markers align, you're locked.

---

## Color FX

CFX Super Knob controls depth. Select type from mixer panel dropdown:

`FILTER · ECHO · ROLL · DELAY · REVERB · FLANGER · PHASER · DRIVE`

Centre = bypass on all effects.

---

## Key Shift & Key Sync

- **UI ▲/▼ buttons** or **Shift + jog rim** → shift pitch ±1 semitone
- **UI 0 button** → reset
- **Shift + PFL** → Key Sync via Camelot wheel
- Key display turns **amber** when shifted

---

## Loudness Normalisation

**NORM** button per deck applies EBU R128 normalisation. Toggle per deck independently.

---

## Library

| Dot | Colour | Meaning |
|-----|--------|---------|
| ● | Green | Fully analysed — BPM, key, beats, cues, stems cached |
| ● | Blue | BPM/key done, stems not yet cached |
| ● | Amber | Beat grid done, some metadata missing |
| ● | Orange | BPM only |
| ○ | Grey | Not yet analysed |

**+ Add Folder** — replaces library with new folder scan  
**⟳ Analyse All** — queues all tracks for analysis and stem pre-computation

---

## 🔬 Stem Inspection Tool

Select tracks in the library, then click **🔬 Inspect Stems** in the toolbar. No deck load required — stems are separated and cached automatically on first inspect.

The inspector runs fully local DSP analysis: band energy ratios, cross-contamination correlation, adaptive auto-fix (HP filters, shelf cuts, Wiener soft-mask refinement), and logs results to `~/.udj/library.db`. High drums→synths and drums→vocals correlation on dance music is identified as rhythmic co-occurrence (`~ RHYTHMIC`) rather than acoustic bleed, so no unnecessary DSP is applied.

---

## File Locations

| Path | Contents |
|------|----------|
| `~/.udj/library.db` | Track database |
| `~/.udj/events.db` | Sync/quantise event log |
| `~/.udj/stems/<hash>.npz` | Stem cache |
| `~/.cache/torch/hub/checkpoints/` | Demucs model weights |

---

## Build from Source

### 1. Requirements

| | Minimum | Recommended |
|---|---|---|
| **OS** | macOS 12 Monterey | macOS 13+ |
| **Chip** | Intel x86_64 | Apple Silicon (M1/M2/M3/M4) |
| **Python** | 3.9 | 3.9 (tested) |
| **Disk** | 2 GB | 5 GB |
| **RAM** | 8 GB | 16 GB |

### 2. Install Python 3.9

```bash
brew install python@3.9
```

### 3. Clone

```bash
git clone https://github.com/PahtrikProper/U-DJ.git
cd U-DJ
```

### 4. Install dependencies

**Core:**
```bash
pip3 install -r requirements.txt
pip3 install pyloudnorm
```

**ML stack — Apple Silicon:**
```bash
pip3 install torch==2.8.0 torchaudio==2.8.0
pip3 install demucs==4.0.1
pip3 install "audio-separator[cpu]"
```

**ML stack — Intel Mac:**
```bash
pip3 install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
pip3 install demucs pyloudnorm "audio-separator[cpu]"
```

**Build tool:**
```bash
pip3 install pyinstaller==6.20.0
```

**Verify:**
```bash
python3 -c "import torch, torchaudio, demucs, sounddevice, PyQt6; print('All good')"
```

### 5. Run directly

```bash
python3 main.py
```

### 6. Build .app bundle

```bash
python3 -m PyInstaller udj.spec --clean -y
rm -rf ~/Desktop/U-DJ.app
cp -R dist/U-DJ.app ~/Desktop/U-DJ.app
open ~/Desktop/U-DJ.app
```

> Always use `--clean -y`. Never incremental builds.

### 7. First launch

1. Approve **Microphone access** when prompted
2. DDJ-200 detected automatically — no driver needed
3. Click **+ Add Folder** → select your music
4. First stem separation triggers a one-time **~50 MB model download**

### 8. Dev tools

**Stem inspection** (local DSP analysis, no API key required):
```bash
python3 dev_inspect_stems.py track1.wav track2.wav
```
