"""Opt-in Windows acceptance run with real WinRT OCR, Tk widgets and loopback HTTP.

Run python -B tests/prep_acceptance.py. All names, web pages, frames and logs
are synthetic. The current match contains only a WaitingRoom event. No live service is used.
"""
from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ba_scout import player_search, provider, recon_provider, settings, ui
from ba_scout.prep_engine import PrepController
from ba_scout.screen_ocr import ScreenOCR
from ba_scout.windows import enable_dpi
from tests.test_opening import match_fixture
from tests.test_provider import page_fixture, rsc_script


NAMES = ('Raven', 'Vector', 'Cobra', 'Sable', 'Orbit')
PLAYERS = {name: 810001 + i for i, name in enumerate(NAMES)}
MATCH = '700018'
DELAYED = 'Vector'
ARRIVALS = (0, 3, 6, 9, 13)


def make_frames(directory, regions):
    """Render only synthetic nameplates into PNGs at the normal calibrated positions."""
    script = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$data = Get-Content -Raw -Encoding UTF8 -LiteralPath $env:BA_SCOUT_ACCEPTANCE_INPUT | ConvertFrom-Json
$font = [Drawing.Font]::new('Arial', 16, [Drawing.FontStyle]::Bold, [Drawing.GraphicsUnit]::Pixel)
try {
    for ($count=0; $count -le 5; $count++) {
        $bitmap = [Drawing.Bitmap]::new(1920,1080)
        $g = [Drawing.Graphics]::FromImage($bitmap)
        try {
            $g.Clear([Drawing.Color]::FromArgb(24,32,39))
            $g.TextRenderingHint = [Drawing.Text.TextRenderingHint]::AntiAliasGridFit
            for ($row=0; $row -lt 5; $row++) {
                $box = $data.boxes[$row]
                $name = if ($row -lt $count) { $data.names[$row] } else { 'Player name' }
                $g.DrawString($name,$font,[Drawing.Brushes]::White,[single]($box[0]*1920),[single]($box[1]*1080))
            }
            $bitmap.Save((Join-Path $data.directory "$count.png"),[Drawing.Imaging.ImageFormat]::Png)
        } finally { $g.Dispose(); $bitmap.Dispose() }
    }
} finally { $font.Dispose() }
"""
    inputs = directory / 'frames.json'
    inputs.write_text(json.dumps(dict(names=NAMES, boxes=regions['Bravo']['rows'], directory=str(directory))), 'utf-8')
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                   env={**os.environ, 'BA_SCOUT_ACCEPTANCE_INPUT': str(inputs)}, check=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)


def run():
    directory = ROOT / 'work' / 'acceptance' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    logs = directory / 'GameLogs'
    logs.mkdir(parents=True)
    log = logs / 'Gamelog__synthetic.log'
    # Two earlier full matches belong to this local account. No ID list for the
    # current match is supplied to either the OCR or the identity resolver.
    prior = logs / "Gamelog__previous.log"
    blocks = []
    for game in range(2):
        allies = [990001, PLAYERS["Cobra"], PLAYERS["Sable"] if game else 880030, 880031, 880032]
        enemies = [PLAYERS["Raven"], 880033, PLAYERS["Sable"] if not game else 880034, 880035, 880036]
        blocks.extend([f"[2025-12-{20 + game} 11:00:00.000]",
                       f"Log: [NET_ROOM] GameRoom entered, fight id: {800000 + game}",
                       "Log: Player list:"])
        for team, ids in (("Alpha", allies), ("Bravo", enemies)):
            blocks.extend(f"ID: {pid}, Name: Past{pid}, Team: {team}" for pid in ids)
        blocks.append("")
    prior.write_text("\n".join(blocks) + "\n", "utf-8")
    os.utime(prior, (1700000000, 1700000000))
    log.touch()
    config = {**settings.DEFAULTS, 'self_id': 990001, 'click_through': True,
              'log_dir': str(logs), 'x': 24, 'y': 70,
              'hotkey_toggle': 'CTRL+SHIFT+F7', 'hotkey_interact': 'CTRL+SHIFT+F8'}
    make_frames(directory, config['prep_regions'])
    protocol = []
    requests = Counter()
    start = time.monotonic()
    def elapsed():
        return round(time.monotonic() - start, 3)

    class Fixture(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == '/search':
                query = parse_qs(url.query)['q'][0]
                requests[query] += 1
                protocol.append([elapsed(), 'search', query, requests[query]])
                if query == DELAYED and requests[query] == 1:
                    # Withhold only the first response past the whole row budget.
                    time.sleep(15)
                elif query == DELAYED:
                    # Leave time for a native Tk paint before recovery completes.
                    time.sleep(0.3)
                rows = [{'id': PLAYERS[query], 'name': query}] if query in PLAYERS else []
                body = json.dumps({'players': rows, 'count': len(rows)}).encode()
                mime = 'application/json'
            elif url.path.startswith('/player/'):
                pid = int(url.path.split('/')[-1])
                name = next(key for key in NAMES if PLAYERS[key] == pid)
                data = {'info': {'id': pid, 'name': name, 'rating': 2100 + NAMES.index(name), 'rank': 155},
                        'statInfo': {'playerId': pid, 'winCountRt': 5, 'lossCountRt': 5, 'fightsCountRt': 10,
                                     'combinedSpecsExp': {'4_5': 1000}},
                        'last_fights_data': [MATCH]}
                protocol.append([elapsed(), 'profile', name])
                body = page_fixture(data).encode('utf-8')
                mime = 'text/html'
            elif url.path == '/match/' + MATCH:
                props = match_fixture(match_id=MATCH)
                records = props['initialMatchData']['matchInfo']['Data']
                template = records.pop('7')
                records.update({str(pid): {**template, 'Id': pid} for pid in PLAYERS.values()})
                body = rsc_script(1, props)
                mime = 'text/html'
            else:
                self.send_error(404)
                return
            try:
                self.send_response(200)
                self.send_header('Content-Type', mime + '; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass  # The deliberately timed-out request can already be disconnected.

        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    site = 'http://127.0.0.1:' + str(server.server_port)
    player_search.SEARCH_URL = site + '/search'
    provider.BASE_URL = recon_provider.BASE_URL = site
    ui.ROOT = directory
    ocr = ScreenOCR(temp_dir=directory / 'ocr')
    first_text = {}
    first_stats = {}
    snapshots = []
    frames = []
    row_states = []
    errors = []
    enable_dpi()
    args = SimpleNamespace(self_test=False, replay_log=None, test_hold=0, test_output=str(directory/'unused.json'))
    window = ui.ScoutWindow(config, args)
    window.brand.config(text='受控验收 · 合成姓名与战绩')
    window.screen_ocr = ocr

    class Frames:
        def submit_capture_game(self, *, timeout, regions):
            count = sum(elapsed() >= t for t in ARRIVALS)
            if not frames or frames[-1][1] != count:
                frames.append([elapsed(), count])
            return ocr.submit_extract(directory / f'{count}.png', regions=regions, timeout=timeout)

    # Keep the real 12 s lookup / 20 s reporting budgets. A long socket timeout
    # makes the single injected failure consume the entire first lookup window.
    prep = PrepController(Frames(), window.request_gate, search_timeout=12)
    window.prep_controller = prep
    native_poll = prep.poll_events
    def poll():
        events = native_poll()
        for event in events:
            for row in event.get('rows', []):
                if row.get('team') == 'Bravo' and row.get('index') == 1:
                    status = row.get('resolution', {}).get('status')
                    if not row_states or row_states[-1][1] != status:
                        row_states.append([elapsed(), status])
        return events
    prep.poll_events = poll
    try:
        warmup = ocr.submit_warmup(timeout=12).result(timeout=15)
        if not warmup.get('ok'):
            raise RuntimeError('Windows OCR warmup failed: ' + str(warmup.get('status')))
        start = time.monotonic()
        log.write_text('[2026-01-01 12:00:00.000]\nLog: [NET_ROOM] WaitingRoom entered, fight id: 810099\n\n', 'utf-8')
        def watch():
            now = elapsed()
            if window.cards.winfo_ismapped() and not window.table.winfo_ismapped() and window.live_phase == 'waiting':
                rows = window.cards.visible_rows()
                if not snapshots or rows != snapshots[-1][1]:
                    snapshots.append([now, rows])
                for row in rows:
                    if row['name'] in PLAYERS:
                        first_text.setdefault(row['name'], now)
                        if '50%' in row['performance'] or '50.0%' in row['performance']:
                            first_stats.setdefault(row['name'], now)
            ready = len(first_stats) == 5 and all(
                window.opening_states.get(pid, {}).get('data', {}).get('sample_count') and
                window.opening20_states.get(pid, {}).get('data', {}).get('sample_count') for pid in PLAYERS.values())
            if ready or now > 60:
                window.root.quit()
            else:
                window.root.after(40, watch)
        def callback_error(exc, error, trace):
            errors.append(repr(error))
            window.root.quit()
        window.root.report_callback_exception = callback_error
        window.root.after(40, watch)
        window.root.mainloop()
        checks = {
            'only_preparation': window.live_phase == 'waiting' and window.current_roster is not None
                                and window.current_roster.source == 'screen_names',
            'five_visible_slots': bool(snapshots) and all(len(rows) == 5 for _, rows in snapshots),
            'one_at_a_time': sorted(set(sum(not r['iid'].startswith('slot-') for r in rows) for _, rows in snapshots)) == list(range(6)),
            'first_stats_before_fifth_name': first_stats.get('Raven', 100) < ARRIVALS[-1],
            'independent_while_timed_out': first_stats.get('Cobra', 100) < first_stats.get(DELAYED, 0),
            'deadline_then_recovery': 'timeout' in [s for _, s in row_states]
                                      and requests[DELAYED] == 2 and DELAYED in first_stats,
            'recovery_visible': any(len(rows) == 5 and rows[1]['status'] == '补查中' for _, rows in snapshots),
            'correct_slots_ids_and_values': bool(snapshots) and [r['iid'] for r in snapshots[-1][1]] ==
                [str(PLAYERS[name]) for name in NAMES] and [r['name'] for r in snapshots[-1][1]] == list(NAMES)
                and set(first_stats) == set(NAMES),
            'no_disappearance': all({r['name'] for r in prev if r['name'] in PLAYERS}.issubset({r['name'] for r in nxt})
                                    for (_, prev), (_, nxt) in zip(snapshots, snapshots[1:])),
            'both_opening_windows': all(window.opening_states.get(pid, {}).get('data', {}).get('sample_count') == 1
                                        and window.opening20_states.get(pid, {}).get('data', {}).get('sample_count') == 1
                                        for pid in PLAYERS.values()),
            'encounters_by_id_and_side': bool(snapshots) and
                [row['encounter'] for row in snapshots[-1][1]] ==
                ['曾为对手', '', '曾为队友', '曾为对手 · 曾为队友', ''],
            'read_before_id_has_no_stats_or_history': any(
                r['iid'].startswith('slot-') and r['name'] in PLAYERS and not r['rating'] and not r['encounter']
                for _, rows in snapshots for r in rows),
            'no_callback_errors': not errors,
        }
        if window.current_roster and str(PLAYERS["Sable"]) in window.cards.cards:
            window.activate_card(str(PLAYERS["Sable"]))
            body = window.cards.detail_body.cget("text")
            checks["both_relations_in_drawer"] = "曾为对手 1 场" in body and "曾为队友 1 场" in body
            txt = window.export_report(copy_file=False)
            exported = txt.read_text("utf-8-sig") if txt else ""
            checks["encounters_in_report"] = "本机历史对局 · 曾为对手 1 场 · 曾为队友 1 场" in exported
        else:
            checks["both_relations_in_drawer"] = checks["encounters_in_report"] = False
        report = dict(passed=all(checks.values()), checks=checks, first_name_seconds=first_text,
                      first_stats_seconds=first_stats, delayed_slot=row_states, frames=frames, requests=protocol,
                      visible_cards=snapshots, errors=errors, data_source='Synthetic loopback HTTP; native OCR and Tk')
        path = directory / 'result.json'
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
        print(json.dumps({**{k: report[k] for k in ('passed','checks','first_stats_seconds','delayed_slot')},
                          'evidence': str(path)}, ensure_ascii=False, indent=2))
        return report['passed']
    finally:
        window.close()
        ocr.close()
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
