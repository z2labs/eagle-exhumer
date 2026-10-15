#!/usr/bin/env python3
"""Eagle Exhumer stand-alone launcher - converts an EAGLE design without opening KiCad by hand.

    <KiCad>\\bin\\pythonw.exe exhumer_launcher.py        (or "Eagle Exhumer.cmd" next to it)

Old-school front end in the style of a 2000s crack intro (plasma, copper bars, sine scroller,
chiptune). Under the hood it runs eagle2kicad_batch.py: KiCad's own importer in a private KiCad
instance, the repairs and the strict quality control. The result goes to <design>_kicad next to
the EAGLE file.

Music: an original chiptune is synthesised on the fly. Put your own WAV files into a "music"
folder next to this file to play those instead (they are not part of the package).
"""
import glob, math, os, random, struct, subprocess, sys, tempfile, threading, time, wave

import wx
try:
    import wx.adv as wxadv
except Exception:                                   # pragma: no cover
    wxadv = None
try:
    import numpy as np
except Exception:                                   # plasma falls back to bars only
    np = None

HERE = os.path.dirname(os.path.abspath(__file__))
W, H = 720, 460
SR = 22050

SCROLL = ('   ***  EAGLE EXHUMER  ***   old EAGLE designs, gently brought back to life in KiCad 10   ...   '
          'KiCad\'s own importer + repairs + a strict quality control: PASS or FAIL, nothing in between   ...   '
          'pick a .sch or .brd, press EXHUME and lean back   ...   greetings to everyone who still has '
          'a drawer full of .brd files   ...   Z2Labs  -  hello [at] z2labs [dot] io   ...   ')


# ---------------------------------------------------------------- music (original, synthesised)

def _note(n):
    return 440.0 * 2 ** ((n - 69) / 12.0)


def synth_tune():
    """An original 4-voice chiptune (square lead with arpeggios, triangle bass, noise drums), 16 bars."""
    bpm = 132; step = 60.0 / bpm / 4                       # 16th notes
    prog = [57, 53, 48, 55]                                # Am F C G (root notes)
    chords = {57: (0, 3, 7), 53: (0, 4, 7), 48: (0, 4, 7), 55: (0, 4, 7)}
    lead = [12, None, 15, 12, 19, None, 17, 15, 12, None, 10, 12, 15, None, 14, 12]
    steps = 16 * 16
    n = int(steps * step * SR)
    t = np.arange(n) / SR if np is not None else None
    out = np.zeros(n) if np is not None else None
    env_track = []
    for s in range(steps):
        bar = s // 16; pos = s % 16
        root = prog[bar % 4]
        a, b = int(s * step * SR), int((s + 1) * step * SR)
        seg = np.arange(b - a) / SR
        lvl = 0.0
        # arpeggio (fast 50 Hz chord cycling, the C64 way)
        ch = chords[root]
        arp_n = root + 12 + np.array(ch)[(np.floor(seg * 50).astype(int)) % 3]
        f = 440.0 * 2 ** ((arp_n - 69) / 12.0)
        ph = np.cumsum(f / SR)
        out[a:b] += 0.10 * np.sign(np.sin(2 * np.pi * ph)) * (0.6 + 0.4 * (pos % 4 == 0))
        # lead (second half of each 4-bar phrase)
        ln = lead[pos] if (bar % 4) in (1, 3) or bar >= 8 else None
        if ln is not None:
            fl = _note(root + ln + 12 if bar >= 8 else root + ln)
            e = np.exp(-seg * 6)
            vib = 1 + 0.004 * np.sin(2 * np.pi * 6 * seg)
            out[a:b] += 0.16 * e * np.sign(np.sin(2 * np.pi * fl * vib * seg) - 0.3)
            lvl += 0.6
        # bass: root on the beat, octave on the off-beat
        fb = _note(root - 12 + (12 if pos % 4 == 2 else 0))
        tri = 2 * np.abs(2 * ((fb * seg) % 1) - 1) - 1
        out[a:b] += 0.22 * tri * np.exp(-seg * 3)
        # drums: kick on 0/8, snare on 4/12, hats on odd steps
        if pos in (0, 8) or (bar >= 4 and pos in (0, 6, 8)):
            out[a:b] += 0.35 * np.sin(2 * np.pi * (120 * np.exp(-seg * 30) + 40) * seg) * np.exp(-seg * 18); lvl += 1
        if pos in (4, 12):
            out[a:b] += 0.18 * np.random.uniform(-1, 1, b - a) * np.exp(-seg * 22); lvl += 0.8
        if pos % 2 == 1 and bar >= 2:
            out[a:b] += 0.05 * np.random.uniform(-1, 1, b - a) * np.exp(-seg * 60)
        env_track.append(min(1.0, 0.3 + lvl * 0.4))
    out = np.clip(out, -1, 1)
    return (out * 30000).astype('<i2').tobytes(), env_track, step


