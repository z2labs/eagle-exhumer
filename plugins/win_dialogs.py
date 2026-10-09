"""Fill and confirm the native Windows file / folder dialogs that KiCad opens in *this* process.

Used to drive KiCad's own "Import Non-KiCad Project > EAGLE" flow without the user having to pick
the same file twice. Runs in a background thread; only touches #32770 dialogs owned by our process
that appear after start().
"""
import os, re, threading, time

if os.name == 'nt':
    import ctypes
    from ctypes import wintypes
    U = ctypes.windll.user32
    K = ctypes.windll.kernel32
    WM_SETTEXT, BM_CLICK = 0x000C, 0x00F5
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    U.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPVOID]
    U.SendMessageW.restype = ctypes.c_ssize_t
    TDM_CLICK_BUTTON, IDOK = 0x0400 + 102, 1

# KiCad messages that are known to be harmless during the EAGLE import and may be confirmed
# automatically. Matched on the message text (any UI language: the file name is the anchor).
# KiCad 10 shows "cannot open '<project>\\sym-lib-table'" while the importer is still writing the
# project library table (seen on a 21-sheet design; the file is complete and valid afterwards,
# and eagle2kicad_fix re-checks / rebuilds the table anyway).
BENIGN = [
    (re.compile(r'sym-lib-table', re.I), 'transient sym-lib-table read during import (KiCad 10, harmless)'),
    (re.compile(r'fp-lib-table', re.I), 'transient fp-lib-table read during import (KiCad 10, harmless)'),
]


def benign_reason(text):
    for rx, why in BENIGN:
        if text and rx.search(text):
            return why
    return None


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


def is_progress(dlg):
    """KiCad's own progress window ('Importing schematic ... Elapsed time') - not a message."""
    return any(_cls(c) == 'msctls_progress32' for c in _children(dlg))


def is_task_dialog(dlg):
    """wxMessageDialog on Windows Vista+ is a TaskDialog: its text and buttons live inside a
    DirectUIHWND, not in Static / Button child windows."""
    return any(_cls(c) == 'DirectUIHWND' for c in _children(dlg))


