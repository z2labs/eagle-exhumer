"""Harvest KiCad libraries from a converted EAGLE-library project (see eagle_lbr.py).

  <out>/<Lib>.kicad_sym          1:1 atomic: one symbol per EAGLE device (deviceset + device),
                                 Footprint field bound to <Lib>:<package>
  <out>/<Lib>_generic.kicad_sym  one symbol per EAGLE deviceset, footprint chosen in KiCad
                                 (ki_fp_filters = the deviceset's packages); only where every
                                 device of the deviceset uses the same pin -> pad numbering
  <out>/<Lib>.pretty             every EAGLE package, taken from the verified board
  <out>/lib_report.md / .json    library QC: every device / package present, pin numbers equal
                                 the EAGLE connects, every Footprint field resolves

Footprints need KiCad's Python (pcbnew); symbols are plain S-expression work.
"""
import copy, glob, json, os, re, sys, xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eagle2kicad_fix import Q, sx_parse, sx_dump, kids, kid, prop, read_sx  # noqa: E402

KEEP_GENERIC = ('Reference', 'Value', 'Footprint', 'Datasheet', 'Description', 'ki_keywords', 'ki_fp_filters')


def _txt(s):
    """EAGLE descriptions are rich text (HTML subset): plain text for KiCad fields"""
    s = re.sub(r'<br\s*/?>|</p>|</li>', '\n', s or '', flags=re.I)
    s = re.sub(r'<[^>]+>', '', s)
    s = s.replace('&nbsp;', ' ').replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
    s = re.sub(r'[ \t]+', ' ', s)
    return re.sub(r'\n\s*\n+', '\n', s).strip()


def eagle_devices(lbr_path):
    """{deviceset+device: dict(ds, dev, package, pads{pin-key: [pads]}, gates, desc, prefix)}"""
    r = ET.parse(lbr_path).getroot()                 # .lbr, or the synthetic .sch that embeds it
    L = r.find('drawing/library')
    if L is None:
        L = r.find('.//libraries/library')
    out = {}
    for ds in L.findall('devicesets/deviceset'):
        desc = _txt(ds.findtext('description') or '')
        for dev in ds.findall('devices/device'):
            pads = {}
            for c in dev.findall('connects/connect'):
                pads[(c.get('gate'), c.get('pin'))] = c.get('pad').split()
            out[ds.get('name') + dev.get('name', '')] = dict(
                ds=ds.get('name'), dev=dev.get('name', ''), package=dev.get('package'), pads=pads,
                gates=[g.get('name') for g in ds.findall('gates/gate')], desc=desc,
                prefix=ds.get('prefix') or '')
    pk = [p.get('name') for p in L.findall('packages/package')]
    return out, pk


def _set_prop(sym, name, value, hide=True):
    p = prop(sym, name)
    if p is not None:
        p[2] = Q(value)
        return p
    # new property: after the last existing one, hidden
    last = max(i for i, x in enumerate(sym) if isinstance(x, list) and x and x[0] == 'property')
    p = ['property', Q(name), Q(value), ['at', '0', '0', '0']] + ([['hide', 'yes']] if hide else []) + \
        [['effects', ['font', ['size', '1.27', '1.27']]]]
    sym.insert(last + 1, p)
    return p


def _pins(sym):
    """all pin numbers of a symbol (all units), as a sorted list"""
    out = []
    def walk(n):
        for x in n:
            if isinstance(x, list) and x:
                if x[0] == 'pin':
                    num = kid(x, 'number')
                    if num is not None:
                        out.append(str(num[1]))
                else:
                    walk(x)
    walk(sym)
    return sorted(out)


def _rename(sym, new):
    old = str(sym[1])
    sym[1] = Q(new)
    for u in kids(sym, 'symbol'):
        n = str(u[1])
        if n.startswith(old + '_'):
            u[1] = Q(new + n[len(old):])


