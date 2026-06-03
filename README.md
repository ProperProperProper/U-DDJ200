# U-DJ

Professional DJ app for macOS. Pioneer DDJ-200 controller. AI stem separation. Rekordbox-style beat sync.

<img width="1468" height="833" alt="U-DJ" src="https://github.com/user-attachments/assets/731037b5-a337-45db-a332-9e86a8faa33f" />

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

Stems load automatically when you put a track on a deck. The **⏳ STEMS** badge shows on the deck while demucs processes (~90 sec on Apple Silicon). Once cached the badge disappears — next load is instant.

**Stem knobs:**

| Knob | Controls | Shift held |
|------|----------|------------|
| HI | Vocals | EQ High |
| MID | Other / Melody | EQ Mid |
| LOW | Bass | EQ Low |
| CFX Super Knob | **Drums** | Color FX |

Center = full volume. Left = kill. Right of center = no change (no boost).

**Stem pads** (STEMS pad mode):
- Pads 1–4: toggle stem on/off (drums / bass / other / vocals)
- Pads 5–8: hold to solo (all others mute while held, restore on release)

**Pre-compute stems for your whole library** (so they're instant at the gig):
```bash
python3 stem_example.py /path/to/your/music
```
Cached to `~/udj_cache/` as WAV files.

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

## Color FX

**Shift + CFX knob** — wet/dry depth for the selected color FX. Select FX type from the mixer panel dropdown.

Available: FILTER · ECHO · ROLL · DELAY · REVERB · FLANGER · PHASER · DRIVE

---

## EQ

**Hold Shift + turn HI / MID / LOW** — 3-band EQ. Takes effect immediately from wherever the knob is. Full left = kill. Full right = +10 dB.

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

## Headphone Mix

**Master headphone volume** knob adjusts the monitor level.

---

## Loudness Normalisation

The **NORM** button on each deck panel applies EBU R128 gain so tracks hit a consistent level. Toggle per deck. Shown as `NORM +X.X dB` when active.

---

## Library

| Dot | Colour | Meaning |
|-----|--------|---------|
| ● | Green | Fully analysed — BPM, key, beats, cues, stems cached |
| ● | Blue | BPM/key done — stems not yet cached |
| ● | Amber | Beats done — missing some metadata |
| ● | Orange | BPM only |
| ○ | Grey | Not analysed |

**⟳ Analyse All** — re-runs analysis and stem generation on every track. Shows live count and CPU% while running.

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
| `~/udj_cache/<hash>/` | Stem WAV cache (vocals, drums, bass, other) |

---

## Build from Source

```bash
cd ~/Desktop/U-DJ
pip install -r requirements.txt
python3 -m PyInstaller udj.spec --clean -y
rm -rf ~/Desktop/U-DJ.app
cp -R dist/U-DJ.app ~/Desktop/U-DJ.app
```
