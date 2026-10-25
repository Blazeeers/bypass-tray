# -*- mode: python ; coding: utf-8 -*-
"""ONEDIR-сборка «Обходы»: быстрый старт, bin/data рядом с exe."""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / "src"
LAUNCHER = ROOT / "packaging" / "launcher.py"
BIN = ROOT / "bin"
DATA = ROOT / "data"
ICON = ROOT / "packaging" / "bypass-tray.ico"
VERSION_FILE = ROOT / "packaging" / "version_info.txt"

hiddenimports = collect_submodules("pystray") + ["PIL._tkinter_finder"]

a = Analysis(
    [str(LAUNCHER)],
    pathex=[str(SRC)],
    binaries=[],
    datas=[(str(BIN), "bin"), (str(DATA), "data"),
           (str(ROOT / "LICENSE"), "."),
           (str(ROOT / "THIRD_PARTY.md"), ".")],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter.test"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Обходы",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ICON),
    version=str(VERSION_FILE),
    contents_directory=".",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Обходы",
)
