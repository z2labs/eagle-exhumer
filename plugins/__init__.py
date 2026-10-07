"""Eagle Exhumer - Eagle -> KiCad import with strong quality control (PCB Editor toolbar button).
https://github.com/z2labs/eagle-exhumer   (GPL-3.0-or-later)

Two modes:
  * One-click import (Windows): pick the Eagle .sch/.brd, everything else is automatic.
  * Fix + verify (all platforms): import with File > Import Non-KiCad Project > EAGLE yourself,
    save, then click the button and pick the Eagle files - the open project is fixed and checked.

Click -> pick the Eagle .sch/.brd -> KiCad's own "Import Non-KiCad Project > EAGLE" runs
(file + destination dialogs filled automatically, layer mapping auto-matched) -> the imported
project is fixed by eagle2kicad_fix.py (power nets, labels, libraries, PWR_FLAG, footprint
library + relink, Eagle design rules, netlist check against the Eagle files) -> the project is
reloaded and the schematic + PCB editors are opened.

Must be started from a PCB Editor that was opened from the KiCad project manager (the import
itself is a project-manager function).
"""
import glob, os, re, subprocess, sys, threading, time
import pcbnew
import wx

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_ACTIVE = []          # keep running jobs referenced after the PCB frame closes


# ------------------------------------------------------------------ helpers

def _python():
    cands = [os.path.join(os.path.dirname(sys.executable), 'python.exe')]
    cands += sorted(glob.glob(r'C:\Program Files\KiCad\*\bin\python.exe'), reverse=True)
    cands += [os.path.join(os.path.dirname(sys.executable), 'python3')]
    cands += sorted(glob.glob('/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/*/bin/python3'),
                    reverse=True)
    cands += [sys.executable, '/usr/bin/python3']
    return next((c for c in cands if os.path.isfile(c) and 'python' in os.path.basename(c).lower()), 'python')


def _walk_menu(menu):
    for it in menu.GetMenuItems():
        yield it
        if it.GetSubMenu():
            yield from _walk_menu(it.GetSubMenu())


def _items(frame):
    mb = frame.GetMenuBar() if isinstance(frame, wx.Frame) else None
    if not mb:
        return
    for i in range(mb.GetMenuCount()):
        yield from _walk_menu(mb.GetMenu(i))


def _find_item(frame, pred):
    for it in _items(frame):
        try:
            if not it.IsSeparator() and pred(it.GetItemLabel()):
                return it.GetId()
        except Exception:
            pass
    return None


def _eagle_import_id(frame):
    """The EAGLE entry of the 'Import Non-KiCad Project' submenu (a submenu that also lists other
    CAD formats) - NOT a recent-projects entry whose path happens to contain 'eagle'."""
    mb = frame.GetMenuBar() if isinstance(frame, wx.Frame) else None
    if not mb:
        return None
    menus = []
    def walk(m):
        menus.append(m)
        for it in m.GetMenuItems():
            if it.GetSubMenu():
                walk(it.GetSubMenu())
    for i in range(mb.GetMenuCount()):
        walk(mb.GetMenu(i))
    for m in menus:
        labels = [(it.GetId(), it.GetItemLabelText()) for it in m.GetMenuItems() if not it.IsSeparator()]
        others = sum(1 for _, l in labels if any(k in l.upper() for k in ('ALTIUM', 'CADSTAR', 'EASYEDA', 'PADS', 'GEDA')))
        if others < 2:
            continue
        for mid, l in labels:
            if l.upper().lstrip().startswith('EAGLE') and '\\' not in l and '/' not in l:
                return mid
    return None


def _manager():
    """(frame, eagle_import_menu_id) of the KiCad project manager in this process."""
    for w in wx.GetTopLevelWindows():
        mid = _eagle_import_id(w)
        if mid is not None and _find_item(w, lambda l: l.endswith('\tCtrl+E')) is not None:
            return w, mid
    return None, None


def _menu(frame, mid):
    evt = wx.CommandEvent(wx.wxEVT_MENU, mid)
    evt.SetEventObject(frame)
    return frame.GetEventHandler().ProcessEvent(evt)


def _menu_action(frame, mid):
    """Editor menus are KiCad ACTION_MENUs: the action is resolved by the menu object itself
    (ACTION_MENU::OnMenuEvent), a wxEVT_MENU sent to the frame does nothing."""
    item = None
    for it in _items(frame):
        if it.GetId() == mid:
            item = it; break
    m = item.GetMenu() if item is not None else None
    if m is not None:
        evt = wx.CommandEvent(wx.wxEVT_MENU, mid)
        evt.SetEventObject(m)
        if m.ProcessEvent(evt):
            return True
    return _menu(frame, mid)


