"""EAGLE library (.lbr) -> synthetic EAGLE project (.sch + .brd) for the normal import / fix / QC chain.

Every device of every deviceset becomes one part: all gates placed on a sheet, every connected pin
gets its own net (short wire + label), and the board carries one element per device with the
same nets on its pads. The strict QC then proves pin -> pad mapping and pad geometry against the
EAGLE source exactly as for a real project. Packages that no device uses are placed as well
(footprint only). A JSON map (ref -> deviceset / device / technology / package / gates) is
written next to the project so the harvested libraries can be named after the EAGLE objects.
"""
import copy, json, os, re, xml.etree.ElementTree as ET

GRID = 2.54
PART_COLS = 8                  # parts per row on a sheet
PARTS_PER_SHEET = 40


def _snap(v):
    return round(round(v / GRID) * GRID, 4)


def _f(e, k, d=0.0):
    v = e.get(k)
    return float(v) if v not in (None, '') else d


def _bbox(items):
    xs, ys = [], []
    for o in items:
        for kx, ky in (('x', 'y'), ('x1', 'y1'), ('x2', 'y2')):
            if o.get(kx) is not None and o.get(ky) is not None:
                xs.append(_f(o, kx)); ys.append(_f(o, ky))
        if o.tag in ('smd',):
            x, y, dx, dy = _f(o, 'x'), _f(o, 'y'), _f(o, 'dx'), _f(o, 'dy')
            r = max(dx, dy) / 2
            xs += [x - r, x + r]; ys += [y - r, y + r]
        if o.tag == 'circle':
            x, y, r = _f(o, 'x'), _f(o, 'y'), _f(o, 'radius')
            xs += [x - r, x + r]; ys += [y - r, y + r]
        for v in o.findall('vertex'):
            xs.append(_f(v, 'x')); ys.append(_f(v, 'y'))
    if not xs:
        return (-1.0, -1.0, 1.0, 1.0)
    return (min(xs), min(ys), max(xs), max(ys))


def _safe(s):
    s = re.sub(r'[^A-Za-z0-9_+\-.]', '_', s or '')
    return s or 'X'


def _outward(rot):
    """direction away from the symbol body for an EAGLE pin (connection point at x,y)"""
    a = int(float((rot or 'R0').lstrip('MR') or 0)) % 360
    m = (rot or '').startswith('M')
    d = {0: (-1, 0), 90: (0, -1), 180: (1, 0), 270: (0, 1)}.get(a, (-1, 0))
    return (-d[0], d[1]) if m else d


def lbr_inventory(lbr_path):
    r = ET.parse(lbr_path).getroot()
    L = r.find('drawing/library')
    if L is None:
        raise ValueError('not an EAGLE XML library (no drawing/library)')
    return r, L


