# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller build specification for the BananaLinux Flasher.

Running this spec produces a single self-contained ``BananaLinuxFlasher.exe``
(``--onefile``) that opens the Tk GUI with no background console window
(``--noconsole`` / ``console=False``).

Build it with:

    pyinstaller --clean --noconfirm BananaLinuxFlasher.spec

The resulting binary lives at ``dist/BananaLinuxFlasher.exe``.
"""

import os

project_root = os.path.abspath(SPECPATH)
docs_dir = os.path.join(project_root, "docs")

# --- Icon assets -----------------------------------------------------------
# Bundle the application icons when present in the checkout so the window and
# taskbar show the banana branding in the frozen build too.
icon_ico = os.path.join(docs_dir, "banana.ico")
icon_png = os.path.join(docs_dir, "banana.png")

datas = [(path, "docs") for path in (icon_ico, icon_png) if os.path.exists(path)]
app_icon = icon_ico if os.path.exists(icon_ico) else None

# --- Hidden imports --------------------------------------------------------
# Tkinter loads several of its sub-modules lazily, and urllib only pulls in
# IDNA support when a non-ASCII host is used. Listing them explicitly means
# PyInstaller's static analysis can never miss them.
hiddenimports = [
    "tkinter",
    "tkinter.ttk",
    "tkinter.font",
    "tkinter.messagebox",
    "tkinter.filedialog",
    "encodings.idna",
]

# Keep the build lean: the app depends on the standard library only, so any
# heavyweight third-party stacks that leak in transitively are excluded.
excludes = [
    "numpy",
    "scipy",
    "matplotlib",
    "pandas",
    "PIL",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "setuptools",
    "pip",
]


a = Analysis(
    ["flash_banana.py"],
    pathex=[project_root],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="BananaLinuxFlasher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,            # --noconsole: no background terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=app_icon,
)