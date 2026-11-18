"""Answers the macOS folder picker (the Open panel) of a running, packaged Lastbook.app from outside. Never packaged.

  python -B mac_pick.py PID "/full/path/to/folder"     go to that folder in the Open panel and press Choose
  python -B mac_pick.py PID cancel                     press Cancel

Sends no keys (the panel takes keys only while its app is in front, and the person may be typing elsewhere). Instead it picks
the deepest folder on the way that the panel's Where menu offers (its path or Recent Places), then opens the list view's rows
one level at a time (AXDisclosing), selects the last one and presses Choose. It reads and changes no file's name.
"""
import sys
import time

import objc

import mac_ax

_f = {}
objc.loadBundleFunctions(mac_ax._b, _f, [('AXUIElementSetAttributeValue', b'i^{__AXUIElement=}@@'), ('AXUIElementGetPid', b'i^{__AXUIElement=}o^i')])


def panel(pid):
    for w in mac_ax.attr(mac_ax._f['AXUIElementCreateApplication'](pid), 'AXWindows') or []:
        if str(mac_ax.attr(w, 'AXTitle') or '') != 'ARK':
            return w
    return None


def press_in(el, title):
    for n in mac_ax.walk(el):
        if n['role'] == 'AXButton' and n['title'] == title:
            return mac_ax._f['AXUIElementPerformAction'](n['el'], 'AXPress')
    return None


def rows_named(outline, name, level):
    for n in mac_ax.walk(outline):
        if n['role'] != 'AXRow' or mac_ax.attr(n['el'], 'AXDisclosureLevel') != level:
            continue
        if any(c['role'] == 'AXTextField' and c['value'] == name for c in mac_ax.walk(n['el'])):
            return n['el']
    return None


def main():
    pid, path = int(sys.argv[1]), sys.argv[2]
    p = panel(pid)
    if p is None:
        sys.exit('no Open panel')
    if path == 'cancel':
        print('cancel', press_in(p, 'Cancel'))
        return
    parts = [x for x in path.split('/') if x]
    pop = next(n for n in mac_ax.walk(p) if n['role'] == 'AXPopUpButton')
    mac_ax._f['AXUIElementPerformAction'](pop['el'], 'AXPress')
    time.sleep(1)
    items = {n['title']: n['el'] for n in mac_ax.walk(pop['el']) if n['role'] == 'AXMenuItem' and n['title']}
    start = next((i for i in range(len(parts) - 2, -1, -1) if parts[i] in items), None)
    if start is None:
        sys.exit('no folder on the way is in the Where menu')
    print('where', parts[start], mac_ax._f['AXUIElementPerformAction'](items[parts[start]], 'AXPress'))
    time.sleep(1.5)
    p = panel(pid)
    lv = next(n['el'] for n in mac_ax.walk(p) if n['role'] == 'AXOutline' and n['desc'] == 'list view')
    for level, name in enumerate(parts[start + 1:]):
        row = None
        for _ in range(20):
            row = rows_named(lv, name, level + 1)   # level 0 is the list's heading row
            if row is not None:
                break
            time.sleep(0.3)
        if row is None:
            sys.exit(f'no row {name!r} at level {level}')
        last = level == len(parts) - start - 2
        attr = 'AXSelected' if last else 'AXDisclosing'
        print(name, attr, _f['AXUIElementSetAttributeValue'](row, attr, True))
        time.sleep(1)
    print('choose', press_in(panel(pid), 'Choose'))


if __name__ == '__main__':
    main()
