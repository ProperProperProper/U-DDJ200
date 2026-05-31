"""
Event logger — live SQLite logging for sync, snap, quantize, and grid events.

Uses a background thread with a bounded queue so audio callbacks never block.
Rows are flushed in batches every 500 ms or when 100 events accumulate.

DB: ~/.udj/events.db
Tables:
  sync_events     — per-second sync state (phase error, rates, BPMs)
  snap_events     — every bar/beat snap (play-start, SYNC button, drift correction)
  quantize_events — hot cue set/activate, loop arm/set snapped to grid
  grid_events     — per-second beat grid position (deck, beat index, bar phase)
"""
from __future__ import annotations

import sqlite3
import queue
import threading
import time
from pathlib import Path

_LOG_DB = Path.home() / ".udj" / "events.db"

_q: "queue.Queue[tuple]" = queue.Queue(maxsize=20_000)
_stop = threading.Event()


# ── schema ──────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS sync_events (
    ts            REAL NOT NULL,
    master_deck   INTEGER,
    follower_deck INTEGER,
    master_bpm    REAL,
    follower_bpm  REAL,
    master_rate   REAL,
    follower_rate REAL,
    phase_error   REAL,
    nominal       REAL
);

CREATE TABLE IF NOT EXISTS snap_events (
    ts              REAL NOT NULL,
    event_type      TEXT,
    follower_deck   INTEGER,
    master_bpm      REAL,
    follower_bpm    REAL,
    follower_rate   REAL,
    master_bar_phase REAL,
    f_bar1_sec      REAL,
    snapped_sec     REAL
);

CREATE TABLE IF NOT EXISTS quantize_events (
    ts           REAL NOT NULL,
    deck         INTEGER,
    event_type   TEXT,
    position_sec REAL,
    snapped_sec  REAL,
    bpm          REAL,
    delta_ms     REAL
);

CREATE TABLE IF NOT EXISTS grid_events (
    ts           REAL NOT NULL,
    deck         INTEGER,
    beat_idx     INTEGER,
    bar_idx      INTEGER,
    beat_phase   REAL,
    position_sec REAL,
    bpm          REAL
);

CREATE INDEX IF NOT EXISTS idx_sync_ts     ON sync_events(ts);
CREATE INDEX IF NOT EXISTS idx_snap_ts     ON snap_events(ts);
CREATE INDEX IF NOT EXISTS idx_quantize_ts ON quantize_events(ts);
CREATE INDEX IF NOT EXISTS idx_grid_ts     ON grid_events(ts);
"""


# ── background writer ────────────────────────────────────────────────────────

def _worker():
    _LOG_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(_LOG_DB))
    con.executescript(_DDL)
    con.commit()

    batch: list[tuple] = []
    last_flush = time.monotonic()

    while not _stop.is_set() or not _q.empty():
        try:
            item = _q.get(timeout=0.1)
            batch.append(item)
        except queue.Empty:
            pass

        now = time.monotonic()
        if batch and (now - last_flush >= 0.5 or len(batch) >= 100):
            _flush(con, batch)
            batch.clear()
            last_flush = now

    if batch:
        _flush(con, batch)
    con.close()


def _flush(con: sqlite3.Connection, batch: list[tuple]):
    sync  = [r[1:] for r in batch if r[0] == "sync"]
    snaps = [r[1:] for r in batch if r[0] == "snap"]
    quant = [r[1:] for r in batch if r[0] == "quantize"]
    grid  = [r[1:] for r in batch if r[0] == "grid"]

    if sync:
        con.executemany(
            "INSERT INTO sync_events VALUES (?,?,?,?,?,?,?,?,?)", sync)
    if snaps:
        con.executemany(
            "INSERT INTO snap_events VALUES (?,?,?,?,?,?,?,?,?)", snaps)
    if quant:
        con.executemany(
            "INSERT INTO quantize_events VALUES (?,?,?,?,?,?,?)", quant)
    if grid:
        con.executemany(
            "INSERT INTO grid_events VALUES (?,?,?,?,?,?,?)", grid)

    con.commit()


_thread = threading.Thread(target=_worker, daemon=True, name="event-logger")
_thread.start()


# ── public API ───────────────────────────────────────────────────────────────

def _put(item: tuple):
    try:
        _q.put_nowait(item)
    except queue.Full:
        pass   # never block the audio thread


def log_sync(master_deck: int, follower_deck: int,
             master_bpm: float, follower_bpm: float,
             master_rate: float, follower_rate: float,
             phase_error: float, nominal: float):
    _put(("sync", time.time(), master_deck, follower_deck,
          master_bpm, follower_bpm, master_rate, follower_rate,
          phase_error, nominal))


def log_snap(event_type: str, follower_deck: int,
             master_bpm: float, follower_bpm: float, follower_rate: float,
             master_bar_phase: float, f_bar1_sec: float, snapped_sec: float):
    _put(("snap", time.time(), event_type, follower_deck,
          master_bpm, follower_bpm, follower_rate,
          master_bar_phase, f_bar1_sec, snapped_sec))
    print(f"[EventLog] snap:{event_type}  deck={follower_deck}"
          f"  bar_phase={master_bar_phase:.3f}"
          f"  f_bar1={f_bar1_sec:.3f}s → snapped={snapped_sec:.3f}s"
          f"  follower={follower_bpm:.2f}bpm  rate={follower_rate:.4f}")


def log_quantize(deck: int, event_type: str,
                 position_sec: float, snapped_sec: float, bpm: float):
    delta_ms = (snapped_sec - position_sec) * 1000.0
    _put(("quantize", time.time(), deck, event_type,
          position_sec, snapped_sec, bpm, delta_ms))
    print(f"[EventLog] quantize:{event_type}  deck={deck}"
          f"  pos={position_sec:.3f}s → snap={snapped_sec:.3f}s"
          f"  Δ={delta_ms:+.1f}ms  bpm={bpm:.2f}")


def log_grid(deck: int, beat_idx: int, bar_idx: int,
             beat_phase: float, position_sec: float, bpm: float):
    _put(("grid", time.time(), deck, beat_idx, bar_idx,
          beat_phase, position_sec, bpm))
