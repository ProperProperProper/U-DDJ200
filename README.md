# U-DJ

**Professional DJ application for macOS — Pioneer DDJ-200 controller, rekordbox-grade feature set.**

Built with Python 3.9 · PyQt6 · demucs htdemucs_ft · sounddevice · librosa

<img width="1468" height="833" alt="U-DJ Screenshot" src="https://github.com/user-attachments/assets/731037b5-a337-45db-a332-9e86a8faa33f" />

---

## Quick Start

1. Connect DDJ-200 via USB
2. Launch `U-DJ.app` from your Desktop
3. Click **+ Add Folder** to import your music library
4. Drag a track onto each deck and press **PLAY**

Tracks are analysed automatically on first import — BPM, key, beat grid, energy, cue points. Stems are loaded on demand when a track is put on a deck (cached after first run, instant thereafter).

---

## Beat Sync

**How to use:**
- Press **SYNC** on the deck you want as **MASTER** → that deck becomes master, the other snaps to match
- Press **SYNC again on the master** → re-snaps the slave to the current bar phase
- Press **SYNC on the slave** while both are playing → slave promotes itself to master

**What happens under the hood:**

1. **Rate lock** — follower rate is set to `master_eff_bpm / follower_bpm` every audio callback (~43 Hz). Zero tempo wobble.
2. **Bar-phase snap** — on sync assignment or play-start, follower position jumps to the same fractional bar phase as master (uniform BPM arithmetic, consistent with phase measurement).
3. **Phase nudge** — continuous position correction (±3% of a beat per callback max) keeps tracks locked despite minor BPM detection error. Auto re-snap fires if drift exceeds 0.4 beats.

---

## Stems

Stem separation uses **demucs htdemucs_ft** (fine-tuned transformer, Serato-grade quality). Stems are separated on demand when a track loads to a deck — the `⏳ STEMS` badge shows on the deck while processing (~90 sec on Apple Silicon MPS), then disappears when ready.

**Pre-compute stems for your whole library** (faster playback):
```bash
cd ~/Desktop/U-DJ
python3 stem_example.py /path/to/your/music/folder
```
Stems are cached to `~/udj_cache/<track_hash>/` as WAV files and load instantly next time.

### Stem Knob Mapping (DDJ-200)

| Knob | Stem | Shift held |
|------|------|------------|
| HI | Vocals | EQ High |
| MID | Other / Melody | EQ Mid |
| LOW | Bass | EQ Low |
| CFX Super Knob | **Drums** | Color FX depth |

Center = full volume. Turn left = fade to kill. Right = full (no boost). All four stems to zero = silence.

---

## Library Status Dots

| Dot | Colour | Meaning |
|-----|--------|---------|
| ● | Green | Fully analysed — BPM, key, beats, cues, stems ready |
| ● | Blue | BPM/key done — stems loading on deck |
| ● | Amber | Beats done — missing some metadata |
| ● | Orange | BPM only |
| ○ | Grey | Not yet analysed |

---

## Track Analysis Pipeline

Run automatically on first import. Per track:

| Step | Detail |
|------|--------|
| BPM | Spectral-flux onset + autocorrelation (Mixxx algorithm), half/double-tempo correction |
| Beat grid | Every beat position stored; locked to best BPM candidate |
| Phrase detection | 8-bar boundary detection (djay-style) |
| Key | Krumhansl-Schmuckler profiles → Camelot wheel code |
| Loudness | EBU R128 BS.1770-4 integrated LUFS + normalisation gain |
| Energy | 1–10 score (MIK-style: loudness + brightness + compression) |
| Auto cue points | Up to 8 INTRO / DROP / BUILD / BREAK / OUTRO markers per track |

---

## Hot Cues

**Pad mode: HOT CUE**

| Action | Result |
|--------|--------|
| Pad press (empty) | Set cue at current position (quantized to beat) |
| Pad press (set) | Jump to cue and play |
| Shift + pad | Delete cue |

Auto-detected cue points load onto pads automatically.

---

## Beat Loop

**Pad mode: BEAT LOOP**

8 pads = `¼ · ½ · 1 · 2 · 4 · 8 · 16 · 32` bars

| Action | Result |
|--------|--------|
| Pad press | Activate loop from current position |
| Pad press (active loop) | Exit loop |
| Shift + lower 4 (loop active) | Halve loop |
| Shift + upper 4 (loop active) | Double loop |

Loop start snaps to nearest phrase boundary, falling back to bar then beat.

**Manual loop:** Shift+SYNC = Loop IN · SYNC (2nd press) = Loop OUT

---

## Beat Jump

**Pad mode: BEAT JUMP**

| Pads 1–4 | Pads 5–8 |
|----------|----------|
| −1 / −2 / −4 / −8 bars | +1 / +2 / +4 / +8 bars |

---

## Pad FX (Gate FX)

