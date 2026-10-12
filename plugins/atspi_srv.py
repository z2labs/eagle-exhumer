#!/usr/bin/env python3
"""AT-SPI helper process of linux_gui.py: one JSON request per stdin line, one JSON reply per line.

A long-lived libatspi client can wedge (a property read never returns) once KiCad opens new
windows; a fresh process always gets answers. So the queries run here, and linux_gui restarts this
process whenever a call does not come back in time.

Requests: {"cmd": "snap", "pid": N} | {"cmd": "menu", "pid", "key", "path": [prefix, ...]}
          {"cmd": "click", "pid", "key", "names": [...]} | {"cmd": "entry", "pid", "key", "text"}
A window key is [role, name, index_in_parent].
Actions that make KiCad open a modal window or run the import must NOT go through AT-SPI (DoAction
runs KiCad's handler inside the D-Bus dispatch, the nested modal loop then deadlocks KiCad):
this helper only reads, and returns screen boxes for real X clicks (linux_gui.py).
"""
import json, sys

import gi
gi.require_version('Atspi', '2.0')
from gi.repository import Atspi  # noqa: E402

DIALOG_ROLES = ('dialog', 'alert', 'file chooser', 'color chooser')
BUTTON_ROLES = ('push button', 'button', 'toggle button')
TEXT_ROLES = ('label', 'static', 'text', 'paragraph')


def kids(a):
    out = []
    try:
        for i in range(a.get_child_count()):
            c = a.get_child_at_index(i)
            if c is not None:
                out.append(c)
    except Exception:
        pass
    return out


def role(a):
    try:
        return a.get_role_name()
    except Exception:
        return ''


def name(a):
    try:
        return a.get_name() or ''
    except Exception:
        return ''


def has(a, st):
    try:
        return a.get_state_set().contains(st)
    except Exception:
        return False


def find(a, pred, depth=30, limit=3000):
    out, stack, n = [], [(a, 0)], 0
    while stack and n < limit:
        x, d = stack.pop(); n += 1
        if pred(x):
            out.append(x)
        if d < depth:
            stack.extend((c, d + 1) for c in reversed(kids(x)))
    return out


def label(s):
    return s.replace('_', '').replace('&', '').replace('…', '...').strip().lower()


def press(a):
    try:
        ai = a.get_action_iface()
        if ai and ai.get_n_actions() > 0:
            return bool(ai.do_action(0))
    except Exception:
        pass
    return False


def app(pid):
    d = Atspi.get_desktop(0)
    for a in kids(d):
        try:
            if a.get_process_id() == pid:
                return a
        except Exception:
            pass
    return None


def tops(pid):
    a = app(pid)
    out = []
    for i, w in enumerate(kids(a) if a else []):
        if has(w, Atspi.StateType.SHOWING):
            out.append((i, w))
    return out


def key(i, w):
    return [role(w), name(w), i]


def window(pid, k):
    for i, w in tops(pid):
        if key(i, w) == list(k):
            return w
    return None


def buttons(d):
    return [b for b in find(d, lambda x: role(x) in BUTTON_ROLES) if has(b, Atspi.StateType.SHOWING)]


def text(d):
    out = []
    for x in find(d, lambda x: role(x) in TEXT_ROLES, limit=800):
        if role(x) == 'text' and has(x, Atspi.StateType.EDITABLE):
            continue
        t = name(x).strip()
        if not t:
            try:
                ti = x.get_text_iface()
                t = (ti.get_text(0, -1) if ti else '').strip()
            except Exception:
                t = ''
        if t and t not in out:
            out.append(t)
    return ' | '.join(out)[:600]


def snap(pid):
    res = []
    for i, w in tops(pid):
        r = role(w)
        e = {'key': key(i, w), 'role': r, 'name': name(w)}
        if r == 'file chooser':
            pass                    # its file list can be huge: buttons / entry are looked up on demand
        elif r in DIALOG_ROLES:
            e['buttons'] = [label(name(b)) for b in buttons(w)]
            e['progress'] = bool(find(w, lambda x: role(x) == 'progress bar', limit=400))
            e['text'] = text(w)
        res.append(e)
    return res


