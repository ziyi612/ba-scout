"""Build a fresh portable Windows folder and ZIP from this checkout only.

Install requirements-build.txt into a local venv, then run:
    python -B tools/build_windows.py

Build/cache/temp files remain inside the repository. The output directory must
be empty: a previously run program's config/cache/report files must never leak
into a release ZIP. Choose --output-dir dist-new for a separate rebuild.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / 'work' / 'build'
TEMP = BUILD / 'tmp'
sys.dont_write_bytecode = True
os.environ.update(PYTHONDONTWRITEBYTECODE='1', TEMP=str(TEMP), TMP=str(TEMP),
                  PYINSTALLER_CONFIG_DIR=str(BUILD / 'pyinstaller-cache'))


def owned_path(value: str | Path) -> Path:
    path = Path(value)
    resolved = (ROOT / path).resolve() if not path.is_absolute() else path.resolve()
    if resolved == ROOT or not resolved.is_relative_to(ROOT):
        raise ValueError('Build output must be a subdirectory of this checkout.')
    return resolved


def copy_file(source: Path, target: Path):
    if source.is_symlink() or not source.is_file():
        raise ValueError(f'Expected a regular release input: {source}')
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', default='dist', help='Empty checkout-local destination (default: dist).')
    args = parser.parse_args()
    if sys.platform != 'win32' or struct.calcsize('P') != 8:
        raise SystemExit('Use 64-bit Python on Windows to build the Windows x64 release.')
    if importlib.metadata.version('pyinstaller') != '6.22.3':
        raise SystemExit('Install the pinned requirements-build.txt before building.')
    import tkinter  # Fail before the build if the Python distribution lacks Tcl/Tk.
    del tkinter
    output = owned_path(args.output_dir)
    if output.exists() and any(output.iterdir()):
        raise SystemExit('Output directory is not empty. Choose a fresh --output-dir; no old runtime files are packaged.')
    required = ['app.py', 'ba_scout/ocr_worker.ps1', 'data/prep-layout-default.json',
                'LICENSE', 'README.md', 'THIRD_PARTY_NOTICES.md', 'docs/USER_GUIDE.zh-CN.txt']
    missing = [name for name in required if not (ROOT / name).is_file()]
    if missing:
        raise SystemExit('Missing release inputs: ' + ', '.join(missing))
    text = (ROOT / 'ba_scout' / '__init__.py').read_text('utf-8')
    match = re.search(r'^__version__\s*=\s*[\'"]([0-9]+\.[0-9]+\.[0-9]+)[\'"]', text, re.M)
    if not match:
        raise SystemExit('No semantic __version__ found in ba_scout/__init__.py.')
    version = match.group(1)
    TEMP.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, '-B', '-m', 'PyInstaller', '--noconfirm',
               '--distpath', str(output), '--workpath', str(BUILD / 'pyinstaller'),
               str(ROOT / 'packaging' / 'BA-Scout.spec')]
    with (BUILD / 'pyinstaller.log').open('w', encoding='utf-8') as log:
        completed = subprocess.run(command, cwd=ROOT, env=os.environ.copy(), stdout=log, stderr=subprocess.STDOUT)
    if completed.returncode:
        raise SystemExit(f'PyInstaller failed; see {BUILD / "pyinstaller.log"}')
    portable = output / 'BA-Scout'
    if not (portable / 'BA-Scout.exe').is_file():
        raise SystemExit('The expected executable was not produced.')
    for name in ['LICENSE', 'README.md', 'THIRD_PARTY_NOTICES.md', 'data/prep-layout-default.json']:
        copy_file(ROOT / name, portable / name)
    # Never redistribute a locally supplied game-derived catalog. It is optional
    # for end users and its license is separate from this application's MIT code.
    for directory in ['licenses', 'docs']:
        for source in sorted((ROOT / directory).iterdir()):
            if source.is_file() and source.suffix.lower() in {'.txt', '.md', '.html', '.terms'}:
                copy_file(source, portable / directory / source.name)
    archive_path = output / f'ba-scout-{version}-windows-x64.zip'
    files = sorted(path for path in portable.rglob('*') if path.is_file())
    if any(path.is_symlink() for path in portable.rglob('*')):
        raise SystemExit('Release folder contains a symbolic link.')
    forbidden = {'config.json', 'runtime.log', 'startup-error.txt'}
    if any(path.name in forbidden or any(part in {'cache', 'exports', 'work', 'tmp'}
                                        for part in path.relative_to(portable).parts) for path in files):
        raise SystemExit('Runtime data appeared in the fresh release folder.')
    with zipfile.ZipFile(archive_path, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in files:
            archive.write(path, path.relative_to(output).as_posix())
    with zipfile.ZipFile(archive_path) as archive:
        if archive.testzip() is not None or len(archive.infolist()) != len(files):
            raise SystemExit('ZIP integrity verification failed.')
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    (output / 'SHA256SUMS.txt').write_text(f'{digest}  {archive_path.name}\n', encoding='utf-8')
    report = {'built_at_utc': datetime.now(timezone.utc).isoformat(), 'version': version,
              'python': sys.version, 'pyinstaller': importlib.metadata.version('pyinstaller'),
              'zip': str(archive_path), 'sha256': digest, 'files': len(files),
              'optional_catalog_included': False, 'crc_passed': True,
              'contains_personal_runtime_files': False}
    (BUILD / 'build-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
