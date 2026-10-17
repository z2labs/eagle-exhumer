"""Fill and confirm the native macOS open / choose-folder panels that KiCad opens in *this* process.

macOS counterpart of win_dialogs.DialogDriver (same interface): drives KiCad's own
"Import Non-KiCad Project > EAGLE" flow so the user does not pick the same file twice.

The panels are driven through System Events (AppleScript): the standard "Go to Folder"
shortcut (Cmd+Shift+G) takes a full path in every NSOpenPanel, in any UI language. This needs
the Accessibility permission for KiCad once (System Settings > Privacy & Security >
Accessibility); without it `available()` is False and the plugin falls back to telling the user
which file to pick.
"""
import os, re, subprocess, threading, time

BENIGN = [
    (re.compile(r'sym-lib-table', re.I), 'transient sym-lib-table read during import (KiCad 10, harmless)'),
    (re.compile(r'fp-lib-table', re.I), 'transient fp-lib-table read during import (KiCad 10, harmless)'),
]


def benign_reason(text):
    for rx, why in BENIGN:
        if text and rx.search(text):
            return why
    return None


def _osa(script, timeout=15):
    try:
        r = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or '').strip(), (r.stderr or '').strip()
    except Exception as ex:
        return 1, '', str(ex)


def _q(s):
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def available():
    """True when System Events may drive this process (Accessibility permission granted)."""
    rc, out, _ = _osa('tell application "System Events" to get UI elements enabled')
    return rc == 0 and out == 'true'


def _proc(pid):
    return f'(first process whose unix id is {pid})'


def _dialogs(pid):
    """[(kind, index, title)] - open panels / alerts of our process: separate dialog windows and
    sheets on any window. kind 'w' = window index, 's' = sheet of window index."""
    script = f'''
tell application "System Events"
  set p to {_proc(pid)}
  set out to ""
  set i to 0
  repeat with w in (windows of p)
    set i to i + 1
    try
      set sr to subrole of w
    on error
      set sr to ""
    end try
    if sr is "AXDialog" or sr is "AXSystemDialog" then
      set out to out & "w|" & i & "|" & (name of w as text) & linefeed
    end if
    try
      if exists sheet 1 of w then set out to out & "s|" & i & "|" & (name of w as text) & linefeed
    end try
  end repeat
  return out
end tell'''
    rc, out, _ = _osa(script)
    res = []
    for line in out.splitlines():
        parts = line.split('|', 2)
        if len(parts) == 3:
            res.append((parts[0], int(parts[1]), parts[2]))
    return res


def _elem(kind, idx):
    return f'window {idx} of p' if kind == 'w' else f'sheet 1 of window {idx} of p'


def _is_panel(pid, kind, idx):
    """An open / choose panel has a browser (outline or browser) - an alert has not."""
    script = f'''
tell application "System Events"
  set p to {_proc(pid)}
  set e to {_elem(kind, idx)}
  set n to (count of (UI elements of e whose role is "AXSplitGroup")) + (count of (UI elements of e whose role is "AXBrowser"))
  try
    set n to n + (count of (outlines of scroll areas of splitter groups of e))
  end try
  return n
end tell'''
    rc, out, _ = _osa(script)
    try:
        return rc == 0 and int(out.split(',')[0] or 0) > 0
    except ValueError:
        return False


def _dialog_text(pid, kind, idx):
    script = f'''
tell application "System Events"
  set p to {_proc(pid)}
  set e to {_elem(kind, idx)}
  set t to ""
  repeat with s in (static texts of e)
    try
      set t to t & (value of s as text) & " | "
    end try
  end repeat
  return t
end tell'''
    rc, out, _ = _osa(script)
    return out


def _buttons(pid, kind, idx):
    script = f'''
tell application "System Events"
  set p to {_proc(pid)}
  return name of buttons of {_elem(kind, idx)}
end tell'''
    rc, out, _ = _osa(script)
    return [b.strip() for b in out.split(',') if b.strip()] if rc == 0 else []


def _enter_path(pid, kind, idx, path):
    """Go to Folder (Cmd+Shift+G), type the full path, confirm, then press the default button."""
    script = f'''
tell application "System Events"
  set p to {_proc(pid)}
  set frontmost of p to true
  delay 0.3
  keystroke "g" using {{command down, shift down}}
  delay 0.8
  keystroke {_q(path)}
  delay 0.5
  key code 36
  delay 1.2
  key code 36
end tell'''
    rc, out, err = _osa(script, timeout=30)
    return rc == 0, err


