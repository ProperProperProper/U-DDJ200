"""
LibraryWidget — searchable track browser.
Double-click a track to load it into the selected deck.
Drag a row onto a deck to load there specifically.
"""
from __future__ import annotations


from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLineEdit,
                              QTableView, QPushButton, QLabel, QProgressBar,
                              QFileDialog, QHeaderView, QAbstractItemView,
                              QDialog, QTextEdit, QSizePolicy, QMessageBox)
from PyQt6.QtCore import Qt, QTimer, QSortFilterProxyModel, QAbstractTableModel, QModelIndex, QMimeData, QUrl, pyqtSignal, QObject, QFileSystemWatcher
from PyQt6.QtGui import QFont, QColor, QBrush
from typing import Callable, Optional

import psutil as _psutil
from ..library.db import all_tracks, init_db, clear_library, stems_are_cached, get_setting, set_setting, clear_stems_cache
from ..library.scanner import scan_directory


COLS    = ["", "Title", "Artist", "BPM", "Key", "Camelot", "Energy", "LUFS", "Duration", "Path"]
COL_MAP = ["_status", "title", "artist", "bpm", "key", "camelot", "energy", "loudness_lufs", "duration", "path"]


_inspecting_paths: set[str] = set()   # paths currently being inspected


def _analysis_status(row: dict) -> tuple[str, str]:
    """Return (label, color) indicating analysis + stems state clearly."""
    has_beats   = bool(row.get("beat_positions_json") not in (None, "", "[]"))
    has_phrases = bool(row.get("phrase_positions_json") not in (None, "", "[]"))
    has_lufs    = row.get("loudness_lufs") is not None
    has_camelot = bool(row.get("camelot"))
    has_bpm     = bool(row.get("bpm"))
    has_stems   = bool(row.get("stems_cached")) or bool(row.get("stems_failed"))
    inspected   = bool(row.get("stems_inspected"))
    full_analysis = has_beats and has_phrases and has_lufs and has_camelot and has_bpm

    path = row.get("path", "")
    if path in _inspecting_paths:
        return "🔬 INSPECTING", "#cc88ff"
    if full_analysis and has_stems and inspected:
        return "🔬 READY",    "#00ee88"
    if full_analysis and has_stems:
        return "✔ READY",    "#00ee88"
    if full_analysis:
        return "⏳ STEMS",   "#44aaff"
    if has_bpm and has_beats:
        return "⚙ ANALYSING","#ffaa00"
    return "✖ NO DATA",      "#ff4444"


class TrackModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self._rows: list[dict] = []

    def reload(self):
        self.beginResetModel()
        self._rows = all_tracks()
        self.endResetModel()

    def rowCount(self, _=QModelIndex()):    return len(self._rows)
    def columnCount(self, _=QModelIndex()): return len(COLS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        key = COL_MAP[index.column()]

        if key == "_status":
            dot, color = _analysis_status(row)
            if role == Qt.ItemDataRole.DisplayRole:
                return dot
            if role == Qt.ItemDataRole.ForegroundRole:
                return QBrush(QColor(color))
            if role == Qt.ItemDataRole.TextAlignmentRole:
                return Qt.AlignmentFlag.AlignCenter
            return None

        if role != Qt.ItemDataRole.DisplayRole:
            return None

        val = row.get(key, "")
        if key == "bpm" and val:
            return f"{float(val):.1f}"
        if key == "duration" and val:
            secs = float(val)
            return f"{int(secs)//60}:{secs%60:04.1f}"
        if key == "loudness_lufs" and val is not None:
            return f"{float(val):.1f}"
        if key == "energy" and val is not None:
            return f"{'█' * int(val)}{'░' * (10 - int(val))}"
        return str(val) if val else ""

    def track_at(self, row: int) -> Optional[dict]:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def mimeTypes(self) -> list[str]:
        return ["text/uri-list", "text/plain"]

    def mimeData(self, indexes) -> QMimeData:
        rows = {i.row() for i in indexes}
        md = QMimeData()
        urls = []
        paths = []
        for r in rows:
            track = self.track_at(r)
            if track and track.get("path"):
                urls.append(QUrl.fromLocalFile(track["path"]))
                paths.append(track["path"])
        md.setUrls(urls)
        if paths:
            md.setText(paths[0])
        return md

    def flags(self, index: QModelIndex):
        f = super().flags(index)
        if index.isValid():
            f |= Qt.ItemFlag.ItemIsDragEnabled
        return f


class LibraryWidget(QWidget):
    def __init__(self,
                 on_load_deck1: Optional[Callable[[dict], None]] = None,
                 on_load_deck2: Optional[Callable[[dict], None]] = None,
                 parent=None):
        super().__init__(parent)
        self.on_load_deck1 = on_load_deck1
        self.on_load_deck2 = on_load_deck2
        self._active_deck     = 0
        self.on_library_changed: Optional[Callable[[], None]] = None
        self._watched_folder  = ""
        self._watcher         = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._on_folder_changed)
        self._build_ui()
        init_db()
        self._model.reload()
        # Resume watching the last-used folder
        saved = get_setting("watched_folder")
        if saved:
            import os
            if os.path.isdir(saved):
                self._watcher.addPath(saved)
                self._watched_folder = saved

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(4)

        # Toolbar
        bar = QHBoxLayout()
        bar.setSpacing(8)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search tracks…")
        self._search.setObjectName("LibSearch")
        self._search.textChanged.connect(self._filter)

        # ── unified toolbar button style ───────────────────────────────────────
        # One base, accent color per semantic group. Height/font/radius identical.
        def _tbtn(label: str, accent: str, tooltip: str = "") -> QPushButton:
            b = QPushButton(label)
            if tooltip:
                b.setToolTip(tooltip)
            b.setFixedHeight(26)
            b.setStyleSheet(
                f"QPushButton{{background:#141420;border:1px solid {accent}55;"
                f"border-radius:3px;color:{accent};font-size:9px;"
                f"font-weight:bold;letter-spacing:0.5px;padding:0 10px;}}"
                f"QPushButton:hover{{background:#1c1c2a;border-color:{accent};"
                f"color:{accent};}}"
                f"QPushButton:pressed{{background:#0e0e18;}}"
                f"QPushButton:disabled{{opacity:0.35;}}")
            return b

        self._btn_scan = _tbtn("+ Add Folder", "#448866")
        self._btn_scan.clicked.connect(self._scan)

        self._btn_stems_inspect = _tbtn(
            "⬡ Inspect Stems", "#9966cc",
            "Inspect stems of loaded deck tracks — analyses quality")
        self._btn_stems_inspect.clicked.connect(self._run_stems_inspect)

        self._btn_inspect_all = _tbtn(
            "⬡ Inspect All", "#9966cc",
            "Inspect stems of all tracks in library")
        self._btn_inspect_all.clicked.connect(self._run_inspect_all)

        self._btn_d1 = _tbtn("→ Deck 1", "#3ab8d8")
        self._btn_d1.clicked.connect(lambda: self._load_selected(0))

        self._btn_d2 = _tbtn("→ Deck 2", "#d89840")
        self._btn_d2.clicked.connect(lambda: self._load_selected(1))

        self._btn_clear = _tbtn(
            "✕ Clear DB", "#cc4444",
            "Clear all tracks from library database")
        self._btn_clear.clicked.connect(self._clear_db)

        self._btn_clear_cache = _tbtn(
            "⊘ Clear Stems Cache", "#cc9922",
            "Delete all cached stem files and reset stems flags.")
        self._btn_clear_cache.clicked.connect(self._clear_stems_cache)

        self._cpu_label = QLabel("")
        self._cpu_label.setVisible(False)
        self._cpu_label.setFixedWidth(72)
        self._cpu_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cpu_label.setStyleSheet(
            "color:#ffaa00;background:#1a1200;border:1px solid #664400;"
            "border-radius:3px;font-size:9px;font-weight:bold;padding:1px 4px;")

        self._cpu_timer = QTimer(self)
        self._cpu_timer.setInterval(1000)
        self._cpu_timer.timeout.connect(self._update_cpu)


        bar.addWidget(self._search, 1)
        bar.addWidget(self._btn_scan)
        bar.addWidget(self._btn_clear)
        bar.addWidget(self._btn_clear_cache)
        bar.addWidget(self._btn_stems_inspect)
        bar.addWidget(self._btn_inspect_all)
        bar.addWidget(self._btn_d1)
        bar.addWidget(self._btn_d2)
        bar.addWidget(self._cpu_label)

        # Table
        self._model = TrackModel()
        self._proxy = QSortFilterProxyModel()
        self._proxy.setSourceModel(self._model)
        self._proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._proxy.setFilterKeyColumn(-1)  # all columns

        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setObjectName("LibTable")
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # Col 0 = status dot (narrow), col 1 = Title (stretch), rest fixed
        self._table.setColumnWidth(0, 90)   # status label
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setColumnWidth(3, 55)   # BPM
        self._table.setColumnWidth(4, 50)   # Key
        self._table.setColumnWidth(5, 52)   # Camelot
        self._table.setColumnWidth(6, 90)   # Energy bar
        self._table.setColumnWidth(7, 48)   # LUFS
        self._table.setColumnWidth(8, 52)   # Duration
        self._table.setColumnHidden(9, True)  # hide raw path
        self._table.setSortingEnabled(True)
        self._table.doubleClicked.connect(self._on_double_click)
        self._table.setAlternatingRowColors(True)
        # Enable drag out to deck widgets
        self._table.setDragEnabled(True)
        self._table.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)

        lay.addLayout(bar)
        lay.addWidget(self._table)

    def _filter(self, text: str):
        self._proxy.setFilterFixedString(text)

    def _clear_db(self):
        clear_library()
        self._model.reload()

    def _clear_stems_cache(self):
        count = clear_stems_cache()
        self._model.reload()
        QMessageBox.information(self, "Stems Cache Cleared",
                                f"Deleted {count} cached stem file(s).\n"
                                "Stems will be re-separated on next track load.")

    def _scan(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select Music Folder", "",
            QFileDialog.Option.DontUseNativeDialog)
        if not folder:
            return

        # Stop watching previous folder
        if self._watcher.directories():
            self._watcher.removePaths(self._watcher.directories())

        clear_library()
        self._model.reload()
        set_setting("watched_folder", folder)
        self._watched_folder = folder
        self._watcher.addPath(folder)

        def _done():
            self._model.reload()
            self._analyse_after_scan()
            if callable(getattr(self, 'on_folder_added', None)):
                self.on_folder_added()

        scan_directory(folder, on_done=_done)

    def _on_folder_changed(self, path: str):
        """Called by QFileSystemWatcher when a file is added/removed in the watched folder."""
        from ..library.db import get_track
        from ..library.analyzer import analyse_track
        from ..core.stems import preanalyse_stems
        import os
        from ..library.db import AUDIO_EXTS
        from pathlib import Path

        folder = getattr(self, '_watched_folder', path)
        existing = {t["path"] for t in all_tracks()}

        new_files = [
            str(p) for p in Path(folder).rglob("*")
            if p.suffix.lower() in AUDIO_EXTS and str(p) not in existing
        ]

        if not new_files:
            return

        print(f"[Watcher] {len(new_files)} new file(s) detected — scanning")

        def _on_track_done(meta):
            self._model.reload()
            # Kick off stem pre-analysis for the new track
            if meta and meta.get("path"):
                preanalyse_stems(
                    meta["path"], 44100,
                    track_meta=meta,
                    on_done=lambda _: self._model.reload(),
                )

        for fp in new_files:
            analyse_track(fp, on_done=_on_track_done, force=False)

    def _analyse_after_scan(self):
        """After scanning, run BPM/key/grid analysis on new tracks.
        Stems are loaded on-demand when a track is put on a deck — not pre-analysed here."""
        from ..library.analyzer import analyse_track

        tracks = all_tracks()
        for t in tracks:
            needs_analysis = not (t.get("bpm") and
                                  t.get("beat_positions_json") not in (None, "", "[]"))
            if needs_analysis:
                analyse_track(t["path"], on_done=lambda _: None, force=False)

    def _selected_track(self) -> Optional[dict]:
        idxs = self._table.selectedIndexes()
        if not idxs:
            return None
        src_row = self._proxy.mapToSource(idxs[0]).row()
        return self._model.track_at(src_row)

    def _load_selected(self, deck: int):
        track = self._selected_track()
        if not track:
            return
        cb = self.on_load_deck1 if deck == 0 else self.on_load_deck2
        if cb:
            cb(track)

    def _on_double_click(self, index):
        self._load_selected(self._active_deck)

    def set_active_deck(self, d: int):
        self._active_deck = d

    def load_selected(self, deck: int):
        self._load_selected(deck)

    # ── Stem inspection ───────────────────────────────────────────────────────

    def set_engine(self, engine):
        """Called from MainWindow after construction so we can access deck paths."""
        self._engine = engine

    def _run_inspect_all(self):
        selected = [t["path"] for t in self._model._rows if t.get("path")]
        self._launch_inspect(selected)

    def _run_stems_inspect(self):
        """Inspect selected library tracks (no deck load required)."""
        selected = []
        for idx in self._table.selectedIndexes():
            src_row = self._proxy.mapToSource(idx).row()
            t = self._model.track_at(src_row)
            if t and t.get("path") and t["path"] not in selected:
                selected.append(t["path"])

        if not selected:
            QMessageBox.warning(self, "No Selection",
                                "Select one or more tracks in the library first.")
            return
        self._launch_inspect(selected)

    def _launch_inspect(self, paths: list):
        if not paths:
            return
        global _inspecting_paths
        _inspecting_paths.update(paths)
        self._model.reload()

        def _on_done():
            _inspecting_paths.difference_update(paths)
            self._model.reload()

        dlg = StemInspectDialog(paths, self, on_complete=_on_done)
        dlg.exec()

    def analysis_progress_start(self, total: int):
        self._cpu_label.setVisible(True)
        _psutil.cpu_percent(interval=None)
        self._cpu_timer.start()

    def analysis_progress_update(self, done: int):
        pass

    def analysis_progress_done(self):
        self._cpu_timer.stop()
        self._cpu_label.setVisible(False)
        self._cpu_label.setText("")

    def _update_cpu(self):
        pct = _psutil.cpu_percent(interval=None)
        self._cpu_label.setText(f"CPU {pct:.0f}%")
        if pct >= 80:
            self._cpu_label.setStyleSheet(
                "color:#ff4400;background:#1a0800;border:1px solid #aa2200;"
                "border-radius:3px;font-size:9px;font-weight:bold;padding:1px 4px;")
        elif pct >= 50:
            self._cpu_label.setStyleSheet(
                "color:#ffaa00;background:#1a1200;border:1px solid #664400;"
                "border-radius:3px;font-size:9px;font-weight:bold;padding:1px 4px;")
        else:
            self._cpu_label.setStyleSheet(
                "color:#00dd88;background:#001a0e;border:1px solid #006633;"
                "border-radius:3px;font-size:9px;font-weight:bold;padding:1px 4px;")


