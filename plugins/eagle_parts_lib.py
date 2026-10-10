"""Atomic parts library from a converted EAGLE project: one KiCad symbol per distinct EAGLE part
(library, deviceset, device, technology, value, attributes) - every resistor / capacitor value is
its own part with MPN, manufacturer and supplier fields, Footprint bound.

Naming follows the established in-house convention for EAGLE-derived libraries:
    <prefix>_<value>_<deviceset+device>_<package>      e.g. R_1.8k_RES-SMD-NEW_0402
EAGLE attributes map onto the house field names:
    PARTNR / MPN / MANUFACTURER_PART_NUMBER   -> MPN (MPN2, ... for further ones)
    VENDOR / MANUFACTURER / MANUFACTURER_NAME -> MANUFACTURER
    OC_FARNELL / OC_DIGIKEY / MOUSER_PART_NUMBER / OC_NEWARK -> SUPPLIERn + SUPPLIER_PART_NUMBERn
    DESCRIPTION (else the deviceset description) -> Description, DATASHEET -> Datasheet
Every other attribute is kept under its own name.
"""
import copy, glob, json, os, re, sys, xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eagle2kicad_fix import Q, sx_dump, kids, kid, prop, prop_val, read_sx, kref  # noqa: E402
from eagle_lib_export import _set_prop, _pins, _rename, _txt  # noqa: E402

MPN_KEYS = ('PARTNR', 'MPN', 'MANUFACTURER_PART_NUMBER', 'PART_NUMBER', 'MPN1', 'MPN2', 'MPN3')
MFR_KEYS = ('VENDOR', 'MANUFACTURER', 'MANUFACTURER_NAME', 'MFR', 'MF')
SUPPLIERS = (('Farnell', 'OC_FARNELL'), ('Digikey', 'OC_DIGIKEY'), ('Digikey', 'DIGI-KEY_PART_NUMBER'),
             ('Digikey', 'DIGIKEY_PART_NUMBER'), ('Mouser', 'MOUSER_PART_NUMBER'), ('Mouser', 'MOUSER'),
             ('Newark', 'OC_NEWARK'), ('LCSC', 'LCSC'), ('LCSC', 'LCSC_PART'))
DROP = {'VALUE', 'MOUSER_PRICE-STOCK', 'MOUSER_PRICE/STOCK', 'ARROW_PRICE-STOCK'}


def map_fields(attrs, ds_desc):
    a = {k.upper(): (v or '').strip() for k, v in attrs.items()}
    out, used = [], set()
    mpns = []
    for k in MPN_KEYS:
        if a.get(k) and a[k] not in mpns:
            mpns.append(a[k]); used.add(k)
    for i, v in enumerate(mpns):
        out.append(('MPN' if i == 0 else f'MPN{i + 1}', v))
    for k in MFR_KEYS:
        if a.get(k):
            out.append(('MANUFACTURER', a[k])); used.update(x for x in MFR_KEYS if x in a); break
    n = 0
    for sup, k in SUPPLIERS:
        if a.get(k):
            n += 1; used.add(k)
            out += [(f'SUPPLIER{n}', sup), (f'SUPPLIER_PART_NUMBER{n}', a[k])]
    desc = a.get('DESCRIPTION') or ds_desc
    used.add('DESCRIPTION')
    ds = a.get('DATASHEET', ''); used.add('DATASHEET')
    for k, v in sorted(a.items()):
        if k not in used and k not in DROP and v:
            out.append((k, v))
    return desc, ds, out


