# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for U-DJ macOS .app bundle."""

import sys
from pathlib import Path

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('assets', 'assets'),
        ('udj', 'udj'),
        ('stem_analyzer.py', '.'),
        ('dev_inspect_stems.py', '.'),
    ],
    hiddenimports=[
        # anthropic SDK (Claude stem inspection)
        'anthropic',
        'anthropic.types',
        'anthropic._client',
        # matplotlib (spectrogram generation for stem inspection)
        'matplotlib',
        'matplotlib.pyplot',
        'matplotlib.backends.backend_agg',
        'matplotlib.figure',
        'matplotlib.gridspec',
        # audio-separator / onnxruntime (Roformer vocal model)
        'audio_separator',
        'audio_separator.separator',
        'onnxruntime',
        'onnxruntime.capi',
        'onnxruntime.capi.onnxruntime_inference_collection',
        # sounddevice / PortAudio
        'sounddevice',
        # soundfile / libsndfile
        'soundfile',
        'cffi',
        '_cffi_backend',
        # scipy submodules used
        'scipy.signal',
        'scipy.signal._upfirdn',
        'scipy.special',
        'scipy.special._ufuncs',
        'scipy.linalg',
        'scipy._lib.messagestream',
        # librosa internals
        'librosa',
        'librosa.core',
        'librosa.beat',
        'librosa.feature',
        'librosa.util',
        'librosa.filters',
        'numba',
        'numba.core',
        # mutagen
        'mutagen',
        'mutagen.mp3',
        'mutagen.flac',
        'mutagen.mp4',
        'mutagen.wave',
        'mutagen.ogg',
        'mutagen.oggvorbis',
        'mutagen.id3',
        # rtmidi
        'rtmidi',
        # PyQt6
        'PyQt6.QtWidgets',
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.sip',
        # numpy
        'numpy',
        'numpy.core',
        'numpy.core._multiarray_umath',
        'numpy.fft',
        'numpy.linalg',
        # sqlite3
        'sqlite3',
        # psutil (CPU monitoring)
        'psutil',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'IPython', 'pytest'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='U-DJ',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='U-DJ',
)

app = BUNDLE(
    coll,
    name='U-DJ.app',
    icon='assets/udj.icns',
    bundle_identifier='com.udj.app',
    info_plist={
        'CFBundleName': 'U-DJ',
        'CFBundleDisplayName': 'U-DJ',
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0',
        'NSHighResolutionCapable': True,
        'NSMicrophoneUsageDescription': 'U-DJ needs audio access for playback.',
        'LSApplicationCategoryType': 'public.app-category.music',
        'NSRequiresAquaSystemAppearance': False,
    },
)
