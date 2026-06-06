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
from PyQt6.QtCore import Qt, QTimer, QSortFilterProxyModel, QAbstractTableModel, QModelIndex, QMimeData, QUrl, pyqtSignal, QObject
from PyQt6.QtGui import QFont, QColor, QBrush
from typing import Callable, Optional

import psutil as _psutil
from ..library.db import all_tracks, init_db, clear_library, stems_are_cached
from ..library.scanner import scan_directory


COLS    = ["", "Title", "Artist", "BPM", "Key", "Camelot", "Energy", "LUFS", "Duration", "Path"]
COL_MAP = ["_status", "title", "artist", "bpm", "key", "camelot", "energy", "loudness_lufs", "duration", "path"]


def _analysis_status(row: dict) -> tuple[str, str]:
    """Return (label, color) indicating analysis + stems state clearly."""
    has_beats   = bool(row.get("beat_positions_json") not in (None, "", "[]"))
    has_phrases = bool(row.get("phrase_positions_json") not in (None, "", "[]"))
    has_lufs    = row.get("loudness_lufs") is not None
    has_camelot = bool(row.get("camelot"))
    has_bpm     = bool(row.get("bpm"))
    has_stems   = bool(row.get("stems_cached")) or bool(row.get("stems_failed"))
    full_analysis = has_beats and has_phrases and has_lufs and has_camelot and has_bpm

    if full_analysis and has_stems:
        return "✔ READY",    "#00ee88"   # bright green — everything done
    if full_analysis:
        return "⏳ STEMS",   "#44aaff"   # blue — analysis done, stems pending
    if has_bpm and has_beats:
        return "⚙ ANALYSING","#ffaa00"   # amber — partial
    return "✖ NO DATA",      "#ff4444"   # red — not analysed


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
        self._active_deck  = 0
        self._build_ui()
        init_db()
        self._model.reload()

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

        self._btn_scan = QPushButton("+ Add Folder")
        self._btn_scan.setObjectName("BtnScan")
        self._btn_scan.clicked.connect(self._scan)

        self._btn_stems_inspect = QPushButton("🔬 Inspect Stems")
        self._btn_stems_inspect.setObjectName("BtnScan")
        self._btn_stems_inspect.setToolTip(
            "Inspect stems of loaded deck tracks with Claude — analyses quality "
            "and recommends post-processing improvements")
        self._btn_stems_inspect.setStyleSheet(
            "QPushButton{background:#111;border:1px solid #2a1a3a;color:#aa66ff;"
            "border-radius:3px;padding:2px 8px;font-size:10px;font-weight:bold;}"
            "QPushButton:disabled{background:#1a1228;border:1px solid #aa66ff;"
            "color:#cc99ff;}"
        )
        self._btn_stems_inspect.clicked.connect(self._run_stems_inspect)

        self._btn_inspect_all = QPushButton("🔬 Inspect All")
        self._btn_inspect_all.setObjectName("BtnScan")
        self._btn_inspect_all.setToolTip("Inspect stems of all tracks in library with Claude")
        self._btn_inspect_all.setStyleSheet(
            "QPushButton{background:#111;border:1px solid #1a2a3a;color:#66aaff;"
            "border-radius:3px;padding:2px 8px;font-size:10px;font-weight:bold;}"
            "QPushButton:disabled{background:#121828;border:1px solid #66aaff;"
            "color:#99ccff;}"
        )
        self._btn_inspect_all.clicked.connect(self._run_inspect_all)

        self._btn_d1 = QPushButton("→ Deck 1")
        self._btn_d1.setObjectName("BtnLoadDeck")
        self._btn_d1.clicked.connect(lambda: self._load_selected(0))

        self._btn_d2 = QPushButton("→ Deck 2")
        self._btn_d2.setObjectName("BtnLoadDeck")
        self._btn_d2.clicked.connect(lambda: self._load_selected(1))

        self._btn_clear = QPushButton("✕ Clear DB")
        self._btn_clear.setObjectName("BtnScan")
        self._btn_clear.setToolTip("Clear all tracks from library database")
        self._btn_clear.setStyleSheet(
            "QPushButton{background:#111;border:1px solid #3a1a1a;color:#aa4444;"
            "border-radius:3px;padding:2px 8px;font-size:10px;font-weight:bold;}"
            "QPushButton:hover{border:1px solid #cc4444;color:#ff6666;}"
        )
        self._btn_clear.clicked.connect(self._clear_db)

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

    def _scan(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Music Folder")
        if not folder:
            return

        # Clear existing library — show only what's in the chosen folder
        clear_library()
        self._model.reload()

        def _done():
            self._model.reload()
            self._analyse_after_scan()
            # Notify main window so it can run stems + inspect when ready
            if callable(getattr(self, 'on_folder_added', None)):
                self.on_folder_added()

        scan_directory(folder, on_done=_done)

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
        """Inspect stems for every track in the library."""
        paths = [t["path"] for t in self._model._rows if t.get("path")]
        if not paths:
            QMessageBox.warning(self, "Empty Library", "No tracks in library.")
            return
        dlg = StemInspectDialog(paths, parent=self)
        dlg.exec()

    def _run_stems_inspect(self):
        """Run dev_inspect_stems on the tracks currently loaded on decks."""
        engine = getattr(self, "_engine", None)
        if engine is None:
            QMessageBox.warning(self, "No Engine", "Engine not connected.")
            return

        paths = []
        for i in range(2):
            deck = engine.deck(i)
            if deck.audio is not None and deck.file_path:
                paths.append(deck.file_path)

        if not paths:
            QMessageBox.warning(self, "No Tracks Loaded",
                                "Load tracks on the decks first, then run inspection.")
            return

        dlg = StemInspectDialog(paths, parent=self)
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
    finished  = pyqtSignal(str)   # analysis text
    error     = pyqtSignal(str)

    def __init__(self, paths: list[str]):
        super().__init__()
        self._paths = paths

    def run(self):
        import sys, pathlib, json, base64, io
        sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent))
        try:
            import numpy as np
            import soundfile as sf
            from ..core.stems import STEM_NAMES as _STEM_NAMES
            from ..core.stems import _cache_path, _load_cache
            import importlib.util, sys as _sys

            sr = 44100
            all_metrics = []
            all_specs   = []

            # Import metric helpers from dev_inspect_stems
            # Check bundle root (PyInstaller) then source tree
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

            skipped = []
            for path in self._paths:
                name = pathlib.Path(path).stem[:50]
                npz = _cache_path(path)
                stems = _load_cache(npz, sr) if npz and pathlib.Path(npz).exists() else None
                if stems is None:
                    skipped.append(name)
                    continue

                stems.pop("_vocal_present", None)
                # Keep only the 4 primary stems
                stems = {k: v for k, v in stems.items() if k in _STEM_NAMES or k == "_vocal_present"}

                orig, orig_sr = sf.read(path, dtype="float32", always_2d=True)
                if orig.shape[1] == 1:
                    orig = np.repeat(orig, 2, axis=1)
                if orig_sr != sr:
                    from math import gcd
                    from scipy.signal import resample_poly
                    g = gcd(orig_sr, sr)
                    orig = resample_poly(orig, sr // g, orig_sr // g).astype(np.float32)
                n = min(len(orig), min(len(v) for v in stems.values() if isinstance(v, np.ndarray)))
                orig = orig[:n]
                stems = {k: v[:n] for k, v in stems.items() if isinstance(v, np.ndarray)}

                metrics = mod.compute_metrics(stems, orig, sr, name)
                spec_png = mod.make_spectrogram(stems, sr, name)
                all_metrics.append(metrics)
                all_specs.append(spec_png)

            if not all_metrics:
                self.error.emit("No tracks with cached stems found. Load tracks on decks and wait for ● STEMS READY.")
                return
            analysis = mod.analyse_with_claude(all_metrics, all_specs)
            if skipped:
                note = f"\n\n---\n⚠ Skipped {len(skipped)} track(s) without stems: {', '.join(skipped[:5])}"
                if len(skipped) > 5:
                    note += f" +{len(skipped)-5} more"
                analysis += note
            self.finished.emit(analysis)

        except Exception as e:
            import traceback
            self.error.emit(f"Inspection failed:\n{traceback.format_exc()}")


class StemInspectDialog(QDialog):

    def __init__(self, track_paths: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("🔬 Stem Inspection — Claude Analysis")
        self.setMinimumSize(820, 600)
        self.setStyleSheet(
            "background:#0a0a14;color:#ccc;"
            "QTextEdit{background:#0e0e1c;border:1px solid #2a2a40;"
            "color:#ccc;font-family:'Courier New';font-size:11px;border-radius:4px;}"
            "QPushButton{background:#1a1a2a;border:1px solid #3a3a55;color:#aaa;"
            "border-radius:4px;padding:4px 14px;font-size:11px;font-weight:bold;}"
            "QPushButton:hover{background:#2a2a3a;color:#fff;}"
            "QPushButton#ApplyBtn{border-color:#aa66ff;color:#cc99ff;}"
            "QPushButton#ApplyBtn:hover{background:#2a1a3a;}"
        )
        self._paths = track_paths
        self._analysis = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        tracks_text = " + ".join(p.split("/")[-1][:40] for p in track_paths)
        hdr = QLabel(f"Analysing: {tracks_text}")
        hdr.setStyleSheet("color:#aa66ff;font-weight:bold;font-size:11px;")
        hdr.setWordWrap(True)
        root.addWidget(hdr)

        self._text = QTextEdit()
        self._text.setReadOnly(True)
        self._text.setPlaceholderText(
            "Computing metrics and spectrograms…\n"
            "Sending to Claude claude-sonnet-4-6 for analysis…\n\n"
            "(stems must be ● READY on both decks)")
        root.addWidget(self._text, 1)

        btn_row = QHBoxLayout()
        self._apply_btn = QPushButton("Apply Changes to stems.py + Rebuild")
        self._apply_btn.setObjectName("ApplyBtn")
        self._apply_btn.setEnabled(False)
        self._apply_btn.clicked.connect(self._apply_and_rebuild)
        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._apply_btn)
        btn_row.addStretch()
        btn_row.addWidget(self._close_btn)
        root.addLayout(btn_row)

        # Start worker thread
        import threading
        self._worker = _InspectWorker(track_paths)
        self._worker.finished.connect(self._on_done)
        self._worker.error.connect(self._on_error)
        t = threading.Thread(target=self._worker.run, daemon=True)
        t.start()

    def _on_done(self, analysis: str):
        self._analysis = analysis
        self._text.setPlainText(analysis)
        self._apply_btn.setEnabled(True)

    def _on_error(self, msg: str):
        self._text.setPlainText(f"ERROR:\n{msg}")

    def _apply_and_rebuild(self):
        if not self._analysis:
            return
        import pathlib, subprocess
        stems_src_path = pathlib.Path(__file__).parent.parent.parent / "udj" / "core" / "stems.py"
        if not stems_src_path.exists():
            QMessageBox.warning(self, "Source Not Found",
                                f"Cannot find source stems.py at:\n{stems_src_path}\n\n"
                                "Run from the source directory, not the bundled app.")
            return

        try:
            import anthropic
            client = anthropic.Anthropic()
            stems_src = stems_src_path.read_text()
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                messages=[{"role": "user", "content":
                    f"Based on this stem quality analysis:\n\n{self._analysis}\n\n"
                    f"Here is the current stems.py:\n\n```python\n{stems_src}\n```\n\n"
                    "Apply the recommended parameter changes. Return ONLY the complete "
                    "updated Python source. No explanation, no markdown fences."}]
            )
            new_src = resp.content[0].text.strip()
            if new_src.startswith("```"):
                lines = new_src.split("\n")
                new_src = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

            compile(new_src, "stems.py", "exec")   # syntax check

            backup = stems_src_path.with_suffix(".py.bak")
            stems_src_path.rename(backup)
            stems_src_path.write_text(new_src)

            QMessageBox.information(self, "Applied",
                f"stems.py updated (backup: {backup.name})\n\n"
                "Now rebuild the app:\n"
                "  cd ~/Desktop/U-DJ\n"
                "  python3 -m PyInstaller udj.spec --clean -y\n"
                "  rm -rf ~/Desktop/U-DJ.app\n"
                "  cp -R dist/U-DJ.app ~/Desktop/U-DJ.app")
        except SyntaxError as e:
            QMessageBox.critical(self, "Syntax Error",
                                 f"Claude returned invalid Python:\n{e}\nNo changes made.")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
