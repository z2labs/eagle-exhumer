"""Eagle Exhumer - manufacturing-file check: compare the Gerber / Excellon files the EAGLE CAM made
with the ones KiCad makes from the converted project.

    python gerber_compare.py <eagle_gerber_dir> <kicad_gerber_dir> [--brd board.brd] [--json out.json]

Per layer: the area of the geometric XOR (mm2) after aligning the two sets on their drill holes,
the significant differences (XOR opened by --tol, so arc-approximation slivers do not count) with
their positions, and for copper whether a difference lies inside an EAGLE polygon pour (pours are
re-computed by every CAD tool) or not (tracks / pads: must be identical). Drill: holes matched by
position and diameter.

Geometry backend: shapely when available (development), else KiCad's own SHAPE_POLY_SET (inside
KiCad's Python, no extra packages).
"""
import argparse, glob, json, math, os, re, sys
from collections import Counter, defaultdict

INCH = 25.4

# --------------------------------------------------------------------------- geometry backends

class ShapelyGeo:
    name = 'shapely'

    def __init__(self):
        import shapely
        from shapely.geometry import Polygon
        from shapely import affinity, ops
        self.sh, self.Polygon, self.affinity, self.ops = shapely, Polygon, affinity, ops

    def poly(self, pts):
        p = self.Polygon(pts)
        return p if p.is_valid else p.buffer(0)

    def union(self, geoms):
        geoms = [g for g in geoms if g is not None and not g.is_empty]
        if not geoms:
            return self.Polygon()
        return self.sh.union_all(geoms)

    def diff(self, a, b):
        return a.difference(b)

    def xor(self, a, b):
        return a.symmetric_difference(b)

    def inter(self, a, b):
        return a.intersection(b)

    def area(self, g):
        return g.area

    def opening(self, g, r):
        if g.is_empty or r <= 0:
            return g
        return g.buffer(-r, join_style='mitre').buffer(r, join_style='mitre')

    def translate(self, g, dx, dy):
        return self.affinity.translate(g, dx, dy)

    def parts(self, g):
        if g.is_empty:
            return []
        gs = getattr(g, 'geoms', [g])
        out = []
        for p in gs:
            if p.area <= 0:
                continue
            x0, y0, x1, y1 = p.bounds
            c = p.centroid
            out.append((p.area, c.x, c.y, x1 - x0, y1 - y0))
        return out

    def empty(self):
        return self.Polygon()


class KicadGeo:
    """SHAPE_POLY_SET from KiCad's pcbnew module, internal units nm."""
    name = 'kicad'
    S = 1e6

    def __init__(self):
        import pcbnew
        self.p = pcbnew

    def poly(self, pts):
        ps = self.p.SHAPE_POLY_SET()
        ps.NewOutline()
        for x, y in pts:
            ps.Append(int(round(x * self.S)), int(round(y * self.S)))
        ps.Simplify()
        return ps

    def empty(self):
        return self.p.SHAPE_POLY_SET()

    def _copy(self, a):
        c = self.p.SHAPE_POLY_SET(); c.Append(a); return c

    def union(self, geoms):
        out = self.p.SHAPE_POLY_SET()
        for g in geoms:
            if g is not None:
                out.Append(g)
        out.Simplify()
        return out

    def diff(self, a, b):
        c = self._copy(a); c.BooleanSubtract(b); return c

    def inter(self, a, b):
        c = self._copy(a); c.BooleanIntersection(b); return c

    def xor(self, a, b):
        return self.union([self.diff(a, b), self.diff(b, a)])

    def area(self, g):
        return g.Area() / self.S ** 2

    def opening(self, g, r):
        c = self._copy(g)
        c.Deflate(int(r * self.S), self.p.CORNER_STRATEGY_CHAMFER_ALL_CORNERS, 1000)
        c.Inflate(int(r * self.S), self.p.CORNER_STRATEGY_CHAMFER_ALL_CORNERS, 1000)
        return c

    def translate(self, g, dx, dy):
        c = self._copy(g); c.Move(self.p.VECTOR2I(int(dx * self.S), int(dy * self.S))); return c

    def parts(self, g):
        out = []
        for i in range(g.OutlineCount()):
            one = self.p.SHAPE_POLY_SET(); one.AddOutline(g.Outline(i))
            for h in range(g.HoleCount(i)):
                one.AddHole(g.Hole(i, h))
            a = one.Area() / self.S ** 2
            if a <= 0:
                continue
            bb = one.BBox()
            c = bb.Centre()
            out.append((a, c.x / self.S, c.y / self.S, bb.GetWidth() / self.S, bb.GetHeight() / self.S))
        return out


