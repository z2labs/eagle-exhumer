"""Review of the deletions the fixer proposes (eaglefix_proposals.json).

Nothing is deleted by the fix itself: every item it would remove comes here with a picture of the
spot (red = would be deleted, green = would be added). Only the ticked items are applied.
"""
import json, math, os
import wx

PROPOSALS_FILE = 'eaglefix_proposals.json'


def test_mode():
    return bool(os.environ.get('EXHUMER_TEST')) or \
        os.path.isfile(os.path.join(os.path.expanduser('~'), '.eagle_exhumer_test'))


def load(project_dir):
    try:
        with open(os.path.join(project_dir, PROPOSALS_FILE), encoding='utf-8') as f:
            return json.load(f).get('proposals', [])
    except Exception:
        return []


_PCB = {'F.Cu': (200, 52, 52), 'B.Cu': (77, 127, 196), 'Edge.Cuts': (232, 224, 96), 'ref': (235, 235, 235)}
_INNER = [(194, 194, 60), (60, 180, 110), (200, 120, 200), (90, 190, 200), (220, 140, 60), (140, 140, 230)]
_SCH = {'ctx': (0, 132, 0), 'body': (132, 0, 0), 'pin': (132, 0, 0), 'ref': (0, 100, 100)}
_DEL = (255, 30, 30)
_ADD = (20, 200, 60)


class PicPanel(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent, style=wx.BORDER_SIMPLE)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.pic = None
        self.after = False
        self.Bind(wx.EVT_PAINT, self._paint)
        self.Bind(wx.EVT_SIZE, lambda e: self.Refresh())

    def show(self, pic, after=False):
        self.pic, self.after = pic, after
        self.Refresh()

    def _col(self, c, kind):
        if c == 'del':
            return _DEL
        if c == 'add':
            return _ADD
        if kind == 'sch':
            return _SCH.get(c, (0, 132, 0))
        if c in _PCB:
            return _PCB[c]
        if c.startswith('In') and c.endswith('.Cu'):
            try:
                return _INNER[(int(c[2:-3]) - 1) % len(_INNER)]
            except ValueError:
                pass
        return (150, 150, 150)

    def _paint(self, _evt):
        dc = wx.AutoBufferedPaintDC(self)
        W, H = self.GetClientSize()
        self.draw(dc, W, H)

    def draw(self, dc, W, H):
        pic = self.pic
        sch = bool(pic) and pic.get('kind') == 'sch'
        dc.SetBackground(wx.Brush(wx.Colour(250, 250, 245) if sch else wx.Colour(16, 20, 24)))
        dc.Clear()
        if not pic:
            return
        gc = wx.GraphicsContext.Create(dc)
        x0, y0, x1, y1 = pic['box']
        sc = min(W / max(1e-6, x1 - x0), H / max(1e-6, y1 - y0))
        ox = (W - (x1 - x0) * sc) / 2 - x0 * sc
        oy = (H - (y1 - y0) * sc) / 2 - y0 * sc
        P = lambda x, y: (ox + x * sc, oy + y * sc)
        kind = pic.get('kind')
        layers = pic['ctx']
        if kind == 'pcb':                    # back side first, front last
            order = lambda g: 0 if g.get('c') == 'B.Cu' else 2 if g.get('c') == 'F.Cu' else 3 if g['t'] == 'text' else 1
            layers = sorted(layers, key=order)
        hl = [g for g in pic['hl'] if not (self.after and g.get('c') == 'del') and not (not self.after and g.get('c') == 'add')]
        for g, strong in [(g, False) for g in layers] + [(g, True) for g in hl]:
            r, gg, b = self._col(g.get('c', ''), kind)
            alpha = 255 if strong else (170 if kind == 'pcb' else 255)
            col = wx.Colour(r, gg, b, alpha)
            t = g['t']
            if t == 'line':
                w = max(1.0, g.get('w', 0.15) * sc) if kind == 'pcb' else max(1.0, (2.5 if strong else 1.2))
                if strong and kind == 'pcb':
                    w = max(w, 3.0)
                pen = wx.GraphicsPenInfo(col).Width(w).Cap(wx.CAP_ROUND)
                gc.SetPen(gc.CreatePen(pen))
                path = gc.CreatePath()
                pts = [P(*q) for q in g['pts']]
                if pts:
                    path.MoveToPoint(*pts[0])
                    for q in pts[1:]:
                        path.AddLineToPoint(*q)
                    gc.StrokePath(path)
            elif t == 'poly':
                pts = [P(*q) for q in g['pts']]
                if len(pts) < 3:
                    continue
                path = gc.CreatePath(); path.MoveToPoint(*pts[0])
                for q in pts[1:]:
                    path.AddLineToPoint(*q)
                path.CloseSubpath()
                if kind == 'sch' and not strong:
                    gc.SetBrush(wx.TRANSPARENT_BRUSH)
                else:
                    gc.SetBrush(gc.CreateBrush(wx.Brush(wx.Colour(r, gg, b, 110 if strong else 150))))
                gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(col).Width(2.0 if strong else 1.0)))
                gc.DrawPath(path)
            elif t == 'circle':
                cx, cy = P(g['x'], g['y']); rr = max(2.0, g['r'] * sc)
                gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(col).Width(2.5 if strong else 1.0)))
                gc.SetBrush(gc.CreateBrush(wx.Brush(wx.Colour(r, gg, b, 90 if strong else 170))) if g.get('fill')
                            else wx.TRANSPARENT_BRUSH)
                gc.DrawEllipse(cx - rr, cy - rr, 2 * rr, 2 * rr)
                if g.get('d'):
                    dr = g['d'] * sc
                    gc.SetBrush(gc.CreateBrush(wx.Brush(wx.Colour(250, 250, 245) if sch else wx.Colour(16, 20, 24))))
                    gc.DrawEllipse(cx - dr, cy - dr, 2 * dr, 2 * dr)
            elif t == 'text':
                x, y = P(g['x'], g['y'])
                size = 9 if strong else (7 if g.get('k') == 'pad' else 8)
                f = wx.Font(size, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL,
                            wx.FONTWEIGHT_BOLD if strong else wx.FONTWEIGHT_NORMAL)
                gc.SetFont(f, col)
                s = g.get('s', '')
                tw, th = gc.GetTextExtent(s)[:2]
                if g.get('k') == 'pad':
                    if tw > 0.9 * max(1, sc):
                        continue                  # pad labels only when they fit
                    gc.DrawText(s, x - tw / 2, y - th / 2)
                elif g.get('a') in (90, 90.0, 270, 270.0):
                    gc.DrawText(s, x - th, y, math.radians(90))
                else:
                    gc.DrawText(s, x + 2, y - th)
        # scale bar
        gc.SetFont(wx.Font(8, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL),
                   wx.Colour(120, 120, 120) if sch else wx.Colour(200, 200, 200))
        L = 10 ** math.floor(math.log10(max(1e-6, (x1 - x0) / 4)))
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(wx.Colour(150, 150, 150)).Width(2)))
        gc.StrokeLine(10, H - 12, 10 + L * sc, H - 12)
        gc.DrawText(f'{L:g} mm', 14 + L * sc, H - 20)


