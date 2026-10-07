#!/usr/bin/env python3
"""
eagle2kicad_batch.py - unattended Eagle -> KiCad 10 conversion + fix + verification.

KiCad 10.0 has no command-line schematic import (kicad-cli only imports boards), so the
schematic import still has to run inside kicad.exe. This script drives a private kicad.exe
instance from the outside (native menu commands, file/folder dialogs, layer mapping) - no
clicks, no user interaction - then runs eagle2kicad_fix.py (fixes + netlist / pad / geometry
verification + fault-injection selftest) and writes one summary table for all designs.

Run with KiCad's python:
  "C:\\Program Files\\KiCad\\10.0\\bin\\python.exe" eagle2kicad_batch.py <file.sch|file.brd|folder> ...
      [--out-suffix _kicad] [--force] [--timeout 600]
A folder is searched recursively for Eagle XML .sch/.brd pairs; binary (Eagle <= 5) files are
reported and skipped. Output: <name>_kicad next to each design, batch_summary.md in the cwd.
"""
import argparse, ctypes, glob, json, os, re, shutil, subprocess, sys, time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from win_dialogs import DialogDriver, _dialogs, _title  # noqa: E402

U = ctypes.windll.user32
WM_COMMAND, WM_CLOSE, MF_BYPOSITION = 0x0111, 0x0010, 0x400
EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def kicad_bin():
    for c in [os.path.dirname(sys.executable)] + sorted(glob.glob(r'C:\Program Files\KiCad\*\bin'), reverse=True):
        if os.path.isfile(os.path.join(c, 'kicad.exe')):
            return c
    sys.exit('kicad.exe not found')


# ------------------------------------------------------------------ Win32 helpers

def windows(pid):
    out = []
    def cb(h, _):
        p = wintypes.DWORD(); U.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid and U.IsWindowVisible(h) and not U.GetParent(h):
            out.append(h)
        return True
    U.EnumWindows(EnumProc(cb), 0)
    return out


def menu_items(hwnd):
    """[(id, text)] of every item in the native menu bar (recursive)."""
    out = []
    def walk(m):
        for i in range(U.GetMenuItemCount(m)):
            b = ctypes.create_unicode_buffer(512)
            U.GetMenuStringW(m, i, b, 512, MF_BYPOSITION)
            sub = U.GetSubMenu(m, i)
            if sub:
                walk(sub)
                out.append((None, b.value, sub))
            else:
                out.append((U.GetMenuItemID(m, i), b.value, m))
    bar = U.GetMenu(hwnd)
    if bar:
        walk(bar)
    return out


def eagle_import_id(hwnd):
    """EAGLE entry of the 'Import non-KiCad project' submenu (a submenu listing other CAD formats)."""
    groups = {}
    for mid, text, parent in menu_items(hwnd):
        if mid is not None:
            groups.setdefault(parent, []).append((mid, text.replace('&', '')))
    for items in groups.values():
        others = sum(1 for _, t in items if any(k in t.upper() for k in ('ALTIUM', 'CADSTAR', 'EASYEDA', 'GEDA')))
        if others >= 2:
            for mid, t in items:
                if t.upper().lstrip().startswith('EAGLE') and '\\' not in t and '/' not in t:
                    return mid
    return None


def accel_id(hwnd, accel):
    for mid, text, _ in menu_items(hwnd):
        if mid is not None and text.endswith('\t' + accel):
            return mid
    return None


def title(h):
    return _title(h)


def dialog_text(h):
    out = []
    def cb(c, _):
        b = ctypes.create_unicode_buffer(512); U.GetClassNameW(c, b, 64)
        if b.value in ('Static', 'Edit'):
            t = _title(c)
            if t.strip():
                out.append(t.strip())
        return True
    U.EnumChildWindows(h, EnumProc(cb), 0)
    return ' | '.join(out)[:400]


# ------------------------------------------------------------------ one design

def log(*a):
    print(*a, flush=True)