def menu(pid, k, path):
    w = window(pid, k)
    if w is None:
        return False
    bars = find(w, lambda x: role(x) == 'menu bar', limit=200)
    if not bars:
        return False
    cur = bars[0]
    for want in path:
        nxt = [c for c in kids(cur) if label(name(c)).startswith(want)]
        if not nxt:
            return False
        cur = nxt[0]
    return press(cur)


def has_menu(pid, k, path):
    w = window(pid, k)
    if w is None:
        return False
    bars = find(w, lambda x: role(x) == 'menu bar', limit=200)
    cur = bars[0] if bars else None
    for want in path:
        if cur is None:
            return False
        nxt = [c for c in kids(cur) if label(name(c)).startswith(want)]
        cur = nxt[0] if nxt else None
    return cur is not None


def click(pid, k, names):
    w = window(pid, k)
    for b in (buttons(w) if w is not None else []):
        if label(name(b)) in names:
            return press(b)
    return False


def _ext(a):
    try:
        e = a.get_extents(Atspi.CoordType.SCREEN)
        return [e.x, e.y, e.width, e.height] if e.width > 0 and e.height > 0 else None
    except Exception:
        return None


def menu_extents(pid, k, path):
    """screen box of the menu item at the end of <path> (prefixes), as long as it is shown"""
    w = window(pid, k)
    bars = find(w, lambda x: role(x) == 'menu bar', limit=200) if w is not None else []
    cur = bars[0] if bars else None
    for want in path:
        if cur is None:
            return None
        nxt = [c for c in kids(cur) if label(name(c)).startswith(want)]
        cur = nxt[0] if nxt else None
    return _ext(cur) if cur is not None else None


def button_extents(pid, k, names):
    w = window(pid, k)
    for b in (buttons(w) if w is not None else []):
        if label(name(b)) in names and has(b, Atspi.StateType.SENSITIVE):
            return _ext(b)
    return None


def extents(pid, k):
    w = window(pid, k)
    if w is None:
        return None
    try:
        e = w.get_extents(Atspi.CoordType.SCREEN)
        return [e.x, e.y, e.width, e.height]
    except Exception:
        return None


def entry(pid, k, value):
    w = window(pid, k)
    if w is None:
        return None
    es = [e for e in find(w, lambda x: role(x) == 'text')
          if has(e, Atspi.StateType.SHOWING) and has(e, Atspi.StateType.EDITABLE)]
    foc = [e for e in es if has(e, Atspi.StateType.FOCUSED)]
    e = (foc or es or [None])[0]
    if e is None:
        return None
    try:
        if value is not None:
            e.get_editable_text_iface().set_text_contents(value)
        return e.get_text_iface().get_text(0, -1)
    except Exception:
        return ''


def main():
    try:
        Atspi.set_timeout(5000, 20000)
    except Exception:
        pass
    for line in sys.stdin:
        try:
            rq = json.loads(line)
            c, pid = rq['cmd'], rq.get('pid')
            if c == 'snap':
                r = snap(pid)
            elif c == 'menu':
                r = menu(pid, rq['key'], rq['path'])
            elif c == 'has_menu':
                r = has_menu(pid, rq['key'], rq['path'])
            elif c == 'click':
                r = click(pid, rq['key'], rq['names'])
            elif c == 'menu_extents':
                r = menu_extents(pid, rq['key'], rq['path'])
            elif c == 'button_extents':
                r = button_extents(pid, rq['key'], rq['names'])
            elif c == 'extents':
                r = extents(pid, rq['key'])
            elif c == 'entry':
                r = entry(pid, rq['key'], rq.get('text'))
            elif c == 'ping':
                r = 'pong'
            else:
                raise ValueError('unknown cmd ' + c)
            out = {'ok': True, 'r': r}
        except Exception as e:
            out = {'ok': False, 'err': repr(e)}
        sys.stdout.write(json.dumps(out) + '\n'); sys.stdout.flush()


if __name__ == '__main__':
    main()
