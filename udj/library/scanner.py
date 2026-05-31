"""
File scanner — walks directories and submits new audio files to the analyzer.
"""
from __future__ import annotations


import os
from pathlib import Path
from typing import Callable, Optional

from .db import AUDIO_EXTS, init_db, get_track
from .analyzer import analyse_many


def scan_directory(root: str,
                   on_progress: Optional[Callable[[int, int], None]] = None,
                   on_done: Optional[Callable[[], None]] = None):
    """
    Recursively scan root for audio files and add new ones to the library.
    on_progress(scanned, total) called as tracks are processed.
    """
    init_db()
    root_path = Path(root)
    paths = [
        str(p) for p in root_path.rglob("*")
        if p.suffix.lower() in AUDIO_EXTS
    ]

    if not paths:
        if on_done:
            on_done()
        return

    # Only analyse tracks not already in DB
    new_paths = [p for p in paths if not get_track(p)]
    total = len(new_paths)
    done = [0]

    def _each(meta):
        done[0] += 1
        if on_progress:
            on_progress(done[0], total)

    analyse_many(new_paths, on_each=_each, on_all_done=on_done)
    print(f"[Scanner] {len(paths)} files found, {total} new — analysing…")