def _msaa_text(dlg):
    """Texts of a TaskDialog through MSAA (oleacc), no focus / clipboard needed. '' on any failure."""
    try:
        from ctypes import POINTER, byref, c_void_p, c_long, c_ushort, c_wchar_p, HRESULT, WINFUNCTYPE

        class VARIANT(ctypes.Structure):
            _fields_ = [('vt', c_ushort), ('r1', c_ushort), ('r2', c_ushort), ('r3', c_ushort),
                        ('val', c_void_p), ('pad', c_void_p)]

        class GUID(ctypes.Structure):
            _fields_ = [('a', wintypes.DWORD), ('b', wintypes.WORD), ('c', wintypes.WORD), ('d', ctypes.c_ubyte * 8)]

        def guid(a, b, c, d):
            g = GUID(a, b, c); g.d[:] = d; return g
        IID_IAcc = guid(0x618736E0, 0x3C3D, 0x11CF, (0x81, 0x0C, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71))
        ole32, oleacc, oleaut = ctypes.windll.ole32, ctypes.windll.oleacc, ctypes.windll.oleaut32
        ole32.CoInitializeEx(None, 2)
        oleacc.AccessibleObjectFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD, c_void_p, c_void_p]
        oleacc.AccessibleChildren.argtypes = [c_void_p, c_long, c_long, c_void_p, c_void_p]
        oleaut.SysFreeString.argtypes = [c_void_p]

        def vcall(obj, idx, proto, *args):
            vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
            return proto(vtbl[idx])(obj, *args)
        P_REL = WINFUNCTYPE(wintypes.ULONG, c_void_p)
        P_QI = WINFUNCTYPE(HRESULT, c_void_p, POINTER(GUID), POINTER(c_void_p))
        P_CNT = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_long))
        P_STR = WINFUNCTYPE(HRESULT, c_void_p, VARIANT, POINTER(c_void_p))       # get_accName(varChild, BSTR*)
        P_ROLE = WINFUNCTYPE(HRESULT, c_void_p, VARIANT, POINTER(VARIANT))       # get_accRole(varChild, VARIANT*)
        ROLE_BUTTON, VT_I4, VT_DISPATCH = 0x2B, 3, 9
        TEXT_ROLES = (0x29, 0x2A)                      # ROLE_SYSTEM_STATICTEXT, ROLE_SYSTEM_TEXT
        out, seen = [], [0]

        def bstr(acc, child):
            b = c_void_p()
            try:
                if vcall(acc, 10, P_STR, child, byref(b)) == 0 and b.value:
                    return ctypes.wstring_at(b.value)
            except OSError:
                pass
            finally:
                if b.value:
                    oleaut.SysFreeString(b)
            return ''

        def role(acc, child):
            r = VARIANT()
            try:
                vcall(acc, 13, P_ROLE, child, byref(r))
            except OSError:
                return 0
            return ((r.val or 0) & 0xFFFFFFFF) if r.vt == VT_I4 else 0

        def walk(acc, depth):
            seen[0] += 1
            if depth > 8 or seen[0] > 400:
                return
            n = c_long()
            if vcall(acc, 8, P_CNT, byref(n)) != 0 or n.value <= 0:
                return
            arr = (VARIANT * n.value)(); got = c_long()
            if oleacc.AccessibleChildren(acc, 0, n.value, arr, byref(got)) != 0:
                return
            for v in arr[:got.value]:
                if v.vt == VT_DISPATCH and v.val:
                    sub = c_void_p()
                    if vcall(v.val, 0, P_QI, byref(IID_IAcc), byref(sub)) == 0 and sub.value:
                        self_ = VARIANT(); self_.vt = VT_I4                      # CHILDID_SELF
                        rl = role(sub.value, self_)
                        if rl != ROLE_BUTTON:
                            t = bstr(sub.value, self_).strip()
                            if t:
                                out.append((rl, t))
                        walk(sub.value, depth + 1)
                        vcall(sub.value, 2, P_REL)
                    vcall(v.val, 2, P_REL)
                elif v.vt == VT_I4:
                    rl = role(acc, v)
                    if rl != ROLE_BUTTON:
                        t = bstr(acc, v).strip()
                        if t:
                            out.append((rl, t))

        for host in [c for c in _children(dlg) if _cls(c) == 'DirectUIHWND'] or [dlg]:
            acc = c_void_p()
            if oleacc.AccessibleObjectFromWindow(host, 0xFFFFFFFC, byref(IID_IAcc), byref(acc)) == 0 and acc.value:
                walk(acc.value, 0)
                vcall(acc.value, 2, P_REL)
        texts = [t for rl, t in out if rl in TEXT_ROLES] or \
                [t for rl, t in out if not t.endswith('Icon')]   # no text roles: all but icons
        res = []
        for t in texts:                              # keep order, drop duplicates
            if t not in res:
                res.append(t)
        return ' | '.join(res)
    except Exception:
        return ''


