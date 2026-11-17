"""Opt-in integration fixtures; no personal recordings ship with the project.

BA_SCOUT_PRIVATE_FIXTURES may point to a local JSON manifest. Each section
(player/search/roster/match/opening) supplies a path, parser inputs, and a
nonempty expected dictionary. Expected keys use dotted dictionary/list paths.
Keep the manifest and its recordings outside git; the default suite skips
these checks without reading any game logs or personal browser responses.
"""
import json
import os
from pathlib import Path


def load_optional_fixture(testcase, section):
    raw_path = os.environ.get('BA_SCOUT_PRIVATE_FIXTURES')
    if not raw_path:
        testcase.skipTest('Set BA_SCOUT_PRIVATE_FIXTURES for private recorded-response integration checks')
    manifest = Path(raw_path).resolve()
    fixture = json.loads(manifest.read_text(encoding='utf-8')).get(section)
    if fixture is None:
        testcase.skipTest('Private fixture manifest does not define ' + section)
    testcase.assertIsInstance(fixture, dict)
    testcase.assertIsInstance(fixture.get('expected'), dict)
    testcase.assertTrue(fixture['expected'], 'Provide independent expected values for the recording')
    source = Path(fixture['path'])
    fixture['path'] = str((manifest.parent / source).resolve() if not source.is_absolute() else source)
    testcase.assertTrue(Path(fixture['path']).is_file())
    return fixture


def assert_expected_fields(testcase, actual, expected):
    for field, value in expected.items():
        result = actual
        for component in field.split('.'):
            result = result[int(component)] if isinstance(result, (list, tuple)) else result[component]
        testcase.assertEqual(result, value, field)
