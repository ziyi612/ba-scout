"""Run public regression tests with all scratch output inside this checkout.

Run from the repository root: python -B tests/run_tests.py
Optional original PNG checks require BA_SCOUT_TEST_OCR_IMAGE and/or
BA_SCOUT_TEST_OCR_REGIONS_IMAGE. No real-player records are included.
"""
from pathlib import Path
import json
import os
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / 'work' / 'test-runner'
sys.dont_write_bytecode = True
os.environ.update(PYTHONDONTWRITEBYTECODE='1', TEMP=str(SCRATCH), TMP=str(SCRATCH))
SCRATCH.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(SCRATCH)
sys.path.insert(0, str(ROOT))


def isolate_test_writes(event, args):
    """Prevent a path-guard regression from creating files outside test scratch."""
    paths = []
    if event == 'open':
        path, mode, flags = args
        if ((isinstance(mode, str) and any(c in mode for c in 'wax+'))
                or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)):
            paths = [path]
    elif event in {'os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod', 'os.utime'}:
        paths = args[:1]
    elif event in {'os.rename', 'os.link', 'os.symlink'}:
        paths = args[:2]
    for value in paths:
        if not isinstance(value, int):
            target = Path(os.fsdecode(value)).resolve()
            if not target.is_relative_to(ROOT / 'work'):
                raise RuntimeError(f'Test write outside checkout work directory: {target}')


def main():
    if sys.platform != 'win32':
        raise SystemExit('The complete test suite requires Windows (Win32/Tk/WinRT integration).')
    sys.addaudithook(isolate_test_writes)
    started = time.monotonic()
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern='test_*.py', top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {
        'tests': result.testsRun,
        'failures': len(result.failures),
        'errors': len(result.errors),
        'skipped': [{'test': str(case), 'reason': reason} for case, reason in result.skipped],
        'seconds': round(time.monotonic() - started, 3),
        'passed': result.wasSuccessful(),
        'scope': 'Public source tests; optional OCR images are caller supplied. Not live-game acceptance.',
    }
    (SCRATCH / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