def _clipboard_text(dlg):
    """Fallback: Ctrl+C on a message box / TaskDialog copies its whole text. Needs the dialog in
    front; the previous clipboard text is restored. '' on any failure."""
    try:
        CF_UNICODETEXT = 13
        K.GlobalLock.restype = ctypes.c_void_p; K.GlobalLock.argtypes = [ctypes.c_void_p]
        K.GlobalUnlock.argtypes = [ctypes.c_void_p]; K.GlobalAlloc.restype = ctypes.c_void_p
        U.GetClipboardData.restype = ctypes.c_void_p; U.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]

        def get():
            if not U.OpenClipboard(None):
                return None
            try:
                h = U.GetClipboardData(CF_UNICODETEXT)
                if not h:
                    return None
                p = K.GlobalLock(h)
                try:
                    return ctypes.wstring_at(p) if p else None
                finally:
                    K.GlobalUnlock(h)
            finally:
                U.CloseClipboard()

        def put(text):
            if not U.OpenClipboard(None):
                return
            try:
                U.EmptyClipboard()
                if text is None:
                    return
                data = ctypes.create_unicode_buffer(text)
                h = K.GlobalAlloc(0x0002, ctypes.sizeof(data))
                p = K.GlobalLock(h); ctypes.memmove(p, data, ctypes.sizeof(data)); K.GlobalUnlock(h)
                U.SetClipboardData(CF_UNICODETEXT, h)
            finally:
                U.CloseClipboard()

        old = get()
        put('')
        me, them = K.GetCurrentThreadId(), U.GetWindowThreadProcessId(dlg, None)
        U.AttachThreadInput(me, them, True)
        try:
            U.ShowWindow(dlg, 5); U.BringWindowToTop(dlg); U.SetForegroundWindow(dlg)
            time.sleep(0.2)
            if U.GetForegroundWindow() != dlg:
                return ''
            VK_CONTROL, KEYUP = 0x11, 0x0002
            U.keybd_event(VK_CONTROL, 0, 0, 0); U.keybd_event(0x43, 0, 0, 0)
            U.keybd_event(0x43, 0, KEYUP, 0); U.keybd_event(VK_CONTROL, 0, KEYUP, 0)
            time.sleep(0.3)
        finally:
            U.AttachThreadInput(me, them, False)
        txt = get() or ''
        put(old)
        lines = [l.strip() for l in txt.replace('\r', '').split('\n')]
        return ' | '.join(l for l in lines if l and not (l.startswith('[') and l.endswith(']'))
                          and l.upper() not in ('OK', '[OK]'))
    except Exception:
        return ''


def dialog_text(dlg, allow_clipboard=True):
    """Best-effort full message text of any dialog: Static/Edit children, else MSAA (TaskDialog),
    else Ctrl+C. Never raises."""
    try:
        t = ' | '.join(x for x in (_title(c).strip() for c in _children(dlg) if _cls(c) in ('Static', 'Edit')) if x)
        if not t and is_task_dialog(dlg):
            t = _msaa_text(dlg)
            if not t and allow_clipboard:
                t = _clipboard_text(dlg)
        return t
    except Exception:
        return ''


def click_ok(dlg):
    """Press OK in a classic dialog (wxID_OK / IDOK button) or a TaskDialog. True if sent."""
    if is_task_dialog(dlg):
        U.SendMessageW(dlg, TDM_CLICK_BUTTON, IDOK, None)
        return True
    for c in _children(dlg):
        if _cls(c) == 'Button' and U.IsWindowVisible(c) and U.GetDlgCtrlID(c) in (5100, IDOK):
            U.SendMessageW(c, BM_CLICK, 0, None)
            return True
    return False

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
        self.messages = []            # [(title, text, action)] every KiCad message box seen
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
                    if not is_progress(d) and (is_task_dialog(d) or not _name_edit(d)):
                        self.handle_message(d)       # e.g. KiCad's sym-lib-table error before the mapping
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
                if d in self.seen or not U.IsWindowEnabled(d) or _name_edit(d) or is_progress(d):
                    continue
                self.handle_message(d)
            self._minimize_new()
            time.sleep(0.3)

    def handle_message(self, d):
        """One KiCad message box: OK-only info boxes and known-harmless errors are confirmed,
        anything else is left open (the caller reports it with its full text)."""
        self.seen.add(d)
        time.sleep(0.3)
        btns = [c for c in _children(d) if _cls(c) == 'Button' and U.IsWindowVisible(c)]
        ok = [c for c in btns if U.GetDlgCtrlID(c) == 5100]
        info_box = len(ok) == 1 and len(btns) <= 2               # classic wx info box, OK only
        msg = dialog_text(d) if is_task_dialog(d) else (_text(d) or dialog_text(d))
        why = benign_reason(msg)
        if info_box or why:
            click_ok(d)
            tag = f'known KiCad issue: {why}' if why else 'confirmed automatically'
            self.messages.append((_title(d), msg, 'confirmed' + (f' ({why})' if why else '')))
            self.log(f'  [KiCad message, {tag}] "{_title(d)}" {msg}')
        else:
            self.messages.append((_title(d), msg, 'left open'))
            self.log(f'  [KiCad message, NOT confirmed] "{_title(d)}" {msg or "(text not readable)"}')
