"""Merge converted libraries into a company library without ever overwriting curated content.

  kicad_lib_merge.py <company.kicad_sym> <new.kicad_sym>... [--fp-lib NICK] [--pretty-in DIR --pretty-out DIR] [--write]

Per symbol of the new libraries:
  new        name not in the company library            -> added
  same       identical (properties, pins, graphics)     -> skipped
  conflict   same name, different content               -> company version kept; the difference
                                                           (fields / pins / graphics) is reported
Footprints (--pretty-in -> --pretty-out): missing ones copied, identical skipped, different ones
reported, never replaced. --fp-lib rewrites the Footprint fields of added symbols to that nickname.
Without --write nothing is changed (dry run); the report is printed and saved (--report-dir, else next to the target).
"""
import argparse, glob, json, os, re, shutil, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eagle2kicad_fix import Q, sx_dump, kids, kid, prop, read_sx  # noqa: E402
from eagle_lib_export import _set_prop, _pins  # noqa: E402

UUID = re.compile(r'\(uuid "[^"]*"\)')


def _props(s):
    return {str(p[1]): str(p[2]) for p in kids(s, 'property')}


def _num(v):
    try:
        return round(float(v), 3)
    except (TypeError, ValueError):
        return str(v)


def _geo(node, keys):
    """[(head, coordinates...)] of the drawing primitives below node - styling (stroke, fill,
    fonts, editor flags) is ignored, only what changes the drawing or the connectivity counts"""
    out = []
    def walk(n):
        for x in n:
            if not (isinstance(x, list) and x):
                continue
            if x[0] in keys:
                item = [x[0]]
                for y in x[1:]:
                    if isinstance(y, list) and y and y[0] in ('at', 'start', 'mid', 'end', 'center', 'radius',
                                                               'length', 'number', 'name', 'size', 'drill', 'layers', 'pts'):
                        vals = tuple(_num(z) if not isinstance(z, list) else tuple(_num(w) for w in z[1:]) for z in y[1:])
                        item.append((y[0],) + (tuple(sorted(vals, key=str)) if y[0] == 'layers' else vals))
                    elif not isinstance(y, list):
                        item.append(str(y))
                out.append(tuple(item))
            else:
                walk(x)
    walk(node)
    return sorted(out, key=str)


SYM_KEYS = ('polyline', 'rectangle', 'circle', 'arc', 'bezier', 'pin', 'text')
FP_PADS = ('pad',)
FP_GFX = ('fp_line', 'fp_rect', 'fp_circle', 'fp_arc', 'fp_poly')


def _body(s):
    return _geo(s, SYM_KEYS)


def merge(target, sources, fp_lib=None, pretty_in=(), pretty_out=None, write=False, report_dir=None):
    tgt = read_sx(target) if os.path.isfile(target) else ['kicad_symbol_lib', ['version', '20251024'], ['generator', Q('eagle_exhumer')]]
    have = {str(s[1]): s for s in kids(tgt, 'symbol')}
    rep = dict(target=target, added=[], same=[], conflicts=[], fp_added=[], fp_same=[], fp_conflicts=[])
    for src in sources:
        for s in kids(read_sx(src), 'symbol'):
            n = str(s[1])
            if fp_lib:
                fp = _props(s).get('Footprint', '')
                if fp:
                    _set_prop(s, 'Footprint', fp_lib + ':' + fp.split(':', 1)[-1])
            if n not in have:
                tgt.append(s); have[n] = s; rep['added'].append(n); continue
            a, b = have[n], s
            pa, pb = _props(a), _props(b)
            fd = {k: [pa.get(k), pb.get(k)] for k in sorted(set(pa) | set(pb)) if pa.get(k) != pb.get(k)}
            pins = _pins(a) != _pins(b)
            gfx = _body(a) != _body(b)
            if not fd and not gfx:
                rep['same'].append(n)
            else:
                rep['conflicts'].append(dict(symbol=n, source=os.path.basename(src), fields=fd,
                                             pins_differ=pins, graphics_differ=gfx and not pins))
    for pin_dir in pretty_in or ():
        for f in sorted(glob.glob(os.path.join(pin_dir, '*.kicad_mod'))):
            n = os.path.basename(f)
            dst = os.path.join(pretty_out, n)
            if not os.path.isfile(dst):
                rep['fp_added'].append(n[:-10])
                if write:
                    os.makedirs(pretty_out, exist_ok=True); shutil.copy2(f, dst)
            else:
                a, b = read_sx(dst), read_sx(f)
                pads = _geo(a, FP_PADS) != _geo(b, FP_PADS)
                gfx = _geo(a, FP_GFX) != _geo(b, FP_GFX)
                if not pads and not gfx:
                    rep['fp_same'].append(n[:-10])
                else:
                    rep['fp_conflicts'].append(dict(footprint=n[:-10], pads_differ=pads, graphics_differ=gfx))
    if write:
        if os.path.isfile(target):
            shutil.copy2(target, target + '.bak')
        with open(target, 'w', encoding='utf-8', newline='\n') as f:
            f.write(sx_dump(tgt) + '\n')
    base = os.path.join(report_dir, os.path.splitext(os.path.basename(target))[0]) if report_dir else os.path.splitext(target)[0]
    if report_dir:
        os.makedirs(report_dir, exist_ok=True)
    with open(base + '_merge_report.json', 'w', encoding='utf-8') as f:
        json.dump(rep, f, indent=1)
    L = [f"# Merge into {os.path.basename(target)}" + ('' if write else ' (dry run)'), '',
         f"- symbols added {len(rep['added'])}, identical {len(rep['same'])}, conflicts {len(rep['conflicts'])} (company version kept)",
         f"- footprints added {len(rep['fp_added'])}, identical {len(rep['fp_same'])}, conflicts {len(rep['fp_conflicts'])} (company version kept)", '']
    for c in rep['conflicts']:
        what = 'PINS DIFFER' if c['pins_differ'] else ('graphics differ' if c['graphics_differ'] else 'fields differ')
        L.append(f"- {c['symbol']} ({c['source']}): {what}" + (': ' + '; '.join(f"{k}: '{v[0]}' -> '{v[1]}'" for k, v in list(c['fields'].items())[:6]) if c['fields'] else ''))
    for c in rep['fp_conflicts']:
        L.append(f"- footprint {c['footprint']}: " + ('PADS DIFFER' if c['pads_differ'] else 'graphics differ') + ' from the company footprint')
    with open(base + '_merge_report.md', 'w', encoding='utf-8') as f:
        f.write('\n'.join(L) + '\n')
    return rep, L


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('target'); ap.add_argument('sources', nargs='+')
    ap.add_argument('--fp-lib'); ap.add_argument('--pretty-in', action='append', default=[])
    ap.add_argument('--pretty-out'); ap.add_argument('--write', action='store_true')
    ap.add_argument('--report-dir', help='where the merge report goes (default: next to the target)')
    a = ap.parse_args()
    rep, L = merge(a.target, a.sources, a.fp_lib, a.pretty_in, a.pretty_out, a.write, a.report_dir)
    print('\n'.join(L[:40]))