class DialogDriver(threading.Thread):
    """steps: list of paths; the n-th open / choose panel gets the n-th path + its default button.
    After the steps, alerts with a single button (KiCad's post-import info boxes) are confirmed
    until stop_flag is set; anything with more buttons is left to the user."""

    def __init__(self, steps, log=print, timeout=180, layer_mapping=False, pid=None, minimize_frames=False):
        super().__init__(daemon=True)
        self.steps, self.log, self.timeout = list(steps), log, timeout
        self.pid = pid or os.getpid()
        self.seen = set((k, i, t) for k, i, t in _dialogs(self.pid))
        self.done, self.messages, self.stop_flag = [], [], False

    def run(self):
        if not available():
            self.log('  [dialog] macOS Accessibility permission missing for KiCad - pick the files by hand')
            return
        for path in self.steps:
            t0 = time.time(); handled = False
            while not self.stop_flag and time.time() - t0 < self.timeout:
                cur = _dialogs(self.pid)
                self.seen &= set(cur)
                for d in cur:
                    if d in self.seen:
                        continue
                    kind, idx, title = d
                    time.sleep(0.6)                      # let the panel finish building
                    if not _is_panel(self.pid, kind, idx):
                        continue                         # an alert: handled after the steps
                    self.seen.add(d)
                    ok, err = _enter_path(self.pid, kind, idx, path)
                    if not ok:
                        self.log(f'  [dialog "{title}"] could not be filled: {err[:120]}')
                        continue
                    self.log(f'  [dialog "{title}"] <- {path}')
                    self.done.append(path); handled = True
                    # the next panel usually reuses the same window index and title ("Open"):
                    # forget this one as soon as it is gone, or the next would look already handled
                    t1 = time.time()
                    while d in _dialogs(self.pid) and time.time() - t1 < 15:
                        time.sleep(0.3)
                    self.seen.discard(d)
                    break
                if handled:
                    break
                time.sleep(0.4)
            if not handled:
                self.log(f'  [dialog] not handled (timeout): {path}')
                return
        self._info_ok()

    def _info_ok(self, timeout=1800):
        t0 = time.time()
        while not self.stop_flag and time.time() - t0 < timeout:
            cur = _dialogs(self.pid)
            self.seen &= set(cur)                        # a closed box's slot can be reused
            for d in cur:
                if d in self.seen:
                    continue
                kind, idx, title = d
                if _is_panel(self.pid, kind, idx):
                    continue
                btns = _buttons(self.pid, kind, idx)
                if len(btns) != 1:
                    continue                             # a question: the user answers it
                text = _dialog_text(self.pid, kind, idx)
                why = benign_reason(text) or 'info box confirmed'
                _osa(f'tell application "System Events"\n  set p to {_proc(self.pid)}\n'
                     f'  click button 1 of {_elem(kind, idx)}\nend tell')
                self.seen.add(d)
                self.messages.append((title, text[:300], why))
                self.log(f'  [KiCad message, {why}] "{title}" {text[:160]}')
            time.sleep(0.5)



def save_windows(name, pid=None):
    """Cmd+S in every editor window of the project NAME (schematic / PCB editor; not the project
    manager, not our own window). Returns the window names or the error. The AX title of a frame
    is not always wx's GetTitle() (modified marker), so the windows are matched by project name.
    KiCad's ACTION_MENU items do not react to a synthesized wxEVT_MENU on macOS, a keystroke does."""
    pid = pid or os.getpid()
    rc, out, err = _osa(f'''
tell application "System Events"
  set p to {_proc(pid)}
  set done to {{}}
  repeat with w in (windows of p)
    set n to name of w as text
    if n contains {_q(name)} and n does not contain "KiCad 1" and n does not start with "Eagle Exhumer" then
      perform action "AXRaise" of w
      set frontmost of p to true
      delay 0.6
      keystroke "s" using command down
      delay 1.5
      set end of done to n
    end if
  end repeat
  return done
end tell''', timeout=60)
    return out if rc == 0 else 'error: ' + err[:160]
