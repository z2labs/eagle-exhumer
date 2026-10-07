"""Fill and confirm the native Windows file / folder dialogs that KiCad opens in *this* process.

Used to drive KiCad's own "Import Non-KiCad Project > EAGLE" flow without the user having to pick
the same file twice. Runs in a background thread; only touches #32770 dialogs owned by our process
that appear after start().
"""
import os, threading, time

if os.name == 'nt':
    import ctypes
    from ctypes import wintypes
    U = ctypes.windll.user32
    K = ctypes.windll.kernel32
    WM_SETTEXT, BM_CLICK = 0x000C, 0x00F5
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    U.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPVOID]
    U.SendMessageW.restype = ctypes.c_ssize_t


def _cls(h):
    b = ctypes.create_unicode_buffer(64); U.GetClassNameW(h, b, 64); return b.value

def _title(h):
    b = ctypes.create_unicode_buffer(256); U.GetWindowTextW(h, b, 256); return b.value

def _dialogs(pid):
    out = []
    def cb(h, _):
        p = wintypes.DWORD(); U.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid and U.IsWindowVisible(h) and _cls(h) == '#32770':
            out.append(h)
        return True
    U.EnumWindows(EnumProc(cb), 0)
    return out

def _top_windows(pid):
    out = []
    def cb(h, _):
        p = wintypes.DWORD(); U.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid and U.IsWindowVisible(h):
            out.append(h)
        return True
    U.EnumWindows(EnumProc(cb), 0)
    return out

def _text(dlg):
    """The message text of a message-box-like dialog (its static texts)."""
    return ' '.join(t for t in (_title(c).strip() for c in _children(dlg) if _cls(c) == 'Static') if t)

def _children(h):
    out = []
    def cb(c, _):
        out.append(c); return True
    U.EnumChildWindows(h, EnumProc(cb), 0)
    return out

def _name_edit(dlg):
    """The 'File name:' / 'Folder:' edit of a common item dialog."""
    kids = _children(dlg)
    for c in kids:
        if _cls(c) == 'Edit' and U.IsWindowVisible(c):
            p = U.GetParent(c)
            if _cls(p) == 'ComboBox' and _cls(U.GetParent(p)) == 'ComboBoxEx32':
                return c
    for c in kids:                       # fallback: first visible, enabled edit
        if _cls(c) == 'Edit' and U.IsWindowVisible(c) and U.IsWindowEnabled(c):
            return c
    return None


class DialogDriver(threading.Thread):
    """steps: list of paths; the n-th native file/folder dialog gets the n-th path + OK."""

    def __init__(self, steps, log=print, timeout=180, layer_mapping=False, pid=None, minimize_frames=False):
        super().__init__(daemon=True)
        self.steps, self.log, self.timeout = list(steps), log, timeout
        self.layer_mapping = layer_mapping
        self.pid = pid or K.GetCurrentProcessId()
        self.seen = set(_dialogs(self.pid))
        # KiCad opens its schematic + PCB editors during the import: keep them minimized so the
        # screen does not flicker (frames that existed before - manager, progress window - stay)
        self.minimize_frames = minimize_frames
        self.old_frames = set(_top_windows(self.pid))
        self.done = []
        self.stop_flag = False

    def _minimize_new(self):
        if not self.minimize_frames:
            return
        for h in _top_windows(self.pid):
            if h not in self.old_frames and _cls(h) == 'wxWindowNR' and not U.IsIconic(h):
                U.ShowWindow(h, 7)                       # SW_SHOWMINNOACTIVE
                self.old_frames.add(h)

    def run(self):
        for path in self.steps:
            t0 = time.time(); handled = False
            while not self.stop_flag and time.time() - t0 < self.timeout:
                for d in _dialogs(self.pid):
                    if d in self.seen or not U.IsWindowEnabled(d):
                        continue
                    time.sleep(0.4)                      # let the dialog finish building
                    ed = _name_edit(d)
                    ok = U.GetDlgItem(d, 1)
                    if not ed or not ok:
                        continue                         # some other message box: leave it to the user
                    self.seen.add(d)
                    U.SendMessageW(ed, WM_SETTEXT, 0, ctypes.c_wchar_p(path))
                    time.sleep(0.2)
                    U.SendMessageW(ok, BM_CLICK, 0, None)
                    self.log(f'  [dialog "{_title(d)}"] <- {path}')
                    self.done.append(path)
                    handled = True
                    break
                if handled:
                    break
                self._minimize_new()
                time.sleep(0.15)
            if not handled:
                self.log(f'  [dialog] not handled (timeout): {path}')
                return
        if self.layer_mapping:
            self._layer_mapping()
        else:
            self._info_ok()                              # keep confirming KiCad info boxes until stopped

    def _layer_mapping(self, timeout=600):
        """KiCad's 'Edit Mapping of Imported Layers' dialog: bring to front, Auto-Match, OK."""
        t0 = time.time()
        while not self.stop_flag and time.time() - t0 < timeout:
            for d in _dialogs(self.pid):
                if d in self.seen or not U.IsWindowEnabled(d):
                    continue
                auto = [c for c in _children(d) if _cls(c) == 'Button' and 'auto' in _title(c).lower()]
                ok = next((c for c in _children(d) if _cls(c) == 'Button' and U.GetDlgCtrlID(c) == 5100), None)  # wxID_OK
                if not auto or not ok:
                    continue
                self.seen.add(d)
                U.ShowWindow(d, 5); U.BringWindowToTop(d); U.SetForegroundWindow(d)
                time.sleep(0.3)
                U.SendMessageW(auto[0], BM_CLICK, 0, None)
                time.sleep(0.4)
                U.SendMessageW(ok, BM_CLICK, 0, None)
                self.log('  [layer mapping] auto-match + OK')
                self._info_ok()
                return
            self._minimize_new()
            time.sleep(0.2)

    def _info_ok(self, timeout=1800):
        """KiCad's post-import message box ('layer Milling (46) not mapped ...'): just OK.
        Only a dialog whose sole button-like control is OK (wxID_OK) is touched."""
        t0 = time.time()
        while not self.stop_flag and time.time() - t0 < timeout:
            for d in _dialogs(self.pid):
                if d in self.seen or not U.IsWindowEnabled(d):
                    continue
                btns = [c for c in _children(d) if _cls(c) == 'Button' and U.IsWindowVisible(c)]
                ok = [c for c in btns if U.GetDlgCtrlID(c) == 5100]
                if len(ok) != 1 or len(btns) > 2 or _name_edit(d):
                    continue
                self.seen.add(d)
                time.sleep(0.3)
                msg = _text(d)
                U.SendMessageW(ok[0], BM_CLICK, 0, None)
                self.log(f'  [KiCad message, confirmed automatically] {msg or _title(d)}')
            self._minimize_new()
            time.sleep(0.3)