# ── Stem Inspection Dialog ────────────────────────────────────────────────────

class _InspectWorker(QObject):
    finished  = pyqtSignal(str)
    error     = pyqtSignal(str)
    progress  = pyqtSignal(str)   # status updates while running

    def __init__(self, paths: list[str]):
        super().__init__()
        self._paths = paths

    def run(self):
        import pathlib, importlib.util, sys as _sys
        try:
            import numpy as np
            import soundfile as sf
            from ..core.stems import (STEM_NAMES as _STEM_NAMES, _cache_path,
                                      _load_cache, _save_cache, _separate_demucs)
            from ..library.db import log_stem_inspection, mark_stems_inspected, get_track

            sr = 44100
            all_metrics = []
            all_specs   = []

            _candidates = [
                pathlib.Path(getattr(_sys, '_MEIPASS', '')) / "dev_inspect_stems.py",
                pathlib.Path(__file__).parent.parent.parent / "dev_inspect_stems.py",
            ]
            dev = next((p for p in _candidates if p.exists()), None)
            if dev is None:
                self.error.emit("dev_inspect_stems.py not found in bundle or source tree.")
                return
            spec = importlib.util.spec_from_file_location("dev_inspect_stems", dev)
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            for path in self._paths:
                name = pathlib.Path(path).stem[:50]
                self.progress.emit(f"Loading stems: {name[:40]}…")

                # Load stems from disk cache, or run separation if not cached
                npz = _cache_path(path)
                stems = _load_cache(npz, sr) if npz and pathlib.Path(npz).exists() else None
                if stems is None:
                    self.progress.emit(f"Separating stems (first time): {name[:30]}…")
                    track_meta = get_track(path) or {}
                    stems = _separate_demucs(path, sr, track_meta)
                    _save_cache(stems, npz)
                    from ..library.db import mark_stems_cached
                    mark_stems_cached(path)

                stems = {k: v for k, v in stems.items()
                         if isinstance(v, np.ndarray) and k in _STEM_NAMES}

                orig, orig_sr = sf.read(path, dtype="float32", always_2d=True)
                if orig.shape[1] == 1:
                    orig = np.repeat(orig, 2, axis=1)
                if orig_sr != sr:
                    from math import gcd
                    from scipy.signal import resample_poly
                    g = gcd(orig_sr, sr)
                    orig = resample_poly(orig, sr // g, orig_sr // g).astype(np.float32)
                n = min(len(orig), min(len(v) for v in stems.values()))
                orig  = orig[:n]
                stems = {k: v[:n] for k, v in stems.items()}

                self.progress.emit(f"Computing metrics: {name[:40]}…")
                metrics_before = mod.compute_metrics(stems, orig, sr, name)

                self.progress.emit(f"Applying auto-fix: {name[:40]}…")
                fixed = mod.auto_fix_stems(stems, metrics_before, sr)
                _save_cache(fixed, npz)

                metrics_after = mod.compute_metrics(fixed, orig, sr, name)
                log_stem_inspection(path, metrics_before, metrics_after)
                mark_stems_inspected(path)
                metrics_before["_after"] = metrics_after
                spec_png = mod.make_spectrogram(fixed, sr, name)
                all_metrics.append(metrics_before)
                all_specs.append(spec_png)

            self.progress.emit("Generating report…")
            analysis = mod.analyse_with_claude(all_metrics, all_specs)
            self.finished.emit(analysis)

        except Exception as e:
            import traceback
            self.error.emit(f"Inspection failed:\n{traceback.format_exc()}")


class StemInspectDialog(QDialog):

    def __init__(self, paths: list[str], parent=None, on_complete=None):
        super().__init__(parent)
        self._on_complete = on_complete
        self.setWindowTitle("🔬 Stem Inspection")
        self.setMinimumSize(820, 600)
        self.setStyleSheet(
            "background:#0a0a14;color:#ccc;"
            "QTextEdit{background:#0e0e1c;border:1px solid #2a2a40;"
            "color:#ccc;font-family:'Menlo';font-size:11px;border-radius:4px;}"
            "QPushButton{background:#1a1a2a;border:1px solid #3a3a55;color:#aaa;"
            "border-radius:4px;padding:4px 14px;font-size:11px;font-weight:bold;}"
            "QPushButton:hover{background:#2a2a3a;color:#fff;}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        tracks_text = " + ".join(p.split("/")[-1][:40] for p in paths)
        hdr = QLabel(f"Inspecting: {tracks_text}")
        hdr.setStyleSheet("color:#aa66ff;font-weight:bold;font-size:11px;")
        hdr.setWordWrap(True)
        root.addWidget(hdr)

        self._status = QLabel("Starting…")
        self._status.setStyleSheet("color:#888;font-size:10px;")
        root.addWidget(self._status)

        self._text = QTextEdit()
        self._text.setReadOnly(True)
        self._text.setPlaceholderText("Running stem inspection…")
        root.addWidget(self._text, 1)

        btn_row = QHBoxLayout()
        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self.accept)
        btn_row.addStretch()
        btn_row.addWidget(self._close_btn)
        root.addLayout(btn_row)

        import threading
        self._worker = _InspectWorker(paths)
        self._worker.finished.connect(self._on_done)
        self._worker.error.connect(self._on_error)
        self._worker.progress.connect(self._on_progress)
        t = threading.Thread(target=self._worker.run, daemon=True)
        t.start()

    def _on_progress(self, msg: str):
        self._status.setText(msg)

    def _on_done(self, analysis: str):
        self._status.setText("Complete.")
        self._text.setPlainText(analysis)
        if self._on_complete:
            self._on_complete()

    def _on_error(self, msg: str):
        self._text.setPlainText(f"ERROR:\n{msg}")
