"""Linux backend of eagle2kicad_batch.py: drive a private KiCad on its own virtual X display.

KiCad 10 has no command-line schematic import, so the batch runs KiCad's own
"Import Non-KiCad Project > EAGLE" flow. On Linux this module provides the same operations as the
Win32 code path, through the accessibility bus (AT-SPI: menu items, dialogs, buttons and message
texts by name) plus xdotool for the few keystrokes GTK needs. Nothing touches the user's desktop:
the batch starts its own Xvfb display, session D-Bus and AT-SPI bus and stops them at the end.

The AT-SPI queries run in a helper process (atspi_srv.py) that is restarted when a call hangs.
AT-SPI is used read-only (window list, texts, screen boxes); every action that moves KiCad on is
a real X click / keystroke (xdotool) - an AT-SPI action deadlocks KiCad when it opens a modal window.
Windows are plain snapshots: {'key': [role, name, index], 'role', 'name', 'buttons', 'text', 'progress'}.

Needs: Xvfb, xdotool, dbus-daemon, at-spi2-core, python3-gi with the Atspi 2.0 typelib.
"""
import json, os, re, shutil, signal, subprocess, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))

# KiCad messages known to be harmless during the EAGLE import (same list as win_dialogs.BENIGN)
BENIGN = [
    (re.compile(r'sym-lib-table', re.I), 'transient sym-lib-table read during import (KiCad 10, harmless)'),
    (re.compile(r'fp-lib-table', re.I), 'transient fp-lib-table read during import (KiCad 10, harmless)'),
]
# KiCad questions with a known right answer in the batch flow: (text, button, why)
ANSWERS = [
    (re.compile(r"\.kicad_pcb' does not exist\. Do you wish to create it", re.I), 'yes',
     'empty board for the save hook (replaced by the kicad-cli board)'),
]
DIALOG_ROLES = ('dialog', 'alert', 'file chooser', 'color chooser')
OK_NAMES = ('ok', 'open', 'select')
EAGLE_MENU = ['file', 'import non-kicad', 'eagle']
PCB_MENU = ['tools', 'pcb editor']


def benign_reason(text):
    for rx, why in BENIGN:
        if text and rx.search(text):
            return why
    return None


def _need(tool):
    if not shutil.which(tool):
        raise RuntimeError(f'{tool} not found (Linux batch needs: Xvfb xdotool dbus-daemon at-spi2-core python3-gi)')


class _Srv:
    """The atspi_srv.py helper: one request at a time, restarted when a call does not return."""

    def __init__(self, env, log):
        self.env, self.log = env, log
        self.lock = threading.Lock()
        self.p = None
        self.restarts = 0

    def _start(self):
        self.p = subprocess.Popen([sys.executable, '-u', os.path.join(HERE, 'atspi_srv.py')], env=self.env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True, bufsize=1)

    def stop(self):
        if self.p:
            try:
                self.p.kill(); self.p.wait(5)
            except Exception:
                pass
        self.p = None

    def call(self, cmd, timeout=8.0, **kw):
        with self.lock:
            for attempt in range(3):
                if self.p is None or self.p.poll() is not None:
                    self._start()
                box = []
                def rd():
                    try:
                        box.append(self.p.stdout.readline())
                    except Exception:
                        pass
                try:
                    self.p.stdin.write(json.dumps(dict(kw, cmd=cmd)) + '\n'); self.p.stdin.flush()
                except Exception:
                    self.stop(); continue
                t = threading.Thread(target=rd, daemon=True); t.start(); t.join(timeout)
                if not box or not box[0]:
                    self.restarts += 1
                    self.stop()                  # wedged AT-SPI client: start a fresh one
                    continue
                rep = json.loads(box[0])
                if rep.get('ok'):
                    return rep['r']
                raise RuntimeError(f'AT-SPI {cmd}: {rep.get("err")}')
            raise RuntimeError(f'AT-SPI {cmd}: no answer after 3 helper restarts')