def backend():
    try:
        return ShapelyGeo()
    except ImportError:
        return KicadGeo()


# --------------------------------------------------------------------------- shapes

def circle_pts(cx, cy, r, n=None):
    n = n or max(12, min(72, int(2 * math.pi * r / 0.02) + 8))
    return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]


def capsule_pts(x1, y1, x2, y2, r):
    a = math.atan2(y2 - y1, x2 - x1)
    n = max(6, min(36, int(math.pi * r / 0.02) + 4))
    pts = []
    for i in range(n + 1):                       # half circle around the end point
        t = a - math.pi / 2 + math.pi * i / n
        pts.append((x2 + r * math.cos(t), y2 + r * math.sin(t)))
    for i in range(n + 1):                       # and around the start point
        t = a + math.pi / 2 + math.pi * i / n
        pts.append((x1 + r * math.cos(t), y1 + r * math.sin(t)))
    return pts


def rect_pts(cx, cy, w, h, rot=0.0):
    c, s = math.cos(math.radians(rot)), math.sin(math.radians(rot))
    out = []
    for dx, dy in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
        out.append((cx + dx * c - dy * s, cy + dx * s + dy * c))
    return out


def hull(pts):
    pts = sorted(set(pts))
    if len(pts) < 3:
        return pts
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo, up = [], []
    for p in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p) <= 0:
            lo.pop()
        lo.append(p)
    for p in reversed(pts):
        while len(up) >= 2 and cross(up[-2], up[-1], p) <= 0:
            up.pop()
        up.append(p)
    return lo[:-1] + up[:-1]


def arc_pts(x0, y0, x1, y1, cx, cy, cw, full_if_same=True):
    r = math.hypot(x0 - cx, y0 - cy)
    a0 = math.atan2(y0 - cy, x0 - cx); a1 = math.atan2(y1 - cy, x1 - cx)
    if cw:
        while a1 >= a0:
            a1 -= 2 * math.pi
        if abs(a1 - a0) < 1e-9 and full_if_same:
            a1 = a0 - 2 * math.pi
    else:
        while a1 <= a0:
            a1 += 2 * math.pi
        if abs(a1 - a0) < 1e-9 and full_if_same:
            a1 = a0 + 2 * math.pi
    if abs(math.hypot(x0 - x1, y0 - y1)) < 1e-9 and not full_if_same:
        return [(x1, y1)]
    n = max(2, int(abs(a1 - a0) * r / 0.02) + 1)
    n = min(n, 720)
    return [(cx + r * math.cos(a0 + (a1 - a0) * i / n), cy + r * math.sin(a0 + (a1 - a0) * i / n)) for i in range(1, n + 1)]


# --------------------------------------------------------------------------- aperture macros

def _eval(expr, var):
    e = expr.strip().replace('x', '*').replace('X', '*')
    e = re.sub(r'\$(\d+)', lambda m: repr(var.get(int(m.group(1)), 0.0)), e)
    if not re.fullmatch(r'[0-9eE.+\-*/() ]*', e):
        raise ValueError('bad macro expression ' + expr)
    return float(eval(e, {'__builtins__': {}}, {})) if e else 0.0


def _rot(pts, deg):
    if not deg:
        return pts
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return [(x * c - y * s, x * s + y * c) for x, y in pts]


