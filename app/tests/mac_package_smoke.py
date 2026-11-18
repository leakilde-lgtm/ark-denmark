"""Smoke run of the built Lastbook.app, never packaged: open -g -j, watch from outside for a short time, stop by PID.

Points the assistant at a dead port so 19443 is never contacted. Ends the app at once if it ever becomes frontmost.
  python -B mac_package_smoke.py --run NAME [--app PATH] [--data FOLDER] [--secs 30]
Needs a Python with pyobjc's Quartz (the proof venv has it)."""
import argparse
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

import Quartz

import mac_ax

ROOT = Path(__file__).resolve().parents[2]
ap = argparse.ArgumentParser()
ap.add_argument('--run', required=True)
ap.add_argument('--app', default=str(ROOT / 'derived/app-build/mac/Lastbook.app'))
ap.add_argument('--data', default=str(ROOT / 'derived/app-only-proof/mac/data'))
ap.add_argument('--secs', type=int, default=30)
ap.add_argument('--press', action='append', default=[], help='text of a link or button to press through Accessibility, one every 8 s after the first read at 10 s')
a = ap.parse_args()
OUT = ROOT / 'derived/app-build/mac-run' / a.run
OUT.mkdir(parents=True, exist_ok=True)
APP = str(Path(a.app).resolve())
LOG = Path(a.data) / 'derived/logs/lastbook.log'
MODEL = re.compile(r'\.onnx|onnxruntime|tokenizers|bge-', re.I)
REAL_LIBRARY = re.compile(r'survival-library|/survival/library-data')


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def web_procs():
    return {int(line.split(None, 1)[0]): line.split(None, 1)[1].strip() for line in sh('ps', '-axo', 'pid=,comm=').splitlines() if 'com.apple.WebKit' in line}


def front_pid():
    m = re.search(r'"?pid"?\s*=\s*(\d+)', sh('lsappinfo', 'info', '-only', 'pid', sh('lsappinfo', 'front').strip()))
    return int(m.group(1)) if m else 0


def windows(pid):
    info = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID) or []
    out = []
    for w in info:
        if w.get('kCGWindowOwnerPID') == pid:
            b = w.get('kCGWindowBounds')
            out.append({'id': int(w['kCGWindowNumber']), 'name': str(w.get('kCGWindowName') or ''), 'layer': int(w.get('kCGWindowLayer', 0)), 'onscreen': bool(w.get('kCGWindowIsOnscreen')), 'w': int(b['Width']), 'h': int(b['Height'])})
    return out


def is_ours(pid):
    return APP in sh('ps', '-o', 'command=', '-p', str(pid))


def stop(pid):
    """By PID only, and only if that PID is still the built app."""
    if not is_ours(pid):
        return 'already gone'
    os.kill(pid, signal.SIGTERM)
    for _ in range(40):
        time.sleep(0.25)
        if not is_ours(pid):
            return 'stopped by TERM'
    os.kill(pid, signal.SIGKILL)
    time.sleep(0.5)
    return 'stopped by KILL'


before_web = web_procs()
front_before = front_pid()
offset = LOG.stat().st_size if LOG.exists() else 0
started = time.time()
print(json.dumps(subprocess.run(['/usr/bin/open', '-g', '-j', '-n', '--env', 'SURVIVAL_MODEL_BASE=http://127.0.0.1:9', APP, '--args', '--data', a.data], capture_output=True, text=True).__dict__, default=str), flush=True)
pid = 0
for _ in range(120):
    time.sleep(0.25)
    if LOG.exists():
        found = re.findall(r'start pid=(\d+)', LOG.read_bytes()[offset:].decode('utf-8', 'replace'))
        if found:
            pid = int(found[-1])
            break
result = {'pid': pid, 'log_seen_after': round(time.time() - started, 2), 'front_before': front_before, 'samples': 0, 'front_was_app': 0, 'front_changed': 0, 'inet_rows': [], 'model_rows': [], 'real_library_rows': [], 'web_inet_rows': [], 'web_procs': {}}
if not pid or not is_ours(pid):
    result['failed'] = 'the app did not start'
else:
    samples = open(OUT / 'samples.jsonl', 'a', buffering=1)
    for tick in range(a.secs):
        time.sleep(1)
        if not is_ours(pid):
            result['died_at'] = tick
            break
        new = {p: c for p, c in web_procs().items() if p not in before_web}
        result['web_procs'].update({str(p): c for p, c in new.items()})
        front = front_pid()
        inet = sh('lsof', '-nP', '-a', '-p', str(pid), '-i').splitlines()[1:]
        files = sh('lsof', '-nP', '-p', str(pid)).splitlines()
        web_inet = sh('lsof', '-nP', '-a', '-p', ','.join(map(str, new)), '-i').splitlines()[1:] if new else []
        web_files = sh('lsof', '-nP', '-p', ','.join(map(str, new))).splitlines() if new else []
        win = windows(pid)
        rss = sh('ps', '-o', 'rss=', '-p', str(pid)).strip()
        web_rss = {p: sh('ps', '-o', 'rss=', '-p', str(p)).strip() for p in new}
        result['samples'] += 1
        result['inet_rows'] += inet
        result['web_inet_rows'] += web_inet
        result['model_rows'] += [x for x in files + web_files if MODEL.search(x)]
        result['real_library_rows'] += [x for x in files + web_files if REAL_LIBRARY.search(x)]
        result['front_changed'] += front != front_before
        samples.write(json.dumps({'t': tick, 'front': front, 'rss_kb': rss, 'web_rss_kb': web_rss, 'windows': win, 'new_web': new, 'inet': len(inet), 'web_inet': len(web_inet), 'files': len(files)}) + '\n')
        if tick >= 10 and (tick - 10) % 8 == 0 and result.setdefault('ax', 0) <= len(a.press) and mac_ax.trusted():
            nodes = mac_ax.tree(pid)
            n = result['ax']
            (OUT / f'ax-{n}.txt').write_text(mac_ax.text(nodes))
            result.setdefault('ax_counts', []).append(len(nodes))
            if n < len(a.press):
                result.setdefault('pressed', []).append(mac_ax.press(nodes, a.press[n]))
            result['ax'] += 1
        if front == pid:
            result['front_was_app'] += 1
            result['stopped_for_focus'] = stop(pid)
            break
    result['windows'] = windows(pid)
    for w in result['windows']:
        if w['name'] == 'ARK':
            shot = OUT / f'window-{w["id"]}.png'
            subprocess.run(['screencapture', '-x', '-o', '-l', str(w['id']), str(shot)], capture_output=True)
            result['shot'] = str(shot) if shot.exists() else 'screencapture made nothing'
    result['alive_at_end'] = is_ours(pid)
    result['stop'] = stop(pid)
    time.sleep(1)
    result['gone'] = not is_ours(pid)
    result['web_left'] = [p for p in map(int, result['web_procs']) if p in web_procs()]
result['log'] = LOG.read_bytes()[offset:].decode('utf-8', 'replace')[-3000:] if LOG.exists() else ''
(OUT / 'result.json').write_text(json.dumps(result, indent=1))
print(json.dumps(result, indent=1))
