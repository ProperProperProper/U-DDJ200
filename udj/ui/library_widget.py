"""
LibraryWidget — searchable track browser.
Double-click a track to load it into the selected deck.
Drag a row onto a deck to load there specifically.
"""
from __future__ import annotations


from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLineEdit,
                              QTableView, QPushButton, QLabel, QProgressBar,
                              QFileDialog, QHeaderView, QAbstractItemView)
from PyQt6.QtCore import Qt, QSortFilterProxyModel, QAbstractTableModel, QModelIndex, QMimeData, QUrl
from PyQt6.QtGui import QFont, QColor, QBrush
from typing import Callable, Optional

import psutil as _psutil
from ..library.db import all_tracks, init_db, clear_library, stems_are_cached
from ..library.scanner import scan_directory


COLS    = ["", "Title", "Artist", "BPM", "Key", "Camelot", "Energy", "LUFS", "Duration", "Path"]
COL_MAP = ["_status", "title", "artist", "bpm", "key", "camelot", "energy", "loudness_lufs", "duration", "path"]


def _analysis_status(row: dict) -> tuple[str, str]:
    """Return (dot_char, color) indicating analysis completeness including stems."""
    has_beats   = bool(row.get("beat_positions_json") not in (None, "", "[]"))
    has_phrases = bool(row.get("phrase_positions_json") not in (None, "", "[]"))
    has_lufs    = row.get("loudness_lufs") is not None
    has_camelot = bool(row.get("camelot"))
    has_bpm     = bool(row.get("bpm"))
    has_stems   = bool(row.get("stems_cached"))

    if has_beats and has_phrases and has_lufs and has_camelot and has_stems:
        return "●", "#00dd88"   # fully analysed including stems — green
    if has_beats and has_phrases and has_lufs and has_camelot:
        return "●", "#44aaff"   # BPM/key done, stems pending — blue
    if has_bpm and has_beats:
        return "●", "#ffaa00"   # beats done, missing extras — amber
    if has_bpm:
        return "●", "#ff6600"   # BPM only — orange
    return "○", "#555555"       # not analysed — grey


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
                 on_analyse_all: Optional[Callable] = None,
                 parent=None):
        super().__init__(parent)
        self.on_load_deck1   = on_load_deck1
        self.on_load_deck2   = on_load_deck2
        self._on_analyse_all = on_analyse_all
        self._active_deck    = 0
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

        self._btn_analyse = QPushButton("⟳ Analyse All")
        self._btn_analyse.setObjectName("BtnScan")
        self._btn_analyse.setToolTip("Re-run BPM analysis + stem generation for all tracks")
        self._btn_analyse.setStyleSheet(
            "QPushButton{background:#111;border:1px solid #2a2a35;color:#888;"
            "border-radius:3px;padding:2px 8px;font-size:10px;font-weight:bold;}"
            "QPushButton:disabled{background:#1a2a1a;border:1px solid #00aa55;"
            "color:#00dd88;}"
        )
        self._btn_analyse.clicked.connect(self._analyse_now)

        self._btn_d1 = QPushButton("→ Deck 1")
        self._btn_d1.setObjectName("BtnLoadDeck")
        self._btn_d1.clicked.connect(lambda: self._load_selected(0))

        self._btn_d2 = QPushButton("→ Deck 2")
        self._btn_d2.setObjectName("BtnLoadDeck")
        self._btn_d2.clicked.connect(lambda: self._load_selected(1))

        self._progress = QProgressBar()
        self._progress.setFixedWidth(200)
        self._progress.setVisible(False)
        self._progress.setFormat("%v / %m")
        self._progress.setObjectName("ScanProgress")
        self._progress.setStyleSheet(
            "QProgressBar{background:#111;border:1px solid #2a2a35;border-radius:3px;"
            "color:#00dd88;font-size:9px;text-align:center;height:18px;}"
            "QProgressBar::chunk{background:#00aa44;border-radius:2px;}"
        )

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
        bar.addWidget(self._btn_analyse)
        bar.addWidget(self._btn_d1)
        bar.addWidget(self._btn_d2)
        bar.addWidget(self._progress)
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
        self._table.setColumnWidth(0, 18)
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

    def _scan(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Music Folder")
        if not folder:
            return

        # Clear existing library — show only what's in the chosen folder
        clear_library()
        self._model.reload()

        self._progress.setVisible(True)
        self._progress.setValue(0)

        def _prog(done, total):
            from PyQt6.QtCore import QMetaObject, Qt
            pct = int(done / total * 100) if total else 100
            self._progress.setValue(pct)

        def _done():
            self._progress.setVisible(False)
            self._model.reload()
            # Kick off BPM/key analysis + stems pre-analysis for all new tracks
            self._analyse_after_scan()

        scan_directory(folder, on_progress=_prog, on_done=_done)

    def _analyse_now(self):
        """Triggered by Analyse All button — highlights while running, resets when done."""
        self._btn_analyse.setText("⟳ Analysing…")
        self._btn_analyse.setEnabled(False)   # :disabled style = green highlight
        if self._on_analyse_all:
            self._on_analyse_all(on_complete=self._analyse_done)

    def _analyse_done(self):
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(0, lambda: (
            self._btn_analyse.setText("⟳ Analyse All"),
            self._btn_analyse.setEnabled(True),
        ))

    def _analyse_after_scan(self):
        """After scanning, run BPM analysis and stem pre-analysis on all new tracks."""
        from ..library.analyzer import analyse_track
        from ..core.stems import preanalyse_stems
        from ..core.engine import SAMPLE_RATE

        tracks = all_tracks()
        for t in tracks:
            # BPM/key/grid analysis
            needs_analysis = not (t.get("bpm") and
                                  t.get("beat_positions_json") not in (None, "", "[]"))
            if needs_analysis:
                analyse_track(t["path"], on_done=lambda _: None, force=False)

            # Stems pre-analysis
            if not t.get("stems_cached") and not stems_are_cached(t["path"]):
                preanalyse_stems(t["path"], SAMPLE_RATE,
                                 track_meta=t, on_done=lambda _: None)

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

    def analysis_progress_start(self, total: int):
        self._progress.setMaximum(total)
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._cpu_label.setVisible(True)
        _psutil.cpu_percent(interval=None)   # prime the counter
        self._cpu_timer.start()

    def analysis_progress_update(self, done: int):
        self._progress.setValue(done)

    def analysis_progress_done(self):
        self._cpu_timer.stop()
        self._progress.setVisible(False)
        self._progress.setValue(0)
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