def macro_shapes(body, params, scale):
    """[(dark, pts)] in file units * scale (mm) around (0,0)."""
    var = {i + 1: v for i, v in enumerate(params)}
    out = []
    for stmt in body:
        stmt = stmt.strip()
        if not stmt or stmt.startswith('0'):
            continue
        m = re.match(r'\$(\d+)\s*=(.*)', stmt)
        if m:
            var[int(m.group(1))] = _eval(m.group(2), var); continue
        f = [_eval(x, var) for x in stmt.split(',')]
        code = int(f[0])
        if code == 1:                                   # circle: exp, dia, x, y [, rot]
            exp, d, x, y = f[1], f[2], f[3], f[4]
            rot = f[5] if len(f) > 5 else 0
            (x, y), = _rot([(x, y)], rot)
            out.append((exp != 0, circle_pts(x * scale, y * scale, d * scale / 2)))
        elif code == 20:                                # vector line: exp, w, x1, y1, x2, y2, rot
            exp, w, x1, y1, x2, y2, rot = f[1:8]
            a = math.atan2(y2 - y1, x2 - x1); nx, ny = -math.sin(a) * w / 2, math.cos(a) * w / 2
            pts = [(x1 + nx, y1 + ny), (x2 + nx, y2 + ny), (x2 - nx, y2 - ny), (x1 - nx, y1 - ny)]
            out.append((exp != 0, [(x * scale, y * scale) for x, y in _rot(pts, rot)]))
        elif code == 21:                                # center line: exp, w, h, x, y, rot
            exp, w, h, x, y, rot = f[1:7]
            out.append((exp != 0, [(px * scale, py * scale) for px, py in _rot(rect_pts(x, y, w, h), rot)]))
        elif code == 4:                                 # outline: exp, n, x0, y0, ..., rot
            exp, n = f[1], int(f[2])
            pts = [(f[3 + 2 * i], f[4 + 2 * i]) for i in range(n + 1)]
            rot = f[3 + 2 * (n + 1)] if len(f) > 3 + 2 * (n + 1) else 0
            out.append((exp != 0, [(x * scale, y * scale) for x, y in _rot(pts, rot)]))
        elif code == 5:                                 # polygon: exp, n, x, y, dia, rot
            exp, n, x, y, d, rot = f[1], int(f[2]), f[3], f[4], f[5], f[6] if len(f) > 6 else 0
            pts = [(x + d / 2 * math.cos(2 * math.pi * i / n), y + d / 2 * math.sin(2 * math.pi * i / n)) for i in range(n)]
            out.append((exp != 0, [(px * scale, py * scale) for px, py in _rot(pts, rot)]))
        elif code == 7:                                 # thermal: x, y, outer, inner, gap, rot  (approx: ring)
            x, y, do, di = f[1], f[2], f[3], f[4]
            out.append((True, circle_pts(x * scale, y * scale, do * scale / 2)))
            out.append((False, circle_pts(x * scale, y * scale, di * scale / 2)))
        else:
            raise ValueError(f'macro primitive {code} not supported')
    return out


# --------------------------------------------------------------------------- Gerber