def write_wav(path, pcm):
    with wave.open(path, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm)


def wav_envelope(path, step=0.05):
    """loudness per `step` seconds of a user WAV (for the VU bars)"""
    try:
        with wave.open(path, 'rb') as w:
            ch, sw, sr, nf = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(nf)
        if sw != 2 or np is None:
            return [0.6], step
        a = np.frombuffer(raw, '<i2').astype(float)
        if ch > 1:
            a = a.reshape(-1, ch).mean(axis=1)
        k = max(1, int(sr * step)); m = len(a) // k
        rms = np.sqrt((a[:m * k].reshape(m, k) ** 2).mean(axis=1)) / 12000.0
        return [float(min(1.0, x)) for x in rms], step
    except Exception:
        return [0.6], step


# ---------------------------------------------------------------- KiCad / batch

def kicad_python():
    cands = [sys.executable]
    if os.name == 'nt':
        cands += sorted(glob.glob(os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'KiCad', '*', 'bin', 'python.exe')), reverse=True)
        cands += sorted(glob.glob(r'C:\Program Files\KiCad\*\bin\python.exe'), reverse=True)
    for c in cands:
        c2 = c.replace('pythonw.exe', 'python.exe')
        if os.path.isfile(c2):
            return c2
    return sys.executable


# ---------------------------------------------------------------- the window