def export_symbols(sym_path, devices, lib, out_dir, rep):
    tree = read_sx(sym_path)
    syms = {str(s[1]): s for s in kids(tree, 'symbol')}
    atomic = [x for x in tree if not (isinstance(x, list) and x and x[0] == 'symbol')]
    by_ds = {}
    missing, badpins = [], []
    for key, dv in devices.items():
        s = syms.get(key)
        if s is None:
            missing.append(key); continue
        s = copy.deepcopy(s)
        _set_prop(s, 'Footprint', f"{lib}:{dv['package']}" if dv['package'] else '')
        if dv['package']:
            _set_prop(s, 'ki_fp_filters', dv['package'])
        if dv['desc']:
            _set_prop(s, 'Description', dv['desc'])
        _set_prop(s, 'ki_keywords', ' '.join(x for x in (dv['ds'], dv['dev'].strip('-_'), dv['package'] or '') if x))
        want = sorted(p for pads in dv['pads'].values() for p in pads)
        have = _pins(s)
        if dv['package'] and want != have:
            badpins.append((key, want, have))
        atomic.append(s)
        mapping = tuple(sorted((g, pin, tuple(p)) for (g, pin), p in dv['pads'].items()))
        by_ds.setdefault(dv['ds'], {}).setdefault(mapping, []).append((key, dv, s))
    with open(os.path.join(out_dir, lib + '.kicad_sym'), 'w', encoding='utf-8', newline='\n') as f:
        f.write(sx_dump(atomic) + '\n')
    # generic: one symbol per deviceset (largest group of devices with the same pin -> pad numbering)
    generic = [x for x in tree if not (isinstance(x, list) and x and x[0] == 'symbol')]
    split = []
    for ds, groups in sorted(by_ds.items()):
        best = max(groups.values(), key=len)
        if len(groups) > 1:
            split.append((ds, len(groups), [k for k, _, _ in best]))
        key, dv, s = best[0]
        g = copy.deepcopy(s)
        name = re.sub(r'[-_]+$', '', ds) or ds
        _rename(g, name)
        for p in list(kids(g, 'property')):
            if str(p[1]) not in KEEP_GENERIC:
                g.remove(p)
        _set_prop(g, 'Value', name, hide=False)
        _set_prop(g, 'Footprint', '')
        pk = sorted({d['package'] for _, d, _ in best if d['package']})
        if pk:
            _set_prop(g, 'ki_fp_filters', ' '.join(pk))
        _set_prop(g, 'ki_keywords', ds)
        generic.append(g)
    with open(os.path.join(out_dir, lib + '_generic.kicad_sym'), 'w', encoding='utf-8', newline='\n') as f:
        f.write(sx_dump(generic) + '\n')
    rep.update(symbols_atomic=len(atomic) - (len(tree) - len(syms)), symbols_generic=len(by_ds),
               missing_devices=missing, pin_mismatch=[dict(device=k, eagle=w, kicad=h) for k, w, h in badpins],
               generic_split=[dict(deviceset=d, numberings=n, generic_from=k[:6]) for d, n, k in split])


def export_footprints(pcb_path, lib, out_dir, packages, rep):
    import pcbnew
    pretty = os.path.join(out_dir, lib + '.pretty')
    io = pcbnew.PCB_IO_MGR.FindPlugin(pcbnew.PCB_IO_MGR.KICAD_SEXP)
    if os.path.isdir(pretty):
        for f in glob.glob(os.path.join(pretty, '*.kicad_mod')):
            os.remove(f)
    else:
        os.makedirs(pretty)
    board = pcbnew.LoadBoard(pcb_path)
    done = {}
    for fp in board.GetFootprints():
        name = str(fp.GetFPID().GetLibItemName())
        if name in done:
            continue
        if fp.IsFlipped():
            fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_TOP_BOTTOM if hasattr(pcbnew, 'FLIP_DIRECTION_TOP_BOTTOM') else False)
        fp.SetOrientationDegrees(0)
        fp.SetPosition(pcbnew.VECTOR2I(0, 0))
        for pad in fp.Pads():
            pad.SetNetCode(0)
        fp.SetReference('REF**')
        fp.SetValue(name)
        fp.SetFPID(pcbnew.LIB_ID('', name))
        io.FootprintSave(pretty, fp)
        done[name] = fp.Pads().size() if hasattr(fp.Pads(), 'size') else len(list(fp.Pads()))
    missing = [p for p in packages if p not in done]
    rep.update(footprints=len(done), missing_packages=missing)
    return done