def lbr_to_project(lbr_path, out_dir, name=None):
    """Write <out_dir>/<name>.sch, <name>.brd and <name>.lbrmap.json; return (sch, brd, map)."""
    root, L = lbr_inventory(lbr_path)
    name = _safe(name or os.path.splitext(os.path.basename(lbr_path))[0])
    lname = L.get('name') or name
    os.makedirs(out_dir, exist_ok=True)
    ver = root.get('version') or '9.6.2'
    layers = root.find('drawing/layers')
    settings, grid = root.find('drawing/settings'), root.find('drawing/grid')
    symbols = {s.get('name'): s for s in L.findall('symbols/symbol')}
    packages = {p.get('name'): p for p in L.findall('packages/package')}

    def lib_copy():
        lib = copy.deepcopy(L)
        lib.set('name', lname)
        return lib

    def skeleton():
        e = ET.Element('eagle', version=ver)
        d = ET.SubElement(e, 'drawing')
        for blk in (settings, grid, layers):
            if blk is not None:
                d.append(copy.deepcopy(blk))
        return e, d

    # ---- parts
    parts, used_pk, counters = [], set(), {}
    for ds in L.findall('devicesets/deviceset'):
        prefix = _safe(ds.get('prefix') or 'U').rstrip('_') or 'U'
        prefix = re.sub(r'[0-9]+$', '', prefix) or 'U'
        gates = ds.findall('gates/gate')
        for dev in ds.findall('devices/device'):
            techs = dev.findall('technologies/technology')
            tech = techs[0].get('name', '') if techs else ''
            counters[prefix] = counters.get(prefix, 0) + 1
            ref = f'{prefix}{counters[prefix]}'
            pk = dev.get('package')
            if pk:
                used_pk.add(pk)
            parts.append(dict(ref=ref, deviceset=ds.get('name'), device=dev.get('name', ''), technology=tech,
                              package=pk, gates=[(g.get('name'), g.get('symbol')) for g in gates],
                              connects=[(c.get('gate'), c.get('pin'), c.get('pad')) for c in dev.findall('connects/connect')],
                              value=ds.get('name') + dev.get('name', '')))
    extra_pk = [p for p in packages if p not in used_pk]

    # ---- nets: one per connected pin (a pin may sit on several pads)
    # supply pins (direction pwr) keep the EAGLE semantics: their net is named after the pin
    pdir = {(sn, pn.get('name')): pn.get('direction', 'io') for sn, sy in symbols.items() for pn in sy.findall('pin')}
    netname, used_names = {}, set()
    for p in parts:
        gsym = dict(p['gates'])
        for g, pin, pads in p['connects']:
            if pdir.get((gsym.get(g), pin)) == 'pwr':
                netname[(p['ref'], g, pin)] = (pin, pads.split()); continue
            base = f"{p['ref']}_{_safe(pin)}"
            n, i = base, 1
            while n in used_names:
                i += 1; n = f'{base}_{i}'
            used_names.add(n)
            netname[(p['ref'], g, pin)] = (n, pads.split())

    # ---- schematic
    e, d = skeleton()
    sch = ET.SubElement(d, 'schematic', xreflabel='%F%N/%S.%C%R', xrefpart='/%S.%C%R')
    ET.SubElement(sch, 'libraries').append(lib_copy())
    ET.SubElement(sch, 'attributes'); ET.SubElement(sch, 'variantdefs')
    ET.SubElement(ET.SubElement(sch, 'classes'), 'class', number='0', name='default', width='0', drill='0')
    pe = ET.SubElement(sch, 'parts')
    sheets = ET.SubElement(sch, 'sheets')
    for p in parts:
        a = dict(name=p['ref'], library=lname, deviceset=p['deviceset'], device=p['device'])
        if p['technology']:
            a['technology'] = p['technology']
        ET.SubElement(pe, 'part', a)
    sheet = None
    col_x, row_y, row_h = 0.0, 0.0, 0.0
    for k, p in enumerate(parts):
        if k % PARTS_PER_SHEET == 0:
            sheet = ET.SubElement(sheets, 'sheet')
            ET.SubElement(sheet, 'plain'); inst = ET.SubElement(sheet, 'instances')
            ET.SubElement(sheet, 'busses'); nets = ET.SubElement(sheet, 'nets'); sheet_nets = {}
            col_x, row_y, row_h, col = 0.0, 0.0, 0.0, 0
        # gates of one part stacked vertically in one column
        y = row_y; w = 0.0
        for g, sname in p['gates']:
            s = symbols.get(sname)
            items = list(s) if s is not None else []
            bx0, by0, bx1, by1 = _bbox(items)
            gx, gy = _snap(col_x - bx0 + 7.62), _snap(y - by1)
            ET.SubElement(inst, 'instance', part=p['ref'], gate=g, x=f'{gx}', y=f'{gy}')
            for pin in (s.findall('pin') if s is not None else []):
                key = (p['ref'], g, pin.get('name'))
                if key not in netname:
                    continue
                px, py = gx + _f(pin, 'x'), gy + _f(pin, 'y')
                ox, oy = _outward(pin.get('rot'))
                ex, ey = round(px + ox * GRID, 4), round(py + oy * GRID, 4)
                nn = netname[key][0]
                if nn not in sheet_nets:
                    sheet_nets[nn] = ET.SubElement(nets, 'net', name=nn, **{'class': '0'})
                seg = ET.SubElement(sheet_nets[nn], 'segment')
                ET.SubElement(seg, 'pinref', part=p['ref'], gate=g, pin=pin.get('name'))
                ET.SubElement(seg, 'wire', x1=f'{px}', y1=f'{py}', x2=f'{ex}', y2=f'{ey}', width='0.1524', layer='91')
                ET.SubElement(seg, 'label', x=f'{ex}', y=f'{ey}', size='1.27', layer='95')
            y -= (by1 - by0) + 10.16
            w = max(w, bx1 - bx0)
        row_h = max(row_h, row_y - y)
        col_x += w + 25.4
        if (k % PARTS_PER_SHEET) % PART_COLS == PART_COLS - 1:
            col_x, row_y, row_h = 0.0, row_y - row_h - 10.16, 0.0
    if sheet is None:                                   # footprint-only library: one empty sheet
        sheet = ET.SubElement(sheets, 'sheet')
        for t in ('plain', 'instances', 'busses', 'nets'):
            ET.SubElement(sheet, t)
    sch_path = os.path.join(out_dir, name + '.sch')
    ET.ElementTree(e).write(sch_path, encoding='utf-8', xml_declaration=True)

    # ---- board
    e, d = skeleton()
    brd = ET.SubElement(d, 'board')
    plain = ET.SubElement(brd, 'plain')
    ET.SubElement(brd, 'libraries').append(lib_copy())
    ET.SubElement(brd, 'attributes'); ET.SubElement(brd, 'variantdefs')
    ET.SubElement(ET.SubElement(brd, 'classes'), 'class', number='0', name='default', width='0', drill='0')
    ET.SubElement(brd, 'designrules', name='default')
    els = ET.SubElement(brd, 'elements')
    sigs = ET.SubElement(brd, 'signals')
    placed = [(p['ref'], p['package'], p['value']) for p in parts if p['package']] + \
             [(f'FP{i + 1}', pk, pk) for i, pk in enumerate(extra_pk)]
    x = y = 0.0; row_h = 0.0; X0 = 5.0; maxx = 0.0; n_col = 0
    pos = {}
    for ref, pk, val in placed:
        P = packages.get(pk)
        if P is None:
            continue
        bx0, by0, bx1, by1 = _bbox(list(P))
        w, h = bx1 - bx0, by1 - by0
        ex, ey = round(X0 + x - bx0, 3), round(-(y) - by1 - X0, 3)
        ET.SubElement(els, 'element', name=ref, library=lname, package=pk, value=val, x=f'{ex}', y=f'{ey}')
        pos[ref] = (ex, ey)
        x += w + 5.0; row_h = max(row_h, h); maxx = max(maxx, x); n_col += 1
        if n_col >= 12:
            x, y, row_h, n_col = 0.0, y + row_h + 5.0, 0.0, 0
    signal = {}
    for (ref, g, pin), (net, pads) in netname.items():
        if ref not in pos:
            continue
        if net not in signal:
            signal[net] = ET.SubElement(sigs, 'signal', name=net)
        for pd in pads:
            ET.SubElement(signal[net], 'contactref', element=ref, pad=pd)
    H = y + row_h + 2 * X0; W = maxx + 2 * X0
    for x1, y1, x2, y2 in ((0, 0, W, 0), (W, 0, W, -H), (W, -H, 0, -H), (0, -H, 0, 0)):
        ET.SubElement(plain, 'wire', x1=f'{x1}', y1=f'{y1}', x2=f'{x2}', y2=f'{y2}', width='0', layer='20')
    brd_path = os.path.join(out_dir, name + '.brd')
    ET.ElementTree(e).write(brd_path, encoding='utf-8', xml_declaration=True)

    m = dict(library=lname, source=os.path.basename(lbr_path), parts={p['ref']: {k: p[k] for k in
             ('deviceset', 'device', 'technology', 'package', 'gates')} for p in parts},
             footprint_only={f'FP{i + 1}': pk for i, pk in enumerate(extra_pk)},
             counts=dict(devicesets=len(L.findall('devicesets/deviceset')), devices=len(parts),
                         symbols=len(symbols), packages=len(packages), nets=len(netname)))
    map_path = os.path.join(out_dir, name + '.lbrmap.json')
    with open(map_path, 'w', encoding='utf-8') as f:
        json.dump(m, f, indent=1)
    return sch_path, brd_path, map_path


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 3:
        print('usage: eagle_lbr.py <lib.lbr> <out_dir> [name]'); sys.exit(2)
    s, b, m = lbr_to_project(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    print('written', s, b, m)
    print(json.load(open(m))['counts'])