class Gerber:
    def __init__(self, path, geo, stroke=None):
        self.path, self.geo = path, geo
        self.stroke = stroke        # outline: every stroke drawn with this width (tools differ in line width)
        self.unit = INCH            # mm per file unit
        self.fmt = (2, 4)
        self.zero_omit = 'L'
        self.apertures = {}
        self.macros = {}
        self.function = ''          # X2 .FileFunction
        self.warnings = []
        self.geom = None
        self._parse(open(path, encoding='latin-1').read())

    # -- aperture -> [(dark, pts)] around (0,0), mm
    def _aperture(self, code):
        ap = self.apertures.get(code)
        if ap is None:
            raise ValueError(f'{os.path.basename(self.path)}: aperture D{code} undefined')
        return ap

    def _define(self, code, spec):
        u = self.unit
        name, _, args = spec.partition(',')
        p = [float(v) for v in args.split('X')] if args else []
        if name == 'C':
            shapes = [(True, circle_pts(0, 0, p[0] * u / 2))]
            hole = p[1] if len(p) > 1 else 0
            self.apertures[code] = {'kind': 'C', 'd': p[0] * u, 'shapes': shapes, 'hole': hole}
            return
        if name == 'R':
            shapes = [(True, rect_pts(0, 0, p[0] * u, p[1] * u))]
        elif name == 'O':
            w, h = p[0] * u, p[1] * u
            if w >= h:
                shapes = [(True, capsule_pts(-(w - h) / 2, 0, (w - h) / 2, 0, h / 2))]
            else:
                shapes = [(True, capsule_pts(0, -(h - w) / 2, 0, (h - w) / 2, w / 2))]
        elif name == 'P':
            d, n = p[0] * u, int(p[1]); rot = p[2] if len(p) > 2 else 0
            shapes = [(True, _rot([(d / 2 * math.cos(2 * math.pi * i / n), d / 2 * math.sin(2 * math.pi * i / n)) for i in range(n)], rot))]
        elif name in self.macros:
            shapes = macro_shapes(self.macros[name], p, u)
        else:
            raise ValueError(f'{os.path.basename(self.path)}: aperture type {name} not supported')
        self.apertures[code] = {'kind': name, 'shapes': shapes}

    def _num(self, s, axis_int):
        if '.' in s:
            return float(s) * self.unit
        neg = s.startswith('-'); s = s.lstrip('+-')
        i, d = self.fmt
        if self.zero_omit == 'T':
            s = s.ljust(i + d, '0')
        v = int(s) / 10 ** d
        return (-v if neg else v) * self.unit

    def _flash(self, ap, x, y, acc):
        for dark, pts in ap['shapes']:
            acc.append((dark, [(x + px, y + py) for px, py in pts]))

    def _parse(self, text):
        geo = self.geo
        prims = []                      # (dark, pts) in drawing order, polarity already applied below
        polarity = True
        x = y = 0.0
        interp = 'G01'; quadrant = 'G75'
        cur = None
        region = None                   # list of contours while G36
        contour = []
        layers = []                     # [(dark, [pts...])] grouped by polarity runs
        # tokenise: extended blocks %...% and plain words ...*
        i, n = 0, len(text)
        words = []
        while i < n:
            c = text[i]
            if c == '%':
                j = text.index('%', i + 1)
                words.append(('X', text[i + 1:j])); i = j + 1
            elif c in '\r\n \t':
                i += 1
            else:
                j = text.find('*', i)
                if j < 0:
                    break
                words.append(('W', text[i:j])); i = j + 1
        for kind, w in words:
            if kind == 'X':
                blocks = [b.strip() for b in w.split('*') if b.strip()]
                if not blocks:
                    continue
                b0 = blocks[0]
                if b0.startswith('FS'):
                    m = re.match(r'FS([LT])?[AI]?X(\d)(\d)Y(\d)(\d)', b0)
                    if m:
                        self.zero_omit = m.group(1) or 'L'
                        self.fmt = (int(m.group(2)), int(m.group(3)))
                elif b0.startswith('MO'):
                    self.unit = INCH if 'IN' in b0 else 1.0
                elif b0.startswith('AM'):
                    self.macros[b0[2:]] = blocks[1:]
                elif b0.startswith('AD'):
                    m = re.match(r'ADD(\d+)(.*)', b0)
                    self._define(int(m.group(1)), m.group(2))
                elif b0.startswith('LP'):
                    polarity = b0[2] == 'D'
                elif b0.startswith('TF.FileFunction'):
                    self.function = b0.split(',', 1)[1] if ',' in b0 else ''
                elif b0.startswith('SR') and b0 not in ('SR', 'SRX1Y1I0J0'):
                    self.warnings.append('step-repeat not supported: ' + b0)
                continue
            # plain word
            s = w
            if s.startswith('G04'):
                continue
            while True:
                mg = re.match(r'G0*(\d+)', s)
                if not mg:
                    break
                g = int(mg.group(1)); s = s[mg.end():]
                if g in (1, 2, 3):
                    interp = f'G0{g}'
                elif g == 36:
                    region = []; contour = []
                elif g == 37:
                    if len(contour) >= 3:
                        region.append(contour)
                    for ct in region or []:
                        prims.append((polarity, ct))
                    region = None; contour = []
                elif g in (74, 75):
                    quadrant = f'G{g}'
                elif g == 70:
                    self.unit = INCH
                elif g == 71:
                    self.unit = 1.0
                elif g in (54, 55):
                    pass
            m = re.search(r'D0*(\d+)$', s)
            dcode = int(m.group(1)) if m else None
            if dcode is not None and dcode >= 10 and not re.search(r'[XYIJ]', s):
                cur = dcode; continue
            coords = dict((k, v) for k, v in re.findall(r'([XYIJ])([+-]?[\d.]+)', s))
            nx = self._num(coords['X'], 0) if 'X' in coords else x
            ny = self._num(coords['Y'], 1) if 'Y' in coords else y
            ii = self._num(coords['I'], 0) if 'I' in coords else 0.0
            jj = self._num(coords['J'], 1) if 'J' in coords else 0.0
            if dcode is None:
                if not coords:
                    continue
                dcode = 1 if region is not None else None   # modal D01 inside regions (old files)
                if dcode is None:
                    x, y = nx, ny; continue
            if dcode == 2:
                if region is not None:
                    if len(contour) >= 3:
                        region.append(contour)
                    contour = [(nx, ny)]
                x, y = nx, ny
            elif dcode == 1:
                if interp == 'G01':
                    path = [(nx, ny)]
                else:
                    cw = interp == 'G02'
                    if quadrant == 'G75':
                        cx, cy = x + ii, y + jj
                    else:
                        best = None
                        for sx in (1, -1):
                            for sy in (1, -1):
                                ccx, ccy = x + sx * abs(ii), y + sy * abs(jj)
                                err = abs(math.hypot(x - ccx, y - ccy) - math.hypot(nx - ccx, ny - ccy))
                                if best is None or err < best[0]:
                                    best = (err, ccx, ccy)
                        cx, cy = best[1], best[2]
                    path = arc_pts(x, y, nx, ny, cx, cy, cw, full_if_same=(quadrant == 'G75'))
                if region is not None:
                    if not contour:
                        contour = [(x, y)]
                    contour.extend(path)
                else:
                    ap = self._aperture(cur)
                    if self.stroke:
                        ap = {'kind': 'C', 'd': self.stroke}
                    px, py = x, y
                    for qx, qy in path:
                        if ap['kind'] == 'C':
                            r = ap['d'] / 2
                            if r > 0:
                                prims.append((polarity, capsule_pts(px, py, qx, qy, r)))
                        else:
                            pts = [p for _, sh in ap['shapes'] for p in sh]
                            prims.append((polarity, hull([(px + a, py + b) for a, b in pts] + [(qx + a, qy + b) for a, b in pts])))
                        px, py = qx, qy
                x, y = nx, ny
            elif dcode == 3:
                if self.stroke:
                    x, y = nx, ny; continue
                ap = self._aperture(cur)
                for dark, pts in ap['shapes']:
                    prims.append((polarity if dark else not polarity, [(nx + a, ny + b) for a, b in pts]))
                x, y = nx, ny
        # polarity runs -> geometry
        geom = geo.empty(); run = []; run_dark = True
        def flush():
            nonlocal geom
            if not run:
                return
            u = geo.union([geo.poly(p) for p in run if len(p) >= 3])
            geom = geo.union([geom, u]) if run_dark else geo.diff(geom, u)
        for dark, pts in prims:
            if dark != run_dark:
                flush(); run = []; run_dark = dark
            run.append(pts)
        flush()
        self.geom = geom


