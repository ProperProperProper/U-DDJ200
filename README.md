# U-DJ

Professional DJ app for macOS. Pioneer DDJ-200 controller. AI stem separation. Rekordbox-style beat sync.

<img width="1461" height="808" alt="Screenshot 2026-06-03 at 10 35 06 PM" src="https://github.com/user-attachments/assets/28111f23-e412-40ec-8ac9-6d7eb598b065" />

---

## Setup

1. Connect DDJ-200 via USB
2. Open `U-DJ.app` from your Desktop
3. Click **+ Add Folder** → select your music folder
4. Tracks analyse automatically (BPM, key, beat grid, cues)
5. Drag a track to each deck → press PLAY

---

## Sync

Press **SYNC** on the deck you want as **master**. The other deck snaps to match and locks tempo.

| Action | What happens |
|--------|-------------|
| SYNC on a deck | That deck becomes master, other deck snaps to bar phase |
| SYNC again on master | Re-snaps the slave to current bar phase |
| SYNC on slave (both playing) | Slave promotes to master, old master snaps |
| PLAY on slave (sync armed) | Slave snaps to master bar phase on play-start |

Sync holds itself — continuous phase nudge corrects drift automatically. The beat grid display shows both decks: when bar markers line up, you're locked.

---

## Stems

Stems load automatically when you put a track on a deck. The mixer shows a per-channel status badge:

| Badge | Meaning |
|-------|---------|
| `○ NO TRACK` | Nothing loaded |
| `◌ STEMS LOADING…` | Demucs separating (~90s first time, instant from cache) |
| `● STEMS READY` | Stems active — blend knobs live |

U-DJ uses **htdemucs_6s** (6-stem ML model) giving: **vocals, drums, bass, other, guitar/lead, piano/pad**. Falls back to htdemucs_ft (4-stem) if unavailable, with DSP split of "other" into lead and pad.

Post-processing is tuned for **Detroit techno / electronic music**:
- Kick: sub-body preserved down to 30 Hz, aggressive transient punch restore
- Bass: high-cut at 1200 Hz so 303 acid squeal/resonance passes through, +3 dB sub boost
- Drums: +2 dB hi-hat air above 10 kHz, -3 dB de-mud at 300 Hz
- Guitar/Lead: presence boost +2 dB at 2–5 kHz
- Piano/Pad: warmth boost +1.5 dB at 200–800 Hz

### Stem Blend Knobs

**3 knobs per deck** in the mixer panel. Each knob is a crossfader between two stems:

| Knob | Turn left | Center | Turn right |
|------|-----------|--------|------------|
| **VOC ←→ DRM** | Kill vocals (drums plays) | Both full | Kill drums (vocals plays) |
| **BSS ←→ OTH** | Kill bass (other plays) | Both full | Kill other (bass plays) |
| **LEAD ←→ PAD** | Kill lead synth (pad plays) | Both full | Kill pad (lead plays) |

Knobs reset to center on every track load. Cuts are hard at the extremes (no bleed). Knobs have no effect until **● STEMS READY** — the full track plays until then.

**Pre-compute stems for your whole library** (instant at the gig):
```bash
python3 stem_example.py /path/to/your/music
```
Cached to `~/udj_cache/`.

---

## EQ

**3 EQ knobs per deck** — HI / MID / LOW — sit alongside the stem blend knobs in the mixer panel.

Center = 0 dB. Full left = kill (−∞). Full right = +10 dB.

**Hold Shift on DDJ-200** then turn HI / MID / LOW hardware knobs for EQ.

---

## Color FX

**CFX Super Knob (Shift held)** — wet/dry depth. Select FX type from the mixer panel dropdown.

Available: FILTER · ECHO · ROLL · DELAY · REVERB · FLANGER · PHASER · DRIVE

Center = bypass on all effects.

---

## Hot Cues

Pad mode: **HOT CUE**

| Action | Result |
|--------|--------|
| Pad (empty) | Set cue at current position (snaps to beat) |
| Pad (set) | Jump to cue and play |
| Shift + pad | Delete cue |

