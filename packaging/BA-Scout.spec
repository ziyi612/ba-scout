# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from runpy import run_path

ROOT = Path(SPECPATH).resolve().parent
release_version = run_path(str(ROOT / 'ba_scout' / '__init__.py'))['__version__']
windows_version = run_path(str(ROOT / 'packaging' / 'version_info.py'))['windows_version_info']

a = Analysis(
    [str(ROOT / 'app.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(ROOT / 'ba_scout' / 'ocr_worker.ps1'), 'ba_scout')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='BA-Scout',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=windows_version(release_version),
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False,
               upx_exclude=[], name='BA-Scout')