# --------------------------------------------------------------------------- Excellon

def read_excellon(path):
    """[(x_mm, y_mm, dia_mm, slot_or_None)]"""
    unit, tz, fmt = INCH, False, None
    tools, holes = {}, []
    cur = None; x = y = 0.0
    header = True
    for raw in open(path, encoding='latin-1'):
        s = raw.strip()
        if not s or s.startswith(';'):
            continue
        if s in ('M48',):
            header = True; continue
        if s in ('%', 'M95'):
            header = False; continue
        if s.startswith(('METRIC', 'M71')):
            unit = 1.0; tz = 'TZ' in s
            m = re.search(r'0+\.0+', s)
            fmt = (m.group(0).index('.'), len(m.group(0)) - m.group(0).index('.') - 1) if m else fmt
            continue
        if s.startswith(('INCH', 'M72')):
            unit = INCH; tz = 'TZ' in s
            continue
        m = re.match(r'T(\d+)(?:F\d+)?(?:S\d+)?C([\d.]+)', s)
        if m:
            tools[int(m.group(1))] = float(m.group(2)) * unit; continue
        m = re.match(r'T(\d+)$', s)
        if m:
            cur = int(m.group(1)); continue
        def num(v):
            if '.' in v:
                return float(v) * unit
            neg = v.startswith('-'); v = v.lstrip('+-')
            i, d = fmt or ((2, 4) if unit == INCH else (3, 3))
            if tz:
                v = v.ljust(i + d, '0')
            r = int(v) / 10 ** d
            return (-r if neg else r) * unit
        m = re.match(r'(?:G0?[01])?X([+-]?[\d.]+)?Y?([+-]?[\d.]+)?(?:G85X([+-]?[\d.]+)Y([+-]?[\d.]+))?', s)
        if s.startswith(('X', 'Y')) or 'G85' in s:
            mx = re.search(r'X([+-]?[\d.]+)', s.split('G85')[0]); my = re.search(r'Y([+-]?[\d.]+)', s.split('G85')[0])
            x = num(mx.group(1)) if mx else x
            y = num(my.group(1)) if my else y
            slot = None
            if 'G85' in s:
                tail = s.split('G85')[1]
                ex = re.search(r'X([+-]?[\d.]+)', tail); ey = re.search(r'Y([+-]?[\d.]+)', tail)
                slot = (num(ex.group(1)) if ex else x, num(ey.group(1)) if ey else y)
            if cur in tools:
                holes.append((x, y, tools[cur], slot))
    return holes


