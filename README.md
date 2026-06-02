# U-DJ

**Professional DJ application for macOS — Pioneer DDJ-200 controller, rekordbox-grade feature set modelled on the DDJ-GRV6.**

Built with Python 3.9, PyQt6, demucs htdemucs_ft, sounddevice, scipy, librosa.

---

## Quick Start

1. Connect DDJ-200 via USB
2. Launch `U-DJ.app` from Desktop
3. Drag tracks from the library onto Deck 1 or Deck 2
4. Press PLAY

Tracks are analysed automatically on first load (BPM, key, beat grid, cues). Stems are pre-generated in the background for every track in the library at startup — the green dot in the library means stems are ready.

---

## Library Status Dots

| Dot | Colour | Meaning |
|-----|--------|---------|
| ● | Green | Fully analysed — BPM, key, beats, cues, **stems ready** |
| ● | Blue | BPM/key done — stems still processing |
| ● | Amber | Beats done — missing some metadata |
| ● | Orange | BPM only |
| ○ | Grey | Not yet analysed |

---

## Beat Sync — how it works (Mixxx/rekordbox exact)

**Hardware:** SYNC button

Pressing SYNC on a deck makes it **MASTER**. The other deck becomes FOLLOWER and snaps to match.

**Rate lock (every audio callback ~43Hz):**
```
follower.rate = master_bpm × master_rate / follower_bpm
```
Exact hard lock — zero tempo wobble.