def write_tables(out_dir, lib):
    with open(os.path.join(out_dir, 'sym-lib-table.add'), 'w', encoding='utf-8') as f:
        f.write(f'  (lib (name "{lib}")(type "KiCad")(uri "${{KIPRJMOD}}/{lib}.kicad_sym")(options "")(descr "EAGLE {lib}, 1:1 per device"))\n')
        f.write(f'  (lib (name "{lib}_generic")(type "KiCad")(uri "${{KIPRJMOD}}/{lib}_generic.kicad_sym")(options "")(descr "EAGLE {lib}, generic per deviceset"))\n')
    with open(os.path.join(out_dir, 'fp-lib-table.add'), 'w', encoding='utf-8') as f:
        f.write(f'  (lib (name "{lib}")(type "KiCad")(uri "${{KIPRJMOD}}/{lib}.pretty")(options "")(descr "EAGLE {lib}"))\n')


def lib_verdict(rep):
    fails = []
    if rep.get('missing_devices'):
        fails.append(f"{len(rep['missing_devices'])} EAGLE devices without a KiCad symbol")
    if rep.get('missing_packages'):
        fails.append(f"{len(rep['missing_packages'])} EAGLE packages without a footprint")
    if rep.get('pin_mismatch'):
        fails.append(f"{len(rep['pin_mismatch'])} symbols whose pin numbers differ from the EAGLE connects")
    if rep.get('qc') and rep['qc'] != 'PASS':
        fails.append('project QC: ' + rep['qc'])
    return fails


def export(proj_dir, lbr_path, out_dir, lib=None):
    devices, packages = eagle_devices(lbr_path)
    lib = lib or os.path.splitext(os.path.basename(lbr_path))[0]
    os.makedirs(out_dir, exist_ok=True)
    rep = dict(library=lib, source=os.path.basename(lbr_path), devices=len(devices), packages=len(packages))
    syms = glob.glob(os.path.join(proj_dir, '*-eagle-import.kicad_sym'))
    if not syms:
        raise FileNotFoundError('no *-eagle-import.kicad_sym in ' + proj_dir)
    if devices:
        export_symbols(syms[0], devices, lib, out_dir, rep)
    pcb = glob.glob(os.path.join(proj_dir, '*.kicad_pcb'))
    pcb = [p for p in pcb if not os.path.basename(p).startswith('_')]
    export_footprints(pcb[0], lib, out_dir, packages, rep)
    write_tables(out_dir, lib)
    m = os.path.join(proj_dir, 'eaglefix_metrics.json')
    if os.path.isfile(m):
        try:
            rep['qc'] = json.load(open(m, encoding='utf-8')).get('qc', '?')
        except Exception:
            rep['qc'] = '?'
    fails = lib_verdict(rep)
    rep['verdict'] = 'PASS' if not fails else 'FAIL'
    rep['fails'] = fails
    with open(os.path.join(out_dir, 'lib_report.json'), 'w', encoding='utf-8') as f:
        json.dump(rep, f, indent=1)
    lines = [f"# Library export: {lib}", '',
             f"- source: {rep['source']} - {rep['devices']} devices, {rep['packages']} packages",
             f"- symbols: {rep.get('symbols_atomic', 0)} atomic (1:1), {rep.get('symbols_generic', 0)} generic",
             f"- footprints: {rep.get('footprints', 0)}",
             f"- project QC (netlist / pads / geometry vs EAGLE): {rep.get('qc', '?')}"]
    for d in rep.get('generic_split', []):
        lines.append(f"- generic {d['deviceset']}: {d['numberings']} different pin numberings - generic symbol from {', '.join(d['generic_from'])}")
    lines += ['', f"**{rep['verdict']}**" + (' - ' + '; '.join(fails) if fails else '')]
    for x in rep.get('pin_mismatch', [])[:20]:
        lines.append(f"  - {x['device']}: EAGLE pads {x['eagle']} / KiCad pins {x['kicad']}")
    for x in rep.get('missing_devices', [])[:20]:
        lines.append(f"  - missing symbol: {x}")
    for x in rep.get('missing_packages', [])[:20]:
        lines.append(f"  - missing footprint: {x}")
    with open(os.path.join(out_dir, 'lib_report.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    return rep


if __name__ == '__main__':
    if len(sys.argv) < 4:
        print('usage: eagle_lib_export.py <converted_project_dir> <lib.lbr> <out_dir> [nickname]'); sys.exit(2)
    r = export(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None)
    print(json.dumps({k: (v if not isinstance(v, list) else len(v)) for k, v in r.items()}, indent=1))
    sys.exit(0 if r['verdict'] == 'PASS' else 1)
