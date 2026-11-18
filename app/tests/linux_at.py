"""Reads and drives a running Lastbook window on Linux through AT-SPI, from outside. Never packaged.
The Linux twin of mac_ax.py + mac_ask_check.py. Run it inside the test bench (linux_env.sh run python3 linux_at.py ...).

  linux_at.py APP read                     the window's text, one node a line
  linux_at.py APP press "words"            press the first link or button holding these words
  linux_at.py APP button "words"           a button only
  linux_at.py APP type "text" [n]          put text into the n-th text box (default first) and press Enter in it
  linux_at.py APP wait "regex" [SECS]      wait until the window text matches; exit 1 on timeout
  linux_at.py APP ask "question" [SECS]    type a question, send it, wait for the answer with sources, print the window text
  linux_at.py APP links ["words"]          links (name -> url) whose words hold this text

APP is a process id or a substring of the AT-SPI application name (e.g. lastbook).
"""
import re
import sys
import time

import pyatspi

TEXTY = {pyatspi.ROLE_ENTRY, pyatspi.ROLE_TEXT, pyatspi.ROLE_PASSWORD_TEXT}
PRESSY = {pyatspi.ROLE_LINK, pyatspi.ROLE_PUSH_BUTTON, pyatspi.ROLE_TOGGLE_BUTTON, pyatspi.ROLE_MENU_ITEM,
          pyatspi.ROLE_PAGE_TAB, pyatspi.ROLE_RADIO_BUTTON, pyatspi.ROLE_CHECK_BOX}


def app(spec, tries=15):
    """The app's AT-SPI root. Right after launch (or the in-place restart after setup) it can take some seconds to register."""
    for _ in range(tries - 1):
        try:
            return _app(spec)
        except SystemExit:
            time.sleep(2)
    return _app(spec)


def _app(spec):
    desk = pyatspi.Registry.getDesktop(0)
    for a in desk:
        if a is None:
            continue
        try:
            if spec.isdigit() and a.get_process_id() == int(spec) or not spec.isdigit() and spec.lower() in (a.name or '').lower():
                return a
        except Exception:
            continue
    names = []
    for a in desk:
        try:
            names.append(f'{a.name}:{a.get_process_id()}')
        except Exception:
            pass
    sys.exit(f'no accessible app {spec!r}; have: {names}')


def own_text(acc):
    try:
        t = acc.queryText()
        return t.getText(0, min(t.characterCount, 400))
    except NotImplementedError:
        return ''
    except Exception:
        return ''


def link_url(acc):
    try:
        h = acc.queryHyperlink()
        return h.getURI(0) or ''
    except Exception:
        return ''


def walk(acc, depth=0, out=None, limit=12000):
    out = [] if out is None else out
    if acc is None or len(out) >= limit or depth > 80:
        return out
    try:
        role = acc.getRole()
        states = acc.getState()
        if states.contains(pyatspi.STATE_DEFUNCT):
            return out
        node = {'acc': acc, 'depth': depth, 'role': role, 'rolename': acc.getRoleName(), 'name': acc.name or '',
                'desc': acc.description or '', 'text': '', 'url': ''}
        if role in TEXTY or role in (pyatspi.ROLE_HEADING, pyatspi.ROLE_PARAGRAPH, pyatspi.ROLE_STATIC, pyatspi.ROLE_LABEL,
                                     pyatspi.ROLE_SECTION, pyatspi.ROLE_LIST_ITEM, pyatspi.ROLE_TABLE_CELL, pyatspi.ROLE_DOCUMENT_WEB):
            node['text'] = own_text(acc).replace('￼', '').strip()
        if role == pyatspi.ROLE_LINK:
            node['url'] = link_url(acc)
        node['showing'] = states.contains(pyatspi.STATE_SHOWING)
        out.append(node)
        for i in range(acc.childCount):
            walk(acc.getChildAtIndex(i), depth + 1, out, limit)
    except Exception:
        pass
    return out


def nodes(spec):
    return walk(app(spec))


def words(n):
    return ' | '.join(x for x in (n['name'], n['text'], n['desc'], n['url']) if x)