# --------------------------------------------------------------------------- layer identification

def layer_key(path, function=''):
    n = os.path.basename(path).lower()
    f = function.lower()
    if f:
        if f.startswith('copper'):
            m = re.search(r'l(\d+)', f)
            if 'top' in f:
                return 'cu:top'
            if 'bot' in f:
                return 'cu:bot'
            return f'cu:in{int(m.group(1)) - 1}' if m else None
        for k, v in (('soldermask', 'mask'), ('paste', 'paste'), ('legend', 'silk'), ('profile', 'outline')):
            if f.startswith(k):
                return f'{v}:' + ('top' if 'top' in f else 'bot') if v != 'outline' else 'outline'
    stem = os.path.splitext(n)[0]
    table = [
        (r'(^|[-_.])(f[._]cu|top|cmp|copper_top|toplayer)$', 'cu:top'),
        (r'(^|[-_.])(b[._]cu|bot|bottom|sol|copper_bottom|bottomlayer|bop)$', 'cu:bot'),
        (r'(^|[-_.])(in(\d+)[._]cu|ln(\d+)_cu|inner(\d+)|l(\d+)_cu|route(\d+))$', 'cu:in'),
        (r'(^|[-_.])(f[._]mask|top_mask|stc|tsm|topmask)$', 'mask:top'),
        (r'(^|[-_.])(b[._]mask|bot_mask|sts|bsm|bottommask)$', 'mask:bot'),
        (r'(^|[-_.])(f[._]paste|top_paste|crc|tcream|toppaste)$', 'paste:top'),
        (r'(^|[-_.])(b[._]paste|bot_paste|crs|bcream|bottompaste)$', 'paste:bot'),
        (r'(^|[-_.])(f[._]silks(creen)?|top_silk|plc|tsilk|topsilk)$', 'silk:top'),
        (r'(^|[-_.])(b[._]silks(creen)?|bot_silk|pls|bsilk|bottomsilk)$', 'silk:bot'),
        (r'(^|[-_.])(edge[._]cuts|dimension|outline|gko|board)$', 'outline'),
    ]
    for rx, key in table:
        m = re.search(rx, stem)
        if m:
            if key == 'cu:in':
                num = next(g for g in m.groups()[2:] if g)
                return f'cu:in{int(num)}'
            return key
    return None


def _unpack(src):
    """A .zip (as fabs get it) or a folder, possibly with sub-folders -> list of plain files.
    Zips are extracted flat into a temp folder (member names never used as paths: no traversal);
    zips inside the folder / zip are unpacked too, one level deep."""
    import tempfile, zipfile
    files = []

    def from_zip(z, depth):
        tmp = tempfile.mkdtemp(prefix='exhumer_mfg_')
        with zipfile.ZipFile(z) as zf:
            for i, info in enumerate(zf.infolist()):
                if info.is_dir() or info.file_size > 200e6:
                    continue
                base = os.path.basename(info.filename.replace('\\', '/'))
                if not base or base.startswith('.'):
                    continue
                dst = os.path.join(tmp, f'{i:04d}_{base}')
                with zf.open(info) as fi, open(dst, 'wb') as fo:
                    fo.write(fi.read())
                if base.lower().endswith('.zip') and depth < 1:
                    from_zip(dst, depth + 1)
                else:
                    files.append(dst)

    if os.path.isfile(src) and src.lower().endswith('.zip'):
        from_zip(src, 0)
    else:
        for root, _, names in os.walk(src):
            for n in sorted(names):
                p = os.path.join(root, n)
                if n.lower().endswith('.zip'):
                    from_zip(p, 0)
                else:
                    files.append(p)
    return sorted(files)