class LinuxUI:
    """A private Xvfb + D-Bus + AT-SPI session; one KiCad process per design is started in it."""

    def __init__(self, log=print):
        for t in ('Xvfb', 'xdotool', 'dbus-daemon', 'dbus-send', 'kicad'):
            _need(t)
        self.log = log
        import faulthandler
        faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)   # kill -USR1: where is it stuck
        self.tmp = tempfile.mkdtemp(prefix='eagle2kicad_x_')
        self.procs, self.p = [], None
        self.display = self._start_x()
        self.env = dict(os.environ, DISPLAY=self.display, LANG='C.UTF-8', LC_ALL='C.UTF-8', LANGUAGE='en',
                        GTK_MODULES='gail:atk-bridge', NO_AT_BRIDGE='0', GDK_BACKEND='x11')
        self.env.pop('WAYLAND_DISPLAY', None)
        # GTK file chooser with its path field always shown (no keyboard needed): a private GSettings
        # keyfile, so the user's own desktop settings are not touched; KiCad keeps its real config dir
        xdg = os.path.join(self.tmp, 'config'); os.makedirs(os.path.join(xdg, 'glib-2.0', 'settings'))
        with open(os.path.join(xdg, 'glib-2.0', 'settings', 'keyfile'), 'w') as f:
            f.write("[org/gtk/settings/file-chooser]\nlocation-mode='filename-entry'\n"
                    "show-hidden=false\nsort-directories-first=true\n")
        self.env['KICAD_CONFIG_HOME'] = self._private_kicad_config()
        self.env.update(XDG_CONFIG_HOME=xdg, GSETTINGS_BACKEND='keyfile')
        bus = 'unix:path=' + os.path.join(self.tmp, 'bus')
        self.procs.append(subprocess.Popen(['dbus-daemon', '--session', '--nofork', '--address=' + bus], env=self.env,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        for _ in range(50):
            if os.path.exists(bus[len('unix:path='):]):
                break
            time.sleep(0.1)
        self.env['DBUS_SESSION_BUS_ADDRESS'] = bus
        launcher = next((x for x in ('/usr/libexec/at-spi-bus-launcher', '/usr/lib/at-spi2-core/at-spi-bus-launcher',
                                     '/usr/lib/at-spi-bus-launcher') if os.path.isfile(x)), None)
        if not launcher:
            raise RuntimeError('at-spi-bus-launcher not found (install at-spi2-core)')
        self.procs.append(subprocess.Popen([launcher, '--launch-immediately'], env=self.env,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        addr = None
        for _ in range(100):                     # the accessibility bus comes up asynchronously
            r = subprocess.run(['dbus-send', '--session', '--print-reply', '--dest=org.a11y.Bus', '/org/a11y/bus',
                                'org.a11y.Bus.GetAddress'], env=self.env, capture_output=True, text=True)
            m = re.search(r'string "([^"]+)"', r.stdout)
            if r.returncode == 0 and m:
                addr = m.group(1); break
            time.sleep(0.1)
        if not addr:
            raise RuntimeError('accessibility bus (AT-SPI) did not start')
        self.env['AT_SPI_BUS_ADDRESS'] = addr
        self.srv = _Srv(self.env, log)
        if self.srv.call('ping') != 'pong':
            raise RuntimeError('AT-SPI helper did not start')

    def _private_kicad_config(self):
        """A copy of the user's KiCad config for this batch only: the editors use the software (Cairo)
        canvas - Xvfb has no OpenGL and KiCad would stop on an 'insufficient GPU memory' box for big
        boards. The user's own settings are never written."""
        src = os.environ.get('KICAD_CONFIG_HOME') or os.path.join(
            os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'kicad')
        dst = os.path.join(self.tmp, 'kicad')
        if os.path.isdir(src):
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns('3d', '*.lck'))
        else:
            os.makedirs(dst)
        vers = [v for v in os.listdir(dst) if re.match(r'\d+\.\d+$', v)] or ['10.0']
        for v in vers:
            for app in ('eeschema', 'pcbnew', 'fpedit', 'symbol_editor', 'gerbview', 'pl_editor'):
                f = os.path.join(dst, v, app + '.json')
                try:
                    cfg = json.load(open(f, encoding='utf-8')) if os.path.isfile(f) else {}
                except ValueError:
                    cfg = {}
                cfg.setdefault('graphics', {})['canvas_type'] = 2        # EDA_DRAW_PANEL_GAL::GAL_TYPE_CAIRO
                os.makedirs(os.path.dirname(f), exist_ok=True)
                with open(f, 'w', encoding='utf-8') as fh:
                    json.dump(cfg, fh, indent=2)
        return dst

    def _start_x(self):
        for n in range(91, 140):
            if os.path.exists(f'/tmp/.X11-unix/X{n}') or os.path.exists(f'/tmp/.X{n}-lock'):
                continue
            p = subprocess.Popen(['Xvfb', f':{n}', '-screen', '0', '1600x1000x24', '-nolisten', 'tcp'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(50):
                if os.path.exists(f'/tmp/.X11-unix/X{n}'):
                    self.procs.append(p)
                    return f':{n}'
                if p.poll() is not None:
                    break
                time.sleep(0.1)
            p.kill()
        raise RuntimeError('could not start Xvfb')

    def shutdown(self):
        self.close_kicad()
        if getattr(self, 'srv', None):
            self.srv.stop()
        for p in reversed(self.procs):
            try:
                p.terminate(); p.wait(5)
            except Exception:
                p.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -------------------------------------------------------------- KiCad process
    def start_kicad(self, pro):
        self.p = subprocess.Popen([shutil.which('kicad'), pro], env=self.env, start_new_session=True,
                                  stdout=open(os.path.join(self.tmp, 'kicad.log'), 'ab'), stderr=subprocess.STDOUT)
        return self.p

    def close_kicad(self):
        if self.p and self.p.poll() is None:
            try:
                os.killpg(self.p.pid, signal.SIGTERM); self.p.wait(15)
            except Exception:
                try:
                    os.killpg(self.p.pid, signal.SIGKILL)
                except Exception:
                    pass
        self.p = None

    def xwindow(self, win):
        """The X window of an AT-SPI window: same screen box (no window manager, no decorations);
        the title is only a fallback (X and AT-SPI titles can differ, e.g. 'KiCad Error' / 'Error')."""
        try:
            ext = self.srv.call('extents', pid=self.p.pid, key=win['key'])
        except RuntimeError:
            ext = None
        r = subprocess.run(['xdotool', 'search', '--onlyvisible', '--pid', str(self.p.pid)],
                           env=self.env, capture_output=True, text=True)
        best = None
        for wid in r.stdout.split():
            g = subprocess.run(['xdotool', 'getwindowgeometry', '--shell', wid], env=self.env,
                               capture_output=True, text=True).stdout
            v = dict(l.split('=', 1) for l in g.split() if '=' in l)
            try:
                box = [int(v['X']), int(v['Y']), int(v['WIDTH']), int(v['HEIGHT'])]
            except (KeyError, ValueError):
                continue
            if box[2] < 20 or box[3] < 20:
                continue
            if ext:
                d = sum(abs(p_ - q) for p_, q in zip(box, ext))
                if best is None or d < best[0]:
                    best = (d, wid)
        if best is not None and best[0] < 40:
            return best[1]
        pat = ''.join(re.escape(c) if c.isascii() else '.' for c in win['name'])   # xdotool: ASCII-only regex
        r = subprocess.run(['xdotool', 'search', '--onlyvisible', '--name', pat], env=self.env,
                           capture_output=True, text=True)
        ids = r.stdout.split()
        if not ids:
            self.log(f'  [X] window not found: {win["name"]!r}')
        return ids[-1] if ids else None

    def front(self, win):
        """Raise + focus a window: no window manager does it here, and KiCad's editors open on top
        of the project manager."""
        wid = self.xwindow(win)
        for cmd in (['windowraise', wid], ['windowfocus', wid]) if wid else ():
            try:
                subprocess.run(['xdotool'] + cmd, env=self.env, timeout=5,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                pass
        time.sleep(0.3)

    def key(self, keys, win=None):
        """Keystrokes for window <win> (a snapshot). No window manager here: the window gets the X
        input focus explicitly (found by its title) and the pointer is moved into it."""
        if win is not None:
            self.front(win)
            try:
                ext = self.srv.call('extents', pid=self.p.pid, key=win['key'])
            except RuntimeError:
                ext = None
            if ext and ext[2] > 0 and ext[3] > 0:
                subprocess.run(['xdotool', 'mousemove', str(ext[0] + ext[2] // 2), str(ext[1] + min(40, ext[3] // 2))],
                               env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.2)
        subprocess.run(['xdotool', 'key', '--clearmodifiers', '--delay', '60'] + keys.split(), env=self.env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def type(self, text):
        subprocess.run(['xdotool', 'type', '--clearmodifiers', '--delay', '15', text], env=self.env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # -------------------------------------------------------------- windows (snapshots)
    def tops(self):
        if not self.p:
            return []
        try:
            return self.srv.call('snap', pid=self.p.pid)
        except RuntimeError as e:
            self.log(f'  [AT-SPI] {e}')
            return []

    def title(self, w):
        return w['name']

    def key_of(self, w):
        return tuple(w['key'])

    def manager(self):
        for w in self.tops():
            if w['role'] == 'frame' and re.search(r'KiCad \d+\.\d+$', w['name']) and \
                    self.srv.call('has_menu', pid=self.p.pid, key=w['key'], path=EAGLE_MENU):
                return w
        return None

    def dialogs(self):
        return [w for w in self.tops() if w['role'] in DIALOG_ROLES]

    def is_progress(self, d):
        return bool(d.get('progress'))

    def is_file_chooser(self, d):
        return d['role'] == 'file chooser'

    def text(self, d):
        return d.get('text') or '(text not readable)'

    def editors(self, name):
        return [w for w in self.tops() if w['role'] == 'frame' and name in w['name']
                and ('Schematic Editor' in w['name'] or 'PCB Editor' in w['name'])]

    def busy(self):
        return bool(self.dialogs())

    def _xmove(self, ext):
        subprocess.run(['xdotool', 'mousemove', str(ext[0] + ext[2] // 2), str(ext[1] + ext[3] // 2)], env=self.env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _xclick(self, ext):
        x, y = ext[0] + ext[2] // 2, ext[1] + ext[3] // 2
        subprocess.run(['xdotool', 'mousemove', str(x), str(y), 'click', '1'], env=self.env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def menu(self, win, path):
        """Open a menu path with real clicks (see atspi_srv.py: no AT-SPI actions)."""
        self.front(win)
        for i in range(1, len(path) + 1):
            ext = None
            for _ in range(20):
                ext = self.srv.call('menu_extents', pid=self.p.pid, key=win['key'], path=path[:i])
                if ext:
                    break
                time.sleep(0.25)
            if not ext:
                self.key('Escape Escape Escape', win=win)
                return False
            if 1 < i < len(path):
                self._xmove(ext)                 # a submenu opens on hover (a click closes it again)
                time.sleep(0.9)
            else:
                self._xclick(ext)
                time.sleep(0.6)
        return True

    def import_eagle(self, mgr):
        mgr = self.manager() or mgr              # its title changes with the project: use a fresh snapshot
        if not self.menu(mgr, EAGLE_MENU):
            raise RuntimeError('EAGLE import menu item not found')

    def open_pcb_editor(self, mgr):
        mgr = self.manager() or mgr
        if not self.menu(mgr, PCB_MENU):
            self.log('  [menu] Tools > PCB Editor not reachable')

    def click(self, d, names):
        self.front(d)
        ext = self.srv.call('button_extents', pid=self.p.pid, key=d['key'], names=list(names))
        if not ext:
            return False
        self._xclick(ext)
        return True

    def button_names(self, d):
        return d.get('buttons') or []

    def set_entry(self, d, text):
        return self.srv.call('entry', pid=self.p.pid, key=d['key'], text=text)

    def screenshot(self, path):
        try:
            from PIL import ImageGrab
            ImageGrab.grab(xdisplay=self.display).save(path)
            return path
        except Exception:
            return None


class DialogDriver(threading.Thread):
    """Linux counterpart of win_dialogs.DialogDriver: the n-th GTK file / folder chooser gets the
    n-th path; then the layer mapping (auto-match + OK) or KiCad's info boxes are confirmed."""

    def __init__(self, ui, steps, log=print, timeout=180, layer_mapping=False):
        super().__init__(daemon=True)
        self.ui, self.steps, self.log, self.timeout = ui, list(steps), log, timeout
        self.layer_mapping = layer_mapping
        self.seen = {ui.key_of(d) for d in ui.dialogs()}
        self.done, self.last, self.messages = [], None, []
        self.stop_flag = False

    def _current(self):
        ds = self.ui.dialogs()
        self.seen &= {self.ui.key_of(d) for d in ds}          # a closed window may come back: forget it
        return ds

    def _enter(self, d, path, tries=4):
        ui = self.ui
        k0 = ui.key_of(d)
        for k in range(tries):
            if ui.set_entry(d, None) is None:            # path field not shown yet: Ctrl+L opens it
                ui.key('ctrl+l', win=d)
                time.sleep(0.6 + 0.3 * k)
            got = ui.set_entry(d, path)
            if got is None:
                self.log(f'  [dialog "{k0[1]}"] no location entry (try {k + 1})')
                continue
            if got != path:                              # AT-SPI text edit not taken: type it
                ui.key('ctrl+a', win=d)
                ui.type(path)
                time.sleep(0.4)
                got = ui.set_entry(d, None)
            if got and got != path:                      # '' = GTK's entry does not expose its text: trust the typing
                self.log(f'  [dialog "{k0[1]}"] path field reads {got!r} (try {k + 1})')
                continue
            time.sleep(0.3)
            if not ui.click(d, OK_NAMES):
                ui.key('Return', win=d)
            t1 = time.time(); pressed = False
            while time.time() - t1 < 10:
                ds = {ui.key_of(x): x for x in ui.dialogs()}
                if k0 not in ds:
                    return True
                err = [x for kk, x in ds.items() if kk != k0 and kk not in self.seen and not ui.is_file_chooser(x)]
                if err:
                    self.log(f'  [dialog "{k0[1]}"] retry {k + 1}: {ui.text(err[0])[:80]}')
                    self._close_box(err[0])
                    break
                if time.time() - t1 > 3 and not pressed and not path.lower().endswith(('.sch', '.brd')):
                    # a folder chooser opened the folder instead of returning it: select it
                    ui.click(ds[k0], OK_NAMES); pressed = True
                time.sleep(0.3)
        return False

    def _close_box(self, box):
        ui = self.ui
        k = ui.key_of(box)
        if not ui.click(box, ('ok', 'close', 'yes')):
            ui.key('Return', win=box)
        t = time.time()
        while time.time() - t < 3 and k in {ui.key_of(x) for x in ui.dialogs()}:
            time.sleep(0.2)

    def run(self):
        try:
            self._run()
        except Exception as e:
            self.log(f'  [dialog driver] error: {e!r}')

    def _run(self):
        ui = self.ui
        for path in self.steps:
            t0 = time.time(); handled = False
            while not self.stop_flag and time.time() - t0 < self.timeout:
                for d in self._current():
                    kd = ui.key_of(d)
                    if kd in self.seen or not ui.is_file_chooser(d):
                        continue
                    time.sleep(0.6)                      # let the chooser finish building
                    self.seen.add(kd)
                    if not self._enter(d, path):
                        self.seen.discard(kd)
                        continue
                    self.log(f'  [dialog "{kd[1]}"] <- {path}')
                    self.done.append(path); self.last = path
                    handled = True
                    break
                if handled:
                    break
                time.sleep(0.3)
            if not handled:
                self.log(f'  [dialog] not handled (timeout): {path}')
                return
        if self.layer_mapping:
            self._layer_mapping()
        self._info_ok()

    def _layer_mapping(self, timeout=600):
        ui = self.ui
        t0 = time.time()
        while not self.stop_flag and time.time() - t0 < timeout:
            for d in self._current():
                kd = ui.key_of(d)
                if kd in self.seen or ui.is_file_chooser(d) or ui.is_progress(d):
                    continue
                names = ui.button_names(d)
                auto = [n for n in names if n.startswith('auto-match')]
                if auto and 'ok' in names:
                    self.seen.add(kd)
                    ui.click(d, auto)
                    time.sleep(0.5)
                    ui.click(d, ('ok',))
                    self.log('  [layer mapping] auto-match + OK')
                    return
                self.handle_message(d)
            time.sleep(0.3)

    def _info_ok(self, timeout=1800):
        ui = self.ui
        t0 = time.time()
        while not self.stop_flag and time.time() - t0 < timeout:
            for d in self._current():
                kd = ui.key_of(d)
                if kd in self.seen or ui.is_file_chooser(d) or ui.is_progress(d):
                    continue
                self.handle_message(d)
            time.sleep(0.3)

    def _close_verified(self, d, names):
        """Press the button and make sure the box is gone (else: front + Return, once more)."""
        ui = self.ui
        kd = ui.key_of(d)
        for attempt in range(3):
            if attempt == 0:
                ui.click(d, names)
            elif attempt == 1:
                ui.key('Return', win=d)
            else:
                ui.click(d, names)
            t = time.time()
            while time.time() - t < 2.5:
                if kd not in {ui.key_of(x) for x in ui.dialogs()}:
                    return True
                time.sleep(0.25)
        return False

    def handle_message(self, d):
        ui = self.ui
        kd = ui.key_of(d)
        self.seen.add(kd)
        names, msg = ui.button_names(d), ui.text(d)
        for rx, btn, why_ in ANSWERS:
            if rx.search(msg or '') and btn in names:
                if not self._close_verified(d, (btn,)):
                    self.seen.discard(kd)
                    return
                self.messages.append((kd[1], msg, f'answered {btn} ({why_})'))
                self.log(f'  [KiCad question, answered {btn}] "{kd[1]}" {msg[:120]}')
                return
        why = benign_reason(msg)
        if names == ['ok'] or why:
            if not self._close_verified(d, ('ok',)):
                self.seen.discard(kd)                # still open: try again on the next pass
                self.log(f'  [KiCad message] "{kd[1]}" did not close, retrying')
                return
            tag = f'known KiCad issue: {why}' if why else 'confirmed automatically'
            self.messages.append((kd[1], msg, 'confirmed' + (f' ({why})' if why else '')))
            self.log(f'  [KiCad message, {tag}] "{kd[1]}" {msg}')
        else:
            self.messages.append((kd[1], msg, 'left open'))
            self.log(f'  [KiCad message, NOT confirmed] "{kd[1]}" {msg}')