Auto cue points (INTRO / DROP / BUILD / BREAK / OUTRO) load onto pads automatically from energy analysis.

---

## Beat Loop

Pad mode: **BEAT LOOP**

Pads left to right: `¼ · ½ · 1 · 2 · 4 · 8 · 16 · 32` bars

| Action | Result |
|--------|--------|
| Pad | Start loop at that size |
| Same pad again | Exit loop |
| Shift + pads 1–4 (loop active) | Halve loop length |
| Shift + pads 5–8 (loop active) | Double loop length |

Loop start snaps to nearest phrase boundary → bar → beat.

**Manual loop:**

| Action | Result |
|--------|--------|
| Shift + SYNC | Set loop IN (snaps to phrase/bar) |
| SYNC (second press) | Set loop OUT (snaps to bar) |

---

## Beat Jump

Pad mode: **BEAT JUMP**

| Pads 1–4 | Pads 5–8 |
|----------|----------|
| −1 bar | +1 bar |
| −2 bars | +2 bars |
| −4 bars | +4 bars |
| −8 bars | +8 bars |

---

## Pad FX

Pad mode: **PAD FX** — hold to apply, release to bypass

| Pad | Effect |
|-----|--------|
| 1 | Filter HPF |
| 2 | Filter LPF |
| 3 | Echo (BPM-synced) |
| 4 | Roll (BPM-synced) |
| 5 | Reverb |
| 6 | Flanger |
| 7 | Phaser |
| 8 | Echo Out (heavy) |

---

## Jog Wheel

| Action | Result |
|--------|--------|
| Touch surface | Scratch (disables sync momentarily) |
| Rim — no touch | Pitch bend |
| Rim + Shift | Key shift (±1 semitone per 5 ticks) |

---

## Key Shift & Key Sync

- **UI ▲/▼ buttons** or **Shift + jog rim** → shift pitch ±12 semitones
- **UI 0 button** → reset key
- **Shift + PFL** → Key Sync: auto-matches key to master deck using Camelot wheel (smallest shift ≤6 semitones)

Key display turns amber when shifted.

---

## CUE Button

| State | Press | Release |
|-------|-------|---------|
| Stopped, no cue set | Set cue at current position | — |
| Stopped, cue set | Play from cue (stutter hold) | Stop + return to cue |
| Playing | Return to cue + stop | — |
| Shift + CUE | Go to cue + stop immediately | — |

---

## Channel Fader & Crossfader

- Channel fader: per-deck volume
- Crossfader: constant-power (cosine law) A→B blend
- PFL: headphone cue per deck
- **Shift + PFL**: Key Sync

---

## Loudness Normalisation

The **NORM** button on each deck panel applies EBU R128 gain so tracks hit a consistent level. Toggle per deck.

---

## Library

| Dot | Colour | Meaning |
|-----|--------|---------|
| ● | Green | Fully analysed — BPM, key, beats, cues, stems cached |
| ● | Blue | BPM/key done — stems not yet cached |
| ● | Amber | Beats done — missing some metadata |
| ● | Orange | BPM only |
| ○ | Grey | Not analysed |

**⟳ Analyse All** — re-runs analysis and stem generation on every track.

**+ Add Folder** — clears the library and replaces it with a new folder scan.

---

## Pad Mode Cycling

**Shift + first BEAT LOOP pad** cycles the pad mode for that deck:

`HOT CUE → BEAT LOOP → STEMS → BEAT JUMP → PAD FX → (back to HOT CUE)`

Default mode on startup: BEAT LOOP.

---

## File Locations

| Path | Contents |
|------|----------|
| `~/.udj/library.db` | Track database |
| `~/.udj/events.db` | Sync/grid event log |
| `~/udj_cache/<hash>/` | Stem WAV cache |

---

## Build from Source

```bash
cd ~/Desktop/U-DJ
pip install -r requirements.txt
python3 -m PyInstaller udj.spec --clean -y
rm -rf ~/Desktop/U-DJ.app
cp -R dist/U-DJ.app ~/Desktop/U-DJ.app
```