def _display(p):
    b = os.path.basename(p)
    return b[5:] if re.match(r'\d{4}_', b) else b


def collect(dirpath, geo):
    """{layer key: Gerber}, [holes] from a folder (sub-folders included) or a .zip"""
    layers, holes, notes = {}, [], []
    for p in _unpack(dirpath):
        if not os.path.isfile(p):
            continue
        ext = os.path.splitext(p)[1].lower()
        if ext in ('.pdf', '.png', '.jpg', '.zip', '.rar', '.7z', '.xlsx', '.csv', '.pos', '.gbrjob', '.json', '.html'):
            continue
        head = open(p, encoding='latin-1').read(400)
        if ext in ('.xnc', '.drl', '.drd', '.exc', '.txt') and ('M48' in head or head.lstrip().startswith(('T', '%', ';'))):
            holes += read_excellon(p); notes.append(f'drill: {_display(p)}'); continue
        if '%FS' not in head and 'G04' not in head and '%MO' not in head:
            continue
        g = Gerber(p, geo)
        k = layer_key(_display(p), g.function)
        if k == 'outline':
            g = Gerber(p, geo, stroke=0.1)
        if k is None:
            notes.append(f'unidentified layer: {os.path.basename(p)}'); continue
        if k in layers:
            notes.append(f'two files for {k}: {os.path.basename(p)} ignored'); continue
        layers[k] = g
    return layers, holes, notes


# --------------------------------------------------------------------------- alignment + comparison

