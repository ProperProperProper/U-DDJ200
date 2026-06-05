"""
DDJ-200 MIDI handler.

All MIDI addresses come from the Pioneer spec and from the Mixxx mapping
we already reverse-engineered.  This class translates raw MIDI into calls
on AudioEngine and sends LED feedback back to the controller.
"""
from __future__ import annotations


import threading
import rtmidi
from ..core.engine import AudioEngine
from ..core.deck import BEAT_LOOP_SIZES, STEM_NAMES

# Camelot wheel: maps Camelot code → semitone (C=0, C#=1, … B=11)
# Minor keys (A suffix) and major keys (B suffix) share the same pitch class.
_CAMELOT_TO_SEMITONE = {
    "1A": 8,  "1B": 11,  # Ab minor / B major
    "2A": 3,  "2B": 6,   # Eb minor / F# major
    "3A": 10, "3B": 1,   # Bb minor / Db major
    "4A": 5,  "4B": 8,   # F minor  / Ab major
    "5A": 0,  "5B": 3,   # C minor  / Eb major
    "6A": 7,  "6B": 10,  # G minor  / Bb major
    "7A": 2,  "7B": 5,   # D minor  / F major
    "8A": 9,  "8B": 0,   # A minor  / C major
    "9A": 4,  "9B": 7,   # E minor  / G major
    "10A": 11, "10B": 2, # B minor  / D major
    "11A": 6, "11B": 9,  # F# minor / A major
    "12A": 1, "12B": 4,  # C# minor / E major
}

def _camelot_semitone_diff(master_camelot: str, follower_camelot: str) -> int:
    """
    Return the number of semitones to shift follower to match master key.
    Chooses the smallest shift (±6 semitones max).
    """
    m = _CAMELOT_TO_SEMITONE.get(master_camelot.upper().strip())
    f = _CAMELOT_TO_SEMITONE.get(follower_camelot.upper().strip())
    if m is None or f is None:
        return 0
    diff = (m - f + 12) % 12
    if diff > 6:
        diff -= 12   # wrap to ±6
    return diff

# Beat jump sizes in bars (pads 0-7 → neg/pos pairs)
BEAT_JUMP_BARS = [-1.0, -2.0, -4.0, -8.0, 1.0, 2.0, 4.0, 8.0]

# ─────────────────────────────────────────────────────────────
#  MIDI address constants
# ─────────────────────────────────────────────────────────────

# Status bytes
CH = [0x90, 0x91]          # note-on per deck
CCH = [0xB0, 0xB1]         # CC per deck
PAD_N  = [0x97, 0x99]      # pad normal (hot cue)
PAD_S  = [0x98, 0x9A]      # pad shift (clear)
MASTER = 0x96

# Notes (shared)
NOTE_PLAY    = 0x0B
NOTE_CUE     = 0x0C
NOTE_CUE_SHF = 0x48
NOTE_SYNC    = 0x58
NOTE_SHIFT   = 0x3F
NOTE_PFL     = 0x54
NOTE_PFL_SHF = 0x68
NOTE_TOUCH   = 0x36

# CC notes
CC_RATE_MSB  = 0x00
CC_RATE_LSB  = 0x20
CC_JOG_SIDE  = 0x21
CC_JOG_SCR   = 0x22
CC_SEEK      = 0x29
CC_TRIM      = 0x27   # channel trim/gain knob — absorbed, not used
CC_VOL_MSB   = 0x13
CC_VOL_LSB   = 0x33
CC_EQ_HI     = 0x07   # → stem blend pair 0 (VOC↔DRM)
CC_EQ_MID    = 0x0B   # → stem blend pair 1 (BSS↔OTH)
CC_EQ_LOW    = 0x0F   # → stem blend pair 2 (LEAD↔PAD)
CC_CFX       = [0x17, 0x18]   # Color FX (Super Knob) — channel 0xB6, confirmed from Mixxx DDJ-200 mapping
CC_XF_MSB    = 0x1F
CC_XF_LSB    = 0x3F
CC_MASTER_HPS = 0x63          # headphone master

