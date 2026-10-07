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
VERSION = '0.9.7'

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


from cli_pcb_import import cli_import_pcb


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

SPIN = '|/-\\'


class LogWindow(wx.Frame):
    """Progress window: what is happening now, how far we are, that we are still alive - and, at the
    end, the verdict with the next actions. The raw log is there, but folded away."""
    def __init__(self, parent, project_dir=None, mode_note=''):
        super().__init__(parent, title=f'Eagle Exhumer {VERSION}', size=(860, 300),
                         style=wx.DEFAULT_FRAME_STYLE | (wx.FRAME_FLOAT_ON_PARENT if parent else 0))
        self.project_dir = project_dir
        self.mgr = None
        self.t0 = time.time()
        self.pct = 0
        self.spin = 0
        self.done = False
        p = self.panel = wx.Panel(self)
        big = wx.Font(13, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        mono = wx.Font(10, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        self.stage = wx.StaticText(p, label='Starting...')
        self.stage.SetFont(big)
        self.gauge = wx.Gauge(p, range=100, size=(-1, 18))
        self.bar = wx.StaticText(p, label='')
        self.bar.SetFont(mono)
        self.note = wx.StaticText(p, label=mode_note)
        self.note.SetForegroundColour(wx.Colour(150, 90, 0))
        self.result = wx.StaticText(p, label='')
        self.result.SetFont(big)
        self.txt = wx.TextCtrl(p, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL | wx.TE_RICH2)
        self.txt.SetFont(wx.Font(9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self.b_details = wx.ToggleButton(p, label='Show details')
        self.b_sch = wx.Button(p, label='Open schematic')
        self.b_pcb = wx.Button(p, label='Open PCB')
        self.b_report_md = wx.Button(p, label='Open report')
        self.b_folder = wx.Button(p, label='Project folder')
        self.b_problem = wx.Button(p, label='Report a problem...')
        for b in (self.b_sch, self.b_pcb, self.b_report_md):
            b.Hide()
        self.b_details.Bind(wx.EVT_TOGGLEBUTTON, self._toggle)
        self.b_folder.Bind(wx.EVT_BUTTON, self._open_folder)
        self.b_problem.Bind(wx.EVT_BUTTON, self._report)
        self.b_report_md.Bind(wx.EVT_BUTTON, self._open_report)
        self.b_sch.Bind(wx.EVT_BUTTON, lambda e: self._open_editor('\tCtrl+E'))
        self.b_pcb.Bind(wx.EVT_BUTTON, lambda e: self._open_editor('\tCtrl+P'))
        btns = wx.BoxSizer(wx.HORIZONTAL)
        btns.Add(self.b_details, 0, wx.RIGHT, 8)
        btns.AddStretchSpacer()
        for b in (self.b_sch, self.b_pcb, self.b_report_md, self.b_folder, self.b_problem):
            btns.Add(b, 0, wx.LEFT, 6)
        box = wx.BoxSizer(wx.VERTICAL)
        box.Add(self.stage, 0, wx.EXPAND | wx.ALL, 10)
        box.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        box.Add(self.bar, 0, wx.EXPAND | wx.ALL, 10)
        box.Add(self.note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        box.Add(self.result, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        box.Add(self.txt, 1, wx.EXPAND | wx.ALL, 10)
        box.Add(btns, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        p.SetSizer(box)
        self.note.Wrap(820)
        self.txt.Hide()
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._tick, self.timer)
        self.timer.Start(250)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.CentreOnScreen()
        self.Show()

    def _on_close(self, evt):
        """Always closable - also while a step is still running (the run itself goes on)."""
        try:
            self.timer.Stop()
        except Exception:
            pass
        self.Destroy()

    def _modal_open(self):
        return any(isinstance(w, wx.Dialog) and w.IsShown() and w.IsModal() for w in wx.GetTopLevelWindows())

    def _keep_usable(self):
        # KiCad disables its frames around its own modal steps and sometimes leaves them disabled;
        # never let that lock this window (or the project manager) once no dialog is open
        if self._modal_open():
            return
        for w in (self, self.mgr):
            try:
                if w and isinstance(w, wx.Window) and not w.IsEnabled():
                    w.Enable(True)
            except Exception:
                pass

    # -- progress
    def set_stage(self, pct, label):
        if not self:
            return
        self.pct = max(self.pct, min(100, int(pct)))
        if label and label != self.stage.GetLabel():
            self.stage.SetLabel(label)
            if not any(isinstance(w, wx.Dialog) and w.IsShown() for w in wx.GetTopLevelWindows()):
                self.Raise()                   # back on top after KiCad's windows - never over a dialog
        self.gauge.SetValue(self.pct)
        self._tick()

    def _tick(self, _evt=None):
        if not self:
            return
        self._keep_usable()
        el = int(time.time() - self.t0)
        n = 30
        k = self.pct * n // 100
        if self.done:
            self.bar.SetLabel(f'[{"#" * n}] 100%   finished in {el // 60:02d}:{el % 60:02d}')
            return
        self.spin = (self.spin + 1) % len(SPIN)
        self.bar.SetLabel(f'[{"#" * k}{"." * (n - k)}] {self.pct:3d}%   {SPIN[self.spin]} working, '
                          f'{el // 60:02d}:{el % 60:02d} elapsed - not frozen, some steps take a minute')

    def log(self, s):
        if self:
            self.txt.AppendText(s + '\n')

    def _toggle(self, _evt=None):
        show = self.b_details.GetValue()
        self.txt.Show(show)
        self.b_details.SetLabel('Hide details' if show else 'Show details')
        w, h = self.GetSize()
        self.SetSize((w, max(h, 640) if show else 300 + (60 if self.done else 0)))
        self.panel.Layout()

    # -- end state
    def finish(self, ok, headline, detail, can_open=True):
        if not self:
            return
        self.done = True
        self.timer.Start(1000)                 # keep the window usable (see _keep_usable)
        self.gauge.SetValue(100 if ok is not None else self.pct)
        self.stage.SetLabel(headline)
        self.result.SetLabel(detail)
        self.result.SetForegroundColour(wx.Colour(0, 130, 40) if ok else wx.Colour(190, 30, 30))
        self.result.Wrap(820)
        self.note.SetLabel('')
        self._tick()
        for b in (self.b_sch, self.b_pcb):
            b.Show(bool(can_open and self.mgr))
        self.b_report_md.Show(bool(self.project_dir and os.path.isfile(os.path.join(self.project_dir, 'eaglefix_report.md'))))
        if ok is False:
            self.b_details.SetValue(True)
        self._toggle()
        self.panel.Layout()
        self.Raise()

    def _open_editor(self, accel):
        """One editor per click - opening both at once makes KiCad abort the first load."""
        if not self.mgr:
            return
        mid = _find_item(self.mgr, lambda l: l.endswith(accel))
        if mid is not None:
            _menu_action(self.mgr, mid)

    def _open_report(self, _evt=None):
        f = os.path.join(self.project_dir or '', 'eaglefix_report.md')
        if os.path.isfile(f):
            wx.LaunchDefaultApplication(f)

    def _open_folder(self, _evt=None):
        if self.project_dir and os.path.isdir(self.project_dir):
            wx.LaunchDefaultApplication(self.project_dir)

    def _report(self, _evt=None):
        """Diagnostic zip + pre-filled GitHub issue. Design files only with explicit consent."""
        import diag, webbrowser
        if not self.project_dir or not os.path.isdir(self.project_dir):
            webbrowser.open(diag.REPO + '/issues/new/choose'); return
        ans = wx.MessageBox(
            'Eagle Exhumer will pack a diagnostic zip (run report, metrics, logs, versions) and open a '
            'pre-filled GitHub issue in your browser. Nothing is uploaded automatically - you drag the zip '
            'into the issue yourself.\n\nThe report lists part references and net names. Also include the '
            'DESIGN FILES (KiCad project + Eagle source)? Only say Yes if you are allowed to share them '
            'publicly - GitHub issues are public.',
            'Report a problem', wx.YES_NO | wx.CANCEL | wx.NO_DEFAULT | wx.ICON_QUESTION)
        if ans == wx.CANCEL:
            return
        try:
            zpath, env, summ = diag.make_bundle(self.project_dir, include_design=(ans == wx.YES),
                                                extra_log=self.txt.GetValue())
        except Exception as e:
            wx.MessageBox(f'Could not create the diagnostic zip: {e}', 'Report a problem', wx.ICON_ERROR); return
        self.log(f'\nDiagnostic zip: {zpath}\nDrag it into the GitHub issue that is opening now.')
        webbrowser.open(diag.issue_url(env, summ, ans == wx.YES))
        wx.LaunchDefaultApplication(os.path.dirname(zpath))


# ------------------------------------------------------------------ job

class ImportJob:
    def __init__(self, manager, eagle_mid, src, target):
        self.mgr, self.mid, self.src, self.target = manager, eagle_mid, src, target
        self.name = os.path.splitext(os.path.basename(src))[0]
        self.pro = os.path.join(target, self.name + '.kicad_pro')
        self.win = LogWindow(None, target, mode_note=self.NOTE)
        self.win.mgr = manager
        self.mapped = set()
        self.timer = None
        self.fix_base, self.fix_step = 20, 'Step 3/3'

    NOTE = ('KiCad now opens and closes its own windows (file dialogs, layer mapping, schematic and PCB '
            'editors) - this is expected, please do not click them. This window shows where we are.')

    def log(self, s):
        wx.CallAfter(self.win.log, s)

    def stage(self, pct, label):
        wx.CallAfter(self.win.set_stage, pct, label)

    # -- step 1: KiCad's own import
    def start(self):
        _ACTIVE.append(self)
        self.t_start = time.time()
        self.log(f'Eagle source : {self.src}')
        self.log(f'KiCad project: {self.pro}')
        base = os.path.splitext(self.src)[0]
        self.brd = next((c for c in (base + '.brd', base + '.BRD') if os.path.isfile(c)), None)
        self.sch = next((c for c in (base + '.sch', base + '.SCH') if os.path.isfile(c)), None)
        self.import_meta = {'method': 'gui', 'kicad_version': getattr(pcbnew, 'GetBuildVersion', lambda: '?')()}
        # 1a) the BOARD through kicad-cli: no windows at all
        self.cli_pcb = None
        if self.brd:
            self.stage(2, 'Step 1/3 - Importing the board (kicad-cli, no windows)')
            self.log('\n1a) kicad-cli pcb import (board)...')
            import tempfile
            # NOT inside the target: KiCad's project import insists on an empty destination folder
            self.tmp_dir = tempfile.mkdtemp(prefix='eagle_exhumer_')
            tmp_pcb = os.path.join(self.tmp_dir, self.name + '.kicad_pcb')
            ok, dt, rep = cli_import_pcb(self.brd, tmp_pcb, log=self.log)
            if ok:
                self.cli_pcb = tmp_pcb
                self.import_meta.update(method='cli-pcb+gui-sch', pcb_import_s=round(dt, 2),
                                        pcb_import_warnings=len(rep.get('warnings') or []))
            else:
                self.log('  falling back to the full KiCad GUI import (board + schematic)')
        # 1b) the SCHEMATIC through KiCad's GUI import (there is no command-line schematic import).
        #     With the board already done, KiCad only gets the .sch: a copy in a folder without the
        #     .brd, so no board import, no layer-mapping dialog, no PCB editor flashing up.
        gui_src = self.src
        if self.cli_pcb and self.sch:
            import shutil
            self.tmp_sch_dir = os.path.join(self.tmp_dir, 'sch'); os.makedirs(self.tmp_sch_dir, exist_ok=True)
            gui_src = os.path.join(self.tmp_sch_dir, os.path.basename(self.sch))
            shutil.copy2(self.sch, gui_src)
        elif self.cli_pcb and not self.sch:
            gui_src = None                              # board only: nothing for the GUI to do
        self.gui_src = gui_src
        if gui_src is None:
            self.log('  no Eagle schematic - board-only project')
            self._write_project_from_cli()
            wx.CallLater(200, self._run_fix)
            return
        self.log('\n1b) KiCad Eagle import of the schematic (File > Import Non-KiCad Project > EAGLE)...')
        self.stage(6, 'Step 1/3 - KiCad imports the schematic')
        from win_dialogs import DialogDriver
        # file dialog, destination dialog, then (full GUI import only) the layer-mapping dialog;
        # the driver keeps confirming KiCad's info boxes until the whole run is over
        self.driver = DialogDriver([gui_src, self.target], log=self.log, layer_mapping=not self.cli_pcb)
        self.driver.start()
        wx.CallAfter(self._run_import)

    def _write_project_from_cli(self):
        """Board-only Eagle design: the project file + the kicad-cli board, no GUI import at all."""
        import shutil
        shutil.move(self.cli_pcb, os.path.join(self.target, self.name + '.kicad_pcb'))
        if not os.path.isfile(self.pro):
            with open(self.pro, 'w', encoding='utf-8') as f:
                f.write('{"meta": {"filename": "%s", "version": 3}}\n' % os.path.basename(self.pro))

    def _place_cli_pcb(self):
        """After the GUI schematic import: the kicad-cli board becomes THE board of the project."""
        import shutil
        if not self.cli_pcb:
            return
        dst = os.path.join(self.target, self.name + '.kicad_pcb')
        if os.path.isfile(dst):
            os.replace(dst, os.path.join(self.target, '_gui_import.kicad_pcb'))   # should not exist; kept
        shutil.move(self.cli_pcb, dst)
        rep = self.cli_pcb + '.import.json'
        if os.path.isfile(rep):
            shutil.move(rep, os.path.join(self.target, 'eaglefix_pcb_import.json'))
        self.log(f'  board from kicad-cli placed as {os.path.basename(dst)}')
        self.cli_pcb = None
        shutil.rmtree(getattr(self, 'tmp_dir', ''), ignore_errors=True)

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
        _menu(self.mgr, self.mid)            # returns when KiCad finished the import (NOT saved!)
        # (pcbnew.GetSettingsManager() is not usable here: the PCB frame that ran the plugin is gone)
        if not os.path.isfile(self.pro) or not self._editor_frames():
            self.log('\nImport cancelled or failed (no imported project / editors found).')
            self._stop_driver()
            self.win.finish(False, 'Stopped - the KiCad import did not finish',
                            'KiCad did not produce the imported project (cancelled, or an import dialog was '
                            'closed). Nothing was changed. Try again, or use "Report a problem...".', can_open=False)
            return
        self.log(f'  KiCad import done in {time.time() - t0:.0f} s')
        self.import_meta['gui_import_s'] = round(time.time() - t0, 1)
        self.stage(16, 'Step 2/3 - Saving the imported project')
        # KiCad 10.0.5 leaves both editors unsaved -> File > Save in each, then close them
        for f in self._editor_frames():
            mid = _find_item(f, lambda l: l.endswith('\tCtrl+S'))
            self.log(f'  saving: {f.GetTitle()}')
            _menu_action(f, mid)
        wx.CallLater(1500, self._close_editors_then_fix)

    def _close_editors_then_fix(self, tries=0):
        need = ('.kicad_sch',) if self.cli_pcb else ('.kicad_sch', '.kicad_pcb')
        missing = [e for e in need if not os.path.isfile(os.path.join(self.target, self.name + e))]
        if missing and tries < 20:
            wx.CallLater(500, self._close_editors_then_fix, tries + 1); return
        if missing:
            self.log(f'\nNot saved by KiCad: {missing} - save the editors manually, then run the fix-only .bat')
            self._stop_driver()
            self.win.finish(False, 'Stopped - KiCad did not save the imported project',
                            'Save the schematic and the PCB editor (Ctrl+S), then click the Eagle Exhumer button '
                            'and choose "Fix + check the OPEN project".', can_open=False)
            return
        for f in self._editor_frames():
            f.Close()
        wx.CallLater(1000, self._after_close)

    def _after_close(self, tries=0):
        if self._editor_frames() and tries < 40:
            wx.CallLater(500, self._after_close, tries + 1); return
        self._place_cli_pcb()
        self._run_fix()

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
        self.stage(self.fix_base, f'{self.fix_step} - Repair and quality control')
        import json as _json
        meta = dict(getattr(self, 'import_meta', {}) or {})
        if getattr(self, 't_start', None):
            meta['import_total_s'] = round(time.time() - self.t_start, 1)
        cmd = [_python(), '-u', os.path.join(HERE, 'eagle2kicad_fix.py'), self.target] + args
        if meta:
            cmd += ['--import-meta', _json.dumps(meta)]
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
            if line.startswith('@@PROGRESS'):
                parts = line.split(' ', 2)
                try:
                    pct = self.fix_base + int(parts[1]) * (100 - self.fix_base) // 100
                    self.stage(pct, f'{self.fix_step} - ' + (parts[2] if len(parts) > 2 else ''))
                except ValueError:
                    pass
                continue
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
    def _save_log(self):
        try:
            with open(os.path.join(self.target, 'eaglefix_console.log'), 'w', encoding='utf-8') as f:
                f.write(self.win.txt.GetValue())
        except Exception:
            pass

    def _stop_driver(self):
        d = getattr(self, 'driver', None)
        if d is not None:
            d.stop_flag = True

    def _warnings(self):
        """Non-blocking warnings of the run (from eaglefix_metrics.json) as one line for the banner."""
        try:
            import json
            w = json.load(open(os.path.join(self.target, 'eaglefix_metrics.json'), encoding='utf-8')).get('warnings') or {}
        except Exception:
            return ''
        out = []
        if w.get('no_3d_model'):
            out.append(f"{w['no_3d_model']} footprints have no 3D model (Eagle designs carry none - the 3D view "
                       f"shows bare pads; assign models in the footprint properties if you need them)")
        if w.get('3d_model_not_found'):
            out.append(f"{w['3d_model_not_found']} footprints point to a 3D model file that is not found")
        return ('\n\nWarning: ' + '; '.join(out) + '.') if out else ''

    def _finish(self, rc):
        self._stop_driver()
        wx.CallLater(500, self._save_log)
        warn = self._warnings()
        rep = os.path.join(self.target, 'eaglefix_report.md')
        if rc == 0:
            self.win.finish(True, 'Done - QC PASS',
                            'The KiCad project matches the Eagle source. Open the schematic or the PCB below; '
                            'in the PCB editor press B to refill the zones.' + warn)
        elif rc == 1:
            self.win.finish(False, 'Done - QC FAIL: please check before you use it',
                            'The project was converted and fixed, but the quality control found differences '
                            'from the Eagle source. They are listed under "QC VERDICT" in the report.' + warn)
        elif rc == 3:
            self.win.finish(False, 'Done - QC self-check failed',
                            'The quality control could not prove itself on this design, so the result is not '
                            'trustworthy. Please use "Report a problem...".')
        else:
            self.win.finish(False, f'Stopped - the repair step failed (exit {rc})',
                            'The KiCad import itself is saved and intact. Please use "Report a problem..." - it '
                            'packs the logs and opens a GitHub issue.', can_open=os.path.isfile(self.pro))
        self.log(f'\nReport: {rep}')
        wx.CallLater(1500, self._reenable)

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
    NOTE = ('The schematic and PCB editors are closed now (KiCad asks you to save unsaved changes - '
            'answer that dialog), then the project is repaired and checked. Use the buttons at the end to '
            'reopen the editors.')

    def __init__(self, manager, src, project_file):
        super().__init__(manager, None, src, os.path.dirname(project_file))
        self.name = os.path.splitext(os.path.basename(project_file))[0]
        self.pro = project_file
        self.fix_base, self.fix_step = 5, 'Step 2/2'

    def start(self):
        _ACTIVE.append(self)
        self.log(f'Eagle source : {self.src}')
        self.log(f'KiCad project: {self.pro}')
        self.log('\n1) Closing the editors (save your changes when KiCad asks)...')
        self.stage(1, 'Step 1/2 - Closing the editors')
        for f in self._editor_frames():
            f.Close()
        wx.CallLater(1500, self._wait_closed)

    def _wait_closed(self, tries=0):
        if self._editor_frames() and tries < 120:
            wx.CallLater(500, self._wait_closed, tries + 1); return
        if self._editor_frames():
            self.win.finish(None, 'Not started - the editors are still open',
                            'Close the schematic and PCB editors, then click the Eagle Exhumer button again.',
                            can_open=False)
            return
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
        modes = ['Fix + check the OPEN project (already imported with File > Import > EAGLE)']
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

        def free(base):
            t, k = os.path.join(base, name + '_kicad'), 2
            while os.path.isdir(t) and os.listdir(t):
                t = os.path.join(base, f'{name}_kicad_{k}'); k += 1
            return t
        target = free(os.path.dirname(src))
        dlg = wx.MessageDialog(None, f'Where should the new KiCad project go?\n\n'
                                     f'Suggested: a new sub-folder next to the Eagle file\n{target}',
                               'Eagle Exhumer - destination', wx.YES_NO | wx.CANCEL | wx.ICON_QUESTION)
        dlg.SetYesNoCancelLabels('Use this folder', 'Choose another folder...', 'Cancel')
        ans = dlg.ShowModal(); dlg.Destroy()
        if ans == wx.ID_CANCEL:
            return
        if ans == wx.ID_NO:
            dd = wx.DirDialog(None, 'Folder for the new KiCad project (a sub-folder is created inside it '
                                    'if it is not empty)', defaultPath=os.path.dirname(src),
                              style=wx.DD_DEFAULT_STYLE)
            if dd.ShowModal() != wx.ID_OK:
                dd.Destroy(); return
            base = dd.GetPath(); dd.Destroy()
            target = base if (os.path.isdir(base) and not os.listdir(base)) else free(base)
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
    need = getattr(_batch_poll, 'need', 2)
    t0 = t0 or time.time()
    try:
        frames = [w for w in wx.GetTopLevelWindows() if isinstance(w, wx.Frame) and name in w.GetTitle()
                  and _find_item(w, lambda l: l.endswith('\tCtrl+S')) is not None]
        modal = any(isinstance(w, wx.Dialog) and w.IsShown() for w in wx.GetTopLevelWindows())
        if len(frames) >= need and not modal and all(f.IsEnabled() for f in frames):
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
                rq = json.load(fh)
            name = rq['name']
            _batch_poll.need = int(rq.get('editors', 2))
            if not getattr(sys, '_eagle2kicad_batch_hook', False):
                sys._eagle2kicad_batch_hook = True
                wx.CallLater(1000, _batch_poll, req, req[:-5] + '.done', name)
    except Exception:
        pass


_batch_hook()