def convert(src, target, timeout, brd=None, method='cli'):
    """method 'cli': board via kicad-cli (no windows), schematic via the GUI import of the .sch alone;
    method 'gui': the full GUI import (board + schematic) - the old path, kept for comparison."""
    name = os.path.splitext(os.path.basename(src))[0]
    os.makedirs(target, exist_ok=True)
    meta = {'method': method}
    gui_src = src
    cli_pcb = None
    if method == 'cli' and brd:
        sys.path.insert(0, HERE)
        from cli_pcb_import import cli_import_pcb
        import tempfile
        tmp = tempfile.mkdtemp(prefix='eagle2kicad_cli_')      # not in target: KiCad wants it empty
        cli_pcb = os.path.join(tmp, name + '.kicad_pcb')
        ok, dt, rep = cli_import_pcb(brd, cli_pcb, log=lambda s_: log('   ', s_.strip()))
        if not ok:
            cli_pcb = None; meta['method'] = 'gui (cli failed)'
        else:
            meta.update(pcb_import_s=round(dt, 2), pcb_import_warnings=len(rep.get('warnings') or []))
            os.makedirs(os.path.join(tmp, 'sch'), exist_ok=True)
            gui_src = os.path.join(tmp, 'sch', os.path.basename(src)); shutil.copy2(src, gui_src)
    n_editors = 1 if cli_pcb else 2          # editors the IMPORT opens (schematic only with the cli board)
    # private kicad.exe with an empty scratch project (never the user's projects)
    scratch = os.path.join(os.environ.get('TEMP', '.'), 'eagle2kicad_batch_scratch')
    os.makedirs(scratch, exist_ok=True)
    pro = os.path.join(scratch, 'scratch.kicad_pro')
    if not os.path.isfile(pro):
        with open(pro, 'w') as f:
            f.write('{"meta": {"filename": "scratch.kicad_pro", "version": 3}}\n')
    for lck in glob.glob(os.path.join(scratch, '*.lck')):
        os.remove(lck)
    p = subprocess.Popen([os.path.join(kicad_bin(), 'kicad.exe'), pro])
    # the eagle2kicad plugin (loaded in that process when the import opens the PCB editor)
    # saves the imported editors in-process - see _batch_hook in __init__.py
    import json, tempfile
    bdir = os.path.join(tempfile.gettempdir(), 'eagle2kicad_batch'); os.makedirs(bdir, exist_ok=True)
    req = os.path.join(bdir, f'save_{p.pid}.json')
    with open(req, 'w') as f:
        json.dump({'name': name, 'target': target, 'editors': 2}, f)   # the save hook always sees both
    t_gui = time.time()
    try:
        t0 = time.time(); mgr = None
        while time.time() - t0 < 120 and mgr is None:
            for h in windows(p.pid):
                if eagle_import_id(h):
                    mgr = h
            time.sleep(0.5)
        if mgr is None:
            raise RuntimeError('KiCad project manager window not found')
        time.sleep(1.5)
        drv = DialogDriver([gui_src, target], log=lambda s: log('   ', s.strip()), layer_mapping=not cli_pcb, pid=p.pid, timeout=120)
        drv.start()
        t_mark = time.time() - 1
        U.PostMessageW(mgr, WM_COMMAND, eagle_import_id(mgr) & 0xFFFF, 0)
        # wait: both editors open with the new project, nothing modal, stable
        t0 = time.time(); stable = 0; eds = []; first = {}
        while time.time() - t0 < timeout:
            for dlg in _dialogs(p.pid):              # a message box nobody answers = error
                first.setdefault(dlg, time.time())
                if time.time() - first[dlg] > 90:
                    raise RuntimeError(f'KiCad dialog left open: "{title(dlg)}" ' + dialog_text(dlg))
            ws = windows(p.pid)
            eds = [h for h in ws if h != mgr and name in title(h) and accel_id(h, 'Ctrl+S')]
            busy = _dialogs(p.pid) or not all(U.IsWindowEnabled(h) for h in ws)
            stable = stable + 1 if (len(eds) >= n_editors and not busy) else 0
            if stable >= 6:
                break
            if p.poll() is not None:
                raise RuntimeError('kicad.exe exited during the import')
            time.sleep(0.5)
        else:
            raise RuntimeError('import did not finish (dialogs left open?): ' +
                               ', '.join(title(h) for h in _dialogs(p.pid)))
        log(f'    import done in {time.time() - t0:.0f} s, saving')
        if cli_pcb:
            # the save hook lives in the pcbnew plugin: open the PCB editor (empty board) so it loads;
            # its empty board is replaced by the kicad-cli board below
            pid_ = accel_id(mgr, 'Ctrl+P')
            if pid_:
                U.PostMessageW(mgr, WM_COMMAND, pid_ & 0xFFFF, 0)
            t1 = time.time(); stable = 0
            while time.time() - t1 < 120:
                ws = windows(p.pid)
                eds = [h for h in ws if h != mgr and name in title(h) and accel_id(h, 'Ctrl+S')]
                busy = _dialogs(p.pid) or not all(U.IsWindowEnabled(h) for h in ws)
                stable = stable + 1 if (len(eds) >= 2 and not busy) else 0
                if stable >= 4:
                    break
                time.sleep(0.5)
        drv.stop_flag = True
        # the in-process hook may already have saved before we got here -> compare with t_mark
        t0 = time.time()
        while time.time() - t0 < 120:
            need = ('.kicad_sch',) if cli_pcb else ('.kicad_sch', '.kicad_pcb')
            if os.path.isfile(req[:-5] + '.done') and all(
                    os.path.exists(os.path.join(target, name + e)) and
                    os.path.getmtime(os.path.join(target, name + e)) >= t_mark for e in need):
                break
            time.sleep(0.5)
        else:
            err = req[:-5] + '.done.err'
            raise RuntimeError('KiCad did not save the imported design'
                               + (' (plugin hook error: ' + open(err).read() + ')' if os.path.isfile(err) else
                                  '' if os.path.isfile(req[:-5] + '.done') else ' (plugin hook did not run - is the plugin installed?)'))
        time.sleep(2)
        meta['gui_import_s'] = round(time.time() - t_gui, 1)
    finally:
        for f in (req, req[:-5] + '.done', req[:-5] + '.done.err'):
            if os.path.isfile(f):
                os.remove(f)
        for h in windows(p.pid):
            U.PostMessageW(h, WM_CLOSE, 0, 0)
        try:
            p.wait(20)
        except subprocess.TimeoutExpired:
            p.kill()                         # everything is saved; a lingering exit is harmless
        for lck in glob.glob(os.path.join(target, '*.lck')):
            os.remove(lck)
    if cli_pcb:
        dst = os.path.join(target, name + '.kicad_pcb')
        if os.path.isfile(dst):
            os.replace(dst, os.path.join(target, '_gui_import.kicad_pcb'))
        shutil.move(cli_pcb, dst)
        rep = cli_pcb + '.import.json'
        if os.path.isfile(rep):
            shutil.move(rep, os.path.join(target, 'eaglefix_pcb_import.json'))
        shutil.rmtree(os.path.dirname(cli_pcb), ignore_errors=True)
        gui = os.path.join(target, '_gui_import.kicad_pcb')
        if os.path.isfile(gui) and os.path.getsize(gui) < 10000:
            os.remove(gui)                              # the empty board the save hook wrote
    return meta