def eagle_parts(sch_path):
    s = ET.parse(sch_path).getroot().find('drawing/schematic')
    libs = {l.get('name'): l for l in s.findall('libraries/library')}
    parts = []
    for p in s.findall('parts/part'):
        L = libs.get(p.get('library'))
        ds = L.find(f"devicesets/deviceset[@name='{p.get('deviceset')}']") if L is not None else None
        if ds is None:
            continue
        dev = ds.find(f"devices/device[@name='{p.get('device', '')}']")
        if dev is None or not dev.get('package'):
            continue                                 # supply symbols, frames: not parts
        attrs = {}
        for t in dev.findall('technologies/technology'):
            if t.get('name', '') == (p.get('technology') or ''):
                attrs.update({x.get('name'): x.get('value') for x in t.findall('attribute')})
        attrs.update({x.get('name'): x.get('value') for x in p.findall('attribute') if x.get('value') is not None})
        value = p.get('value') or ds.get('name') + dev.get('name', '')
        parts.append(dict(name=p.get('name'), library=p.get('library'), ds=ds.get('name'), dev=dev.get('name', ''),
                          tech=p.get('technology') or '', value=value, package=dev.get('package'),
                          prefix=(ds.get('prefix') or re.sub(r'\d+$', '', p.get('name')) or 'U'),
                          desc=_txt(ds.findtext('description') or ''), attrs=attrs))
    return parts


def kicad_instances(proj_dir):
    """Reference -> (lib_id, Footprint) from every schematic sheet of the project"""
    out = {}
    for f in glob.glob(os.path.join(proj_dir, '*.kicad_sch')):
        t = read_sx(f)
        for s in kids(t, 'symbol'):
            li = kid(s, 'lib_id')
            ref = prop_val(s, 'Reference')
            if li is not None and ref:
                out.setdefault(ref, (str(li[1]), prop_val(s, 'Footprint', '')))
    return out


def _name(prefix, value, dsdev, package):
    v = re.sub(r'\s+', '_', value.strip())
    n = f'{prefix}_{dsdev}_{package}' if v in ('', dsdev) else f'{prefix}_{v}_{dsdev}_{package}'
    return n.replace(':', '_').replace('"', '')