def text(ns):
    lines, roles = [], {}
    for n in ns:
        roles[n['rolename']] = roles.get(n['rolename'], 0) + 1
        w = words(n)
        if w or n['role'] in (pyatspi.ROLE_DOCUMENT_WEB, pyatspi.ROLE_FRAME):
            lines.append('  ' * min(n['depth'], 14) + f"{n['rolename']}: {w[:300]}")
    return '\n'.join(lines + [f'roles: {roles}'])


def do_action(acc):
    try:
        act = acc.queryAction()
    except Exception:
        return 'no action'
    names = [act.getName(i) for i in range(act.nActions)]
    for want in ('click', 'press', 'activate', 'jump'):
        if want in names:
            return act.doAction(names.index(want))
    return act.doAction(0) if act.nActions else 'no action'


def press(ns, wanted, roles=PRESSY):
    for n in ns:
        if n['role'] in roles and wanted in words(n):
            return {'pressed': words(n)[:200], 'ok': do_action(n['acc'])}
    return None


def text_boxes(ns):
    out = []
    for n in ns:
        try:
            if n['role'] in TEXTY and n['acc'].getState().contains(pyatspi.STATE_EDITABLE):
                out.append(n)
        except Exception:
            pass
    return out


def put_text(n, value, enter=True):
    acc = n['acc']
    try:
        acc.queryComponent().grabFocus()
    except Exception:
        pass
    try:
        ed = acc.queryEditableText()
        ed.setTextContents('')
        ed.insertText(0, value, len(value))
    except NotImplementedError:   # WebKitGTK's own AT-SPI has no EditableText: focus it, then type on the X keyboard
        import subprocess
        subprocess.run(['xdotool', 'search', '--onlyvisible', '--name', 'ARK', 'windowfocus', '--sync'], capture_output=True)
        acc.queryComponent().grabFocus()
        time.sleep(0.3)
        subprocess.run(['xdotool', 'key', 'ctrl+a', 'BackSpace'])
        subprocess.run(['xdotool', 'type', '--delay', '15', value])
    if enter:
        time.sleep(0.3)
        import subprocess
        subprocess.run(['xdotool', 'key', 'Return'])
    return True


def wait(spec, pattern, secs):
    t0, rx = time.time(), re.compile(pattern, re.I | re.S)
    while time.time() - t0 < secs:
        page = text(nodes(spec))
        if rx.search(page):
            return page
        time.sleep(2)
    return None


def main():
    spec, what = sys.argv[1], sys.argv[2]
    arg = sys.argv[3] if len(sys.argv) > 3 else ''
    if what == 'read':
        print(text(nodes(spec)))
    elif what == 'press':
        print(press(nodes(spec), arg))
    elif what == 'button':
        print(press(nodes(spec), arg, {pyatspi.ROLE_PUSH_BUTTON, pyatspi.ROLE_TOGGLE_BUTTON}))
    elif what == 'type':
        boxes = text_boxes(nodes(spec))
        i = int(sys.argv[4]) if len(sys.argv) > 4 else 0
        if len(boxes) <= i:
            sys.exit(f'only {len(boxes)} text boxes')
        print('typed', put_text(boxes[i], arg), words(boxes[i])[:120])
    elif what == 'wait':
        page = wait(spec, arg, float(sys.argv[4]) if len(sys.argv) > 4 else 120)
        print(page if page else 'TIMEOUT')
        sys.exit(0 if page else 1)
    elif what == 'ask':
        secs = float(sys.argv[4]) if len(sys.argv) > 4 else 300
        boxes = text_boxes(nodes(spec))
        if not boxes:
            sys.exit('no question box on screen')
        put_text(boxes[-1], arg, enter=False)
        print('send', press(nodes(spec), 'Send', {pyatspi.ROLE_PUSH_BUTTON}))
        page = wait(spec, r'Sources \(|Here are the pages that best match', secs)
        print(page if page else 'TIMEOUT\n' + text(nodes(spec)))
        sys.exit(0 if page else 1)
    elif what == 'links':
        for n in nodes(spec):
            if n['role'] == pyatspi.ROLE_LINK and arg in words(n):
                print(f"{n['name'] or n['text']} -> {n['url']}")
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == '__main__':
    main()
