"""
SQLite-backed track library.
Schema: tracks(id, path, title, artist, album, bpm, key, duration, bitrate,
               last_played, play_count, date_added)
"""
from __future__ import annotations


import sqlite3
import os
import time
from pathlib import Path
from typing import Optional


DB_PATH = Path.home() / ".udj" / "library.db"

AUDIO_EXTS = {".mp3", ".flac", ".wav", ".aiff", ".aif", ".ogg", ".m4a", ".opus"}


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS tracks (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                path                TEXT UNIQUE NOT NULL,
                title               TEXT,
                artist              TEXT,
                album               TEXT,
                bpm                 REAL,
                key                 TEXT,
                duration            REAL,
                bitrate             INTEGER,
                first_beat_sec        REAL    DEFAULT 0.0,
                beat_positions_json   TEXT    DEFAULT '[]',
                phrase_positions_json TEXT    DEFAULT '[]',
                loudness_lufs         REAL    DEFAULT NULL,
                gain_db               REAL    DEFAULT 0.0,
                energy                INTEGER DEFAULT NULL,
                camelot               TEXT    DEFAULT NULL,
                cue_points_json       TEXT    DEFAULT '[]',
                downbeat_offset       INTEGER DEFAULT 0,
                last_played         REAL,
                play_count          INTEGER DEFAULT 0,
                date_added          REAL
            )
        """)
        for col, defval in [("first_beat_sec", "0.0"),
                            ("beat_positions_json", "'[]'"),
                            ("phrase_positions_json", "'[]'"),
                            ("loudness_lufs", "NULL"),
                            ("gain_db", "0.0"),
                            ("energy", "NULL"),
                            ("camelot", "NULL"),
                            ("cue_points_json", "'[]'"),
                            ("downbeat_offset", "0")]:
            try:
                con.execute(f"ALTER TABLE tracks ADD COLUMN {col} DEFAULT {defval}")
            except Exception:
                pass
        con.execute("CREATE INDEX IF NOT EXISTS idx_artist ON tracks(artist)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_bpm    ON tracks(bpm)")
        con.commit()


def upsert_track(meta: dict):
    """Insert or update a track record."""
    with _conn() as con:
        con.execute("""
            INSERT INTO tracks (path, title, artist, album, bpm, key,
                                duration, bitrate, first_beat_sec,
                                beat_positions_json, phrase_positions_json,
                                loudness_lufs, gain_db, energy, camelot,
                                cue_points_json, downbeat_offset, date_added)
            VALUES (:path, :title, :artist, :album, :bpm, :key,
                    :duration, :bitrate, :first_beat_sec,
                    :beat_positions_json, :phrase_positions_json,
                    :loudness_lufs, :gain_db, :energy, :camelot,
                    :cue_points_json, :downbeat_offset, :date_added)
            ON CONFLICT(path) DO UPDATE SET
                title                 = excluded.title,
                artist                = excluded.artist,
                album                 = excluded.album,
                bpm                   = excluded.bpm,
                key                   = excluded.key,
                duration              = excluded.duration,
                bitrate               = excluded.bitrate,
                first_beat_sec        = excluded.first_beat_sec,
                beat_positions_json   = excluded.beat_positions_json,
                phrase_positions_json = excluded.phrase_positions_json,
                loudness_lufs         = excluded.loudness_lufs,
                gain_db               = excluded.gain_db,
                energy                = excluded.energy,
                camelot               = excluded.camelot,
                cue_points_json       = excluded.cue_points_json,
                downbeat_offset       = excluded.downbeat_offset
        """, {**meta,
              "first_beat_sec":        meta.get("first_beat_sec", 0.0),
              "beat_positions_json":   meta.get("beat_positions_json", "[]"),
              "phrase_positions_json": meta.get("phrase_positions_json", "[]"),
              "loudness_lufs":         meta.get("loudness_lufs"),
              "gain_db":               meta.get("gain_db", 0.0),
              "energy":                meta.get("energy"),
              "camelot":               meta.get("camelot"),
              "cue_points_json":       meta.get("cue_points_json", "[]"),
              "downbeat_offset":       int(meta.get("downbeat_offset", 0) or 0),
              "date_added":            meta.get("date_added", time.time())})
        con.commit()


def clear_library():
    """Delete all tracks from the library database."""
    with _conn() as con:
        con.execute("DELETE FROM tracks")
        con.commit()


def record_play(path: str):
    with _conn() as con:
        con.execute("""
            UPDATE tracks
            SET last_played = ?, play_count = play_count + 1
            WHERE path = ?
        """, (time.time(), path))
        con.commit()


def search(query: str = "", order: str = "artist") -> list[dict]:
    allowed = {"artist", "title", "bpm", "date_added", "last_played"}
    order_col = order if order in allowed else "artist"
    q = f"%{query}%"
    with _conn() as con:
        rows = con.execute(f"""
            SELECT * FROM tracks
            WHERE title LIKE ? OR artist LIKE ? OR album LIKE ?
            ORDER BY {order_col}
        """, (q, q, q)).fetchall()
    return [dict(r) for r in rows]


def all_tracks(order: str = "artist") -> list[dict]:
    return search("", order)


def get_track(path: str) -> Optional[dict]:
    with _conn() as con:
        row = con.execute("SELECT * FROM tracks WHERE path = ?",
                          (path,)).fetchone()
    return dict(row) if row else None