class Intro(wx.Frame):
    PAL = None

    def __init__(self):
        super().__init__(None, title='EAGLE EXHUMER', style=wx.DEFAULT_FRAME_STYLE & ~(wx.RESIZE_BORDER | wx.MAXIMIZE_BOX))
        self.SetClientSize((W, H)); self.SetBackgroundColour(wx.BLACK)
        self.t0 = time.time(); self.src = None; self.libfix = True; self.muted = False
        self.lines = ['> ready. pick an EAGLE .sch or .brd']
        self.state = 'idle'; self.verdict = None; self.proc = None
        self.buttons = []               # (rect, label, action)
        self.hover = None
        self.font_big = wx.Font(30, wx.FONTFAMILY_MODERN, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        self.font_btn = wx.Font(11, wx.FONTFAMILY_MODERN, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        self.font_log = wx.Font(9, wx.FONTFAMILY_MODERN, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        self.font_scr = wx.Font(16, wx.FONTFAMILY_MODERN, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_LEFT_DOWN, self.on_click)
        self.Bind(wx.EVT_MOTION, self.on_move)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.timer = wx.Timer(self); self.Bind(wx.EVT_TIMER, lambda e: self.Refresh(False), self.timer)
        self.timer.Start(33)
        self.env, self.env_step, self.sound = [0.5], 0.1, None
        threading.Thread(target=self.start_music, daemon=True).start()
        if np is not None:
            ys, xs = np.mgrid[0:H // 4, 0:W // 4]
            self.px, self.py = xs.astype(float), ys.astype(float)

    # -- music
    def start_music(self):
        try:
            mine = sorted(glob.glob(os.path.join(HERE, 'music', '*.wav')))
            if mine:
                path = random.choice(mine)
                self.env, self.env_step = wav_envelope(path)
            elif np is not None:
                path = os.path.join(tempfile.gettempdir(), 'eagle_exhumer_intro.wav')
                pcm, self.env, self.env_step = synth_tune()
                write_wav(path, pcm)
            else:
                return
            if wxadv is not None:
                self.sound = wxadv.Sound(path)
                wx.CallAfter(self.play)
        except Exception as ex:
            self.log(f'(no music: {ex})')

    def play(self):
        if self.sound and self.sound.IsOk() and not self.muted:
            self.sound.Play(wxadv.SOUND_ASYNC | wxadv.SOUND_LOOP); self.t_music = time.time()

    def level(self):
        if self.muted or not self.env:
            return 0.15
        i = int((time.time() - getattr(self, 't_music', self.t0)) / self.env_step) % len(self.env)
        return self.env[i]

    # -- drawing
    def plasma(self, t):
        x, y = self.px, self.py
        v = np.sin(x / 9.0 + t) + np.sin(y / 7.0 - t * 1.3) + np.sin((x + y) / 11.0 + t * 0.7) \
            + np.sin(np.sqrt((x - 90 + 30 * np.sin(t)) ** 2 + (y - 55) ** 2) / 6.0)
        v = (v + 4) / 8.0
        r = (np.sin(v * math.pi * 2) * 0.5 + 0.5) * 70
        g = (np.sin(v * math.pi * 2 + 2.1) * 0.5 + 0.5) * 30
        b = (np.sin(v * math.pi * 2 + 4.2) * 0.5 + 0.5) * 110 + 20
        img = np.dstack([r, g, b]).astype(np.uint8)
        im = wx.Image(img.shape[1], img.shape[0]); im.SetData(img.tobytes())
        return im.Scale(W, H, wx.IMAGE_QUALITY_NORMAL).ConvertToBitmap()

    def on_paint(self, _e):
        self.render(wx.AutoBufferedPaintDC(self), time.time() - self.t0)

    def snapshot(self, path, t=2.3):
        bmp = wx.Bitmap(W, H); dc = wx.MemoryDC(bmp)
        self.render(dc, t); dc.SelectObject(wx.NullBitmap); bmp.SaveFile(path, wx.BITMAP_TYPE_PNG)

    def render(self, dc, t):
        if np is not None:
            dc.DrawBitmap(self.plasma(t), 0, 0)
        else:
            dc.SetBackground(wx.BLACK_BRUSH); dc.Clear()
        lvl = self.level()
        # copper bars
        for k in range(5):
            yc = int(70 + 34 * math.sin(t * 2.2 + k * 0.6))
            for j in range(10):
                c = int(255 * (1 - j / 10.0) * (0.55 + 0.45 * lvl))
                dc.SetPen(wx.Pen(wx.Colour(c, int(c * 0.55), int(c * 0.15)) if k % 2 == 0 else wx.Colour(int(c * 0.2), int(c * 0.8), c)))
                dc.DrawLine(0, yc - j, W, yc - j); dc.DrawLine(0, yc + j, W, yc + j)
        # logo with a shadow and a colour cycle
        dc.SetFont(self.font_big)
        title = 'EAGLE EXHUMER'
        tw, th = dc.GetTextExtent(title); x0 = (W - tw) // 2
        for i, chx in enumerate(title):
            cw = dc.GetTextExtent(title[:i])[0]
            yy = 50 + int(6 * math.sin(t * 4 + i * 0.5))
            dc.SetTextForeground(wx.Colour(0, 0, 0)); dc.DrawText(chx, x0 + cw + 3, yy + 3)
            hue = (t * 80 + i * 18) % 360
            dc.SetTextForeground(wx.Colour(*[int(127 + 127 * math.sin(math.radians(hue + o))) for o in (0, 120, 240)]))
            dc.DrawText(chx, x0 + cw, yy)
        # VU / spectrum bars (Winamp style)
        nb = 24
        for i in range(nb):
            hgt = int(60 * max(0.05, lvl * (0.55 + 0.45 * math.sin(t * 9 + i * 1.7)) * (1 - abs(i - nb / 2) / nb)))
            for s in range(0, hgt, 4):
                g = 255 - int(255 * s / 60)
                dc.SetBrush(wx.Brush(wx.Colour(255 - g, g, 40))); dc.SetPen(wx.TRANSPARENT_PEN)
                dc.DrawRectangle(W - 20 - (nb - i) * 9, 300 - s, 7, 3)
        # buttons
        self.buttons = []
        def button(x, y, w, label, act, on=None):
            r = wx.Rect(x, y, w, 30); self.buttons.append((r, act))
            hot = self.hover == act
            bg = wx.Colour(255, 220, 0) if hot else (wx.Colour(0, 200, 120) if on else wx.Colour(20, 20, 30))
            fg = wx.BLACK if (hot or on) else wx.Colour(255, 220, 0)
            dc.SetBrush(wx.Brush(bg)); dc.SetPen(wx.Pen(wx.Colour(255, 220, 0), 2)); dc.DrawRectangle(r)
            dc.SetFont(self.font_btn); dc.SetTextForeground(fg)
            lw, lh = dc.GetTextExtent(label); dc.DrawText(label, x + (w - lw) // 2, y + (30 - lh) // 2)
        busy = self.state == 'busy'
        button(20, 130, 200, '[ SELECT EAGLE FILE ]', 'pick')
        button(20, 168, 200, '[ LIB CLEAN-UP: ' + ('ON ]' if self.libfix else 'OFF ]'), 'lib', on=self.libfix)
        button(20, 206, 200, '[ >>> EXHUME! <<< ]' if not busy else '[  WORKING ...  ]', 'go', on=busy)
        button(20, 244, 95, 'MUSIC ' + ('OFF' if self.muted else 'ON'), 'mute')
        button(125, 244, 95, 'EXIT', 'exit')
        # file + log box
        dc.SetBrush(wx.Brush(wx.Colour(0, 0, 0))); dc.SetPen(wx.Pen(wx.Colour(0, 255, 120), 1))
        dc.DrawRectangle(236, 130, W - 256 - 230, 144)
        dc.SetFont(self.font_log); dc.SetTextForeground(wx.Colour(0, 255, 120))
        lines = [('file: ' + os.path.basename(self.src)) if self.src else 'file: -'] + self.lines[-10:]
        for i, ln in enumerate(lines):
            dc.DrawText(ln[:32], 242, 134 + i * 13)
        # verdict
        if self.verdict:
            dc.SetFont(self.font_big)
            ok = self.verdict == 'QC PASS'
            if int(t * 3) % 3 or not ok:
                dc.SetTextForeground(wx.Colour(0, 255, 120) if ok else wx.Colour(255, 60, 60))
                vw, _ = dc.GetTextExtent(self.verdict); dc.DrawText(self.verdict, (W - vw) // 2, 310)
        # sine scroller
        dc.SetFont(self.font_scr)
        cw = dc.GetTextExtent('M')[0]
        off = int(t * 120) % (len(SCROLL) * cw)
        for i, chx in enumerate(SCROLL):
            x = i * cw - off
            if x < -cw:
                x += len(SCROLL) * cw
            if -cw <= x <= W:
                y = H - 60 + int(14 * math.sin(t * 3 + x / 45.0))
                dc.SetTextForeground(wx.Colour(255, 255, int(120 + 120 * math.sin(x / 30.0 + t))))
                dc.DrawText(chx, x, y)

    # -- input
    def hit(self, pos):
        for r, act in self.buttons:
            if r.Contains(pos):
                return act
        return None

    def on_move(self, e):
        self.hover = self.hit(e.GetPosition())

    def on_click(self, e):
        act = self.hit(e.GetPosition())
        if act == 'pick':
            dlg = wx.FileDialog(self, 'EAGLE schematic or board', wildcard='EAGLE (*.sch;*.brd)|*.sch;*.brd;*.SCH;*.BRD',
                                style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
            if dlg.ShowModal() == wx.ID_OK:
                self.src = dlg.GetPath(); self.verdict = None
                self.log('> loaded. press EXHUME!')
            dlg.Destroy()
        elif act == 'lib':
            self.libfix = not self.libfix
        elif act == 'mute':
            self.muted = not self.muted
            if self.sound:
                if self.muted: self.sound.Stop()
                else: self.play()
        elif act == 'exit':
            self.Close()
        elif act == 'go' and self.state != 'busy':
            if not self.src:
                self.log('> pick a file first!'); return
            self.run()

    def log(self, s):
        self.lines.append(s)

    # -- the actual work
    def run(self):
        self.state = 'busy'; self.verdict = None
        out = os.path.splitext(self.src)[0] + '_kicad'
        self.log(f'> exhuming -> {os.path.basename(out)}')
        self.log('> KiCad opens and closes by itself - hands off')
        cmd = [kicad_python(), '-u', os.path.join(HERE, 'eagle2kicad_batch.py'), self.src, '--force']
        if self.libfix:
            cmd.append('--libfix')
        flags = 0x08000000 if os.name == 'nt' else 0
        def work():
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                             encoding='utf-8', errors='replace', creationflags=flags,
                                             cwd=os.path.dirname(self.src))
                verdict = None
                for line in self.proc.stdout:
                    line = line.rstrip()
                    if line.startswith('| ') and ('QC PASS' in line or 'FAIL' in line):
                        verdict = 'QC PASS' if 'QC PASS' in line else 'QC FAIL'
                    if line.strip() and not line.startswith(('|', '#')):
                        wx.CallAfter(self.log, '  ' + line.strip()[:60])
                rc = self.proc.wait()
                wx.CallAfter(self.done, verdict or ('QC PASS' if rc == 0 else 'FAILED'), out)
            except Exception as ex:
                wx.CallAfter(self.done, 'FAILED', out, str(ex))
        threading.Thread(target=work, daemon=True).start()

    def done(self, verdict, out, err=None):
        self.state = 'idle'; self.verdict = verdict
        if err:
            self.log(f'> error: {err}')
        self.log(f'> {verdict}. report: {os.path.basename(out)}/eaglefix_report.md')
        if os.path.isdir(out) and os.name == 'nt':
            os.startfile(out)

    def on_close(self, e):
        try:
            if self.sound: self.sound.Stop()
            if self.proc and self.proc.poll() is None: self.proc.terminate()
        except Exception:
            pass
        self.timer.Stop(); e.Skip()


def main():
    app = wx.App(False)
    f = Intro()
    if '--snapshot' in sys.argv:                    # render one frame to a PNG (docs / tests) and quit
        out = sys.argv[sys.argv.index('--snapshot') + 1]
        f.muted = True; f.src = 'KrakenSDR_3v3.sch'; f.verdict = 'QC PASS'
        f.lines += ['> exhuming -> KrakenSDR_3v3_kicad', '  kicad-cli pcb import: ok', '  QC: netlists, pads, Gerbers match']
        def shot():
            f.snapshot(out); f.Destroy()
        wx.CallLater(800, shot)
    else:
        f.Show()
    app.MainLoop()


if __name__ == '__main__':
    main()