def align(he, hk):
    """Translation (dx, dy) that maps the KiCad drill set onto the Eagle one (vote on hole pairs of
    equal diameter)."""
    if not he or not hk:
        return None, 0
    byd = defaultdict(list)
    for x, y, d, _ in hk:
        byd[round(d, 2)].append((x, y))
    votes = Counter()
    sample = he if len(he) <= 400 else he[::max(1, len(he) // 400)]
    for x, y, d, _ in sample:
        for kd in (round(d, 2), round(d + 0.01, 2), round(d - 0.01, 2)):
            for kx, ky in byd.get(kd, ()):
                votes[(round(x - kx, 2), round(y - ky, 2))] += 1
    if not votes:
        return None, 0
    (dx, dy), n = votes.most_common(1)[0]
    # refine: mean residual of matched pairs
    res = []
    for x, y, d, _ in he:
        best = None
        for kx, ky, kd, _ in hk:
            e = math.hypot(x - (kx + dx), y - (ky + dy))
            if e < 0.05 and (best is None or e < best[0]):
                best = (e, x - kx, y - ky)
        if best:
            res.append(best[1:])
    if res:
        dx = sum(r[0] for r in res) / len(res); dy = sum(r[1] for r in res) / len(res)
    return (dx, dy), n


def compare_drills(he, hk, dx, dy, tol=0.03, dtol=0.02):
    used = set(); miss = []; dia = []
    grid = defaultdict(list)
    for i, (x, y, d, s) in enumerate(hk):
        grid[(round((x + dx) / 0.5), round((y + dy) / 0.5))].append(i)
    for x, y, d, s in he:
        gx, gy = round(x / 0.5), round(y / 0.5)
        best = None
        for ax in (gx - 1, gx, gx + 1):
            for ay in (gy - 1, gy, gy + 1):
                for i in grid.get((ax, ay), ()):
                    if i in used:
                        continue
                    kx, ky, kd, ks = hk[i]
                    e = math.hypot(x - kx - dx, y - ky - dy)
                    if e <= tol and (best is None or e < best[0]):
                        best = (e, i)
        if best is None:
            miss.append((x, y, d)); continue
        used.add(best[1])
        kd = hk[best[1]][2]
        if abs(kd - d) > dtol:
            dia.append((x, y, d, kd))
    extra = [(hk[i][0] + dx, hk[i][1] + dy, hk[i][2]) for i in range(len(hk)) if i not in used]
    return dict(eagle=len(he), kicad=len(hk), matched=len(used), missing_in_kicad=miss, extra_in_kicad=extra,
                diameter_differs=dia)


def eagle_pours(brd_path, geo):
    """{layer key: polygon-pour region} from the EAGLE board (signal polygons, outline vertices)."""
    if not brd_path:
        return {}
    import xml.etree.ElementTree as ET
    b = ET.parse(brd_path).getroot().find('drawing/board')
    key = {'1': 'cu:top', '16': 'cu:bot'}
    key.update({str(i): f'cu:in{i - 1}' for i in range(2, 16)})
    out = defaultdict(list)
    for s in b.findall('signals/signal'):
        for p in s.findall('polygon'):
            k = key.get(p.get('layer'))
            pts = [(float(v.get('x')), float(v.get('y'))) for v in p.findall('vertex')]
            if k and len(pts) >= 3:
                out[k].append(geo.poly(pts))
    return {k: geo.union(v) for k, v in out.items()}


def compare(eagle_dir, kicad_dir, brd=None, tol=0.03, geo=None, log=print):
    geo = geo or backend()
    le, he, ne = collect(eagle_dir, geo)
    lk, hk, nk = collect(kicad_dir, geo)
    res = dict(backend=geo.name, notes=ne + nk, layers={}, drill=None, align=None)
    (dx, dy), votes = align(he, hk) if (he and hk) else ((None, None), 0)
    if dx is None:
        res['notes'].append('no drill holes to align on - layers compared without alignment')
        dx = dy = 0.0
    res['align'] = dict(dx=round(dx, 4), dy=round(dy, 4), votes=votes)
    if he and hk:
        res['drill'] = compare_drills(he, hk, dx, dy)
    pours = eagle_pours(brd, geo) if brd else {}
    for k in sorted(set(le) | set(lk)):
        if k not in le or k not in lk:
            res['layers'][k] = dict(only_in='eagle' if k in le else 'kicad')
            continue
        a = le[k].geom
        b = geo.translate(lk[k].geom, dx, dy)
        x = geo.xor(a, b)
        sig = geo.opening(x, tol)
        ae, ak = geo.area(a), geo.area(b)
        entry = dict(eagle_mm2=round(ae, 3), kicad_mm2=round(ak, 3), xor_mm2=round(geo.area(x), 3),
                     significant_mm2=round(geo.area(sig), 3))
        if k.startswith('cu:') and k in pours:
            in_pour = geo.inter(sig, pours[k])
            entry['significant_in_pours_mm2'] = round(geo.area(in_pour), 3)
            out = geo.diff(sig, pours[k])
            entry['significant_outside_pours_mm2'] = round(geo.area(out), 3)
            spots = geo.parts(out)
        else:
            spots = geo.parts(sig)
        spots.sort(key=lambda t: -t[0])
        entry['spots'] = [dict(mm2=round(a_, 4), x=round(cx, 3), y=round(cy, 3), w=round(w, 3), h=round(h, 3))
                          for a_, cx, cy, w, h in spots[:40]]
        entry['n_spots'] = len(spots)
        res['layers'][k] = entry
        log(f"  {k:10s} eagle {ae:10.2f}  kicad {ak:10.2f}  xor {entry['xor_mm2']:9.3f}  significant {entry['significant_mm2']:8.3f} mm2"
            + (f"  (outside pours {entry['significant_outside_pours_mm2']:.3f})" if 'significant_outside_pours_mm2' in entry else '')
            + f"  spots {len(spots)}")
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('eagle_dir', help='folder or .zip with the EAGLE CAM output'); ap.add_argument('kicad_dir')
    ap.add_argument('--brd'); ap.add_argument('--json'); ap.add_argument('--tol', type=float, default=0.03)
    a = ap.parse_args()
    r = compare(a.eagle_dir, a.kicad_dir, a.brd, a.tol)
    d = r['drill']
    if d:
        print(f"  drill: eagle {d['eagle']}, kicad {d['kicad']}, matched {d['matched']}, missing in KiCad {len(d['missing_in_kicad'])}, "
              f"extra in KiCad {len(d['extra_in_kicad'])}, diameter differs {len(d['diameter_differs'])}")
    print('  align', r['align'], '| notes:', '; '.join(r['notes']))
    if a.json:
        json.dump(r, open(a.json, 'w'), indent=1)


if __name__ == '__main__':
    main()