def export(proj_dir, eagle_sch, out_dir, lib=None, fp_lib=None):
    proj = os.path.basename(os.path.normpath(proj_dir))
    lib = lib or re.sub(r'_kicad$', '', proj) + '_parts'
    os.makedirs(out_dir, exist_ok=True)
    symlib = glob.glob(os.path.join(proj_dir, '*-eagle-import.kicad_sym'))
    tree = read_sx(symlib[0])
    syms = {str(s[1]): s for s in kids(tree, 'symbol')}
    inst = kicad_instances(proj_dir)
    groups, unmapped = {}, []
    for p in eagle_parts(eagle_sch):
        ki = inst.get(kref(p['name']))
        if ki is None:
            unmapped.append(p['name']); continue
        key = (p['library'], p['ds'], p['dev'], p['tech'], p['value'], tuple(sorted(p['attrs'].items())))
        g = groups.setdefault(key, dict(p=p, lib_id=ki[0], fp=ki[1], refs=[]))
        g['refs'].append(kref(p['name']))
    out = [x for x in tree if not (isinstance(x, list) and x and x[0] == 'symbol')]
    names, rows, nompn, nofp, missing_sym = {}, [], [], [], []
    firsts, variants = {}, []
    for key, g in sorted(groups.items(), key=lambda kv: (kv[1]['p']['prefix'], kv[1]['p']['value'])):
        p = g['p']
        base = syms.get(g['lib_id'].split(':', 1)[-1])
        if base is None:
            missing_sym.append(g['lib_id']); continue
        s = copy.deepcopy(base)
        n = _name(p['prefix'], p['value'], p['ds'] + p['dev'], p['package'])
        if n in names:
            names[n] += 1
            first = firsts[n]
            cur = dict(p['attrs'], **{'(library)': p['library'], '(technology)': p['tech']})
            diff = sorted(k for k in set(first) | set(cur) if first.get(k) != cur.get(k))
            variants.append((n, f'{n}_{names[n]}', diff))
            n = f'{n}_{names[n]}'
        else:
            names[n] = 1; firsts[n] = dict(p['attrs'], **{'(library)': p['library'], '(technology)': p['tech']})
        _rename(s, n)
        desc, ds_url, fields = map_fields(p['attrs'], p['desc'])
        _set_prop(s, 'Reference', p['prefix'])
        _set_prop(s, 'Value', p['value'], hide=False)
        _set_prop(s, 'Footprint', (fp_lib + ':' + g['fp'].split(':', 1)[-1]) if fp_lib and g['fp'] else g['fp'])
        _set_prop(s, 'Datasheet', ds_url)
        _set_prop(s, 'Description', desc)
        for k, v in fields:
            _set_prop(s, k, v)
        _set_prop(s, 'ki_fp_filters', p['package'])
        _set_prop(s, 'ki_keywords', ' '.join(x for x in (p['ds'], p['package'], p['value']) if x))
        out.append(s)
        mpn = dict(fields).get('MPN', '')
        if not mpn:
            nompn.append(n)
        fpf = g['fp'].split(':', 1)
        if not g['fp'] or not os.path.isfile(os.path.join(proj_dir, fpf[0] + '.pretty', fpf[-1] + '.kicad_mod')):
            nofp.append((n, g['fp']))
        rows.append(dict(symbol=n, uses=len(g['refs']), refs=sorted(g['refs'])[:6], value=p['value'],
                         footprint=g['fp'], mpn=mpn, pins=len(_pins(s))))
    with open(os.path.join(out_dir, lib + '.kicad_sym'), 'w', encoding='utf-8', newline='\n') as f:
        f.write(sx_dump(out) + '\n')
    fails = []
    if unmapped:
        fails.append(f'{len(unmapped)} EAGLE parts without a KiCad instance')
    if missing_sym:
        fails.append(f'{len(missing_sym)} parts whose KiCad symbol is missing in the project library')
    if nofp:
        fails.append(f'{len(nofp)} parts whose footprint does not resolve')
    rep = dict(library=lib, parts=sum(r['uses'] for r in rows), atomic=len(rows), without_mpn=nompn,
               unmapped=unmapped, missing_symbols=missing_sym, unresolved_footprints=nofp,
               variants=[dict(base=a, variant=b, differs_in=c) for a, b, c in variants],
               verdict='PASS' if not fails else 'FAIL', fails=fails, table=rows)
    with open(os.path.join(out_dir, lib + '_report.json'), 'w', encoding='utf-8') as f:
        json.dump(rep, f, indent=1)
    L = [f'# Parts library: {lib}', '', f"- {rep['parts']} placed parts -> {rep['atomic']} atomic parts",
         f"- without MPN: {len(nompn)}" + (' (' + ', '.join(nompn[:12]) + (' ...' if len(nompn) > 12 else '') + ')' if nompn else ''),
         '', f"**{rep['verdict']}**" + (' - ' + '; '.join(fails) if fails else ''), '']
    L += [f"- variant {b} of {a}: attributes differ in {', '.join(c)}" for a, b, c in variants]
    L += ['',
         '| symbol | uses | value | footprint | MPN |', '|---|---|---|---|---|']
    L += [f"| {r['symbol']} | {r['uses']} | {r['value']} | {r['footprint']} | {r['mpn']} |" for r in rows]
    with open(os.path.join(out_dir, lib + '_report.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(L) + '\n')
    return rep


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='atomic parts library from a converted EAGLE project')
    ap.add_argument('project'); ap.add_argument('eagle_sch'); ap.add_argument('out')
    ap.add_argument('--name', help='symbol library nickname (default <project>_parts)')
    ap.add_argument('--fp-lib', help='footprint library nickname written into the Footprint fields')
    a = ap.parse_args()
    r = export(a.project, a.eagle_sch, a.out, a.name, a.fp_lib)
    print(json.dumps({k: (v if not isinstance(v, list) else len(v)) for k, v in r.items()}, indent=1))
    sys.exit(0 if r['verdict'] == 'PASS' else 1)