**Bar-phase snap (on SYNC press):**
1. Find the bar currently playing on master (the bar that CONTAINS master's position)
2. Compute `m_offset = master.position − bar_start` (how far into the bar)
3. Find the bar containing follower's current position
4. Scale offset: `snapped = follower_bar_start + m_offset × (follower_bar_len / master_bar_len)`

Both decks end up at the same fractional position within their respective bars — grids lock visually.

**Phase drift correction (Mixxx exact constants):**
- `adj = clamp(beat_error × 0.5, ±0.02)` applied as `rate × (1 + adj)` each callback
- ±2% rate tweak max — completely inaudible, converges in ~1 bar
- Does NOT nudge position (which caused audible jumps)

**Shift + SYNC** = set loop IN point (see Loops)

---

## Quantize

Always on. All hot cue jumps, beat loops, and beat jumps snap to the nearest beat grid position. Beat grid is stored from BPM analysis — same algorithm as Mixxx AnalyzerBeats.

---

## Hot Cues

**Pad mode:** HOT CUE (press HOT CUE mode button or Shift+cycle)

| Action | Result |
|--------|--------|
| Pad press (empty) | Set cue at current position (quantized to beat) |
| Pad press (set) | Jump to cue and play |
| Shift + pad | Delete cue |
| Right-click pad (UI) | Delete cue |

Auto-detected cue points (INTRO / DROP / BUILD / BREAK / OUTRO) are loaded onto pads automatically from MIK-style energy analysis.

---

## Beat Loop

**Pad mode:** BEAT LOOP

8 pads = loop sizes: 1/4 · 1/2 · 1 · 2 · 4 · 8 · 16 · 32 bars

| Action | Result |
|--------|--------|
| Pad press | Activate loop of that size from current position |
| Pad press (active) | Exit loop |
| Shift + lower 4 pads (loop active) | Halve loop length |
| Shift + upper 4 pads (loop active) | Double loop length |

Loop start snaps to nearest phrase boundary (djay-style smart quantize), falling back to nearest bar, then nearest beat.

**Manual loop:**
- Shift + SYNC = Loop IN (snaps to nearest phrase/bar)
- SYNC (second press) = Loop OUT (snaps to nearest bar)

---

## Stems / Stem ISO

**Pad mode:** STEMS

Stems are generated using **demucs htdemucs_ft** (fine-tuned model, Serato-grade quality) with `shifts=4, overlap=0.5` for maximum separation quality. All stems are pre-generated during library analysis and stored on disk — instant load during performance.

**EQ knobs (no Shift) = Stem ISO:**

| Knob | Stem |
|------|------|
| HI | Vocals |
| MID | Other / Melody |
| LOW | Bass |

Center = full volume. Turn left = fade to kill. Right of center = full (no boost). Same behavior as GRV6 Stem ISO.

**Pads 1–4:** Toggle stem on/off (drums / bass / other / vocals)

**Pads 5–8:** Hold to **solo** that stem (all others muted while held, restore on release)

**Shift + EQ knobs** = actual EQ (3-band LR8 Linkwitz-Riley, ±10 dB, true kill at full left)

**CFX knob** = Color FX depth (always, independent of shift)

---

## Beat Jump

**Pad mode:** BEAT JUMP

| Pad | Jump |
|-----|------|
| 1 | −1 bar |
| 2 | −2 bars |
| 3 | −4 bars |
| 4 | −8 bars |
| 5 | +1 bar |
| 6 | +2 bars |
| 7 | +4 bars |
| 8 | +8 bars |

---

## Pad FX (Gate FX)

**Pad mode:** PAD FX

Hold a pad to apply an FX momentarily. Release = bypass. Matches GRV6 Beat FX palette.

| Pad | Effect |
|-----|--------|
| 1 | Filter HPF sweep |
| 2 | Filter LPF sweep |
| 3 | Echo (beat-synced) |
| 4 | Roll (beat-synced loop) |
| 5 | Reverb |
| 6 | Flanger |
| 7 | Phaser |
| 8 | Echo Out (heavy feedback) |

Echo and Roll are BPM-synced — delay/loop size tracks the playing track's BPM in real time.

---

## Pad Mode Cycling (Hardware)

**Shift + first beat-loop pad** = cycle pad mode:

`HOT CUE → BEAT LOOP → STEMS → BEAT JUMP → PAD FX → HOT CUE`

---

## Key Shift

**UI:** ▲ / ▼ buttons next to the key display on each deck. Press **0** to reset.

**Hardware:** Shift + jog rim (outer ring, not touch surface) — 5 ticks per semitone.

Range: ±12 semitones. Applied as playback rate adjustment — sync engine compensates tempo, so only pitch changes. Key display turns amber when shifted.

---

## Key Sync

**Hardware:** Shift + PFL

Calculates the semitone difference between this deck's key and the master deck's key using the Camelot wheel, then applies it as a key shift. Aligns keys harmonically (selects the smallest shift ≤ 6 semitones).

Requires both decks to have Camelot data (blue dot or green dot in library).

---

## Cue Behavior (CDJ/rekordbox style)

| State | Press CUE | Release CUE |
|-------|-----------|-------------|
| Stopped, no cue set | Set cue at position | — |
| Stopped, cue set | **Stutter:** play from cue while held | Stop and return to cue |
| Playing | Return to cue and stop | — |
| Shift + CUE | Go to cue and stop immediately | — |

---

## Color FX

**CFX knob** = FX wet/dry depth. Center = bypass. Select FX type from the dropdown in the mixer panel.

Available effects:

| Effect | Description |
|--------|-------------|
| FILTER | Resonant 4-pole Moog-style (LPF right, HPF left) |
| ECHO | Beat-synced echo with filtered feedback |
| ROLL | Beat-synced loop/stutter |
| DELAY | Stereo ping-pong delay with warm feedback |
| REVERB | Freeverb plate reverb |
| FLANGER | Tape-style flanger with feedback |
| PHASER | 8-stage allpass phaser |
| DRIVE | Tube/tape saturation |

---

## EQ

**Shift + HI / MID / LOW knobs**

3-band LR8 Linkwitz-Riley crossover (−48 dB/octave isolation). Pioneer DJM-900-style taper: −∞ at full left (true kill), +10 dB at full right. Soft-pickup prevents jumps when entering Shift mode.

---

## VU Meters & Crossfader

- CH1 and CH2 compact VU meters — post-fader peak
- Master stereo VU with dBFS scale
- Crossfader — constant-power (cosine law)
- PFL buttons per channel

---

## Loudness Normalisation

**NORM button** (appears on deck when track has LUFS data)

Applies EBU R128 gain offset to match tracks to a target LUFS. Off by default — user enables per-deck. Displayed as `NORM +X.X dB`.

---

## Analyse All Button

The **⟳ Analyse All** button in the library toolbar queues every track for full BPM + stems re-analysis.

| State | Appearance |
|-------|-----------|
| Idle | Grey — "⟳ Analyse All" |
| Running | Green highlight — "⟳ 3 / 24" (live count) |
| Done | Resets to idle |

While running, a **progress bar** (`X / N`) and a **CPU % badge** appear next to the button:
- Green = CPU < 50%
- Amber = CPU 50–80%
- Red = CPU > 80%

Both disappear when analysis completes.

---

## Track Analysis (pre-analysis before performance)

All analysis runs before you perform:

1. **On app startup** — all library tracks are queued for BPM/key/beats/cues analysis + stems pre-generation
2. **On +Add Folder** — same pipeline runs immediately after scan
3. **On track load** — if not yet analysed, analysis runs in background and patches the live deck (no interruption)

Analysis pipeline per track:
- BPM (spectral-flux onset + Mixxx autocorrelation algorithm)
- Beat grid (all beat positions stored)
- Phrase detection (8-bar boundaries)
- Key (Krumhansl-Schmuckler) + Camelot wheel
- Loudness (EBU R128 BS.1770-4 LUFS)
- Energy (1–10 MIK style)
- Auto cue points (INTRO / DROP / BUILD / BREAK / OUTRO)
- **Stems** (demucs htdemucs_ft, shifts=4, overlap=0.5)

Stems are cached to `~/.udj/stems/` as compressed `.npz` files. Second load of any track is instant.

---

## DDJ-200 Hardware Map

### Deck buttons

| Button | Normal | Shift |
|--------|--------|-------|
| PLAY | Play/pause | — |
| CUE | CDJ cue stutter (hold to play from cue) | Go to cue + stop |
| SYNC | Make this deck a follower | Loop IN |
| SYNC (2nd press, loop armed) | Loop OUT | — |
| PFL | Headphone cue | Key Sync to master |

### Jog wheel

| Action | Result |
|--------|--------|
| Touch surface | Scratch |
| Rim (no touch) | Pitch bend |
| Rim + Shift | Key shift (±1 semitone per 5 ticks) |

### EQ knobs (no Shift) — Stem ISO

| Knob | Stem |
|------|------|
| HI | Vocals |
| MID | Other / Melody |
| LOW | Bass |
| CFX | Color FX depth (always) |

### EQ knobs (Shift held) — actual EQ

| Knob | Band |
|------|------|
| Shift + HI | High |
| Shift + MID | Mid |
| Shift + LOW | Low |

### Performance pads — mode cycling

**Shift + first beat-loop pad** = cycle mode: `HOT CUE → BEAT LOOP → STEMS → BEAT JUMP → PAD FX`

| Mode | Pads 1–8 |
|------|----------|
| HOT CUE | Trigger/set hot cues 1–8 |
| BEAT LOOP | Loop sizes 1/4 · 1/2 · 1 · 2 · 4 · 8 · 16 · 32 |
| STEMS | 1–4 = toggle stem · 5–8 = hold to solo |
| BEAT JUMP | −1/−2/−4/−8 bars · +1/+2/+4/+8 bars |
| PAD FX | Hold = gate FX (Filter↓/↑, Echo, Roll, Reverb, Flange, Phase, Echo Out) |

---

## GRV6/rekordbox Features Not Yet Implemented

| Feature | Notes |
|---------|-------|
| **Groove Circuit** | Live drum loop injection/replacement — rekordbox proprietary, requires drum loop library + real-time drum stem swap |
| **Smart Rotary Selector** | DDJ-200 has no joystick — hardware limitation |
| **4-deck operation** | DDJ-200 is 2-deck hardware |
| **Keyboard pad mode** | Chromatic pitch play across pads — needs per-pad pitch shift DSP |
| **DISCOVER** | AI next-track recommendation — needs ML model or cloud API |
| **Beat FX BPM fraction** | Dedicated BPM division selector per effect — hardware button not available |
| **Sound Color FX (9 types)** | GRV6 has 9; U-DJ has 8 (missing Noise, Dub Echo, Jet) |
| **Mobile / iOS** | GRV6 supports rekordbox iOS — N/A |

---

## Build

```bash
cd ~/Desktop/U-DJ
python3 -m PyInstaller udj.spec --clean -y
rm -rf ~/Desktop/U-DJ.app
cp -R dist/U-DJ.app ~/Desktop/U-DJ.app
```

## Install dependencies

```bash
pip install PyQt6 sounddevice soundfile scipy librosa pyloudnorm \
            python-rtmidi demucs torch torchaudio mutagen
```

## File locations

| Path | Contents |
|------|----------|
| `~/.udj/library.db` | SQLite track database |
| `~/.udj/stems/` | Pre-computed stem cache (`.npz` files) |