def run_fix(target, sch, brd, meta=None):
    src_dir = os.path.join(target, 'eagle_source')
    os.makedirs(src_dir, exist_ok=True)
    args = []
    for f, opt in ((sch, '--eagle-sch'), (brd, '--eagle-brd')):
        if f:
            dst = os.path.join(src_dir, os.path.basename(f)); shutil.copy2(f, dst); args += [opt, dst]
    if meta:
        args += ['--import-meta', json.dumps(meta)]
    r = subprocess.run([sys.executable, '-u', os.path.join(HERE, 'eagle2kicad_fix.py'), target] + args,
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    with open(os.path.join(target, 'eaglefix_console.log'), 'w', encoding='utf-8') as f:
        f.write(r.stdout + '\n' + '\n'.join(l for l in r.stderr.splitlines() if 'image handler' not in l))
    return r.returncode, r.stdout


SUMMARY_KEYS = [
    ('sch', r'\*\*Eagle schematic vs KiCad schematic\*\*: (\d+) merged.*?, (\d+) split.*?, (\d+) named.*?, (\d+) Eagle pads absent'),
    ('pcb', r'\*\*Eagle board vs KiCad PCB pads\*\*: (\d+) merged.*?, (\d+) split'),
    ('pads', r'\*\*Eagle pads vs KiCad pads\*\*: (\d+) pads, (\d+) misplaced, (\d+) missing, (\d+) SMD size, (\d+) drill'),
    ('geom', r'\*\*Eagle board vs KiCad geometry\*\*: (\d+) parts \((\d+) moved, (\d+) wrong side, (\d+) rotated, (\d+) values.*?length differs on (\d+) nets, via count/drill differs on (\d+) nets'),
    ('erc', r'ERC: total (\d+)'),
    ('drc', r'DRC \(\+schematic parity\): total (\d+)'),
    ('selftest', r'selftest: (\d+)/(\d+)'),
]


def summarize(out):
    tail = out[out.rfind('VERIFY (after)'):] if 'VERIFY (after)' in out else out
    res = {}
    for k, rx in SUMMARY_KEYS:
        src = out if k == 'selftest' else tail
        m = list(re.finditer(rx, src))
        res[k] = m[0].groups() if m else None
    return res


def is_xml(f):
    with open(f, 'rb') as fh:
        h = fh.read(400)
    return b'<eagle' in h or b'<?xml' in h


def find_designs(paths):
    pairs = {}
    for p in paths:
        p = os.path.abspath(p)
        files = [p] if os.path.isfile(p) else glob.glob(os.path.join(p, '**', '*.*'), recursive=True)
        for f in files:
            if f.lower().endswith(('.sch', '.brd')) and '_kicad' not in f and 'eagle_source' not in f:
                pairs.setdefault(os.path.splitext(f)[0], {})[f.lower()[-3:]] = f
    return pairs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('paths', nargs='+')
    ap.add_argument('--out-suffix', default='_kicad')
    ap.add_argument('--force', action='store_true', help='re-convert even if the output folder exists')
    ap.add_argument('--timeout', type=int, default=600)
    ap.add_argument('--method', choices=['cli', 'gui'], default='cli',
                    help='cli: board via kicad-cli + schematic via GUI (default); gui: full GUI import')
    a = ap.parse_args()
    rows = []
    for base, fs in sorted(find_designs(a.paths).items()):
        sch, brd = fs.get('sch'), fs.get('brd')
        label = os.path.relpath(base)
        bad = [f for f in (sch, brd) if f and not is_xml(f)]
        if bad or not sch:
            why = 'binary Eagle (<=5): ' + ', '.join(os.path.basename(b) for b in bad) if bad else 'no .sch'
            log(f'SKIP {label}: {why}'); rows.append((label, 'SKIP', why, {})); continue
        target = base + a.out_suffix
        if os.path.isdir(target) and os.listdir(target):
            if not a.force:
                log(f'SKIP {label}: {target} exists (use --force)'); rows.append((label, 'SKIP', 'exists', {})); continue
            shutil.rmtree(target)
        log(f'== {label}')
        t0 = time.time()
        try:
            t_c = time.time()
            meta = convert(sch, target, a.timeout, brd=brd, method=a.method)
            meta['convert_total_s'] = round(time.time() - t_c, 1)
            t_f = time.time()
            rc, out = run_fix(target, sch, brd, meta)
            log(f'    fix {time.time() - t_f:.0f} s, convert {meta["convert_total_s"]} s ({meta["method"]})')
            res = summarize(out)
            log(f'    fix exit {rc}, {time.time() - t0:.0f} s total')
            st = {0: 'QC PASS', 1: 'QC FAIL', 3: 'SELFTEST FAIL'}.get(rc, f'FIX ERROR rc={rc}')
            why = ''
            if rc == 1:
                m = re.search(r'\*\*FAIL\*\* - \d+ finding\(s\):\n((?:  .*\n)+)', out)
                why = '; '.join(l.strip() for l in (m.group(1).splitlines() if m else [])
                                if not l.strip().startswith(('3D render', 'eaglefix_metrics')))[:300]
            res['time'] = (f"{meta.get('convert_total_s', 0):.0f}", f"{time.time() - t_f:.0f}")
            rows.append((label, st, why, res))
        except Exception as e:
            log(f'    FAILED: {e}')
            rows.append((label, 'FAIL', str(e), {}))
    # summary table
    def g(res, k, i, d='-'):
        v = res.get(k)
        return v[i] if v else d
    lines = ['# Eagle -> KiCad batch summary', '',
             '| design | status | sch short/open/renamed/absent | pcb short/open | pads misplaced/missing/size/drill '
             '| parts moved/side/rot/value | len nets / via nets | ERC | DRC | selftest | import s / fix+QC s |',
             '|---|---|---|---|---|---|---|---|---|---|---|']
    for label, st, why, res in rows:
        if not res:
            lines.append(f'| {label} | {st} | {why} ||||||||| '); continue
        lines.append(f"| {label} | {st}{(' (' + why + ')') if why else ''} | {'/'.join(res['sch'] or ['-'])} | {'/'.join(res['pcb'] or ['-'])} | "
                     f"{'/'.join((res['pads'] or ['-'] * 5)[1:])} | {'/'.join((res['geom'] or ['-'] * 7)[1:5])} | "
                     f"{g(res, 'geom', 5)} / {g(res, 'geom', 6)} | {g(res, 'erc', 0)} | {g(res, 'drc', 0)} | "
                     f"{'/'.join(res['selftest'] or ['-'])} | {' / '.join(res.get('time') or ['-'])} |")
    with open('batch_summary.md', 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    log('\n'.join(lines))


if __name__ == '__main__':
    main()