# Beat loop pad notes
BL_BASE = 0x60   # pads 0x60–0x67
# Sampler pad notes
SP_BASE = 0x30   # pads 0x30–0x37

# LED velocities
LED_ON   = 0x7F
LED_OFF  = 0x00
LED_RED  = 0x27   # pad hot-cue red


# ─────────────────────────────────────────────────────────────
#  Controller class
# ─────────────────────────────────────────────────────────────

class DDJ200:
    """Binds DDJ-200 MIDI I/O to an AudioEngine."""

    DDJ_PORT_NAME = "DDJ-200"

    def __init__(self, engine: AudioEngine):
        self.engine  = engine
        self.library = None   # set by main window after construction
        self._in  = rtmidi.MidiIn()
        self._out = rtmidi.MidiOut()

        # Per-deck transient state
        self._shift          = [False, False]
        self._vol_msb        = [0, 0]
        self._rate_msb       = [0, 0]
        self._xf_msb         = 0
        self._jog_touch      = [False, False]
        self._loop_recording  = [False, False]   # True while awaiting loop-out press
        # Pad mode per deck: cycles HOT CUE → BEAT LOOP → STEMS → BEAT JUMP → PAD FX
        self._pad_mode       = ["BEAT LOOP", "BEAT LOOP"]
        _PAD_MODES = ["HOT CUE", "BEAT LOOP", "BEAT JUMP", "PAD FX"]
        # STEMS removed — bottom row pads (5-8) always control stems in any mode
        self._PAD_MODES      = _PAD_MODES
        self._pfx_held       = [[False]*8, [False]*8]   # pad FX gate state per deck
        self._jog_key_accum  = [0, 0]                   # fractional key shift accumulator

        # EQ soft-pickup: last known physical knob position per (deck, band).
        # EQ only activates once the physical knob crosses the live EQ value.
        # Prevents sudden jumps when entering shift mode.
        self._eq_pickup_armed = [[True, True, True],   # deck0: hi/mid/low
                                 [True, True, True]]   # deck1: hi/mid/low
        self._eq_target       = [[63, 63, 63],          # physical knob target (0-127)
                                 [63, 63, 63]]          # 63 = centre = 0 dB

        self._connected = False
        self._synced    = [False, False]   # per-deck sync-mode state

    # ──────────────────────────────────────────────────────────
    #  Connection
    # ──────────────────────────────────────────────────────────

    def connect(self) -> bool:
        in_port  = self._find_port(self._in.get_ports())
        out_port = self._find_port(self._out.get_ports())

        if in_port is None or out_port is None:
            print("[DDJ-200] controller not found — running without MIDI")
            return False

        self._in.open_port(in_port)
        self._out.open_port(out_port)
        self._in.set_callback(self._midi_in)
        self._in.ignore_types(sysex=False, timing=False, active_sense=True)

        self._connected = True
        self._init_leds()
        print(f"[DDJ-200] connected")
        return True

    def _find_port(self, ports: list[str]) -> int | None:
        for i, p in enumerate(ports):
            if self.DDJ_PORT_NAME in p:
                return i
        return None

    def disconnect(self):
        if self._connected:
            self._leds_off()
            self._in.close_port()
            self._out.close_port()
            self._connected = False

    # ──────────────────────────────────────────────────────────
    #  MIDI in callback
    # ──────────────────────────────────────────────────────────

    def _midi_in(self, event, _data):
        msg, _delta = event
        if len(msg) < 2:
            return
        status, *data = msg
        self._dispatch(status, data)

    def _dispatch(self, status: int, data: list[int]):
        # Debug — log anything not handled so we can find CFX address
        pass  # handled below; unmatched messages fall through silently
        note = data[0] if data else 0
        val  = data[1] if len(data) > 1 else 0

        # ── Deck buttons ──────────────────────────────────────
        for d in range(2):
            ch = CH[d]
            if status == ch:
                if note == NOTE_SHIFT:
                    was_shift = self._shift[d]
                    self._shift[d] = bool(val)
                    if val and not was_shift:
                        # Shift engaged — EQ applies immediately from current knob position
                        self._eq_pickup_armed[d] = [False, False, False]
                    if not val and was_shift:
                        pass  # FX state is now persistent — CFX knob controls it
                    return

                if note == NOTE_PLAY and val:
                    deck = self.engine.deck(d)
                    was_paused = not deck.playing
                    deck.play_pause()
                    self._send(ch, NOTE_PLAY,
                               LED_ON if deck.playing else LED_OFF)
                    if deck.playing and deck.sync_follower:
                        if was_paused:
                            # Ensure just_started fires in _apply_sync
                            self.engine._was_playing[d] = False
                        self.engine.snap_sync(d)
                    return

                if note == NOTE_CUE:
                    deck = self.engine.deck(d)
                    if val:
                        if self._shift[d]:
                            deck.cue_gotoandstop()
                        elif not deck.playing and deck.cue_point >= 0:
                            # CDJ stutter: hold CUE = play from cue
                            deck.cue_stutter_start()
                        else:
                            deck.cue_default(True)
                    else:
                        # Release: if stutter was active (deck playing from cue), stop
                        if not self._shift[d] and deck.playing and deck.cue_point >= 0:
                            # Only stutter-stop if we started the play via CUE hold
                            # (heuristic: deck was stopped before, now playing from cue point)
                            deck.cue_stutter_stop()
                    self._update_cue_led(d)
                    return

                if note == NOTE_SYNC and val:
                    deck = self.engine.deck(d)
                    if self._shift[d]:
                        # Shift+Sync: arm loop start (rekordbox Loop In)
                        deck.beat_quantize_loop_start()
                        self._loop_recording[d] = True
                        self._send(CH[d], NOTE_SYNC, LED_ON)
                        return
                    if self._loop_recording[d]:
                        # Second Sync press: close the loop (Loop Out)
                        deck.beat_quantize_loop_end()
                        self._loop_recording[d] = False
                        self._send(CH[d], NOTE_SYNC, LED_OFF)
                        return
                    # Already follower → re-snap to master immediately
                    if deck.sync_follower:
                        self.engine.snap_sync(d)
                        print(f"[Sync] D{d+1} re-snapped to master")
                        return
                    # Otherwise: make this deck the follower, other deck the master
                    self._sync_as_follower(d)
                    return

                if note == NOTE_PFL and val:
                    if self._shift[d]:
                        # Shift+PFL: Key Sync — match this deck's key to master
                        self._key_sync(d)
                    else:
                        mixer = self.engine.mixer
                        mixer.pfl[d] = not mixer.pfl[d]
                        self._send(ch, NOTE_PFL,
                                   LED_ON if mixer.pfl[d] else LED_OFF)
                    return

                if note == NOTE_TOUCH:
                    if val:
                        self.engine.deck(d).jog_scratch_start()
                        self._jog_touch[d] = True
                        if self.engine.deck(d).sync_follower:
                            self._unsync_deck(d)  # follower scratch breaks sync
                    else:
                        self.engine.deck(d).jog_scratch_stop()
                        self._jog_touch[d] = False
                    return

        # ── CC (faders, jog, EQ, rate) ───────────────────────
        for d in range(2):
            cch = CCH[d]
            if status == cch:
                if note == CC_RATE_MSB:
                    self._rate_msb[d] = val; return
                if note == CC_RATE_LSB:
                    raw  = (self._rate_msb[d] << 7) | val
                    # DDJ-200 tempo slider: centre (0x1FFF) = 0 %
                    # top of slider = negative offset (faster in Pioneer's convention
                    # where slider up = faster).  We invert so slider UP = rate > 1.
                    # DDJ-200: slider UP = minus (slower), DOWN = plus (faster)
                    # Physical up → raw < centre → pct negative → rate < 1
                    centre = 0x2000
                    pct    = (raw - centre) / centre   # up=-1 (slow), down=+1 (fast)
                    rate   = 1.0 + pct * 0.16          # ±16 %
                    self.engine.deck(d).rate = max(0.5, min(2.0, rate))
                    # Master can freely adjust tempo — PLL tracks automatically.
                    # Only the follower manually overriding tempo breaks sync.
                    if self.engine.deck(d).sync_follower:
                        self._unsync_deck(d)
                    return
                if note == CC_VOL_MSB:
                    self._vol_msb[d] = val; return
                if note == CC_VOL_LSB:
                    raw = (self._vol_msb[d] << 7) | val
                    self.engine.mixer.set_volume_raw(d, raw)
                    return
                if note == CC_TRIM:
                    return   # trim/gain knob — absorbed
                if note == CC_EQ_HI:
                    if self._eq_apply(d, 0, val):
                        self.engine.mixer.set_eq_raw(d, "high", val)
                    return
                if note == CC_EQ_MID:
                    if self._eq_apply(d, 1, val):
                        self.engine.mixer.set_eq_raw(d, "mid", val)
                    return
                if note == CC_EQ_LOW:
                    if self._eq_apply(d, 2, val):
                        self.engine.mixer.set_eq_raw(d, "low", val)
                    return
                if note == CC_JOG_SIDE and not self._jog_touch[d]:
                    delta = val if val < 64 else val - 128
                    if self._shift[d]:
                        # Shift + jog rim = key shift ±1 semitone per 5 ticks
                        self._jog_key_accum[d] += delta
                        while self._jog_key_accum[d] >= 5:
                            self._jog_key_accum[d] -= 5
                            deck = self.engine.deck(d)
                            deck.key_shift = max(-12, min(12, deck.key_shift + 1))
                        while self._jog_key_accum[d] <= -5:
                            self._jog_key_accum[d] += 5
                            deck = self.engine.deck(d)
                            deck.key_shift = max(-12, min(12, deck.key_shift - 1))
                    else:
                        # Pitch bend: accumulated across callbacks so it
                        # outlasts the sync engine's nominal reset.
                        # Scale is larger so feel matches physical platter.
                        self.engine.deck(d).jog_bend(delta * 0.012)
                    return
                if note == CC_JOG_SCR and self._jog_touch[d]:
                    delta = val if val < 64 else val - 128
                    self.engine.deck(d).jog_scratch_tick(delta * 2.0)
                    return


        # ── Crossfader + CFX (all on 0xB6) ───────────────────
        if status == 0xB6:
            if note == CC_XF_MSB:
                self._xf_msb = val; return
            if note == CC_XF_LSB:
                raw = (self._xf_msb << 7) | val
                self.engine.mixer.set_crossfader_raw(raw)
                return
            if note == CC_CFX[0]:
                self.engine.mixer.set_fx_raw(0, val)
                return
            if note == CC_CFX[1]:
                self.engine.mixer.set_fx_raw(1, val)
                return

        # ── Performance pads — mode-aware ─────────────────────
        for d in range(2):
            mode = self._pad_mode[d]

            # Shift+hot-cue pad = clear cue (works in any mode while shift held)
            if status == PAD_S[d] and 0x00 <= note <= 0x07 and val:
                self.engine.deck(d).hotcue_clear(note)
                self._send(PAD_N[d], note, LED_OFF)
                return

            # Pad mode cycle: Shift + pad-bank select buttons (BL_BASE pad 0)
            # Use Shift + first pad of beat-loop bank to cycle modes
            if status == PAD_N[d] and note == BL_BASE and self._shift[d] and val:
                idx = (self._PAD_MODES.index(mode) + 1) % len(self._PAD_MODES)
                self._pad_mode[d] = self._PAD_MODES[idx]
                print(f"[DDJ] deck {d} pad mode → {self._pad_mode[d]}")
                self._refresh_pad_leds(d)
                return

            if status == PAD_N[d] and val:
                # Bottom row pads always toggle stems regardless of pad mode
                # HOT CUE bottom row = notes 0x04-0x07
                # BEAT LOOP bottom row = notes BL_BASE+4 to BL_BASE+7
                # Other modes bottom row = SP_BASE+4 to SP_BASE+7
                stem_idx = None
                if 0x04 <= note <= 0x07:
                    stem_idx = note - 0x04
                elif BL_BASE + 4 <= note <= BL_BASE + 7:
                    stem_idx = note - BL_BASE - 4
                elif SP_BASE + 4 <= note <= SP_BASE + 7:
                    stem_idx = note - SP_BASE - 4

                if stem_idx is not None and stem_idx < len(STEM_NAMES):
                    deck = self.engine.deck(d)
                    name = STEM_NAMES[stem_idx]
                    if deck.stems_ready:
                        deck.toggle_stem(name)
                        active = name in deck.active_stems
                        self._send(PAD_N[d], note, LED_ON if active else LED_OFF)
                    # Shift + stem pad = echo out: engage echo at high depth
                    if self._shift[d]:
                        self.engine.mixer.set_fx_type(d, "ECHO")
                        self.engine.mixer.set_fx_raw(d, 110)  # ~86% depth — long echo tail
                        # Track which stems have echo for UI display
                        if not hasattr(self.engine.deck(d), '_echo_stems'):
                            self.engine.deck(d)._echo_stems = set()
                        echo_set = self.engine.deck(d)._echo_stems
                        if name in echo_set:
                            echo_set.discard(name)
                        else:
                            echo_set.add(name)
                    return

                if mode == "HOT CUE" and 0x00 <= note <= 0x07:
                    self.engine.deck(d).hotcue_activate(note)
                    enabled = self.engine.deck(d).hot_cues[note] is not None
                    self._send(PAD_N[d], note, LED_RED if enabled else LED_OFF)
                    return

                if mode == "BEAT LOOP" and BL_BASE <= note <= BL_BASE + 7:
                    idx = note - BL_BASE
                    deck = self.engine.deck(d)
                    if self._shift[d] and deck.loop_active:
                        # Shift + lower half pads = halve; Shift + upper half = double
                        if idx < 4:
                            self._loop_halve(d)
                        else:
                            self._loop_double(d)
                    elif idx < len(BEAT_LOOP_SIZES):
                        deck.beatloop_toggle(BEAT_LOOP_SIZES[idx])
                        lit = deck.loop_active
                        self._send(PAD_N[d], note, LED_ON if lit else LED_OFF)
                    return

                if mode == "STEMS":
                    if SP_BASE <= note <= SP_BASE + 3:
                        # Pads 0-3: toggle stem
                        idx  = note - SP_BASE
                        name = STEM_NAMES[idx]
                        deck = self.engine.deck(d)
                        if deck.stems_ready:
                            deck.toggle_stem(name)
                            active = name in deck.active_stems
                            self._send(PAD_N[d], note, LED_ON if active else LED_OFF)
                        return
                    if SP_BASE + 4 <= note <= SP_BASE + 7:
                        # Pads 4-7: hold to solo (mute all others)
                        idx  = note - SP_BASE - 4
                        name = STEM_NAMES[idx]
                        deck = self.engine.deck(d)
                        if deck.stems_ready:
                            deck._solo_stem = name
                            for s in STEM_NAMES:
                                deck.stem_volumes[s] = 1.0 if s == name else 0.0
                            self._send(PAD_N[d], note, LED_ON)
                        return

                if mode == "BEAT JUMP" and SP_BASE <= note <= SP_BASE + 7:
                    idx = note - SP_BASE
                    if idx < len(BEAT_JUMP_BARS):
                        self.engine.deck(d).beat_jump(BEAT_JUMP_BARS[idx])
                    return

                if mode == "PAD FX" and SP_BASE <= note <= SP_BASE + 7:
                    idx = note - SP_BASE
                    self._pfx_held[d][idx] = True
                    self._apply_pad_fx(d, idx, True)
                    self._send(PAD_N[d], note, LED_ON)
                    return

            # Note-off for pad FX gate and stem solo release
            if status == PAD_N[d] and not val:
                if mode == "PAD FX" and SP_BASE <= note <= SP_BASE + 7:
                    idx = note - SP_BASE
                    self._pfx_held[d][idx] = False
                    self._apply_pad_fx(d, idx, False)
                    self._send(PAD_N[d], note, LED_OFF)
                    return
                if mode == "STEMS" and SP_BASE + 4 <= note <= SP_BASE + 7:
                    # Release stem solo
                    deck = self.engine.deck(d)
                    deck._solo_stem = None
                    for s in STEM_NAMES:
                        deck.stem_volumes[s] = 1.0
                    self._send(PAD_N[d], note, LED_OFF)
                    return

        # ── Unrecognised — print so we can identify CFX/other controls ──
        print(f"[MIDI?] status=0x{status:02X}  note=0x{note:02X}  val={val}")

    # ──────────────────────────────────────────────────────────
    #  Sync  (Mixxx master/follower model)
    # ──────────────────────────────────────────────────────────

    def _sync_as_follower(self, d: int):
        """
        Press SYNC on deck d → deck d becomes FOLLOWER, other deck is MASTER.
        Deck d snaps its beat phase to the master immediately.
        If the other deck has no audio/BPM, deck d is just armed as follower
        and will snap when the other deck loads.
        """
        other = self.engine.deck(1 - d)
        this  = self.engine.deck(d)

        # This deck = FOLLOWER, other deck = MASTER
        this.sync_follower  = True
        this.sync_master    = False
        other.sync_master   = True
        other.sync_follower = False

        sr = other.sample_rate
        m_pos_sec = other.position / max(sr, 1)
        print(f"[SYNC] D{d+1}=FOLLOWER  D{2-d}=MASTER"
              f"  master_pos={m_pos_sec:.3f}s  master_bpm={other.bpm*other.rate:.2f}"
              f"  follower_bpm={this.bpm:.2f}")

        # Snap THIS deck (follower) to the master now
        if other.audio is not None and other.bpm > 0 and this.audio is not None and this.bpm > 0:
            self.engine.snap_sync(d)

        self._synced[d]     = True
        self._synced[1 - d] = True
        self._send(CH[d],     NOTE_SYNC, LED_ON)
        self._send(CH[1 - d], NOTE_SYNC, LED_OFF)
        print(f"[Sync] D{d+1}=FOLLOWER locked to D{2-d}=MASTER  "
              f"bpm={other.bpm * other.rate:.2f}")

    def _sync_deck(self, d: int):
        """Legacy: keep for backwards compat, delegates to _sync_as_follower."""
        self._sync_as_follower(d)

    def _key_sync(self, d: int):
        """
        rekordbox KEY SYNC: shift pitch of deck d to match master key.
        Computes semitone difference using Camelot wheel then applies key_shift.
        """
        this  = self.engine.deck(d)
        other = self.engine.deck(1 - d)
        if not this._camelot or not other._camelot:
            print(f"[KeySync] missing Camelot data — cannot key sync")
            return
        semitones = _camelot_semitone_diff(other._camelot, this._camelot)
        this.key_shift = semitones
        print(f"[KeySync] deck {d}: {this._camelot} → {other._camelot}  shift={semitones:+d}st")

    def _loop_halve(self, d: int):
        """Halve the current loop length (Shift + any beat loop pad when loop active)."""
        self.engine.deck(d).loop_halve()
        print(f"[Loop] deck {d} halved → {self.engine.deck(d)._current_loop_beats:.2f} beats")

    def _loop_double(self, d: int):
        """Double the current loop length."""
        self.engine.deck(d).loop_double()
        print(f"[Loop] deck {d} doubled → {self.engine.deck(d)._current_loop_beats:.2f} beats")

    # PAD_FX_PARAMS: (fx_type, depth 0-127) applied to mixer channel while held
    # Matches rekordbox Beat FX palette on GRV6
    _PAD_FX_MAP = [
        ("FILTER",  13),   # Filter HPF sweep (left side)
        ("FILTER", 114),   # Filter LPF sweep (right side)
        ("ECHO",   102),   # Beat echo
        ("ROLL",   102),   # Beat roll
        ("REVERB", 102),   # Reverb
        ("FLANGER",102),   # Flanger
        ("PHASER",  102),  # Phaser
        ("ECHO",   120),   # Echo out (heavy feedback)
    ]

    def _apply_pad_fx(self, d: int, pad_idx: int, on: bool):
        if pad_idx >= len(self._PAD_FX_MAP):
            return
        fx_type, depth = self._PAD_FX_MAP[pad_idx]
        if on:
            self.engine.mixer.set_fx_type(d, fx_type)
            self.engine.mixer.set_fx_raw(d, depth)
        else:
            self.engine.mixer.set_fx_raw(d, 64)   # centre = bypass

    def _refresh_pad_leds(self, d: int):
        """Light pads according to current mode after a mode change."""
        mode = self._pad_mode[d]
        for i in range(8):
            self._send(PAD_N[d], i,            LED_OFF)
            self._send(PAD_N[d], BL_BASE + i,  LED_OFF)
            self._send(PAD_N[d], SP_BASE + i,  LED_OFF)
        if mode == "HOT CUE":
            for i in range(8):
                lit = self.engine.deck(d).hot_cues[i] is not None
                self._send(PAD_N[d], i, LED_RED if lit else LED_OFF)
        elif mode == "BEAT LOOP":
            deck = self.engine.deck(d)
            for i, sz in enumerate(BEAT_LOOP_SIZES):
                lit = deck.loop_active and abs(deck._current_loop_beats - sz) < 0.01
                self._send(PAD_N[d], BL_BASE + i, LED_ON if lit else LED_OFF)
        elif mode == "STEMS":
            deck = self.engine.deck(d)
            for i, name in enumerate(STEM_NAMES):
                if deck.stems_ready:
                    active = name in deck.active_stems
                    self._send(PAD_N[d], SP_BASE + i, LED_ON if active else LED_OFF)

    def _eq_apply(self, deck: int, band: int, val: int) -> bool:
        """
        Soft-pickup for EQ knobs.

        When shift is first pressed, the physical knob position may differ from
        the live EQ value. We only apply the change once the knob has crossed
        the target position (±5 counts) — preventing sudden level jumps.

        Returns True when the knob is picked up and EQ should be updated.
        """
        if not self._eq_pickup_armed[deck][band]:
            # Already picked up — pass all values through
            self._eq_target[deck][band] = val
            return True

        target = self._eq_target[deck][band]
        if abs(val - target) <= 5:
            # Knob has reached target — disengage pickup mode
            self._eq_pickup_armed[deck][band] = False
            self._eq_target[deck][band] = val
            return True

        # Still hunting — update direction tracking but don't apply
        return False

    def _unsync_deck(self, d: int):
        for i in range(2):
            deck = self.engine.deck(i)
            deck.sync_follower = False
            deck.sync_master   = False
            self._synced[i]    = False
            self._send(CH[i], NOTE_SYNC, LED_OFF)

    # ──────────────────────────────────────────────────────────
    #  LED helpers
    # ──────────────────────────────────────────────────────────

    def _send(self, status: int, note: int, vel: int):
        if self._connected:
            self._out.send_message([status, note, vel])

    def _update_cue_led(self, d: int):
        set_ = self.engine.deck(d).cue_point >= 0
        self._send(CH[d], NOTE_CUE, LED_ON if set_ else LED_OFF)

    def _init_leds(self):
        """Light up initial LED state after connection."""
        for d in range(2):
            self._send(CH[d], NOTE_PLAY, LED_OFF)
            self._send(CH[d], NOTE_CUE,  LED_OFF)
            self._send(CH[d], NOTE_SYNC, LED_OFF)
            for i in range(8):
                self._send(PAD_N[d], i,          LED_OFF)
                self._send(PAD_N[d], BL_BASE + i, LED_OFF)
                self._send(PAD_N[d], SP_BASE + i, LED_OFF)

    def _leds_off(self):
        self._init_leds()

    # ──────────────────────────────────────────────────────────
    #  UI-driven updates (called from UI when state changes)
    # ──────────────────────────────────────────────────────────

    def update_play_led(self, d: int, playing: bool):
        self._send(CH[d], NOTE_PLAY, LED_ON if playing else LED_OFF)

    def update_hotcue_led(self, d: int, n: int, set_: bool):
        self._send(PAD_N[d], n, LED_RED if set_ else LED_OFF)

    def update_loop_led(self, d: int, active: bool, pad_idx: int):
        self._send(PAD_N[d], BL_BASE + pad_idx, LED_ON if active else LED_OFF)
