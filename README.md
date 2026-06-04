# U-DJ

Professional DJ app for macOS. Pioneer DDJ-200 controller. AI stem separation. Rekordbox-style beat sync.

<img width="1467" height="830" alt="Screenshot 2026-06-04 at 8 57 39 PM" src="https://github.com/user-attachments/assets/6014058a-73c6-4ea5-8669-2a57d2bf9b49" />

---

## Setup

1. Connect DDJ-200 via USB before launching
2. Open `U-DJ.app` from your Desktop
3. Click **+ Add Folder** → select your music folder
4. Tracks analyse automatically in the background (BPM, key, beat grid, cues)
5. Drag a track onto each deck or double-click a track to load it
6. Press PLAY

---

## DDJ-200 Control Reference

### Transport

| Control | Action |
|---------|--------|
| **PLAY/PAUSE** | Play / pause |
| **CUE** | Set cue / return to cue / stutter play |
| **Shift + CUE** | Jump to cue and stop |
| **SYNC** | Sync this deck to the other (see Sync section) |
| **Shift + SYNC** | Set loop IN point |
| **SYNC (2nd press)** | Set loop OUT point and activate |

### Jog Wheel

| Control | Action |
|---------|--------|
| **Touch surface (playing)** | Scratch — suspends sync momentarily |
| **Rim only** | Pitch bend |
| **Shift + Rim** | Key shift ±1 semitone per 5 ticks |

### Tempo Slider

Adjusts playback rate ±16%. Moving the slider while sync is active breaks sync for that deck.

---

## Stems & EQ Knobs

This is the core of U-DJ. The **HI / MID / LOW knobs** on the DDJ-200 do double duty:

| Mode | How to activate | What the knobs do |
|------|----------------|-------------------|
| **Stem blend** | Normal (no Shift) | Crossfade between two stem groups |
| **EQ** | Hold **Shift** | 3-band EQ (kill to +10 dB) |

### Stem Blend Mode (normal, no Shift)

Each knob is a crossfader between two stems. **Center = both stems full.**

| Knob | Turn LEFT | Center | Turn RIGHT |
|------|-----------|--------|------------|
| **HI** | Kill vocals → only drums | Both full | Kill drums → only vocals |
| **MID** | Kill bass → only other/synths | Both full | Kill other → only bass |
| **LOW** | Kill lead synth → only pads | Both full | Kill pads → only lead |

Hard left and hard right are **hard cuts** — complete silence on that stem.

> Stems take ~90 seconds to separate the first time (demucs ML). Check the **● STEMS READY** indicator in the mixer panel. Knobs have no effect on audio until stems are ready — the full track plays until then.

### EQ Mode (hold Shift)

Hold **Shift** then turn HI / MID / LOW for 3-band EQ. Uses soft-pickup — EQ only engages once the knob crosses the current EQ value, preventing jumps.

| Knob | Band | Range |
|------|------|-------|
| **Shift + HI** | High (3.5 kHz+) | Kill to +10 dB |
| **Shift + MID** | Mid (200 Hz – 3.5 kHz) | Kill to +10 dB |
| **Shift + LOW** | Low (0 – 200 Hz) | Kill to +10 dB |

### CFX Super Knob

Controls **Color FX depth** directly. Select FX type from the mixer panel dropdown (FILTER, DELAY, REVERB, FLANGER, PHASER, DRIVE).

---

## Sync

Press **SYNC** on the deck you want as master. The other deck locks to its tempo and bar phase.

| Action | Result |
|--------|--------|
| SYNC on a deck | That deck becomes master, other snaps to bar |
| SYNC on master again | Re-snaps the follower |
| SYNC on follower (both playing) | Follower promotes to master |
| PLAY while sync armed | Snaps to master bar phase on play-start |

Sync is continuous — phase nudge corrects drift automatically. The beat grid strip at the top shows both decks: when bar markers align, you're locked.

---

## Performance Pads

Switch pad mode with **Shift + first BEAT LOOP pad**:

`HOT CUE → BEAT LOOP → STEMS → BEAT JUMP → PAD FX → (loop)`

Default on startup: **BEAT LOOP**

### HOT CUE mode

| Action | Result |
|--------|--------|
| Pad (empty) | Set cue at current position (beat-snapped) |
| Pad (set, lit) | Jump to cue and play |
| Shift + pad | Delete cue |

Up to 8 auto cue points (INTRO / DROP / BUILD / BREAK / OUTRO) load automatically from energy analysis.

### BEAT LOOP mode

Pads left→right: `¼  ½  1  2  4  8  16  32` bars

| Action | Result |
|--------|--------|
| Pad | Activate loop at that size |
| Same pad | Exit loop |
| Shift + pads 1–4 (loop active) | Halve loop |
| Shift + pads 5–8 (loop active) | Double loop |

Loop start snaps to nearest phrase → bar → beat.

### STEMS mode