def _click(btn):
    evt = wx.CommandEvent(wx.wxEVT_BUTTON, btn.GetId())
    evt.SetEventObject(btn)
    btn.GetEventHandler().ProcessEvent(evt)


def _project():
    try:
        return pcbnew.GetSettingsManager().Prj().GetProjectFullName()
    except Exception:
        return ''


def _norm(p):
    return os.path.normcase(os.path.abspath(p)) if p else ''


# ------------------------------------------------------------------ log window

class LogWindow(wx.Frame):
    def __init__(self, parent):
        super().__init__(parent, title='Eagle Exhumer', size=(980, 640),
                         style=wx.DEFAULT_FRAME_STYLE | wx.FRAME_FLOAT_ON_PARENT)
        self.txt = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL | wx.TE_RICH2)
        self.txt.SetFont(wx.Font(9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self.Show()

    def log(self, s):
        if self:
            self.txt.AppendText(s + '\n')


# ------------------------------------------------------------------ job

class ImportJob:
    def __init__(self, manager, eagle_mid, src, target):
        self.mgr, self.mid, self.src, self.target = manager, eagle_mid, src, target
        self.name = os.path.splitext(os.path.basename(src))[0]
        self.pro = os.path.join(target, self.name + '.kicad_pro')
        self.win = LogWindow(manager)
        self.mapped = set()
        self.timer = None

    def log(self, s):
        wx.CallAfter(self.win.log, s)

    # -- step 1: KiCad's own import
    def start(self):
        _ACTIVE.append(self)
        self.log(f'Eagle source : {self.src}')
        self.log(f'KiCad project: {self.pro}')
        self.log('\n1) KiCad Eagle import (File > Import Non-KiCad Project > EAGLE)...')
        from win_dialogs import DialogDriver
        # file dialog, destination dialog, then the layer-mapping dialog (opens BEHIND the
        # main window in KiCad 10.0.5 -> looks like a freeze): brought to front + auto-matched
        self.driver = DialogDriver([self.src, self.target], log=self.log, layer_mapping=True)
        self.driver.start()
        wx.CallAfter(self._run_import)

    def _editor_frames(self):
        out = []
        for w in wx.GetTopLevelWindows():
            if w is self.mgr or w is self.win or not isinstance(w, wx.Frame):
                continue
            if _find_item(w, lambda l: l.endswith('\tCtrl+S')) is not None:
                out.append(w)
        return out

    def _run_import(self):
        t0 = time.time()
        try:
            _menu(self.mgr, self.mid)        # returns when KiCad finished the import (NOT saved!)
        finally:
            self.driver.stop_flag = True
        # (pcbnew.GetSettingsManager() is not usable here: the PCB frame that ran the plugin is gone)
        if not os.path.isfile(self.pro) or not self._editor_frames():
            self.log('\nImport cancelled or failed (no imported project / editors found).')
            return
        self.log(f'  KiCad import done in {time.time() - t0:.0f} s')
        # KiCad 10.0.5 leaves both editors unsaved -> File > Save in each, then close them
        for f in self._editor_frames():
            mid = _find_item(f, lambda l: l.endswith('\tCtrl+S'))
            self.log(f'  saving: {f.GetTitle()}')
            _menu_action(f, mid)
        wx.CallLater(1500, self._close_editors_then_fix)

    def _close_editors_then_fix(self, tries=0):
        missing = [e for e in ('.kicad_sch', '.kicad_pcb')
                   if not os.path.isfile(os.path.join(self.target, self.name + e))]
        if missing and tries < 20:
            wx.CallLater(500, self._close_editors_then_fix, tries + 1); return
        if missing:
            self.log(f'\nNot saved by KiCad: {missing} - save the editors manually, then run the fix-only .bat')
            return
        for f in self._editor_frames():
            f.Close()
        wx.CallLater(1000, self._run_fix)

    # -- step 2: fix-up in a separate python process
    def _run_fix(self):
        import shutil
        src_dir = os.path.join(self.target, 'eagle_source')
        os.makedirs(src_dir, exist_ok=True)
        args = []
        base = os.path.splitext(self.src)[0]
        for ext, opt in (('.sch', '--eagle-sch'), ('.brd', '--eagle-brd')):
            for cand in (base + ext, base + ext.upper()):
                if os.path.isfile(cand):
                    dst = os.path.join(src_dir, os.path.basename(cand))
                    shutil.copy2(cand, dst)
                    args += [opt, dst]
                    break
        self.log('\n2) eagle2kicad_fix...')
        cmd = [_python(), '-u', os.path.join(HERE, 'eagle2kicad_fix.py'), self.target] + args
        flags = 0x08000000 if os.name == 'nt' else 0
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding='utf-8', errors='replace', creationflags=flags, cwd=self.target)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        blank = False
        for line in self.proc.stdout:
            line = line.rstrip()
            if 'image handler' in line or 'memory leak' in line:
                continue                       # wx / swig noise from the pcbnew module
            if not line.strip():
                if blank:
                    continue
                blank = True
            else:
                blank = False
            self.log(line)
        rc = self.proc.wait()
        wx.CallAfter(self._finish, rc)

    # -- step 3: open the fixed project in the editors
    def _finish(self, rc):
        if rc == 1:
            self.log('\n*** QC FAIL: the converted design is NOT identical to the Eagle source - '
                     'see "QC VERDICT" in the report before using it! ***')
        elif rc == 3:
            self.log('\n*** SELFTEST FAIL: the verifier missed an injected fault - results not trustworthy ***')
        elif rc != 0:
            self.log(f'\nFix-up FAILED (exit {rc}) - the KiCad import itself is saved and intact.')
            return
        else:
            self.log('\nQC PASS: netlists, pads, geometry and values identical to the Eagle source.')
        self.log('\n3) Opening the schematic and PCB editors...')
        for acc in ('\tCtrl+E', '\tCtrl+P'):
            mid = _find_item(self.mgr, lambda l, a=acc: l.endswith(a))
            if mid is not None:
                _menu_action(self.mgr, mid)
        self.log(f'\nDONE.  Report: {os.path.join(self.target, "eaglefix_report.md")}')
        self.log('PCB Editor: Tools > Update PCB from Schematic (F8), then B (refill zones), ERC/DRC.')
        wx.CallLater(3000, self._reenable)

    def _reenable(self):
        # KiCad leaves the project manager disabled after the editors were opened from here
        for w in (self.mgr, self.win):
            try:
                if w and not w.IsEnabled():
                    w.Enable(True)
            except Exception:
                pass
        if self.win:
            self.win.Raise()


class FixJob(ImportJob):
    """Fix + verify an ALREADY imported project (any platform): close the editors, run the fixer
    on the project folder against the Eagle source, reopen the editors."""
    def __init__(self, manager, src, project_file):
        super().__init__(manager, None, src, os.path.dirname(project_file))
        self.name = os.path.splitext(os.path.basename(project_file))[0]
        self.pro = project_file

    def start(self):
        _ACTIVE.append(self)
        self.log(f'Eagle source : {self.src}')
        self.log(f'KiCad project: {self.pro}')
        self.log('\n1) Closing the editors (save your changes when KiCad asks)...')
        for f in self._editor_frames():
            f.Close()
        wx.CallLater(1500, self._wait_closed)

    def _wait_closed(self, tries=0):
        if self._editor_frames() and tries < 120:
            wx.CallLater(500, self._wait_closed, tries + 1); return
        if self._editor_frames():
            self.log('Editors are still open - close them and run the button again.'); return
        wx.CallLater(500, self._run_fix)


# ------------------------------------------------------------------ plugin

class Eagle2KiCadImport(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = 'Eagle Exhumer: Eagle -> KiCad import + quality control'
        self.category = 'Import'
        self.description = ('KiCad Eagle project import + automatic fix of the known importer defects, then a '
                            'strict check of the result against the Eagle source (netlist, every pad, geometry)')
        self.show_toolbar_button = True
        icon = os.path.join(HERE, 'icon.png')
        if os.path.isfile(icon):
            self.icon_file_name = icon
            self.dark_icon_file_name = icon

    def Run(self):
        mgr, mid = _manager()
        if mgr is None:
            wx.MessageBox('Open the PCB Editor from the KiCad project manager window '
                          '(the Eagle import is a project-manager function).', 'Eagle Exhumer', wx.ICON_WARNING)
            return
        modes = ['Fix + verify the OPEN project (already imported with File > Import > EAGLE)']
        if os.name == 'nt':
            modes.insert(0, 'One-click import of an Eagle design into a NEW KiCad project')
        if len(modes) > 1:
            dlg = wx.SingleChoiceDialog(None, 'What should Eagle Exhumer do?', 'Eagle Exhumer', modes)
            if dlg.ShowModal() != wx.ID_OK:
                return
            fix_mode = dlg.GetSelection() == 1
            dlg.Destroy()
        else:
            fix_mode = True
        if fix_mode:
            pro = _project()
            if not pro or not os.path.isfile(pro):
                wx.MessageBox('No saved KiCad project is open.', 'Eagle Exhumer', wx.ICON_WARNING)
                return
            dlg = wx.FileDialog(None, 'The Eagle schematic or board this project was imported from',
                                defaultDir=os.path.dirname(os.path.dirname(pro)),
                                wildcard='Eagle (*.sch;*.brd)|*.sch;*.brd;*.SCH;*.BRD',
                                style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
            if dlg.ShowModal() != wx.ID_OK:
                return
            src = dlg.GetPath()
            dlg.Destroy()
            if wx.MessageBox('The schematic and PCB editors will be closed (KiCad asks to save unsaved '
                             'changes), the project files are fixed in place - a backup is kept in '
                             '_eaglefix_backup - and the editors are reopened.\n\nContinue?',
                             'Eagle Exhumer', wx.YES_NO | wx.ICON_QUESTION) != wx.YES:
                return
            job = FixJob(mgr, src, pro)
            wx.CallAfter(job.start)
            return
        dlg = wx.FileDialog(None, 'Eagle schematic / board (.sch, .brd)',
                            wildcard='Eagle (*.sch;*.brd)|*.sch;*.brd;*.SCH;*.BRD',
                            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        if dlg.ShowModal() != wx.ID_OK:
            return
        src = dlg.GetPath()
        dlg.Destroy()
        base = os.path.splitext(src)[0]
        for f in (src, base + '.sch', base + '.brd'):
            if os.path.isfile(f):
                with open(f, 'rb') as fh:
                    head = fh.read(400)
                if b'<eagle' not in head and b'<?xml' not in head:
                    wx.MessageBox(f'{os.path.basename(f)} is a binary EAGLE file (EAGLE 5.x or older).\n\n'
                                  'KiCad can only import EAGLE 6+ XML files. Open it in EAGLE 6...9 '
                                  '(or Fusion Electronics) and save it once, then run this import again.',
                                  'Eagle Exhumer', wx.ICON_WARNING)
                    return
        name = os.path.splitext(os.path.basename(src))[0]
        target = os.path.join(os.path.dirname(src), name + '_kicad')
        k = 2
        while os.path.isdir(target) and os.listdir(target):
            target = os.path.join(os.path.dirname(src), f'{name}_kicad_{k}'); k += 1
        os.makedirs(target, exist_ok=True)
        job = ImportJob(mgr, mid, src, target)
        wx.CallAfter(job.start)        # leave the PCB frame's event handler first: KiCad closes it


Eagle2KiCadImport().register()


# ------------------------------------------------------------------ batch hook
# eagle2kicad_batch.py drives a private kicad.exe from outside. Saving the imported editors needs
# an in-process menu dispatch (KiCad ACTION_MENU), so the batch script leaves a request file for
# this process; the plugin module is loaded when the import opens the PCB editor and does the save.

def _batch_dir():
    import tempfile
    return os.path.join(tempfile.gettempdir(), 'eagle2kicad_batch')


def _batch_poll(req, done, name, t0=None, tries=[0]):
    t0 = t0 or time.time()
    try:
        frames = [w for w in wx.GetTopLevelWindows() if isinstance(w, wx.Frame) and name in w.GetTitle()
                  and _find_item(w, lambda l: l.endswith('\tCtrl+S')) is not None]
        modal = any(isinstance(w, wx.Dialog) and w.IsShown() for w in wx.GetTopLevelWindows())
        if len(frames) >= 2 and not modal and all(f.IsEnabled() for f in frames):
            tries[0] += 1
            if tries[0] >= 3:
                for f in frames:
                    _menu_action(f, _find_item(f, lambda l: l.endswith('\tCtrl+S')))
                with open(done, 'w') as fh:
                    fh.write('saved ' + ', '.join(f.GetTitle() for f in frames))
                os.remove(req)
                return
        else:
            tries[0] = 0
    except Exception as e:
        with open(done + '.err', 'w') as fh:
            fh.write(repr(e))
    if time.time() - t0 < 900:
        wx.CallLater(1000, _batch_poll, req, done, name, t0)


def _batch_hook():
    try:
        req = os.path.join(_batch_dir(), f'save_{os.getpid()}.json')
        if os.path.isfile(req):
            import json
            with open(req) as fh:
                name = json.load(fh)['name']
            if not getattr(sys, '_eagle2kicad_batch_hook', False):
                sys._eagle2kicad_batch_hook = True
                wx.CallLater(1000, _batch_poll, req, req[:-5] + '.done', name)
    except Exception:
        pass


_batch_hook()
