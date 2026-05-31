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
CC_VOL_MSB   = 0x13
CC_VOL_LSB   = 0x33
CC_EQ_HI     = 0x07
CC_EQ_MID    = 0x0B
CC_EQ_LOW    = 0x0F
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
                        # Shift engaged — re-arm EQ pickup so knobs don't jump
                        self._eq_pickup_armed[d] = [True, True, True]
                    if not val and was_shift:
                        # Shift released — kill FX on this deck (back to bypass)
                        self.engine.mixer.set_fx_raw(d, 64)
                    return

                if note == NOTE_PLAY and val:
                    deck = self.engine.deck(d)
                    deck.play_pause()
                    self._send(ch, NOTE_PLAY,
                               LED_ON if deck.playing else LED_OFF)
                    # Re-snap if resuming as a sync follower — re-sync from master's perspective
                    if deck.playing and deck.sync_follower:
                        master_d = 1 - d
                        self._sync_deck(master_d)  # re-press master's sync to re-snap follower
                    return

                if note == NOTE_CUE and val:
                    if self._shift[d]:
                        self.engine.deck(d).cue_gotoandstop()
                    else:
                        self.engine.deck(d).cue_default(True)
                    self._update_cue_led(d)
                    return

                if note == NOTE_SYNC and val:
                    deck = self.engine.deck(d)
                    if self._shift[d]:
                        # Shift+Sync: toggle quantize on/off for this deck
                        deck.quantize = not deck.quantize
                        self._send(CH[d], NOTE_SYNC,
                                   LED_ON if deck.quantize else LED_OFF)
                        print(f"[DDJ] deck {d} quantize={'ON' if deck.quantize else 'OFF'}")
                        return
                    if self._loop_recording[d]:
                        # Second Sync press: close the loop
                        deck.beat_quantize_loop_end()
                        self._loop_recording[d] = False
                        self._send(CH[d], NOTE_SYNC, LED_OFF)
                        return
                    # Normal Sync: THIS deck becomes master, other deck follows
                    self._sync_deck(d)
                    return

                if note == NOTE_PFL and val:
                    if self._shift[d]:
                        pass  # 4-deck toggle placeholder
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
                if note == CC_EQ_HI:
                    if self._shift[d]:
                        if self._eq_apply(d, 0, val):
                            self.engine.mixer.set_eq_raw(d, "high", val)
                    else:
                        self.engine.deck(d).set_stem_volume("vocals", val / 127.0)
                    return
                if note == CC_EQ_MID:
                    if self._shift[d]:
                        if self._eq_apply(d, 1, val):
                            self.engine.mixer.set_eq_raw(d, "mid", val)
                    else:
                        self.engine.deck(d).set_stem_volume("bass", val / 127.0)
                    return
                if note == CC_EQ_LOW:
                    if self._shift[d]:
                        if self._eq_apply(d, 2, val):
                            self.engine.mixer.set_eq_raw(d, "low", val)
                    else:
                        self.engine.deck(d).set_stem_volume("drums", val / 127.0)
                    return
                if note == CC_JOG_SIDE and not self._jog_touch[d]:
                    delta = val if val < 64 else val - 128
                    if self._shift[d]:
                        if self.engine.deck(d).audio is not None:
                            total = len(self.engine.deck(d).audio)
                            seek  = delta * total // 256
                            self.engine.deck(d).position = max(
                                0.0, min(total - 2,
                                         self.engine.deck(d).position + seek))
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
                if self._shift[0]:
                    self.engine.mixer.set_fx_raw(0, val)   # shift = color FX always
                else:
                    self.engine.deck(0).set_stem_volume("other", val / 127.0)
                return
            if note == CC_CFX[1]:
                if self._shift[1]:
                    self.engine.mixer.set_fx_raw(1, val)   # shift = color FX always
                else:
                    self.engine.deck(1).set_stem_volume("other", val / 127.0)
                return

        # ── Hot cue pads ──────────────────────────────────────
        for d in range(2):
            if status == PAD_N[d] and 0x00 <= note <= 0x07 and val:
                self.engine.deck(d).hotcue_activate(note)
                enabled = self.engine.deck(d).hot_cues[note] is not None
                self._send(PAD_N[d], note, LED_RED if enabled else LED_OFF)
                return
            if status == PAD_S[d] and 0x00 <= note <= 0x07 and val:
                self.engine.deck(d).hotcue_clear(note)
                self._send(PAD_N[d], note, LED_OFF)
                return

            # ── Beat loop pads ────────────────────────────────
            if status == PAD_N[d] and BL_BASE <= note <= BL_BASE + 7 and val:
                idx = note - BL_BASE
                if idx < len(BEAT_LOOP_SIZES):
                    self.engine.deck(d).beatloop_toggle(BEAT_LOOP_SIZES[idx])
                    lit = self.engine.deck(d).loop_active
                    self._send(PAD_N[d], note, LED_ON if lit else LED_OFF)
                return

            # ── Sampler pads → Stem toggles ───────────────────
            # Pad 0=drums  1=bass  2=other  3=vocals
            if status == PAD_N[d] and SP_BASE <= note <= SP_BASE + 3 and val:
                idx  = note - SP_BASE
                name = STEM_NAMES[idx]
                deck = self.engine.deck(d)
                if deck.stems_ready:
                    deck.toggle_stem(name)
                    active = name in deck.active_stems
                    self._send(PAD_N[d], note, LED_ON if active else LED_OFF)
                return

        # ── Unrecognised — print so we can identify CFX/other controls ──
        print(f"[MIDI?] status=0x{status:02X}  note=0x{note:02X}  val={val}")

    # ──────────────────────────────────────────────────────────
    #  Sync  (Mixxx master/follower model)
    # ──────────────────────────────────────────────────────────

    def _sync_deck(self, d: int):
        """
        Press SYNC on deck d → deck d becomes MASTER, other deck FOLLOWS.
        Tempo-matches and bar-snaps immediately, then PLL maintains lock.
        """
        master   = self.engine.deck(d)
        follower = self.engine.deck(1 - d)

        if master.bpm <= 0 or master.audio is None:
            print(f"[Sync] deck {d} has no BPM — cannot set as master")
            return

        # Mark roles (master never changes its own rate)
        master.sync_master     = True
        master.sync_follower   = False
        follower.sync_follower = True
        follower.sync_master   = False

        # Snap follower's tempo + phase to master using engine's accurate method
        self.engine.snap_sync(1 - d)

        # LEDs: master LED on, follower LED off
        self._synced[d]     = True
        self._synced[1 - d] = True
        self._send(CH[d],     NOTE_SYNC, LED_ON)
        self._send(CH[1 - d], NOTE_SYNC, LED_OFF)
        print(f"[Sync] deck {d}=MASTER  deck {1-d}=FOLLOWER  "
              f"bpm={master.bpm * master.rate:.2f}")

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