**Pad mode: PAD FX** — hold a pad to apply, release to bypass

| Pad | Effect |
|-----|--------|
| 1 | Filter HPF sweep |
| 2 | Filter LPF sweep |
| 3 | Echo (BPM-synced) |
| 4 | Roll (BPM-synced loop) |
| 5 | Reverb |
| 6 | Flanger |
| 7 | Phaser |
| 8 | Echo Out |

---

## Color FX

**CFX Super Knob (Shift held)** = Color FX wet/dry depth

| Effect | Description |
|--------|-------------|
| FILTER | Resonant 4-pole Moog-style (LPF right, HPF left) |
| ECHO | Beat-synced echo with filtered feedback |
| ROLL | Beat-synced loop/stutter |
| DELAY | Stereo ping-pong delay |
| REVERB | Freeverb plate reverb |
| FLANGER | Tape-style flanger |
| PHASER | 8-stage allpass phaser |
| DRIVE | Tube/tape saturation |

---

## EQ

**Shift + HI / MID / LOW** — 3-band Linkwitz-Riley (−48 dB/oct). Full left = kill. Full right = +10 dB. Soft-pickup prevents jumps when engaging Shift.

---

## Key Shift & Key Sync

- **UI buttons ▲/▼** or **Shift + jog rim** (5 ticks per semitone) — range ±12 semitones
- **0 button** = reset to original key
- **Shift + PFL** = Key Sync — auto-calculates semitone offset to match master key (Camelot wheel, smallest shift ≤6 semitones)

---

## Jog Wheel

| Action | Result |
|--------|--------|
| Touch surface | Scratch |
| Rim (not touching) | Pitch bend |
| Shift + rim | Key shift |

---

## Cue Behavior (CDJ/rekordbox style)

| State | CUE press | CUE release |
|-------|-----------|-------------|
| Stopped, no cue | Set cue | — |
| Stopped, cue set | Stutter play from cue | Return to cue + stop |
| Playing | Return to cue + stop | — |
| Shift + CUE | Go to cue + stop | — |

---

## Loudness Normalisation

**NORM button** on the deck panel. Applies EBU R128 gain offset so tracks match a target LUFS. Per-deck toggle.

---

## Analyse All

The **⟳ Analyse All** button re-runs BPM + stems analysis on every track in the library. Live progress counter and CPU % badge shown during analysis.

---

## DDJ-200 Full Control Map

### Deck Controls

| Control | Normal | Shift |
|---------|--------|-------|
| PLAY | Play / pause | — |
| CUE | CDJ stutter cue | Go to cue + stop |
| SYNC | Make this deck MASTER (re-snap if already master) | Loop IN |
| SYNC 2nd press | Loop OUT (when loop armed) | — |
| PFL | Headphone cue | Key Sync to master |

### Knobs

| Knob | Normal | Shift |
|------|--------|-------|
| HI | Vocals stem | EQ High |
| MID | Other / melody stem | EQ Mid |
| LOW | Bass stem | EQ Low |
| CFX Super Knob | Drums stem | Color FX depth |

### Performance Pads — Mode Cycling

**Shift + first beat-loop pad** → cycles: `HOT CUE → BEAT LOOP → STEMS → BEAT JUMP → PAD FX`

| Mode | Pads 1–4 | Pads 5–8 |
|------|----------|----------|
| HOT CUE | Trigger / set cues 1–4 | Trigger / set cues 5–8 |
| BEAT LOOP | ¼ · ½ · 1 · 2 bars | 4 · 8 · 16 · 32 bars |
| STEMS | Toggle stem on/off | Hold to solo stem |
| BEAT JUMP | −1/−2/−4/−8 bars | +1/+2/+4/+8 bars |
| PAD FX | Hold = gate FX | Hold = gate FX |

---

## File Locations

| Path | Contents |
|------|----------|
| `~/.udj/library.db` | Track database (BPM, key, beats, cues) |
| `~/.udj/events.db` | Sync / quantize / grid event log |
| `~/udj_cache/<hash>/` | Pre-computed WAV stems (vocals, drums, bass, other) |

---

## Build from Source

```bash
cd ~/Desktop/U-DJ
pip install -r requirements.txt
python3 -m PyInstaller udj.spec --clean -y
rm -rf ~/Desktop/U-DJ.app
cp -R dist/U-DJ.app ~/Desktop/U-DJ.app
```

**Stem pre-computation** (optional, for whole library):
```bash
pip install -r requirements_stem_analyzer.txt
python3 stem_example.py /path/to/music/folder
```

---

## Not Yet Implemented

| Feature | Notes |
|---------|-------|
| Groove Circuit | Live drum loop injection — rekordbox proprietary |
| Smart Rotary Selector | DDJ-200 has no joystick — hardware limitation |
| 4-deck | DDJ-200 is 2-deck only |
| DISCOVER | AI track recommendation |
| rekordbox iOS | N/A |