class ReviewDialog(wx.Dialog):
    """List (grouped by fix step, a tick per item) + picture of the selected item."""
    def __init__(self, parent, props):
        super().__init__(parent, title='Eagle Exhumer - approve deletions',
                         size=(1180, 760), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.props = props
        p = self
        head = wx.StaticText(p, label=(
            f'The fix would delete {len(props)} items. Nothing has been deleted yet.\n'
            'Tick what may go (a step header ticks its whole group). Red = deleted, green = added instead. '
            'The approved deletions are checked with DRC / ERC / netlist; if anything gets worse, nothing is changed.'))
        head.Wrap(1140)
        self.rows = []                       # ('h', step) or ('i', proposal)
        steps = []
        for pr in props:
            if pr['step'] not in steps:
                steps.append(pr['step'])
        for st in steps:
            items = [pr for pr in props if pr['step'] == st]
            self.rows.append(('h', st, len(items)))
            for pr in items:
                self.rows.append(('i', pr))
        self.lst = wx.CheckListBox(p, choices=[self._label(r) for r in self.rows], size=(520, -1))
        self.pic = PicPanel(p)
        self.title = wx.StaticText(p, label='')
        self.title.SetFont(wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.cb_after = wx.CheckBox(p, label='Show the result (after the deletion)')
        self.count = wx.StaticText(p, label='')
        b_all = wx.Button(p, label='Tick all')
        b_none = wx.Button(p, label='Untick all')
        self.b_ok = wx.Button(p, wx.ID_OK, label='Delete the ticked items')
        b_cancel = wx.Button(p, wx.ID_CANCEL, label='Keep everything')
        left = wx.BoxSizer(wx.VERTICAL)
        left.Add(self.lst, 1, wx.EXPAND)
        lb = wx.BoxSizer(wx.HORIZONTAL)
        lb.Add(b_all, 0, wx.RIGHT, 6); lb.Add(b_none, 0)
        left.Add(lb, 0, wx.TOP, 6)
        right = wx.BoxSizer(wx.VERTICAL)
        right.Add(self.title, 0, wx.EXPAND | wx.BOTTOM, 4)
        right.Add(self.pic, 1, wx.EXPAND)
        right.Add(self.cb_after, 0, wx.TOP, 6)
        mid = wx.BoxSizer(wx.HORIZONTAL)
        mid.Add(left, 0, wx.EXPAND | wx.RIGHT, 10)
        mid.Add(right, 1, wx.EXPAND)
        bot = wx.BoxSizer(wx.HORIZONTAL)
        bot.Add(self.count, 1, wx.ALIGN_CENTER_VERTICAL)
        bot.Add(b_cancel, 0, wx.RIGHT, 8)
        bot.Add(self.b_ok, 0)
        box = wx.BoxSizer(wx.VERTICAL)
        box.Add(head, 0, wx.EXPAND | wx.ALL, 10)
        box.Add(mid, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        box.Add(bot, 0, wx.EXPAND | wx.ALL, 10)
        self.SetSizer(box)
        self.lst.Bind(wx.EVT_LISTBOX, self._sel)
        self.lst.Bind(wx.EVT_CHECKLISTBOX, self._check)
        self.cb_after.Bind(wx.EVT_CHECKBOX, lambda e: self._show())
        b_all.Bind(wx.EVT_BUTTON, lambda e: self._set_all(True))
        b_none.Bind(wx.EVT_BUTTON, lambda e: self._set_all(False))
        first = next((i for i, r in enumerate(self.rows) if r[0] == 'i'), 0)
        self.lst.SetSelection(first)
        self._show()
        self._update()
        if wx.Display.GetCount() > 0:          # (a headless X server without RandR has none)
            self.CentreOnScreen()

    def _label(self, r):
        if r[0] == 'h':
            return f'[{r[1]}]  ({r[2]})'
        return '      ' + r[1]['title']

    def _set_all(self, on):
        for i in range(len(self.rows)):
            self.lst.Check(i, on)
        self._update()

    def _check(self, evt):
        i = evt.GetInt()
        r = self.rows[i]
        if r[0] == 'h':                      # a header ticks / unticks its group
            on = self.lst.IsChecked(i)
            j = i + 1
            while j < len(self.rows) and self.rows[j][0] == 'i':
                self.lst.Check(j, on); j += 1
        self.lst.SetSelection(i)
        self._show()
        self._update()

    def _sel(self, _evt):
        self._show()

    def _show(self):
        i = self.lst.GetSelection()
        if i == wx.NOT_FOUND:
            return
        r = self.rows[i]
        if r[0] == 'h':
            j = i + 1
            if j >= len(self.rows):
                return
            r = self.rows[j]
        pr = r[1]
        loc = ''
        pic = pr.get('pic') or {}
        if pic.get('kind') == 'sch':
            loc = f"  [sheet {pic.get('sheet', pr['file'])}]"
        elif pic.get('box'):
            b = pic['box']
            loc = f'  [x {(b[0] + b[2]) / 2:.2f}  y {(b[1] + b[3]) / 2:.2f} mm]'
        self.title.SetLabel(pr['title'] + loc)
        self.title.Wrap(max(200, self.pic.GetSize().x))
        self.Layout()
        self.pic.show(pic, self.cb_after.GetValue())

    def _update(self):
        n = len(self.selected())
        self.count.SetLabel(f'{n} of {len(self.props)} ticked')
        self.b_ok.SetLabel(f'Delete the ticked items ({n})')
        self.b_ok.Enable(n > 0)
        self.Layout()

    def selected(self):
        return [r[1]['id'] for i, r in enumerate(self.rows) if r[0] == 'i' and self.lst.IsChecked(i)]
