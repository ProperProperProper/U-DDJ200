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

from ..library.db import all_tracks, init_db, clear_library
from ..library.scanner import scan_directory


COLS    = ["", "Title", "Artist", "BPM", "Key", "Camelot", "Energy", "LUFS", "Duration", "Path"]
COL_MAP = ["_status", "title", "artist", "bpm", "key", "camelot", "energy", "loudness_lufs", "duration", "path"]


def _analysis_status(row: dict) -> tuple[str, str]:
    """Return (dot_char, tooltip_colour) indicating analysis completeness."""
    has_beats   = bool(row.get("beat_positions_json") not in (None, "", "[]"))
    has_phrases = bool(row.get("phrase_positions_json") not in (None, "", "[]"))
    has_lufs    = row.get("loudness_lufs") is not None
    has_camelot = bool(row.get("camelot"))
    has_bpm     = bool(row.get("bpm"))

    if has_beats and has_phrases and has_lufs and has_camelot:
        return "●", "#00dd88"   # fully analysed — green
    if has_bpm and has_beats:
        return "●", "#ffaa00"   # beats done, missing some extras — amber
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
                 parent=None):
        super().__init__(parent)
        self.on_load_deck1 = on_load_deck1
        self.on_load_deck2 = on_load_deck2
        self._active_deck = 0
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

        self._btn_d1 = QPushButton("→ Deck 1")
        self._btn_d1.setObjectName("BtnLoadDeck")
        self._btn_d1.clicked.connect(lambda: self._load_selected(0))

        self._btn_d2 = QPushButton("→ Deck 2")
        self._btn_d2.setObjectName("BtnLoadDeck")
        self._btn_d2.clicked.connect(lambda: self._load_selected(1))

        self._progress = QProgressBar()
        self._progress.setFixedWidth(120)
        self._progress.setVisible(False)
        self._progress.setObjectName("ScanProgress")

        bar.addWidget(self._search, 1)
        bar.addWidget(self._btn_scan)
        bar.addWidget(self._btn_d1)
        bar.addWidget(self._btn_d2)
        bar.addWidget(self._progress)

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

        scan_directory(folder, on_progress=_prog, on_done=_done)

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