| Action | Result |
|--------|--------|
| Pads 1–4 | Toggle stem on/off (drums / bass / other / vocals) |
| Pads 5–8 | Hold to solo a stem (all others mute while held) |

### BEAT JUMP mode

| Pads 1–4 | Pads 5–8 |
|----------|----------|
| −1 bar | +1 bar |
| −2 bars | +2 bars |
| −4 bars | +4 bars |
| −8 bars | +8 bars |

### PAD FX mode

Hold to apply, release to bypass:

| Pad | Effect |
|-----|--------|
| 1 | Filter HPF |
| 2 | Filter LPF |
| 3 | Echo (BPM-synced) |
| 4 | Roll (BPM-synced) |
| 5 | Reverb |
| 6 | Flanger |
| 7 | Phaser |
| 8 | Echo Out |

---

## Channel Fader & Crossfader

| Control | Function |
|---------|----------|
| **Channel fader** | Per-deck volume |
| **Crossfader** | Constant-power A→B blend |
| **PFL button** | Headphone cue for that deck |
| **Shift + PFL** | Key Sync — auto-match key to master using Camelot wheel (≤6 semitones) |

---

## Key Shift

- **UI ▲/▼ buttons** or **Shift + jog rim** → ±1 semitone per press/5 ticks
- **UI 0 button** → reset to original key
- Key display turns **amber** when shifted

---

## Loudness Normalisation

Press **NORM** on either deck panel to apply EBU R128 gain normalisation. Tracks hit a consistent level. Toggle per deck independently.

---

## Library

| Dot | Colour | Meaning |
|-----|--------|---------|
| ● | Green | Fully analysed — BPM, key, beat grid, cues, stems cached |
| ● | Blue | BPM/key done, stems not yet cached |
| ● | Amber | Beat grid done, some metadata missing |
| ● | Orange | BPM only |
| ○ | Grey | Not yet analysed |

**+ Add Folder** — replaces the library with a new folder scan.

**⟳ Analyse All** — queues all tracks for full analysis and stem pre-computation.

### Pre-compute stems for your whole library (do this before a gig)

```bash
python3 stem_example.py /path/to/your/music
```

Stems cached to `~/udj_cache/` as WAV files. Next load is instant.

---

## Stems — How It Works

U-DJ uses **htdemucs_6s** (6-stem ML model) to separate:

| Stem | Content |
|------|---------|
| Vocals | Voice, spoken word |
| Drums | Kick, snare, hats, cymbals |
| Bass | Sub-bass, bass line |
| Other | Remaining elements |
| Guitar / Lead | Lead synths, melodic lines |
| Piano / Pad | Chords, pads, sustained textures |

Falls back to htdemucs_ft (4-stem) if 6s is unavailable, with DSP split of "other" into lead/pad at 1500 Hz.

Post-processing tuned for **Detroit techno / electronic music**:
- Kick: sub-body preserved to 30 Hz, transient punch enhanced
- Bass: high-cut at 1200 Hz so 303 acid resonance passes through
- Drums: +2 dB hat air above 10 kHz, −3 dB de-mud at 300 Hz

Per-channel status in the mixer panel:

| Badge | Meaning |
|-------|---------|
| `○ NO TRACK` | Nothing loaded on this deck |
| `◌ STEMS LOADING…` | Separating (~90s first time, instant from cache) |
| `● STEMS READY` | Stem blend knobs are live |

---

## File Locations

| Path | Contents |
|------|----------|
| `~/.udj/library.db` | Track database |
| `~/.udj/events.db` | Sync/quantise event log |
| `~/udj_cache/<hash>/` | Stem WAV cache |

---

## Build from Source

### Requirements

- macOS (Apple Silicon recommended — MPS acceleration makes stem separation 5–10× faster)
- Python 3.9+
- Pioneer DDJ-200 (optional — app runs fine with keyboard/mouse)

### Dependencies

```bash
pip install PyQt6 sounddevice soundfile scipy numpy librosa pyloudnorm demucs torch torchaudio rtmidi pyinstaller
```

Or if you have a `requirements.txt`:

```bash
pip install -r requirements.txt
```

### Clone and run directly

```bash
git clone https://github.com/PahtrikProper/U-DJ.git
cd U-DJ
python3 main.py
```

### Build the .app bundle

```bash
cd U-DJ
python3 -m PyInstaller udj.spec --clean -y
rm -rf ~/Desktop/U-DJ.app
cp -R dist/U-DJ.app ~/Desktop/U-DJ.app
open ~/Desktop/U-DJ.app
```

The built app is fully self-contained — no Python install needed to run it.

### First launch notes

- On first launch macOS may ask to allow audio and MIDI access — approve both
- The first track you load will trigger a one-time download of the demucs ML model (~50 MB)
- After that, stems are cached locally and load instantly on repeat plays
- Library database is stored at `~/.udj/library.db` and persists across builds
