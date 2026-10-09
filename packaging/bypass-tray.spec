# -*- mode: python ; coding: utf-8 -*-
"""Сборка портативного ``bypass-tray.exe`` — один файл, Python не нужен.

Запуск из корня репозитория (после ``python packaging/make_icon.py``)::

    pyinstaller --clean --noconfirm packaging/bypass-tray.spec

Результат: ``dist/bypass-tray.exe``. Это и есть установка «в один клик»:
файл скачали, запустили двойным щелчком, вставили ссылку на подписку.
"""

import os

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))  # noqa: F821
SRC = os.path.join(ROOT, "src")
ICON = os.path.join(ROOT, "packaging", "bypass-tray.ico")

hiddenimports = collect_submodules("pystray") + ["PIL._tkinter_finder"]

a = Analysis(  # noqa: F821
    [os.path.join(ROOT, "packaging", "launcher.py")],
    pathex=[SRC],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter.test"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="bypass-tray",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)
