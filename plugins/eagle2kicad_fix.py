#!/usr/bin/env python3
"""
eagle2kicad_fix.py  -  Post-processor for KiCad 10 Eagle imports.

Workflow:
  1. In KiCad 10: File > Import Non-KiCad Project > Eagle (.sch + .brd), save both.
  2. Run with KiCad's own Python (needed for the footprint-library step):
       "C:\\Program Files\\KiCad\\10.0\\bin\\python.exe" eagle2kicad_fix.py <kicad_project_dir>
         [--eagle-sch path.sch] [--eagle-brd path.brd] [--no-pwr-flags] [--dry-run]
  3. Re-open the project, run Update PCB from Schematic (F8), refill zones, ERC/DRC.

What it fixes (each step logged in eaglefix_report.md):
  SCH  power symbols whose Value != hidden pin name (KiCad >=8 takes the net from Value,
       Eagle from the pin name -> nets get merged/split, e.g. GND/GND2)
  SCH  local labels -> global labels (Eagle has a flat, global net namespace; local labels
       become "/NAME" and no longer join the power net of the same name)
  SCH  pin-less symbols (frames, logos) -> not on board / not in BOM
  SCH  missing project symbol library -> rebuilt from the schematic cache + sym-lib-table
  SCH  PWR_FLAG added on nets ERC reports as "power input not driven" (kicad-cli ERC driven)
  PCB  items on UNDEFINED layers -> Dwgs.User (CLI import / unmapped Eagle layers)
  PCB  board-only Eagle items (holes, logos, fiducials w/o schematic) -> board_only attr
  PCB  project footprint library exported from the board, fp-lib-table entry, FPIDs and
       schematic Footprint fields re-linked (KiCad 10 no longer creates it -> F8 fails);
       Eagle-9 managed-library "_<URN>" suffixes stripped; same-name/different-geometry
       packages split into variants
  PRO  Eagle design rules (.brd <designrules>, net classes) -> KiCad board constraints /
       Default net class, so DRC checks the rules the board was designed with
  VERIFY  Eagle netlist (ground truth) vs KiCad schematic netlist and PCB pad nets:
          reports every merged (short) and split (open) net.
"""
import argparse, datetime, glob, json, os, re, shutil, subprocess, sys, time, uuid
import xml.etree.ElementTree as ET
from collections import defaultdict, Counter

# --------------------------------------------------------------------------- S-expressions

class Q(str):
    """Quoted string atom."""
    __slots__ = ()

def sx_parse(text):
    i, n = 0, len(text)
    stack = [[]]
    while i < n:
        c = text[i]
        if c == '(':
            stack.append([]); i += 1
        elif c == ')':
            lst = stack.pop(); stack[-1].append(lst); i += 1
        elif c in ' \t\r\n':
            i += 1
        elif c == '"':
            j = i + 1; buf = []
            while True:
                d = text[j]
                if d == '\\':
                    e = text[j + 1]
                    buf.append({'n': '\n', 't': '\t', 'r': '\r'}.get(e, e)); j += 2
                elif d == '"':
                    break
                else:
                    buf.append(d); j += 1
            stack[-1].append(Q(''.join(buf))); i = j + 1
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n()"':
                j += 1
            stack[-1].append(text[i:j]); i = j
    return stack[0][0]

def _q(s):
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') + '"'

def sx_dump(node, ind=0):
    if not isinstance(node, list):
        return _q(node) if isinstance(node, Q) else node
    if not any(isinstance(x, list) for x in node):
        return '(' + ' '.join(sx_dump(x) for x in node) + ')'
    out = ['('];  first = True
    for x in node:
        if isinstance(x, list):
            out.append('\n' + '\t' * (ind + 1) + sx_dump(x, ind + 1))
        else:
            out.append(('' if first else ' ') + sx_dump(x))
        first = False
    out.append('\n' + '\t' * ind + ')')
    return ''.join(out)

def kids(node, head):
    return [x for x in node if isinstance(x, list) and x and x[0] == head]

def kid(node, head):
    for x in node:
        if isinstance(x, list) and x and x[0] == head:
            return x
    return None

def prop(node, name):
    for p in kids(node, 'property'):
        if len(p) > 2 and p[1] == name:
            return p
    return None

def prop_val(node, name, default=None):
    p = prop(node, name)
    return str(p[2]) if p else default

def new_uuid():
    return Q(str(uuid.uuid4()))

def read_sx(path):
    with open(path, encoding='utf-8') as f:
        return sx_parse(f.read())

def write_sx(path, node):
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(sx_dump(node) + '\n')

# --------------------------------------------------------------------------- reporting

class Report:
    def __init__(self):
        self.lines = []
    def h(self, t):
        print('\n== ' + t); self.lines.append('\n## ' + t + '\n')
    def p(self, t):
        print('  ' + t); self.lines.append('- ' + t)
    def raw(self, t):
        self.lines.append(t)
    def save(self, path):
        with open(path, 'w', encoding='utf-8') as f:
            f.write('# Eagle -> KiCad fix report\n\n' + '\n'.join(self.lines) + '\n')

R = Report()

# --------------------------------------------------------------------------- Eagle ground truth

def _unit(v):
    """Eagle design-rule value '8mil' / '0.2mm' / '0.35' -> mm"""
    m = re.match(r'^\s*([-+0-9.eE]+)\s*([a-z]*)', v or '')
    if not m:
        return None
    x = float(m.group(1)); u = m.group(2)
    return {'mil': x * 0.0254, 'mm': x, 'mic': x / 1000.0, 'inch': x * 25.4, 'in': x * 25.4}.get(u, x)

def eagle_root(path):
    try:
        r = ET.parse(path).getroot()
        return r if r.tag == 'eagle' else None
    except Exception:
        return None

def kref(r):
    """Reference as KiCad's Eagle importer writes it (sch_io_eagle / pcb_io_eagle)."""
    if r and not r[-1].isdigit():
        r += '0'
    if r and r[0].isdigit():
        r = 'UNK' + r
    if r.startswith('#'):
        r = 'UNK' + r
    return r

def eagle_sch_truth(path):
    """net name -> set((part, pad)), from schematic pinrefs mapped through device connects."""
    root = eagle_root(path)
    sch = root.find('drawing/schematic')
    conmap = {}
    for lib in sch.findall('libraries/library'):
        lk = (lib.get('name'), lib.get('urn', ''))
        for ds in lib.findall('devicesets/deviceset'):
            for dev in ds.findall('devices/device'):
                cm = {}
                for c in dev.findall('connects/connect'):
                    cm[(c.get('gate'), c.get('pin'))] = (c.get('pad') or '').split()
                conmap[(lk, ds.get('name'), dev.get('name', ''))] = cm
    parts = {}
    for p in sch.findall('parts/part'):
        parts[p.get('name')] = ((p.get('library'), p.get('library_urn', '')), p.get('deviceset'), p.get('device', ''))
    nets = defaultdict(set)
    for net in sch.iter('net'):
        if net.find('segment') is None:
            continue
        name = net.get('name')
        for pr in net.iter('pinref'):
            key = parts.get(pr.get('part'))
            if not key:
                continue
            cm = conmap.get(key)
            if cm is None:  # try ignoring urn
                cm = next((v for k, v in conmap.items() if k[0][0] == key[0][0] and k[1:] == key[1:]), None)
            for pad in (cm or {}).get((pr.get('gate'), pr.get('pin')), []):
                nets[name].add((kref(pr.get('part')), pad))
    return nets

def eagle_brd_truth(path):
    root = eagle_root(path)
    nets = defaultdict(set)
    for sig in root.iter('signal'):
        for c in sig.findall('contactref'):
            nets[sig.get('name')].add((kref(c.get('element')), c.get('pad')))
    return nets

def eagle_brd_rules(path):
    root = eagle_root(path)
    rules = {}
    dr = root.find('drawing/board/designrules')
    if dr is not None:
        for p in dr.findall('param'):
            rules[p.get('name')] = p.get('value')
    classes = {}
    for c in root.findall('drawing/board/classes/class'):
        classes[c.get('number')] = dict(name=c.get('name'), width=_unit(c.get('width', '0')),
                                        drill=_unit(c.get('drill', '0')),
                                        clearance={x.get('class'): _unit(x.get('value')) for x in c.findall('clearance')})
    return rules, classes

def eagle_brd_net_classes(path):
    """Eagle board signal name -> net class number (only signals not in class 0)."""
    root = eagle_root(path) if path else None
    out = {}
    if root is None:
        return out
    for sig in root.iter('signal'):
        c = sig.get('class', '0')
        if c != '0' and sig.get('name'):
            out[sig.get('name')] = c
    return out

# --------------------------------------------------------------------------- KiCad helpers

def find_kicad_cli(arg):
    cands = [arg] if arg else []
    cands += [shutil.which('kicad-cli') or '']
    exe_dir = os.path.dirname(sys.executable)
    cands += [os.path.join(exe_dir, 'kicad-cli.exe'), os.path.join(exe_dir, 'kicad-cli')]
    cands += sorted(glob.glob(r'C:\Program Files\KiCad\*\bin\kicad-cli.exe'), reverse=True)
    cands += ['/usr/bin/kicad-cli', '/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli']
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None

class CliView:
    """KiCad 10 Eagle import writes one *top-level* sheet per Eagle sheet (.kicad_pro
    "top_level_sheets"). kicad-cli 10.0 only loads the one file it is given, so netlist / ERC /
    DRC-parity would see sheet 1 only. This builds a throw-away copy of the project with a classic
    hierarchy (a wrapper root that references every top-level sheet) for kicad-cli; symbol and
    pin uuids are kept, so results map back to the real files."""

    def __init__(self, d, proj):
        self.d, self.proj, self.tmp = d, proj, None
        self.tls = []
        try:
            with open(os.path.join(d, proj + '.kicad_pro'), encoding='utf-8') as f:
                self.pro = json.load(f)
            self.tls = [t['filename'] for t in self.pro.get('schematic', {}).get('top_level_sheets', [])
                        if os.path.isfile(os.path.join(d, t.get('filename', '')))]
        except Exception:
            self.pro = None
        self.multi = len(self.tls) > 1

    def _setup(self):
        # re-run before every kicad-cli call: the fix steps change the .kicad_pro (design rules),
        # the footprint library and the lib tables
        import tempfile
        if self.tmp is None:
            self.tmp = tempfile.mkdtemp(prefix='eaglefix_view_')
            self.w = new_uuid()
        with open(os.path.join(self.d, self.proj + '.kicad_pro'), encoding='utf-8') as f:
            self.pro = json.load(f)
        for f in os.listdir(self.d):
            p = os.path.join(self.d, f)
            if os.path.isdir(p) and f.endswith('.pretty'):
                shutil.copytree(p, os.path.join(self.tmp, f), dirs_exist_ok=True)
            elif os.path.isfile(p) and not f.endswith(('.kicad_sch', '.kicad_pro', '.kicad_pcb', '.sch', '.brd', '.lck')):
                shutil.copy2(p, self.tmp)
        pro = json.loads(json.dumps(self.pro))
        pro.get('schematic', {}).pop('top_level_sheets', None)
        with open(os.path.join(self.tmp, self.proj + '.kicad_pro'), 'w', encoding='utf-8') as f:
            json.dump(pro, f, indent=2)

    def sch(self, root_sch):
        if not self.multi:
            return root_sch
        self._setup()
        rootname = self.proj + '.kicad_sch'
        ver = '20250114'
        sheets = []
        for f in glob.glob(os.path.join(self.d, '*.kicad_sch')):
            name = os.path.basename(f)
            with open(f, encoding='utf-8') as fh:
                txt = fh.read()
            if name == rootname:
                m = re.search(r'\(version (\d+)\)', txt)
                if m: ver = m.group(1)
            if name in self.tls:
                m = re.search(r'\(path "/([0-9a-fA-F-]{36})', txt)
                su = m.group(1) if m else new_uuid()
                txt = re.sub(r'\(path "/(?=[0-9a-fA-F]{8}-)', f'(path "/{self.w}/', txt)
                vname = '_top_' + name if name == rootname else name
                sheets.append((self.tls.index(name), su, vname))
            else:
                vname = name
            with open(os.path.join(self.tmp, vname), 'w', encoding='utf-8') as fh:
                fh.write(txt)
        sheets.sort()
        out = [f'(kicad_sch (version {ver}) (generator "eeschema") (generator_version "10.0") '
               f'(uuid "{self.w}") (paper "A1") (lib_symbols)']
        for i, (_, su, vname) in enumerate(sheets):
            x, y = 20 + 30 * i, 20
            out.append(
                f'(sheet (at {x} {y}) (size 25.4 10.16) (stroke (width 0.1524) (type solid)) '
                f'(fill (color 0 0 0 0.0000)) (uuid "{su}") '
                f'(property "Sheetname" "S{i + 1}" (at {x} {y - 0.7} 0) (effects (font (size 1.27 1.27)) (justify left bottom))) '
                f'(property "Sheetfile" "{vname}" (at {x} {y + 10.8} 0) (effects (font (size 1.27 1.27)) (justify left top))) '
                f'(instances (project "{self.proj}" (path "/{self.w}" (page "{i + 2}")))))')
        out.append('(sheet_instances (path "/" (page "1"))))')
        root = os.path.join(self.tmp, rootname)
        with open(root, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(out) + '\n')
        return root

    def pcb(self, pcb):
        if not self.multi:
            return pcb
        self.sch(None)
        dst = os.path.join(self.tmp, self.proj + '.kicad_pcb')
        shutil.copy2(pcb, dst)
        return dst

    def close(self):
        if self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)


VIEW = None
GEOM_LOG = []
METRICS = {}
LAST_CMP = {}
CLASH = []
SCH_VAL = {}
COMPARE_LOG = []   # (label, shorts, opens, absent) of every compare_nets call


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    return p.returncode, p.stdout + p.stderr

def kicad_unescape(s):
    for a, b in (('{slash}', '/'), ('{backslash}', '\\'), ('{lt}', '<'), ('{gt}', '>'), ('{colon}', ':'),
                 ('{dblquote}', '"'), ('{quote}', "'"), ('{space}', ' '), ('{tab}', '\t'), ('{return}', '\n')):
        s = s.replace(a, b)
    return s

def norm_net(name):
    """Compare names Eagle-style: drop sheet path prefix and KiCad escaping."""
    n = kicad_unescape(name)
    if n.startswith('/'):
        n = n.rsplit('/', 1)[-1]
    n = re.sub(r'~\{([^}]*)\}', r'!\1', n)  # KiCad overbar -> Eagle !..
    return n.replace('!', '') 

def kicad_sch_netlist(cli, root_sch, tmpdir):
    out = os.path.join(tmpdir, 'eaglefix_netlist.xml')
    if VIEW: root_sch = VIEW.sch(root_sch)
    rc, txt = run([cli, 'sch', 'export', 'netlist', '--format', 'kicadxml', '-o', out, root_sch])
    if rc != 0 or not os.path.isfile(out):
        R.p('netlist export failed: ' + txt.strip()[:300]); return None, None
    root = ET.parse(out).getroot()
    nets = defaultdict(set)
    for n in root.iter('net'):
        for nd in n.findall('node'):
            nets[n.get('name')].add((nd.get('ref'), nd.get('pin')))
    comps = {c.get('ref') for c in root.iter('comp')}
    os.remove(out)
    return nets, comps

def kicad_pcb_padnets(pcb_tree):
    nets = defaultdict(set)
    for fp in kids(pcb_tree, 'footprint'):
        ref = prop_val(fp, 'Reference', '')
        for pad in kids(fp, 'pad'):
            n = kid(pad, 'net')
            if n and len(n) > 1:
                name = str(n[-1]) if isinstance(n[-1], Q) else None
                if name:
                    nets[name].add((ref, str(pad[1])))
    return nets

def compare_nets(truth, test, label, ignore_refs=()):
    """Partition comparison. Returns (#merged, #split)."""
    t_of = {}
    for name, pads in truth.items():
        for pd in pads:
            t_of[pd] = name
    k_of = {}
    for name, pads in test.items():
        for pd in pads:
            k_of[pd] = name
    merged = []
    for kname, pads in test.items():
        tn = {t_of[pd] for pd in pads if pd in t_of}
        if len(tn) > 1:
            merged.append((kname, sorted(tn)))
    split = []
    renamed = 0
    ren = []
    for tname, pads in truth.items():
        kn = {k_of[pd] for pd in pads if pd in k_of}
        if len(kn) > 1:
            split.append((tname, sorted(kn)))
        elif len(kn) == 1 and not tname.startswith('N$'):
            if norm_net(next(iter(kn))) != tname.replace('!', ''):
                renamed += 1
                ren.append((tname, next(iter(kn))))
    missing = sorted({pd for pd in t_of if pd not in k_of and pd[0] not in ignore_refs})
    R.p(f'**{label}**: {len(merged)} merged (short) nets, {len(split)} split (open) nets, '
        f'{renamed} named nets with a different name, {len(missing)} Eagle pads absent')
    for k, t in merged[:40]:
        R.p(f'  SHORT  KiCad net `{k}` joins Eagle nets {t}')
    for t, k in split[:40]:
        R.p(f'  OPEN   Eagle net `{t}` is split into KiCad nets {k}')
    if missing:
        R.p('  absent pads (first 20): ' + ', '.join(f'{a}.{b}' for a, b in missing[:20]))
    if ren:
        R.p('  renamed (Eagle -> KiCad, first 15): ' + ', '.join(f'`{a}`->`{b}`' for a, b in ren[:15]))
    COMPARE_LOG.append((label, len(merged), len(split), len(missing)))
    LAST_CMP[label] = dict(short=len(merged), open=len(split), renamed=renamed, absent=len(missing))
    return len(merged), len(split)

def erc_run(cli, root_sch, tmpdir):
    out = os.path.join(tmpdir, 'eaglefix_erc.json')
    if VIEW: root_sch = VIEW.sch(root_sch)
    rc, txt = run([cli, 'sch', 'erc', '--format', 'json', '--severity-all', '-o', out, root_sch])
    if not os.path.isfile(out):
        R.p('ERC failed: ' + txt.strip()[:300]); return None
    with open(out, encoding='utf-8') as f:
        d = json.load(f)
    os.remove(out)
    return d

def erc_summary(d):
    c = Counter()
    for s in d.get('sheets', []):
        for v in s.get('violations', []):
            c[(v['type'], v['severity'])] += 1
    return c

def drc_run(cli, pcb, tmpdir, parity=True):
    out = os.path.join(tmpdir, 'eaglefix_drc.json')
    if VIEW: pcb = VIEW.pcb(pcb)
    cmd = [cli, 'pcb', 'drc', '--format', 'json', '--severity-all', '--refill-zones', '-o', out]
    if parity and os.path.isfile(os.path.splitext(pcb)[0] + '.kicad_sch'):
        cmd.append('--schematic-parity')
    rc, txt = run(cmd + [pcb])
    if not os.path.isfile(out):
        R.p('DRC failed: ' + txt.strip()[:300]); return None
    with open(out, encoding='utf-8') as f:
        d = json.load(f)
    os.remove(out)
    return d

def drc_summary(d):
    c = Counter((v['type'], v['severity']) for v in d.get('violations', []))
    c[('unconnected_items', 'error')] += len(d.get('unconnected_items', []))
    for v in d.get('schematic_parity', []):
        c[('parity:' + v['type'], v['severity'])] += 1
    return c

def print_counter(title, c):
    R.p(f'{title}: total {sum(c.values())}')
    for (t, s), n in c.most_common():
        R.p(f'  {n:5d}  {s:8s} {t}')

# --------------------------------------------------------------------------- schematic model

class Sheet:
    def __init__(self, path):
        self.path = path
        self.tree = read_sx(path)
        self.dirty = False
        self.libsyms = {}
        ls = kid(self.tree, 'lib_symbols')
        for s in kids(ls, 'symbol') if ls else []:
            self.libsyms[str(s[1])] = s

    def instances(self):
        return kids(self.tree, 'symbol')

    def save(self):
        if self.dirty:
            write_sx(self.path, self.tree)

def lib_pins(libsym):
    pins = []
    def walk(n):
        for x in n:
            if isinstance(x, list) and x:
                if x[0] == 'pin':
                    at = kid(x, 'at'); name = kid(x, 'name'); num = kid(x, 'number')
                    pins.append(dict(type=x[1], x=float(at[1]), y=float(at[2]),
                                     name=str(name[1]) if name else '', number=str(num[1]) if num else '',
                                     node=x))
                elif x[0] == 'symbol':
                    walk(x)
    walk(libsym)
    return pins

def is_power(libsym):
    p = kid(libsym, 'power')
    return p is not None and (len(p) == 1 or 'local' not in p)

def lib_body_up(libsym):
    ys = []
    def walk(n):
        for x in n:
            if isinstance(x, list) and x:
                if x[0] == 'xy':
                    ys.append(float(x[2]))
                elif x[0] != 'property':
                    walk(x)
    walk(libsym)
    return (sum(ys) / len(ys)) > 0 if ys else True

def inst_xform(inst, lx, ly):
    at = kid(inst, 'at'); x0, y0 = float(at[1]), float(at[2]); a = int(float(at[3])) % 360 if len(at) > 3 else 0
    x, y = lx, -ly
    mir = kid(inst, 'mirror')
    if mir and mir[1] == 'x':
        y = -y
    if mir and mir[1] == 'y':
        x = -x
    for _ in range(a // 90):
        x, y = y, -x
    return round(x0 + x, 4), round(y0 + y, 4)

# --------------------------------------------------------------------------- schematic fixes

def eagle_supply_nets(path):
    """Eagle supply-symbol part -> the name of the Eagle net its supply pin sits on.
    Eagle lets a net named VDD_3V3_1_TUNER carry a VDD_3V3 supply symbol (the explicit net name
    wins); in KiCad a power symbol IS a global net named by its Value, so all such branches merge."""
    out = {}
    root = eagle_root(path) if path else None
    if root is None:
        return out
    sch = root.find('drawing/schematic')
    libs = {l.get('name'): l for l in sch.findall('libraries/library')}
    parts = {p.get('name'): p for p in sch.findall('parts/part')}
    sup = {}
    def is_sup(part, gate, pin):
        k = (part, gate, pin)
        if k not in sup:
            sup[k] = False
            p = parts.get(part); lib = libs.get(p.get('library')) if p is not None else None
            ds = lib.find(f"devicesets/deviceset[@name='{p.get('deviceset')}']") if lib is not None else None
            g = ds.find(f"gates/gate[@name='{gate}']") if ds is not None else None
            sym = lib.find(f"symbols/symbol[@name='{g.get('symbol')}']") if g is not None else None
            pe = sym.find(f"pin[@name='{pin}']") if sym is not None else None
            sup[k] = pe is not None and pe.get('direction') == 'sup'
        return sup[k]
    for net in sch.iter('net'):
        for pr in net.iter('pinref'):
            if is_sup(pr.get('part'), pr.get('gate'), pr.get('pin')):
                out[pr.get('part')] = net.get('name')
    return out


def fix_power_values(sheets, esch=None):
    R.h('SCH: power symbol Value vs. pin name / Eagle net name')
    n = 0
    eagle_net = eagle_supply_nets(esch)
    for sh in sheets:
        for inst in sh.instances():
            lid = str(kid(inst, 'lib_id')[1])
            ls = sh.libsyms.get(lid)
            if ls is None or not is_power(ls):
                continue
            pins = lib_pins(ls)
            if len(pins) != 1 or not pins[0]['name']:
                continue
            pname = pins[0]['name']
            ref0 = prop_val(inst, 'Reference', '')
            en = eagle_net.get(ref0.lstrip('#')) or eagle_net.get(ref0)
            if en and en != pname:
                pname = en                       # Eagle net renamed over the supply symbol
            vp = prop(inst, 'Value')
            if vp is not None and str(vp[2]) != pname:
                ref = prop_val(inst, 'Reference', '?')
                R.p(f'{os.path.basename(sh.path)} {ref}: Value `{vp[2]}` -> `{pname}`'
                    + (' (Eagle net name over the supply symbol)' if pname == en else ' (Eagle net = pin name)'))
                vp[2] = Q(pname); sh.dirty = True; n += 1
    R.p(f'{n} power symbols corrected')

def is_bus_name(t):
    return bool(re.search(r'\[\d+\.\.\d+\]|\{.*\}', t))

def fix_local_labels(sheets):
    R.h('SCH: local labels -> global labels (Eagle nets are global)')
    if any(kids(sh.tree, 'sheet') for sh in sheets):
        R.p('hierarchical schematic (Eagle modules) - labels left local, check nets manually')
        return
    hier = set()
    for sh in sheets:
        for hl in kids(sh.tree, 'hierarchical_label'):
            hier.add(str(hl[1]))
    n = 0; names = Counter()
    for sh in sheets:
        for i, node in enumerate(sh.tree):
            if isinstance(node, list) and node and node[0] == 'label':
                text = str(node[1])
                if is_bus_name(text) or text in hier:
                    continue
                at = kid(node, 'at'); eff = kid(node, 'effects')
                new = ['global_label', Q(text), ['shape', 'passive']]
                new += [x for x in node[2:] if not (isinstance(x, list) and x and x[0] in ('fields_autoplaced',))]
                new.append(['property', Q('Intersheetrefs'), Q('${INTERSHEET_REFS}'),
                            ['at', at[1], at[2], '0'],
                            ['effects', ['font', ['size', '1.27', '1.27']], ['hide', 'yes']]])
                sh.tree[i] = new; sh.dirty = True; n += 1; names[text] += 1
    R.p(f'{n} local labels converted ({len(names)} net names)' +
        (': ' + ', '.join(f'{k}' for k, _ in names.most_common(15)) if names else ''))

def _sym_pins(node):
    """all (pin ...) nodes of a lib symbol, including unit sub-symbols"""
    for k in node:
        if isinstance(k, list) and k:
            if k[0] == 'pin':
                yield k
            elif k[0] == 'symbol':
                yield from _sym_pins(k)


def _inst_refs(inst):
    refs = {str(kid(p, 'reference')[1]) for pr in kids(kid(inst, 'instances') or [], 'project')
            for p in kids(pr, 'path') if kid(p, 'reference')}
    r = prop_val(inst, 'Reference')
    if r:
        refs.add(r)
    return refs


def fix_nc_pins(proj_dir, sheets, eagle_truth, dry):
    """Eagle pins with direction="nc" are imported as KiCad 'no_connect' pins. KiCad never connects
    such a pin to anything - but Eagle does, when a net is drawn to it (NAND/DDR 'NC' pins tied to
    GND/VCC or used as data lines on other footprints). Pins that sit on a net in Eagle -> passive."""
    R.h('SCH: Eagle "nc" pins that are wired in Eagle -> passive')
    if not eagle_truth:
        R.p('no Eagle schematic given - skipped'); return
    connected = {pd for pads in eagle_truth.values() for pd in pads}
    change = defaultdict(set)              # lib_id -> pin numbers
    for sh in sheets:
        for inst in sh.instances():
            lid = str(kid(inst, 'lib_id')[1])
            ls = sh.libsyms.get(lid)
            if ls is None:
                continue
            refs = _inst_refs(inst)
            for pin in _sym_pins(ls):
                if len(pin) > 1 and str(pin[1]) == 'no_connect':
                    num = kid(pin, 'number')
                    if num is not None and any((r, str(num[1])) in connected for r in refs):
                        change[lid].add(str(num[1]))
    if not change:
        R.p('none'); return
    n = 0
    for sh in sheets:
        for lid, nums in change.items():
            ls = sh.libsyms.get(lid)
            if ls is None:
                continue
            for pin in _sym_pins(ls):
                num = kid(pin, 'number')
                if str(pin[1]) == 'no_connect' and num is not None and str(num[1]) in nums:
                    pin[1] = 'passive'; sh.dirty = True; n += 1
    for path in glob.glob(os.path.join(proj_dir, '*.kicad_sym')):
        lib = read_sx(path); dirty = False
        for lid, nums in change.items():
            short = lid.split(':', 1)[-1]
            for s in kids(lib, 'symbol'):
                if str(s[1]) == short:
                    for pin in _sym_pins(s):
                        num = kid(pin, 'number')
                        if str(pin[1]) == 'no_connect' and num is not None and str(num[1]) in nums:
                            pin[1] = 'passive'; dirty = True
        if dirty and not dry:
            write_sx(path, lib)
    R.p(f'{n} pins in {len(change)} symbols: ' + ', '.join(f'{k.split(":")[-1]} ({len(v)})' for k, v in sorted(change.items())))


def fix_pinless(sheets):
    R.h('SCH: pin-less symbols (frames/logos) excluded from board/BOM')
    n = 0
    for sh in sheets:
        for inst in sh.instances():
            ls = sh.libsyms.get(str(kid(inst, 'lib_id')[1]))
            if ls is None or lib_pins(ls) or is_power(ls):
                continue
            for key in ('in_bom', 'on_board'):
                k = kid(inst, key)
                if k and k[1] != 'no':
                    k[1] = 'no'; sh.dirty = True
            n += 1
    R.p(f'{n} instances')

def ensure_symbol_lib(proj_dir, proj, sheets, dry):
    R.h('SCH: project symbol library')
    nicks = Counter()
    for sh in sheets:
        for inst in sh.instances():
            lid = str(kid(inst, 'lib_id')[1])
            if ':' in lid:
                nicks[lid.split(':', 1)[0]] += 1
    table_path = os.path.join(proj_dir, 'sym-lib-table')
    table = read_sx(table_path) if os.path.isfile(table_path) else ['sym_lib_table', ['version', '7']]
    have = {str(kid(l, 'name')[1]) for l in kids(table, 'lib')}
    for nick, _ in nicks.most_common():
        if nick in ('power',) or nick in have:
            continue
        if not nick.endswith(('eagle-import', '-eagle-import')) and nick not in (proj,):
            continue
        path = os.path.join(proj_dir, nick + '.kicad_sym')
        if not os.path.isfile(path):
            lib = ['kicad_symbol_lib', ['version', '20251024'], ['generator', Q('eagle2kicad_fix')]]
            seen = set()
            for sh in sheets:
                for name, ls in sh.libsyms.items():
                    if name.startswith(nick + ':'):
                        short = name.split(':', 1)[1]
                        if short in seen:
                            continue
                        seen.add(short)
                        cp = _requote(ls)
                        cp[1] = Q(short)
                        lib.append(cp)
            R.p(f'created `{os.path.basename(path)}` with {len(seen)} symbols from schematic cache')
            if not dry:
                write_sx(path, lib)
        table.append(['lib', ['name', Q(nick)], ['type', Q('KiCad')], ['uri', Q('${KIPRJMOD}/' + nick + '.kicad_sym')],
                      ['options', Q('')], ['descr', Q('Eagle import (eagle2kicad_fix)')]])
        R.p(f'sym-lib-table: added `{nick}`')
        if not dry:
            write_sx(table_path, table)
    if all(n in have for n in nicks if n != 'power'):
        R.p('all symbol libraries referenced by the schematic are in sym-lib-table')

def _requote(n):
    if isinstance(n, list):
        return [_requote(x) for x in n]
    return Q(n) if isinstance(n, Q) else str(n)

# ---- PWR_FLAG

PWR_FLAG_FALLBACK = '''(symbol "power:PWR_FLAG" (power global) (pin_numbers (hide yes)) (pin_names (offset 0) (hide yes))
 (exclude_from_sim no) (in_bom yes) (on_board yes)
 (property "Reference" "#FLG" (at 0 1.905 0) (effects (font (size 1.27 1.27)) (hide yes)))
 (property "Value" "PWR_FLAG" (at 0 3.81 0) (effects (font (size 1.27 1.27))))
 (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
 (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
 (property "Description" "Special symbol for telling ERC where power comes from" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
 (symbol "PWR_FLAG_0_0" (pin power_out line (at 0 0 90) (length 0) (name "" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27))))))
 (symbol "PWR_FLAG_0_1" (polyline (pts (xy 0 0) (xy 0 1.27) (xy -1.016 1.905) (xy 0 2.54) (xy 1.016 1.905) (xy 0 1.27)) (stroke (width 0) (type default)) (fill (type none))))
 (embedded_fonts no))'''

def pwr_flag_libsym(cli):
    if cli:
        base = os.path.dirname(os.path.dirname(cli))
        for p in (os.path.join(base, 'share', 'kicad', 'symbols', 'power.kicad_sym'),
                  '/usr/share/kicad/symbols/power.kicad_sym'):
            if os.path.isfile(p):
                try:
                    lib = read_sx(p)
                    for s in kids(lib, 'symbol'):
                        if s[1] == 'PWR_FLAG':
                            s[1] = Q('power:PWR_FLAG'); return s
                except Exception:
                    pass
    return sx_parse(PWR_FLAG_FALLBACK)

def add_pwr_flags(cli, root_sch, sheets, tmpdir):
    R.h('SCH: PWR_FLAG on undriven power nets (ERC driven)')
    erc = erc_run(cli, root_sch, tmpdir)
    if erc is None:
        return
    # pin uuid / symbol uuid / reference -> (sheet, inst, pin number)
    by_uuid, by_ref = {}, {}
    for sh in sheets:
        for inst in sh.instances():
            u = kid(inst, 'uuid')
            if u: by_uuid[str(u[1])] = (sh, inst, None)
            for pn in kids(inst, 'pin'):
                pu = kid(pn, 'uuid')
                if pu: by_uuid[str(pu[1])] = (sh, inst, str(pn[1]))
            for path in [p for pr in kids(kid(inst, 'instances') or [], 'project') for p in kids(pr, 'path')]:
                r = kid(path, 'reference')
                if r: by_ref[str(r[1])] = (sh, inst)
            r = prop_val(inst, 'Reference')
            if r: by_ref.setdefault(r, (sh, inst))
    power_by_net = defaultdict(list)
    for sh in sheets:
        for inst in sh.instances():
            ls = sh.libsyms.get(str(kid(inst, 'lib_id')[1]))
            if ls is not None and is_power(ls) and not str(kid(inst, 'lib_id')[1]).endswith('PWR_FLAG'):
                power_by_net[prop_val(inst, 'Value', '')].append((sh, inst, ls))
    flg_max = 0
    for r in by_ref:
        m = re.match(r'#FLG0*(\d+)$', r)
        if m: flg_max = max(flg_max, int(m.group(1)))
    netlist, _ = kicad_sch_netlist(cli, root_sch, tmpdir)
    node_net = {}
    for name, nodes in (netlist or {}).items():
        for nd in nodes:
            node_net[nd] = name
    done, manual = set(), []
    libsym = pwr_flag_libsym(cli)
    for s in erc.get('sheets', []):
        for v in s.get('violations', []):
            if v['type'] != 'power_pin_not_driven':
                continue
            net = None; pin_hit = None
            for it in v.get('items', []):
                hit = by_uuid.get(it.get('uuid', ''))
                m = re.search(r'Symbol (\S+) Pin (\S+)', it.get('description', '')) or \
                    re.search(r'[Pp]in (\S+).* of symbol (\S+)', it.get('description', ''))
                if hit:
                    sh, inst, pin = hit
                    ref = prop_val(inst, 'Reference', '')
                elif m:
                    ref, pin = (m.group(1), m.group(2)) if m.re.pattern.startswith('Symbol') else (m.group(2), m.group(1))
                    hit2 = by_ref.get(ref)
                    if not hit2: continue
                    sh, inst = hit2
                else:
                    continue
                ls = sh.libsyms.get(str(kid(inst, 'lib_id')[1]))
                if 'pos' in it:
                    pin_hit = (sh, inst, erc_xy(it['pos'], sh))
                if ls is not None and is_power(ls):
                    net = prop_val(inst, 'Value', '')
                else:
                    net = node_net.get((ref, pin))
                    if net: net = norm_net(net) if net.startswith('/') else net
                if net: break
            if not net or net in done:
                if not net: manual.append(v['items'][0].get('description', '?') if v.get('items') else '?')
                continue
            if re.fullmatch(r'(N\.?C\.?|DNC|N/C)\d*', net, re.I):
                manual.append(f'net `{net}`: a power pin on a "not connected" net - check the symbol pin type')
                done.add(net); continue
            cands = power_by_net.get(net)
            if cands:
                sh, inst, ls = cands[0]
                p = lib_pins(ls)[0]
                px, py = inst_xform(inst, p['x'], p['y'])
                ang = (int(float(kid(inst, 'at')[3])) if len(kid(inst, 'at')) > 3 else 0) + (180 if lib_body_up(ls) else 0)
                mir = kid(inst, 'mirror')
                if mir and mir[1] == 'x': ang += 180
                ang %= 360
            elif pin_hit:
                # no power symbol on this net (e.g. an on-chip regulator output like VDDCORE):
                # put the flag directly on the pin end
                sh, inst, (px, py) = pin_hit
                ang = 0
            else:
                manual.append(f'net `{net}` (no power symbol to attach to)'); continue
            flg_max += 1
            ref = f'#FLG{flg_max:02d}'
            paths = [p2 for pr in kids(kid(inst, 'instances') or [], 'project') for p2 in kids(pr, 'path')]
            proj_name = kids(kid(inst, 'instances') or [], 'project')
            new = ['symbol', ['lib_id', Q('power:PWR_FLAG')], ['at', f'{px:g}', f'{py:g}', str(ang)], ['unit', '1'],
                   ['exclude_from_sim', 'no'], ['in_bom', 'yes'], ['on_board', 'yes'], ['dnp', 'no'],
                   ['uuid', new_uuid()]]
            for nm, val, hide in (('Reference', ref, True), ('Value', 'PWR_FLAG', False), ('Footprint', '', True),
                                  ('Datasheet', '', True)):
                e = ['effects', ['font', ['size', '1.27', '1.27']]]
                if hide: e.append(['hide', 'yes'])
                new.append(['property', Q(nm), Q(val), ['at', f'{px:g}', f'{py:g}', '0'], e])
            new.append(['pin', Q('1'), ['uuid', new_uuid()]])
            if paths:
                inst_block = ['instances']
                for pr in kids(kid(inst, 'instances'), 'project'):
                    npr = ['project', Q(str(pr[1]))]
                    for pth in kids(pr, 'path'):
                        npr.append(['path', Q(str(pth[1])), ['reference', Q(ref)], ['unit', '1']])
                    inst_block.append(npr)
                new.append(inst_block)
            idx = next(i for i, x in enumerate(sh.tree) if x is inst)
            sh.tree.insert(idx + 1, new)
            if 'power:PWR_FLAG' not in sh.libsyms:
                lsn = kid(sh.tree, 'lib_symbols')
                lsn.append(_requote(libsym)); sh.libsyms['power:PWR_FLAG'] = lsn[-1]
            sh.dirty = True; done.add(net)
            R.p(f'{ref} on net `{net}` at ({px:g}, {py:g}) in {os.path.basename(sh.path)} (next to {prop_val(inst, "Reference")})')
    for mm in manual:
        R.p('manual check: ' + mm)
    n_flags = sum(1 for n in done if not re.fullmatch(r'(N\.?C\.?|DNC|N/C)\d*', n, re.I))
    R.p(f'{n_flags} PWR_FLAGs added - review them: a flag means "this net is supplied from off-sheet/connector"')

_PIN_PTS = {}


def sheet_pin_points(sh):
    """Connection points of every symbol pin on a sheet (both mirror/rotate orders), 0.01 mm grid."""
    key = id(sh)
    if key not in _PIN_PTS:
        pts = set()
        for inst in sh.instances():
            ls = sh.libsyms.get(str(kid(inst, 'lib_id')[1]))
            if ls is None:
                continue
            u = kid(inst, 'unit'); unit = int(u[1]) if u else 1
            for pn in _unit_pins(ls, unit):
                for f in (inst_xform, _inst_xform2):
                    x, y = f(inst, pn['x'], pn['y'])
                    pts.add((round(x, 2), round(y, 2)))
        _PIN_PTS[key] = pts
    return _PIN_PTS[key]


def erc_xy(pos, sh=None):
    """kicad-cli ERC JSON position -> schematic mm, snapped to the 0.0254 mm (1 mil) grid.
    The JSON is normally in units of 100 mm, but KiCad 10 reports some sheets of a multi-root
    project (seen: the sub-sheets of a 21-sheet Eagle import) in mm. Positions 100x too large put
    PWR_FLAGs and no-connect flags far outside the page. Pick the scale that lands on a pin of that
    sheet, else the one that fits on a page (<= 1500 mm)."""
    def snap(scale):
        return tuple(round(round(float(pos[k]) * scale / 0.0254) * 0.0254, 4) for k in ('x', 'y'))
    cands = [snap(100), snap(1)]
    if sh is not None:
        pts = sheet_pin_points(sh)
        for c in cands:
            if max(abs(v) for v in c) <= 1500 and (round(c[0], 2), round(c[1], 2)) in pts:
                return c
    return cands[0] if max(abs(v) for v in cands[0]) <= 1500 else cands[1]


def wire_len(w):
    pts = kid(w, 'pts')
    xy = [(float(p[1]), float(p[2])) for p in kids(pts, 'xy')] if pts else []
    if len(xy) != 2:
        return None
    return ((xy[0][0] - xy[1][0]) ** 2 + (xy[0][1] - xy[1][1]) ** 2) ** 0.5


def netlist_partition(nets):
    return sorted(sorted(v) for v in (nets or {}).values() if len(v) > 1)


def _unit_pins(libsym, unit):
    """pins of one unit (sub-symbols NAME_<unit>_<style>; unit 0 = common to all units)"""
    out = []
    for s in kids(libsym, 'symbol'):
        m = re.search(r'_(\d+)_(\d+)$', str(s[1]))
        if m and int(m.group(1)) in (0, unit):
            for p in lib_pins(s):
                out.append(p)
    return out or lib_pins(libsym)


def _inst_xform2(inst, lx, ly):
    """alternative order (mirror applied before the rotation) - used when the first guess lands
    the label somewhere else; the netlist decides which one is right."""
    at = kid(inst, 'at'); x0, y0 = float(at[1]), float(at[2]); a = int(float(at[3])) % 360 if len(at) > 3 else 0
    x, y = lx, -ly
    for _ in range(a // 90):
        x, y = y, -x
    mir = kid(inst, 'mirror')
    if mir and mir[1] == 'x':
        y = -y
    if mir and mir[1] == 'y':
        x = -x
    return round(x0 + x, 4), round(y0 + y, 4)


def restore_net_names(cli, root_sch, sheets, tmpdir, eagle_truth):
    """Eagle nets are named even where no label is drawn (XTALI, SDQ0, ...). KiCad's importer
    only keeps names that come with a label, so these nets become 'Net-(U1-CLK24M_IN)' and after
    F8 the board loses its net names too. Put a global label with the Eagle name on one pin of
    every such net - and check in the netlist that every label landed on the intended net."""
    R.h('SCH: Eagle net names without a label -> label added')
    if not eagle_truth:
        R.p('no Eagle schematic given - skipped'); return
    net, _ = kicad_sch_netlist(cli, root_sch, tmpdir)
    if net is None:
        return
    def index(nl):
        k = {}
        for name, nodes in nl.items():
            for nd in nodes:
                k[nd] = name
        return k
    k_of = index(net)
    inst_by = defaultdict(list)                 # ref -> [(sheet, inst)]
    for sh in sheets:
        for inst in sh.instances():
            for r in _inst_refs(inst):
                inst_by[r].append((sh, inst))
    todo = {}                                   # eagle name -> (text, [(sheet, inst, pin_dict, pad)])
    for ename, pads in sorted(eagle_truth.items()):
        if ename.startswith('N$') or is_bus_name(ename):
            continue
        knames = {k_of.get(pd) for pd in pads if pd in k_of}
        if len(knames) != 1:
            continue
        kname = next(iter(knames))
        if not kname or not (kname.startswith('Net-(') or kname.startswith('unconnected-')):
            continue
        cands = []
        for ref, pin in sorted(pads):
            for sh, inst in inst_by.get(ref, []):
                ls = sh.libsyms.get(str(kid(inst, 'lib_id')[1]))
                if ls is None:
                    continue
                u = kid(inst, 'unit'); unit = int(u[1]) if u else 1
                p = next((q for q in _unit_pins(ls, unit) if q['number'] == pin), None)
                if p is not None:
                    cands.append((sh, inst, p, (ref, pin)))
        text = ('~{' + ename[1:] + '}') if ename.startswith('!') and '!' not in ename[1:] else ename.replace('!', '')
        if cands:
            todo[ename] = (text, cands[:3])
    placed = {}                                 # ename -> label node (+sheet)
    good, bad = [], []
    for attempt, xf in enumerate((inst_xform, _inst_xform2, inst_xform, _inst_xform2, inst_xform, _inst_xform2)):
        cand_i = attempt // 2
        batch = {}
        for ename, (text, cands) in todo.items():
            if ename in placed or cand_i >= len(cands):
                continue
            sh, inst, p, pad = cands[cand_i]
            px, py = xf(inst, p['x'], p['y'])
            lab = ['global_label', Q(text), ['shape', 'passive'], ['at', f'{px:g}', f'{py:g}', '0'],
                   ['fields_autoplaced', 'yes'],
                   ['effects', ['font', ['size', '1.27', '1.27']], ['justify', 'left']],
                   ['uuid', Q(new_uuid())],
                   ['property', Q('Intersheetrefs'), Q('${INTERSHEET_REFS}'), ['at', f'{px:g}', f'{py:g}', '0'],
                    ['effects', ['font', ['size', '1.27', '1.27']], ['hide', 'yes']]]]
            sh.tree.append(lab); sh.dirty = True
            batch[ename] = (sh, lab, pad, text)
        if not batch:
            break
        for sh in sheets: sh.save()
        after, _ = kicad_sch_netlist(cli, root_sch, tmpdir)
        ka = index(after or {})
        ok_part = after is not None and netlist_partition(after) == netlist_partition(net)
        for ename, (sh, lab, pad, text) in batch.items():
            if ok_part and norm_net(ka.get(pad, '')) == norm_net(text):
                placed[ename] = lab
            else:
                sh.tree.remove(lab); sh.dirty = True
        for sh in sheets: sh.save()
    good = sorted(placed)
    bad = sorted(e for e in todo if e not in placed)
    R.p(f'{len(good)} Eagle net names restored with a label (each checked in the netlist)'
        + (': ' + ', '.join(good[:20]) + (' ...' if len(good) > 20 else '') if good else ''))
    if bad:
        R.p('could not place a verified label for: ' + ', '.join(bad[:20]))


def fix_stub_wires(cli, root_sch, sheets, tmpdir, max_len=1.3):
    """Eagle leaves short wire stubs (typ. 4..50 mil) dangling next to pins/junctions ->
    'unconnected wire endpoint' warnings. Delete wires shorter than max_len mm that have a
    dangling end reported by ERC - but only if the netlist stays identical."""
    R.h('SCH: dangling Eagle wire stubs')
    erc = erc_run(cli, root_sch, tmpdir) if cli else None
    dang = set()
    for s_ in (erc or {}).get('sheets', []):
        for v in s_.get('violations', []):
            if v['type'] == 'unconnected_wire_endpoint':
                for it in v.get('items', []):
                    if 'pos' in it:
                        x, y = float(it['pos']['x']) * 100, float(it['pos']['y']) * 100
                        dang.add((round(x, 2), round(y, 2)))
    if not dang:
        R.p('none'); return
    def ends(w):
        pts = kid(w, 'pts')
        return [(round(float(p[1]), 2), round(float(p[2]), 2)) for p in kids(pts, 'xy')] if pts else []
    def near(pt):
        return any(abs(pt[0] - x) <= 0.02 and abs(pt[1] - y) <= 0.02 for x, y in dang)
    before, _ = kicad_sch_netlist(cli, root_sch, tmpdir)
    removed = []
    for sh in sheets:
        keep = []
        for node in sh.tree:
            if isinstance(node, list) and node and node[0] == 'wire':
                L = wire_len(node)
                if L is not None and L < max_len and any(near(p) for p in ends(node)):
                    removed.append((sh, node)); continue
            keep.append(node)
        if len(keep) != len(sh.tree):
            sh.tree[:] = keep; sh.dirty = True
    if not removed:
        R.p(f'{len(dang)} dangling wire ends, none of them on a stub < {max_len} mm'); return
    for sh in sheets: sh.save()
    after, _ = kicad_sch_netlist(cli, root_sch, tmpdir)
    if before is not None and netlist_partition(before) != netlist_partition(after):
        for sh in sheets:              # connectivity changed -> put them back
            sh.tree.extend(n for s2, n in removed if s2 is sh); sh.dirty = True; sh.save()
        R.p(f'{len(removed)} stubs found, but removing them changed the netlist -> left in place')
    else:
        R.p(f'{len(removed)} dangling wire stubs (< {max_len} mm) removed, netlist unchanged'
            + (f'; {len(dang) - len(removed)} other dangling ends left for review' if len(dang) > len(removed) else ''))


def add_no_connects(cli, root_sch, sheets, tmpdir, eagle_truth):
    """Pins that are unconnected in Eagle too get a KiCad no-connect flag (Eagle has no NC marker)."""
    R.h('SCH: no-connect flags on pins that are open in Eagle')
    if not eagle_truth:
        R.p('no Eagle schematic given - skipped'); return
    connected = {pd for pads in eagle_truth.values() for pd in pads}
    erc = erc_run(cli, root_sch, tmpdir)
    if erc is None:
        return
    by_ref = {}
    for sh in sheets:
        for inst in sh.instances():
            r = prop_val(inst, 'Reference')
            if r: by_ref.setdefault(r, sh)
    n, skipped = 0, []
    for s in erc.get('sheets', []):
        for v in s.get('violations', []):
            if v['type'] != 'pin_not_connected':
                continue
            it = v['items'][0]
            m = re.match(r'\S+ (\S+) \S+ (\S+) \[', it.get('description', ''))
            if not m or 'pos' not in it:
                continue
            ref, pin = m.group(1), m.group(2)
            if (ref, pin) in connected:
                skipped.append(f'{ref}.{pin}'); continue
            sh = by_ref.get(ref)
            if not sh:
                continue
            x, y = erc_xy(it['pos'], sh)
            sh.tree.append(['no_connect', ['at', f'{x:g}', f'{y:g}'], ['uuid', new_uuid()]])
            sh.dirty = True; n += 1
    R.p(f'{n} no-connect flags added (pins unconnected in Eagle as well)')
    if skipped:
        R.p('NOT flagged - connected in Eagle but open in KiCad (check!): ' + ', '.join(skipped))


# --------------------------------------------------------------------------- PCB fixes

def _pip(x, y, pts):
    """point in polygon (ray casting)"""
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]; xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-30) + xi:
            inside = not inside
        j = i
    return inside


def _seg_x(a, b, c, d):
    def o(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    return (o(a, b, c) * o(a, b, d) < 0) and (o(c, d, a) * o(c, d, b) < 0)


def _poly_touch(P, Q):
    if (max(x for x, _ in P) < min(x for x, _ in Q) or max(x for x, _ in Q) < min(x for x, _ in P) or
            max(y for _, y in P) < min(y for _, y in Q) or max(y for _, y in Q) < min(y for _, y in P)):
        return False
    if any(_pip(x, y, Q) for x, y in P) or any(_pip(x, y, P) for x, y in Q):
        return True
    for i in range(len(P)):
        for j in range(len(Q)):
            if _seg_x(P[i - 1], P[i], Q[j - 1], Q[j]):
                return True
    return False


def _fp_copper_polys_to_pads(fp):
    """Eagle idiom: a copper polygon inside a package takes the signal of the SMD/pad it touches
    (USB-C shell tabs: big polygons + tiny 0.1 mm 'S1..S4' smds). KiCad imports them as net-less
    graphics -> shorts / clearance errors with the tracks of that net. Each polygon group that
    contains exactly one numbered pad on the same layer becomes a custom pad with that number."""
    at = kid(fp, 'at')
    fpang = float(at[3]) if len(at) > 3 else 0.0
    n = 0
    for layer in ('F.Cu', 'B.Cu'):
        polys = []
        for k in fp:
            if isinstance(k, list) and k and k[0] == 'fp_poly':
                ly = kid(k, 'layer')
                fill = kid(k, 'fill')
                if ly and str(ly[1]) == layer and (fill is None or str(fill[1]) in ('yes', 'solid')):
                    pts = [(float(p[1]), float(p[2])) for p in kids(kid(k, 'pts'), 'xy')]
                    if len(pts) >= 3:
                        polys.append((k, pts))
        if not polys:
            continue
        par = list(range(len(polys)))
        def f(i):
            while par[i] != i:
                par[i] = par[par[i]]; i = par[i]
            return i
        for i in range(len(polys)):
            for j in range(i + 1, len(polys)):
                if f(i) != f(j) and _poly_touch(polys[i][1], polys[j][1]):
                    par[f(i)] = f(j)
        groups = defaultdict(list)
        for i in range(len(polys)):
            groups[f(i)].append(i)
        pads = []
        for p in kids(fp, 'pad'):
            lys = [str(x) for x in (kid(p, 'layers') or [])[1:]]
            if str(p[1]) and (layer in lys or '*.Cu' in lys):
                pa = kid(p, 'at')
                pads.append((p, float(pa[1]), float(pa[2])))
        for idx in groups.values():
            hit = [(p, x, y) for p, x, y in pads if any(_pip(x, y, polys[i][1]) for i in idx)]
            nets = {sx_dump(kid(p, 'net')) if kid(p, 'net') is not None else '' for p, _, _ in hit}
            if not hit or (len({str(p[1]) for p, _, _ in hit}) != 1 and (len(nets) != 1 or nets == {''})):
                # Eagle 'board mounted 0R': a copper RECTANGLE bridging two pads of different nets
                # (the pad centres are outside it). KiCad: net tie between those pads.
                touch = []
                for p, x, y in pads:
                    sz = kid(p, 'size')
                    if sz is None:
                        continue
                    hw, hh = float(sz[1]) / 2, float(sz[2]) / 2
                    rect = [(x - hw, y - hh), (x + hw, y - hh), (x + hw, y + hh), (x - hw, y + hh)]
                    if any(_poly_touch(rect, polys[i][1]) for i in idx):
                        touch.append(p)
                tnets = {sx_dump(kid(p, 'net')) for p in touch if kid(p, 'net') is not None}
                if len(touch) >= 2 and len(tnets) >= 2:
                    nums = sorted({str(p[1]) for p in touch})
                    old = kid(fp, 'net_tie_pad_groups')
                    groups_old = [str(x) for x in old[1:]] if old is not None else []
                    if old is not None:
                        fp.remove(old)
                    node = ['net_tie_pad_groups'] + [Q(g) for g in groups_old] + [Q(', '.join(nums))]
                    at_i = next((i for i, k in enumerate(fp) if isinstance(k, list) and k and k[0] in ('fp_line', 'fp_poly', 'fp_text', 'pad')), len(fp))
                    fp.insert(at_i, node)
                    n += 1
                continue                    # no pad (Eagle: no signal either) or pads of different nets
            p0, lx, ly0 = hit[0]
            prims = ['primitives']
            for i in idx:
                prims.append(['gr_poly', ['pts'] + [['xy', f'{x - lx:.6g}', f'{y - ly0:.6g}'] for x, y in polys[i][1]],
                              ['width', '0'], ['fill', 'yes']])
            new = ['pad', Q(str(p0[1])), 'smd', 'custom', ['at', f'{lx:.6g}', f'{ly0:.6g}', f'{fpang:g}'],
                   ['size', '0.1', '0.1'], ['layers', Q(layer)]]
            if kid(p0, 'net') is not None:
                new.append(kid(p0, 'net'))
            new += [['zone_connect', '2'], ['options', ['clearance', 'outline'], ['anchor', 'circle']], prims,
                    ['uuid', Q(new_uuid())]]
            for i in idx:
                fp.remove(polys[i][0])
            last_pad = max(i for i, k in enumerate(fp) if isinstance(k, list) and k and k[0] == 'pad')
            fp.insert(last_pad + 1, new)
            n += 1
    return n


def _npth_no_copper(fp):
    """Eagle <hole>: KiCad's importer gives the NPTH pad a copper size > drill on *.Cu, i.e. a
    net-less copper ring -> 'shorting items' with every track/pad it touches. Size := drill."""
    n = 0
    for p in kids(fp, 'pad'):
        if len(p) > 2 and str(p[2]) == 'np_thru_hole':
            dr = kid(p, 'drill'); sz = kid(p, 'size')
            if dr is None or sz is None:
                continue
            vals = [x for x in dr[1:] if not isinstance(x, list) and str(x) != 'oval']
            if not vals:
                continue
            dx = float(vals[0]); dy = float(vals[1]) if len(vals) > 1 else dx
            if float(sz[1]) > dx + 1e-4 or float(sz[2]) > dy + 1e-4:
                sz[1], sz[2] = f'{dx:g}', f'{dy:g}'
                n += 1
    return n


def _fp_net_ties(fp):
    """Eagle 'board mounted' parts (0R resistor arrays, solder jumpers...) are copper wires inside
    the package between two pads of different signals. KiCad: footprint net tie."""
    pads = []
    for p in kids(fp, 'pad'):
        num = str(p[1])
        if not num:
            continue
        pa = kid(p, 'at'); sz = kid(p, 'size')
        lys = [str(x) for x in (kid(p, 'layers') or [])[1:]]
        if pa is None or sz is None:
            continue
        r = max(float(sz[1]), float(sz[2])) / 2
        pads.append((num, float(pa[1]), float(pa[2]), r, lys))
    par = {}
    def f(x):
        while par.setdefault(x, x) != x:
            x = par[x]
        return x
    linked = False
    for k in fp:
        if not (isinstance(k, list) and k and k[0] == 'fp_line'):
            continue
        ly = kid(k, 'layer')
        if ly is None or str(ly[1]) not in ('F.Cu', 'B.Cu'):
            continue
        st, en = kid(k, 'start'), kid(k, 'end')
        if st is None or en is None:
            continue
        hits = []
        for xy in ((float(st[1]), float(st[2])), (float(en[1]), float(en[2]))):
            for num, x, y, r, lys in pads:
                if (str(ly[1]) in lys or '*.Cu' in lys) and (xy[0] - x) ** 2 + (xy[1] - y) ** 2 <= (r + 0.05) ** 2:
                    hits.append(num); break
        if len(hits) == 2 and hits[0] != hits[1]:
            par[f(hits[0])] = f(hits[1]); linked = True
    if not linked:
        return 0
    old = kid(fp, 'net_tie_pad_groups')
    if old is not None:                      # keep groups added earlier (copper rectangles)
        for g in old[1:]:
            nums = [x.strip() for x in str(g).split(',') if x.strip()]
            for a_ in nums[1:]:
                par[f(nums[0])] = f(a_)
        fp.remove(old)
    groups = defaultdict(list)
    for x in list(par):
        groups[f(x)].append(x)
    groups = [sorted(g) for g in groups.values() if len(g) > 1]
    node = ['net_tie_pad_groups'] + [Q(', '.join(g)) for g in sorted(groups)]
    idx = next((i for i, k in enumerate(fp) if isinstance(k, list) and k and k[0] in ('fp_line', 'fp_poly', 'fp_text', 'pad')), len(fp))
    fp.insert(idx, node)
    return len(groups)


def _via_restring(tree, rules):
    """Eagle draws a via with max(diameter, drill + 2*restring), restring = clamp(rvViaOuter*drill,
    rlMinViaOuter, rlMaxViaOuter). KiCad's importer keeps a smaller copper diameter -> annular
    width errors and a board that differs from Eagle's CAM output."""
    g = lambda k, d=None: _unit(rules.get(k)) if rules.get(k) else d
    rv = float(rules.get('rvViaOuter', '0.25') or 0.25)
    lo, hi = g('rlMinViaOuter', 0.1016), g('rlMaxViaOuter', 0.508)
    n = 0
    for v in kids(tree, 'via'):
        if 'micro' in v or 'blind' in v:
            continue
        sz, dr = kid(v, 'size'), kid(v, 'drill')
        if sz is None or dr is None:
            continue
        d = float(dr[1])
        need = round(d + 2 * min(max(d * rv, lo), hi), 4)
        if float(sz[1]) < need - 1e-4:
            sz[1] = f'{need:g}'; n += 1
    return n


def eagle_dru_rules(proj_dir, proj, brd, dry):
    """Eagle's clearance matrix (wire/pad/via/smd pairs) -> KiCad custom rules (.kicad_dru)."""
    rules, _ = eagle_brd_rules(brd)
    g = lambda k: _unit(rules.get(k)) if rules.get(k) else None
    base = g('mdWireWire')
    if not base:
        return
    pairs = [('mdWirePad', "A.Type == 'Track' && B.Type == 'Pad' && B.Pad_Type == 'Through-hole'"),
             ('mdWireVia', "A.Type == 'Track' && B.Type == 'Via'"),
             ('mdPadPad', "A.Pad_Type == 'Through-hole' && B.Pad_Type == 'Through-hole'"),
             ('mdPadVia', "A.Pad_Type == 'Through-hole' && B.Type == 'Via'"),
             ('mdViaVia', "A.Type == 'Via' && B.Type == 'Via'"),
             ('mdSmdPad', "A.Pad_Type == 'SMD' && B.Pad_Type == 'Through-hole'"),
             ('mdSmdVia', "A.Pad_Type == 'SMD' && B.Type == 'Via'"),
             ('mdSmdSmd', "A.Pad_Type == 'SMD' && B.Pad_Type == 'SMD'")]
    body = []
    for k, cond in pairs:
        v = g(k)
        if v is not None and abs(v - base) > 1e-4:
            body.append(f'(rule "eagle_{k}"\n  (condition "{cond}")\n  (constraint clearance (min {v:g}mm)))')
    if g('mdWirePad') is not None and abs(g('mdWirePad') - base) > 1e-4:
        body.append(f'(rule "eagle_mdWireSmd"\n  (condition "A.Type == \'Track\' && B.Pad_Type == \'SMD\'")\n'
                    f'  (constraint clearance (min {g("mdWirePad"):g}mm)))')
    path = os.path.join(proj_dir, proj + '.kicad_dru')
    old = open(path, encoding='utf-8').read() if os.path.isfile(path) else '(version 1)\n'
    old = re.sub(r'\n?# eagle2kicad_fix begin.*?# eagle2kicad_fix end\n?', '\n', old, flags=re.S)
    if not old.lstrip().startswith('(version'):
        old = '(version 1)\n' + old
    if body:
        old = old.rstrip() + '\n# eagle2kicad_fix begin (Eagle clearance matrix)\n' + '\n'.join(body) + '\n# eagle2kicad_fix end\n'
    R.p(f'{len(body)} Eagle pair clearances written as custom rules to `{os.path.basename(path)}`'
        + (': ' + ', '.join(k for k, _ in pairs if g(k) is not None and abs(g(k) - base) > 1e-4) if body else ''))
    if not dry:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(old)


def _eagle_wire_len(w):
    import math
    x1, y1, x2, y2 = (float(w.get(k)) for k in ('x1', 'y1', 'x2', 'y2'))
    c = float(w.get('curve', '0') or 0)
    chord = math.hypot(x2 - x1, y2 - y1)
    if abs(c) < 1e-6 or chord == 0:
        return chord
    a = math.radians(abs(c))
    return chord / (2 * math.sin(a / 2)) * a


def verify_geometry(pcb_path, ebrd):
    """Independent geometry check of the KiCad board against the Eagle .brd (ground truth):
    part placement / side / rotation, copper length per net, via count + drill per net,
    copper polygons per net, board outline length, drill histogram."""
    import math
    try:
        import pcbnew
    except ImportError:
        R.p('geometry check: pcbnew module not available - skipped'); return
    root = eagle_root(ebrd)
    brd = root.find('drawing/board')
    board = pcbnew.LoadBoard(pcb_path)
    fps = {fp.GetReference(): fp for fp in board.GetFootprints()}
    # offset (importer re-centres the board)
    els = brd.findall('elements/element')
    offs = sorted((round(fps[kref(e.get('name'))].GetPosition().x / 1e6 - float(e.get('x')), 3),
                   round(fps[kref(e.get('name'))].GetPosition().y / 1e6 + float(e.get('y')), 3))
                  for e in els if kref(e.get('name')) in fps)
    if not offs:
        R.p('geometry check: no matching parts - skipped'); return
    ox, oy = Counter(offs).most_common(1)[0][0]
    issues = []
    # 1. placement
    seen = set()
    n_pos = n_side = n_rot = n_missp = 0
    for e in els:
        r = kref(e.get('name')); fp = fps.get(r)
        if fp is None:
            n_missp += 1
            issues.append(f'part `{r}` missing on the KiCad board'); continue
        seen.add(r)
        p = fp.GetPosition()
        dx, dy = p.x / 1e6 - (float(e.get('x')) + ox), p.y / 1e6 - (-float(e.get('y')) + oy)
        if math.hypot(dx, dy) > 0.01:
            n_pos += 1
            if n_pos <= 10: issues.append(f'part `{r}` moved by ({dx:+.3f}, {dy:+.3f}) mm')
        mir, ang = _eagle_rot(e.get('rot'))
        if mir != fp.IsFlipped():
            n_side += 1
            if n_side <= 10: issues.append(f'part `{r}` on the wrong side')
        ka = fp.GetOrientationDegrees() % 360
        cands = {ang % 360, (-ang) % 360, (180 - ang) % 360, (180 + ang) % 360} if mir else {ang % 360}
        if not any(min(abs(ka - c), 360 - abs(ka - c)) < 0.05 for c in cands):
            n_rot += 1
            if n_rot <= 10: issues.append(f'part `{r}` rotation {ka:g} deg, Eagle {e.get("rot") or "R0"}')
    extra = [r for r, fp in fps.items() if r not in seen and not fp.IsBoardOnly() and not r.startswith(('UNK', 'REF'))]
    # 1b. every pad: absolute position / size / drill from the Eagle package + element transform.
    # Catches footprints that sit right but are rotated 180 deg or have swapped pad NUMBERS
    # (the copper overlaps perfectly, only the numbering is wrong - netlist+DRC stay clean).
    pk = {}
    for lib in brd.findall('libraries/library'):
        for p in lib.findall('packages/package'):
            pk[(lib.get('name'), lib.get('urn', ''), p.get('name'))] = p
    n_pad = n_padpos = n_padsize = n_paddrill = n_padmiss = 0
    for e in els:
        fp = fps.get(kref(e.get('name')))
        if fp is None:
            continue
        key = (e.get('library'), e.get('library_urn', ''), e.get('package'))
        pkg = pk.get(key) or next((v for k, v in pk.items() if k[0] == key[0] and k[2] == key[2]), None)
        if pkg is None:
            continue
        mir, ang = _eagle_rot(e.get('rot'))
        ca, sa = math.cos(math.radians(ang)), math.sin(math.radians(ang))
        ex, ey = float(e.get('x')), float(e.get('y'))
        kp = defaultdict(list)
        for p in fp.Pads():
            kp[str(p.GetNumber())].append(p)
        for q in pkg:
            if q.tag not in ('pad', 'smd'):
                continue
            n_pad += 1
            x, y = float(q.get('x')), float(q.get('y'))
            rx, ry = x * ca - y * sa, x * sa + y * ca      # Eagle: rotate, then mirror (MR270 != mirror+R270)
            if mir:
                rx = -rx
            gx, gy = ex + rx, ey + ry
            kx, ky = gx + ox, -gy + oy
            cands = kp.get(q.get('name'), [])
            if not cands:
                n_padmiss += 1
                if n_padmiss <= 5: issues.append(f'pad `{kref(e.get("name"))}.{q.get("name")}` missing in KiCad')
                continue
            if q.tag == 'pad':          # THT: prefer the drilled pad (a custom SMD pad may share the number)
                cands = [p for p in cands if p.GetDrillSize().x > 0] or cands
            best = min(cands, key=lambda p: math.hypot(p.GetPosition().x / 1e6 - kx, p.GetPosition().y / 1e6 - ky))
            d = math.hypot(best.GetPosition().x / 1e6 - kx, best.GetPosition().y / 1e6 - ky)
            if d > 0.005:
                n_padpos += 1
                if n_padpos <= 10:
                    hit = [str(p.GetNumber()) for p in fp.Pads()
                           if math.hypot(p.GetPosition().x / 1e6 - kx, p.GetPosition().y / 1e6 - ky) < 0.005]
                    issues.append(f'pad `{kref(e.get("name"))}.{q.get("name")}` is {d:.3f} mm off'
                                  + (f' - KiCad has pad {hit} at that spot (numbering swapped?)' if hit else ''))
            if q.tag == 'smd':
                es = sorted((float(q.get('dx')), float(q.get('dy'))))
                # several pads may share the number at this spot (package copper polygon merged in
                # as an extra custom pad, see _fp_copper_polys_to_pads): the Eagle smd must exist
                # among them with its own size
                here = [p for p in cands
                        if math.hypot(p.GetPosition().x / 1e6 - kx, p.GetPosition().y / 1e6 - ky) <= 0.005] or [best]
                def _ks(p):
                    try:
                        sz = p.GetSize(pcbnew.F_Cu)
                    except Exception:
                        sz = p.GetSize()
                    return sorted((sz.x / 1e6, sz.y / 1e6))
                ks = min((_ks(p) for p in here), key=lambda k: abs(es[0] - k[0]) + abs(es[1] - k[1]))
                if abs(es[0] - ks[0]) > 0.005 or abs(es[1] - ks[1]) > 0.005:
                    n_padsize += 1
                    if n_padsize <= 5: issues.append(f'smd `{kref(e.get("name"))}.{q.get("name")}` size Eagle {es}, KiCad {[round(v, 4) for v in ks]}')
            else:
                dr = float(q.get('drill'))
                if abs(best.GetDrillSize().x / 1e6 - dr) > 0.005:
                    n_paddrill += 1
                    if n_paddrill <= 5: issues.append(f'pad `{kref(e.get("name"))}.{q.get("name")}` drill Eagle {dr}, KiCad {best.GetDrillSize().x / 1e6:g}')
    R.p(f'**Eagle pads vs KiCad pads**: {n_pad} pads, {n_padpos} misplaced, {n_padmiss} missing, '
        f'{n_padsize} SMD size, {n_paddrill} drill differences')
    # 1c. values
    n_val = 0
    for e in els:
        fp = fps.get(kref(e.get('name')))
        # an EMPTY Eagle value: KiCad needs a Value and the importer fills in the device name -> not a difference
        if fp is not None and (e.get('value') or '').strip() and fp.GetValue().strip() != e.get('value').strip():
            n_val += 1
            if n_val <= 5: issues.append(f'part `{kref(e.get("name"))}` value Eagle `{e.get("value")}`, KiCad `{fp.GetValue()}`')
    # 2. copper per net
    e_len, e_via, e_poly = Counter(), defaultdict(Counter), Counter()
    for sig in brd.findall('signals/signal'):
        n = sig.get('name')
        for w in sig.findall('wire'):
            if w.get('layer') and 1 <= int(w.get('layer')) <= 16:
                e_len[n] += _eagle_wire_len(w)
        for v in sig.findall('via'):
            e_via[n][round(float(v.get('drill')), 3)] += 1
        e_poly[n] += sum(1 for pg in sig.findall('polygon') if pg.get('layer') and 1 <= int(pg.get('layer')) <= 16)
    k_len, k_via, k_poly = Counter(), defaultdict(Counter), Counter()
    for t in board.GetTracks():
        n = t.GetNetname()
        if t.GetClass() == 'PCB_VIA':
            k_via[n][round(t.GetDrillValue() / 1e6, 3)] += 1
        else:
            k_len[n] += t.GetLength() / 1e6
    for z in board.Zones():
        if not z.GetIsRuleArea():
            k_poly[z.GetNetname()] += z.GetLayerSet().count() if hasattr(z.GetLayerSet(), 'count') else 1
    # KiCad net names may carry '/' or ~{} escapes -> compare on norm_net
    kn = {}
    for n in set(k_len) | set(k_via) | set(k_poly):
        kn.setdefault(norm_net(n), n)
    n_len = n_via = n_poly = 0
    for n in sorted(set(e_len) | set(e_via)):
        k = kn.get(n.replace('!', ''), kn.get(n))
        el, kl = e_len.get(n, 0), k_len.get(k, 0) if k else 0
        if abs(el - kl) > max(0.01, 0.002 * el):
            n_len += 1
            if n_len <= 10: issues.append(f'net `{n}` copper length Eagle {el:.3f} mm, KiCad {kl:.3f} mm')
        ev, kv = e_via.get(n, Counter()), k_via.get(k, Counter()) if k else Counter()
        if ev != kv:
            n_via += 1
            if n_via <= 10: issues.append(f'net `{n}` vias Eagle {dict(ev)}, KiCad {dict(kv)}')
        if e_poly.get(n, 0) and not (k_poly.get(k, 0) if k else 0):
            n_poly += 1
            if n_poly <= 10: issues.append(f'net `{n}` has {e_poly[n]} Eagle polygon(s), no KiCad zone')
    # 3. outline
    e_out = sum(_eagle_wire_len(w) for w in brd.findall('plain/wire') if w.get('layer') == '20')
    e_out += sum(2 * math.pi * float(c.get('radius')) for c in brd.findall('plain/circle') if c.get('layer') == '20')
    # outline drawn inside packages (enclosure packages): counts too, once per placed element
    pk20 = {}
    for lib in brd.findall('libraries/library'):
        for p in lib.findall('packages/package'):
            pk20[(lib.get('name'), p.get('name'))] = (
                sum(_eagle_wire_len(w) for w in p.findall('wire') if w.get('layer') == '20')
                + sum(2 * math.pi * float(c.get('radius')) for c in p.findall('circle') if c.get('layer') == '20'))
    e_out += sum(pk20.get((e.get('library'), e.get('package')), 0) for e in els)
    def _klen(d):
        # EDA_SHAPE::GetLength() asserts (and aborts kicad-cli python) for circles/rectangles
        sh = d.GetShape() if hasattr(d, 'GetShape') else None
        if sh == getattr(pcbnew, 'SHAPE_T_CIRCLE', -1):
            return 2 * math.pi * d.GetRadius() / 1e6
        if sh == getattr(pcbnew, 'SHAPE_T_RECTANGLE', getattr(pcbnew, 'SHAPE_T_RECT', -1)):
            return 2 * (abs(d.GetEnd().x - d.GetStart().x) + abs(d.GetEnd().y - d.GetStart().y)) / 1e6
        if sh == getattr(pcbnew, 'SHAPE_T_POLY', -1):
            return 0.0
        return d.GetLength() / 1e6
    # Eagle milling (plain layer 46) becomes Edge.Cuts contours via add_milling(): those are NOT
    # outline - classify every KiCad Edge.Cuts shape by its distance to the Eagle milling paths
    mill_paths, mill_circles = [], []
    for w in brd.findall('plain/wire'):
        if w.get('layer') != '46':
            continue
        x1, y1, x2, y2 = (float(w.get(k)) for k in ('x1', 'y1', 'x2', 'y2'))
        pts = [(x1, y1), (x2, y2)]
        c = float(w.get('curve', '0') or 0)
        if abs(c) > 1e-6:
            def _sub(a, b, cv, depth):
                if depth == 0:
                    return [a, b]
                m = _eagle_arc_mid(a[0], a[1], b[0], b[1], cv)
                return _sub(a, m, cv / 2, depth - 1)[:-1] + _sub(m, b, cv / 2, depth - 1)
            pts = _sub((x1, y1), (x2, y2), c, 4)
        mill_paths.append(([(x + ox, -y + oy) for x, y in pts], float(w.get('width', '0') or 0) / 2))
    for ci in brd.findall('plain/circle'):
        if ci.get('layer') == '46':
            mill_circles.append((float(ci.get('x')) + ox, -float(ci.get('y')) + oy))
    def _dseg(px, py, a, b):
        dx, dy = b[0] - a[0], b[1] - a[1]
        L2 = dx * dx + dy * dy
        t = 0 if L2 == 0 else max(0, min(1, ((px - a[0]) * dx + (py - a[1]) * dy) / L2))
        return math.hypot(px - a[0] - t * dx, py - a[1] - t * dy)
    def _is_mill(g):
        sh = g.GetShape()
        if sh == getattr(pcbnew, 'SHAPE_T_CIRCLE', -1):
            cx, cy = g.GetCenter().x / 1e6, g.GetCenter().y / 1e6
            return any(math.hypot(cx - x, cy - y) < 0.05 for x, y in mill_circles)
        if sh == getattr(pcbnew, 'SHAPE_T_ARC', -1):
            m = g.GetArcMid(); px, py = m.x / 1e6, m.y / 1e6
        elif sh == getattr(pcbnew, 'SHAPE_T_SEGMENT', -1):
            px, py = (g.GetStart().x + g.GetEnd().x) / 2e6, (g.GetStart().y + g.GetEnd().y) / 2e6
        else:
            return False
        return any(min(_dseg(px, py, pts[i], pts[i + 1]) for i in range(len(pts) - 1)) <= hw + 0.03
                   for pts, hw in mill_paths)
    edge_shapes = [d for d in board.GetDrawings() if d.GetLayer() == pcbnew.Edge_Cuts and hasattr(d, 'GetLength')]
    # footprint Edge.Cuts: outline only where the Eagle package draws layer 20; a package with only
    # milling (layer 46, e.g. USB-C shell slots) contributes cut-outs, not outline
    has20 = {kref(e.get('name')) for e in els if pk20.get((e.get('library'), e.get('package')), 0) > 0}
    edge_shapes += [g for fp in board.GetFootprints() if fp.GetReference() in has20 for g in fp.GraphicalItems()
                    if g.GetLayer() == pcbnew.Edge_Cuts and hasattr(g, 'GetLength')]
    n_mill = sum(1 for g in edge_shapes if _is_mill(g))
    k_out = sum(_klen(g) for g in edge_shapes if not _is_mill(g))
    if (mill_paths or mill_circles) and not n_mill:
        issues.append(f'Eagle milling (layer 46, {len(mill_paths) + len(mill_circles)} items) has no Edge.Cuts counterpart')
    outline_bad = bool(e_out and abs(e_out - k_out) > max(0.05, 0.002 * e_out))
    if outline_bad:
        issues.append(f'board outline length Eagle {e_out:.2f} mm, KiCad {k_out:.2f} mm')
    # the KiCad outline must also be a VALID closed board polygon (zone fill and fab depend on it)
    try:
        ps_ = pcbnew.SHAPE_POLY_SET()
        outline_ok = bool(board.GetBoardPolygonOutlines(ps_, False)) and ps_.OutlineCount() > 0
    except Exception:
        outline_ok = False
    outline_invalid = int(bool(e_out) and not outline_ok)
    if outline_invalid:
        issues.append('KiCad board outline is not a valid closed polygon (DRC invalid_outline)')
    # 4. copper connectivity WITH filled pours vs the Eagle file's own airwires (layer 19):
    #    KiCad must not be less connected than the Eagle source (catches pours that do not fill)
    e_air = sum(1 for s_ in brd.findall('signals/signal') for w in s_.findall('wire') if w.get('layer') == '19')
    k_unc = None
    try:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
        board.BuildConnectivity()
        k_unc = board.GetConnectivity().GetUnconnectedCount(True)
    except Exception as ex:
        issues.append(f'zone fill / connectivity failed: {ex}')
    # a track that lands on the 'other' pad of a closed (printed) solder jumper / net tie is copper-
    # continuous through the footprint's own copper (Eagle accepts it), KiCad counts it unconnected
    tie_x = 0
    try:
        trk = [t for t in board.GetTracks() if t.GetClass() != 'PCB_VIA']
        # GetNetTiePadGroups() is not iterable from Python (unwrapped std::vector) -> read the file
        tie_groups = {}
        for fpx in kids(read_sx(pcb_path), 'footprint'):
            g_ = kid(fpx, 'net_tie_pad_groups')
            if g_ is not None:
                tie_groups[prop_val(fpx, 'Reference', '')] = [str(x) for x in g_[1:]]
        for fp in board.GetFootprints():
            groups = tie_groups.get(fp.GetReference(), [])
            for grp in groups:
                nums = {n.strip() for n in str(grp).split(',')}
                gp = [p for p in fp.Pads() if str(p.GetNumber()) in nums]
                gnets = {p.GetNetname() for p in gp}
                for p in gp:
                    for t in trk:
                        if t.GetNetname() in gnets and t.GetNetname() != p.GetNetname() and t.GetLayer() in (pcbnew.F_Cu, pcbnew.B_Cu) \
                                and p.IsOnLayer(t.GetLayer()) \
                                and p.GetEffectiveShape(t.GetLayer()).Collide(t.GetEffectiveShape(t.GetLayer()), 0):
                            tie_x += 1
    except Exception as ex:
        issues.append(f'net-tie crossing check failed: {ex}')
    unrouted_bad = int(k_unc is not None and k_unc > e_air + tie_x)
    if unrouted_bad:
        issues.append(f'unrouted connections after zone fill: KiCad {k_unc}, Eagle airwires {e_air}'
                      + (f', tracks crossing a net tie {tie_x}' if tie_x else ''))
    # 5. copper layer count: the KiCad stack must be what the Eagle board USES (not what its
    #    design-rule layerSetup declares); every enabled copper layer must carry copper
    e_cu = 2 + len(eagle_inner_layers(brd))
    k_cu = board.GetCopperLayerCount()
    layers_bad = 0
    try:
        cnt = _kicad_layer_items(pcbnew, board)
        empty_cu = [board.GetLayerName(l) for l in board.GetEnabledLayers().CuStack()
                    if l not in (pcbnew.F_Cu, pcbnew.B_Cu) and not cnt.get(l)]
    except Exception as ex:
        empty_cu = []; issues.append(f'layer check failed: {ex}')
    if k_cu != e_cu:
        layers_bad += 1
        issues.append(f'copper layer count: Eagle board uses {e_cu}, KiCad project has {k_cu} (fab would quote {k_cu} layers)')
    if empty_cu:
        layers_bad += 1
        issues.append('copper layer(s) with no copper on them: ' + ', '.join(empty_cu))
    tot_v = sum(sum(c.values()) for c in e_via.values()), sum(sum(c.values()) for c in k_via.values())
    R.p(f'**Eagle board vs KiCad geometry**: {len(els)} parts ({n_pos} moved, {n_side} wrong side, '
        f'{n_rot} rotated, {n_val} values differ, {len(extra)} extra on KiCad side), copper length differs on {n_len} nets, '
        f'via count/drill differs on {n_via} nets (vias {tot_v[0]} vs {tot_v[1]}), '
        f'{n_poly} nets lost their polygon, outline {e_out:.1f} vs {k_out:.1f} mm, '
        f'unrouted after zone fill {k_unc} (Eagle airwires {e_air}, through net ties {tie_x}), '
        f'{n_mill} Edge.Cuts shapes from Eagle milling, copper layers {e_cu} vs {k_cu}')
    for s_ in issues[:60]:
        R.p('  ' + s_)
    GEOM_LOG.append(dict(moved=n_pos, side=n_side, rot=n_rot, val=n_val, padpos=n_padpos, padmiss=n_padmiss,
                         padsize=n_padsize, drill=n_paddrill, length=n_len, via=n_via, poly=n_poly,
                         missing_parts=n_missp, extra=len(extra), outline=int(outline_bad), outline_invalid=outline_invalid, unrouted=unrouted_bad,
                         layers=layers_bad))
    if extra:
        R.p('  extra KiCad footprints: ' + ', '.join(extra[:20]))


CU_LAYERS = {str(i) for i in range(1, 17)}


def eagle_copper_layers(brd):
    """Copper layers the Eagle board actually USES (items drawn on them, via extents), as a set of
    Eagle layer numbers 1..16. Top and bottom always count. The design-rule 'layerSetup' string is
    deliberately NOT trusted: Olimex's 2-layer iMX233-Micro carries a 4-layer setup (1+2*15+16)
    with nothing on 2/15, and KiCad's importer turns that into a 4-layer board."""
    used = {'1', '16'}
    placed = {(e.get('library'), e.get('package')) for e in brd.findall('elements/element')}
    scopes = [brd.find('plain'), brd.find('signals')] + \
             [p for lib in brd.findall('libraries/library') for p in lib.findall('packages/package')
              if (lib.get('name'), p.get('name')) in placed]
    for sc in scopes:
        if sc is None:
            continue
        for el in sc.iter():
            if el.tag in ('wire', 'polygon', 'rectangle', 'circle', 'smd', 'text') and el.get('layer') in CU_LAYERS:
                used.add(el.get('layer'))
            elif el.tag == 'via' and el.get('extent'):
                a, b = el.get('extent').split('-')
                used.update((a, b))
    return used


def eagle_inner_layers(brd):
    return {int(l) for l in eagle_copper_layers(brd)} - {1, 16}


def _kicad_layer_items(pcbnew, board):
    """Count of items per copper layer id on the KiCad board (tracks, vias by type, zones, footprint
    pads/graphics). Through-hole pads and through vias are not counted: they say nothing about
    whether an inner layer is needed."""
    cnt = Counter()
    for t in board.GetTracks():
        if t.GetClass() == 'PCB_VIA':
            if t.GetViaType() != pcbnew.VIATYPE_THROUGH:
                cnt[t.TopLayer()] += 1; cnt[t.BottomLayer()] += 1
        else:
            cnt[t.GetLayer()] += 1
    for z in board.Zones():
        for l in z.GetLayerSet().CuStack():
            cnt[l] += 1
    for fp in board.GetFootprints():
        for g in fp.GraphicalItems():
            if board.IsLayerEnabled(g.GetLayer()) and pcbnew.IsCopperLayer(g.GetLayer()):
                cnt[g.GetLayer()] += 1
        for p in fp.Pads():
            if p.GetAttribute() == pcbnew.PAD_ATTRIB_SMD:
                cnt[p.GetLayer()] += 1
    for d_ in board.GetDrawings():
        if pcbnew.IsCopperLayer(d_.GetLayer()):
            cnt[d_.GetLayer()] += 1
    return cnt


LAYER_MODE = 'auto'      # auto | ask | keep | trim  (--layers)


def ask_user(kind, title, text, options, default):
    """Interactive decision point. The plugin GUI runs this script as a subprocess and answers on
    stdin; a human at a terminal types the option id. Protocol: one line
    `@@ASK <json>` on stdout, one line with the option id on stdin. Any failure (no stdin, EOF,
    timeout, unknown answer) returns `default`, which is always the non-destructive choice."""
    import threading
    msg = json.dumps(dict(kind=kind, title=title, text=text, options=options, default=default))
    print('@@ASK ' + msg, flush=True)
    ans = [None]

    def _read():
        try:
            ans[0] = sys.stdin.readline()
        except Exception:
            ans[0] = ''
    t = threading.Thread(target=_read, daemon=True)
    t.start(); t.join(timeout=900)
    a = (ans[0] or '').strip().lower()
    ids = {o[0] for o in options}
    if a not in ids:
        R.p(f'no answer for "{title}" -> {default}')
        return default
    R.p(f'user decision for "{title}": {a}')
    return a


def trim_empty_inner_layers(pcb_path, ebrd, dry):
    """KiCad's Eagle importer sizes the copper stack from the Eagle design-rule 'layerSetup', which
    may declare more layers than the board uses. The result is a 4-layer KiCad project of a 2-layer
    board: the fab quotes 4 layers. When the Eagle source uses no inner layer and the KiCad inner
    layers are empty, the stack is reduced to 2 layers (asked first in --layers ask mode). Every
    other mismatch is only reported here and failed by the quality control: no guessing."""
    R.h('PCB: copper layer count vs the Eagle source')
    try:
        import pcbnew
    except ImportError:
        R.p('pcbnew module not available - skipped'); return
    try:
        brd = eagle_root(ebrd).find('drawing/board')
        e_inner = eagle_inner_layers(brd)
        board = pcbnew.LoadBoard(pcb_path)
        n = board.GetCopperLayerCount()
        setup = brd.find('designrules/param[@name="layerSetup"]')
        ls = setup.get('value') if setup is not None else '?'
        e_cu = 2 + len(e_inner)
        R.p(f'Eagle: layerSetup {ls}, copper layers in use {e_cu}; KiCad: {n} copper layers')
        if n == e_cu:
            return
        cnt = _kicad_layer_items(pcbnew, board)
        inner = [l for l in board.GetEnabledLayers().CuStack() if l not in (pcbnew.F_Cu, pcbnew.B_Cu)]
        busy = [board.GetLayerName(l) for l in inner if cnt.get(l)]
        if n < e_cu:
            R.p(f'KiCad has FEWER copper layers than the Eagle board uses ({n} < {e_cu}) - not touched, the quality control fails this')
            if LAYER_MODE == 'ask':
                ask_user('layers', 'Copper layer count', f'The EAGLE board uses {e_cu} copper layers (Eagle layers '
                         f'{sorted(int(x) for x in eagle_copper_layers(brd))}), the imported KiCad board has only {n}. '
                         'Eagle Exhumer does not add layers; the quality control will report a FAIL. Please check the import.',
                         [['ok', 'OK']], 'ok')
            return
        if e_inner or busy:
            why = (f'the Eagle board uses inner layer(s) {sorted(e_inner)}' if e_inner else '') + \
                  (' and ' if e_inner and busy else '') + (f'KiCad inner layer(s) {", ".join(busy)} carry items' if busy else '')
            R.p(f'{n} KiCad copper layers vs {e_cu} used by Eagle, but {why} - not touched, the quality control fails this')
            if LAYER_MODE == 'ask':
                ask_user('layers', 'Copper layer count', f'KiCad imported {n} copper layers, the EAGLE board uses {e_cu}, '
                         f'but {why}. Eagle Exhumer cannot decide this safely and leaves the stack as it is; '
                         'the quality control will report a FAIL. Fix the layer setup in KiCad (File > Board Setup) by hand.',
                         [['ok', 'OK']], 'ok')
            return
        # the clean case: a 2-layer board declared as more in the Eagle rule set, nothing on the inner layers
        decision = {'auto': 'trim', 'trim': 'trim', 'keep': 'keep'}.get(LAYER_MODE)
        if decision is None:
            decision = ask_user('layers', 'Copper layer count',
                                f'The EAGLE board is a {e_cu}-layer board (rule set says {ls}), but KiCad imported it with {n} copper layers '
                                f'and the {n - 2} inner layer(s) are empty. A fab would quote {n} layers.\n\n'
                                f'Remove the empty inner layers and make it a {e_cu}-layer KiCad board?',
                                [['trim', f'Remove, make it {e_cu} layers'], ['keep', f'Keep {n} layers']], 'keep')
        if decision != 'trim':
            R.p(f'kept {n} copper layers on request - the quality control reports the mismatch')
            return
        R.p(f'{n - 2} empty inner layer(s) removed -> {e_cu}-layer board (as in the Eagle source)')
        if dry:
            return
        board.SetCopperLayerCount(e_cu)
        pcbnew.SaveBoard(pcb_path, board)
        # KiCad keeps a stale (stackup ...) block if one exists: it would still list the old copper layers
        tree = read_sx(pcb_path)
        setup_ = kid(tree, 'setup')
        if setup_ is not None:
            st = kid(setup_, 'stackup')
            if st is not None:
                setup_.remove(st); write_sx(pcb_path, tree)
    except Exception as ex:
        R.p(f'layer step failed ({ex}) - board left as imported, the quality control decides')


def promote_fp_edge_cuts(pcb_path, dry):
    """Eagle designs often draw the board outline (layer 20 Dimension) inside a library package
    (an enclosure / 'case' package without copper). KiCad imports it as Edge.Cuts of a board-only
    footprint; the zone filler then clips every copper pour to an inferred outline: on
    A10-OLinuXino-Lime 254 pads/tracks of GND/VCC lost their pour connection (16483 -> 1080 mm2
    filled). Board-level Edge.Cuts = identical geometry, correct fill."""
    R.h('PCB: board outline drawn inside a package -> board Edge.Cuts')
    try:
        import pcbnew
    except ImportError:
        R.p('pcbnew module not available - skipped'); return
    board = pcbnew.LoadBoard(pcb_path)
    moved = []
    for fp in board.GetFootprints():
        if any(p.GetAttribute() != pcbnew.PAD_ATTRIB_NPTH for p in fp.Pads()):
            continue                                    # real parts keep their own cutouts/slots
        gs = [g for g in fp.GraphicalItems() if g.GetLayer() == pcbnew.Edge_Cuts and hasattr(g, 'GetShape')]
        if not gs:
            continue
        for g in gs:
            n = pcbnew.PCB_SHAPE(board)
            n.SetShape(g.GetShape()); n.SetLayer(pcbnew.Edge_Cuts); n.SetWidth(g.GetWidth())
            if g.GetShape() == pcbnew.SHAPE_T_ARC:
                n.SetArcGeometry(g.GetStart(), g.GetArcMid(), g.GetEnd())
            elif g.GetShape() == pcbnew.SHAPE_T_POLY:
                n.SetPolyShape(g.GetPolyShape())
            else:
                n.SetStart(g.GetStart()); n.SetEnd(g.GetEnd())
            board.Add(n); fp.Remove(g)
        moved.append(f'{fp.GetReference()} ({len(gs)})')
    R.p(f'{len(moved)} footprint(s) carried the board outline: ' + (', '.join(moved) if moved else '-'))
    if moved and not dry:
        pcbnew.SaveBoard(pcb_path, board)


def assign_orphan_pads(pcb_path, dry):
    """Eagle connector shield / mounting pads often have no signal, but the GND copper of the
    board is routed straight into them (Eagle connects by copper, DRC 'overlap' approved).
    KiCad: net-less pad + track = 'shorting items'. A net-less pad touched by the copper of
    exactly ONE net gets that net (the physical board is identical)."""
    R.h('PCB: net-less pads touched by copper of one net')
    try:
        import pcbnew
    except ImportError:
        R.p('pcbnew module not available - skipped'); return
    board = pcbnew.LoadBoard(pcb_path)
    tracks = [t for t in board.GetTracks() if t.GetNetCode() > 0]
    zones = [z for z in board.Zones() if z.GetNetCode() > 0 and not z.GetIsRuleArea()]
    done, amb = [], []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetCode() > 0 or not pad.GetNumber() or pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH:
                continue
            nets = set()
            for layer in (pcbnew.F_Cu, pcbnew.B_Cu) + tuple(getattr(pcbnew, f'In{i}_Cu') for i in range(1, 31) if hasattr(pcbnew, f'In{i}_Cu')):
                if not pad.IsOnLayer(layer):
                    continue
                shp = pad.GetEffectiveShape(layer)
                for t in tracks:
                    if t.IsOnLayer(layer) and shp.Collide(t.GetEffectiveShape(layer), 0):
                        nets.add(t.GetNetname())
            if len(nets) == 1:
                n = nets.pop()
                pad.SetNet(board.FindNet(n))
                done.append(f'{fp.GetReference()}.{pad.GetNumber()}->{n}')
            elif len(nets) > 1:
                amb.append(f'{fp.GetReference()}.{pad.GetNumber()} ({", ".join(sorted(nets))})')
    R.p(f'{len(done)} pads got the net of the copper routed into them'
        + (': ' + ', '.join(done[:30]) + (' ...' if len(done) > 30 else '') if done else ''))
    if amb:
        R.p('touched by several nets (real short in the Eagle design, check!): ' + ', '.join(amb[:20]))
    if done and not dry:
        pcbnew.SaveBoard(pcb_path, board)


def fix_pcb_sexpr(pcb_path, sch_refs, dry, ebrd=None):
    R.h('PCB: layers / board-only items')
    with open(pcb_path, encoding='utf-8') as f:
        text = f.read()
    n_undef = text.count('(layer "UNDEFINED")')
    text = text.replace('(layer "UNDEFINED")', '(layer "Dwgs.User")')
    tree = sx_parse(text)
    n_bo = 0
    for fp in kids(tree, 'footprint'):
        ref = prop_val(fp, 'Reference', '')
        has_net = any((kid(p, 'net') is not None) for p in kids(fp, 'pad'))
        if ref.startswith('UNK_HOLE') or (sch_refs is not None and ref not in sch_refs and not has_net):
            attr = kid(fp, 'attr')
            if attr is None:
                attr = ['attr']; fp.insert(4, attr)
            for t in ('board_only', 'exclude_from_pos_files', 'exclude_from_bom'):
                if t not in attr: attr.append(t)
            n_bo += 1
    n_np = n_cu = n_nt = 0
    for fp in kids(tree, 'footprint'):
        n_np += _npth_no_copper(fp)
        n_cu += _fp_copper_polys_to_pads(fp)
        n_nt += _fp_net_ties(fp)
    n_via = _via_restring(tree, eagle_brd_rules(ebrd)[0]) if ebrd else 0
    R.p(f'{n_undef} items moved from UNDEFINED layer to Dwgs.User')
    R.p(f'{n_np} NPTH holes: net-less copper ring removed (pad size = drill)')
    R.p(f'{n_cu} package copper polygons -> custom pads with the net of the pad they touch (Eagle polygon-in-package)')
    R.p(f'{n_nt} net-tie pad groups (copper links inside Eagle packages, e.g. board-mounted 0R arrays)')
    R.p(f'{n_via} vias enlarged to Eagle restring (drill + 2 x clamp(rvViaOuter*drill, rlMin, rlMax))')
    R.p(f'{n_bo} footprints without schematic symbol marked board_only (holes, logos, fiducials)')
    if not dry:
        write_sx(pcb_path, tree)
    return tree

def _fp_copy(pcbnew, fp):
    for f in (lambda: pcbnew.FOOTPRINT(fp), lambda: fp.Duplicate(), lambda: fp.Duplicate(False)):
        try:
            c = f()
            if not isinstance(c, pcbnew.FOOTPRINT) and hasattr(c, 'Cast'):
                c = c.Cast()
            if isinstance(c, pcbnew.FOOTPRINT):
                return c
        except Exception:
            pass
    raise RuntimeError('cannot copy footprint ' + fp.GetReference())

def _pad_w(pcbnew, p):
    try:
        return round(p.GetSize(pcbnew.F_Cu).x / 1e4)
    except Exception:
        try:
            return round(p.GetSize().x / 1e4)
        except Exception:
            return 0

def _eagle_rot(rot):
    """'MR90' -> (mirror, angle_deg)"""
    rot = rot or 'R0'
    m = re.match(r'S?(M?)S?R([-\d.]+)', rot)
    return (bool(m.group(1)), float(m.group(2))) if m else (False, 0.0)


def _eagle_arc_mid(x1, y1, x2, y2, curve):
    """Mid point of an Eagle curved wire (curve = included angle, + = CCW), Eagle coordinates."""
    import math
    a = math.radians(curve)
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    dx, dy = x2 - x1, y2 - y1
    h = math.hypot(dx, dy) / 2
    if h == 0:
        return mx, my
    d = h / math.tan(a / 2)
    nx, ny = -dy / (2 * h), dx / (2 * h)          # left normal of P1->P2
    cx, cy = mx + d * nx, my + d * ny
    c, s = math.cos(a / 2), math.sin(a / 2)
    px, py = x1 - cx, y1 - cy
    return cx + px * c - py * s, cy + px * s + py * c


def _shape_pts(sh):
    pts = [sh.GetStart(), sh.GetEnd()]
    try:
        if sh.GetShapeStr().lower().startswith('arc'):
            pts.append(sh.GetArcMid())
    except Exception:
        pass
    return pts


def _clusters(shapes_):
    """Group milling shapes into contours (shared end points, 10 um tolerance)."""
    par = list(range(len(shapes_)))
    def f(i):
        while par[i] != i:
            par[i] = par[par[i]]; i = par[i]
        return i
    seen = {}
    for i, sh in enumerate(shapes_):
        for p in (sh.GetStart(), sh.GetEnd()):
            k = (round(p.x / 1e4), round(p.y / 1e4))
            if k in seen:
                par[f(i)] = f(seen[k])
            else:
                seen[k] = i
    g = defaultdict(list)
    for i, sh in enumerate(shapes_):
        g[f(i)].append(sh)
    return list(g.values())


def _slot_to_pad(pcbnew, fp, grp):
    """A closed milled contour around exactly one plated hole = a plated slot. KiCad models that
    as an oval drill (Edge.Cuts inside a pad would be a NON-plated cut-out + DRC errors)."""
    if len(grp) < 2 or any(sh.GetShape() == pcbnew.SHAPE_T_CIRCLE for sh in grp):
        return False
    pts = [p for sh in grp for p in _shape_pts(sh)]
    x0, x1 = min(p.x for p in pts), max(p.x for p in pts)
    y0, y1 = min(p.y for p in pts), max(p.y for p in pts)
    W, H = x1 - x0, y1 - y0
    if min(W, H) <= 0 or max(W, H) > 10e6:
        return False
    inside = [p for p in fp.Pads() if p.GetDrillSize().x > 0 and
              x0 - 1e4 <= p.GetPosition().x <= x1 + 1e4 and y0 - 1e4 <= p.GetPosition().y <= y1 + 1e4]
    if len(inside) != 1:
        return False
    pad = inside[0]
    if pad.GetAttribute() not in (pcbnew.PAD_ATTRIB_PTH,):
        return False
    ang = round(pad.GetOrientation().AsDegrees()) % 180
    if ang not in (0, 90):
        return False
    dw, dh = (W, H) if ang == 0 else (H, W)
    old_drill = pad.GetDrillSize()
    try:
        cur = pad.GetSize(pcbnew.F_Cu)
    except Exception:
        cur = pad.GetSize()
    ring = max(150000, (min(cur.x, cur.y) - max(old_drill.x, old_drill.y)) // 2)
    size = pcbnew.VECTOR2I(int(dw + 2 * ring), int(dh + 2 * ring))
    pad.SetPosition(pcbnew.VECTOR2I(int((x0 + x1) / 2), int((y0 + y1) / 2)))
    shp = getattr(pcbnew, 'PAD_DRILL_SHAPE_OBLONG', None)
    if shp is None:
        shp = getattr(pcbnew, 'PAD_DRILL_SHAPE_T_OBLONG')
    pad.SetDrillShape(shp)
    pad.SetDrillSize(pcbnew.VECTOR2I(int(dw), int(dh)))
    for call in (lambda: pad.SetSize(pcbnew.F_Cu, size), lambda: pad.SetSize(size)):
        try:
            call(); break
        except Exception:
            pass
    for call in (lambda: pad.SetShape(pcbnew.F_Cu, pcbnew.PAD_SHAPE_OVAL), lambda: pad.SetShape(pcbnew.PAD_SHAPE_OVAL)):
        try:
            call(); break
        except Exception:
            pass
    return True


def add_milling(pcb_path, ebrd, dry):
    """Eagle layer 46 (Milling) is not mapped by the layer auto-match, so slots / cut-outs
    (e.g. USB-C shell slots in the package) silently disappear. Re-create them on Edge.Cuts,
    inside the owning footprint, from the Eagle .brd."""
    R.h('PCB: Eagle milling (layer 46) -> Edge.Cuts')
    import math
    try:
        import pcbnew
    except ImportError:
        R.p('pcbnew module not available - skipped'); return
    root = eagle_root(ebrd)
    brd = root.find('drawing/board')
    pk = {}
    for lib in brd.findall('libraries/library'):
        for p in lib.findall('packages/package'):
            items = [w for w in p if w.get('layer') == '46' and w.tag in ('wire', 'circle')]
            if items:
                pk[(lib.get('name'), lib.get('urn', ''), p.get('name'))] = items
    plain = [w for w in brd.findall('plain/*') if w.get('layer') == '46' and w.tag in ('wire', 'circle')]
    board = pcbnew.LoadBoard(pcb_path)
    fps = {fp.GetReference(): fp for fp in board.GetFootprints()}
    # Eagle -> KiCad offset (the importer re-centres the board): kx = ex + ox, ky = -ey + oy
    offs = []
    for el in brd.findall('elements/element'):
        fp = fps.get(kref(el.get('name')))
        if fp is not None:
            p = fp.GetPosition()
            offs.append((p.x / 1e6 - float(el.get('x')), p.y / 1e6 + float(el.get('y'))))
    if not offs:
        R.p('no footprint matched to an Eagle element - skipped'); return
    offs.sort()
    ox = sorted(o[0] for o in offs)[len(offs) // 2]
    oy = sorted(o[1] for o in offs)[len(offs) // 2]
    spread = max(max(abs(o[0] - ox), abs(o[1] - oy)) for o in offs)
    if spread > 0.01:
        R.p(f'warning: element offsets differ by up to {spread:.3f} mm')

    def V(x, y):
        return pcbnew.VECTOR2I(int(round((x + ox) * 1e6)), int(round((-y + oy) * 1e6)))

    def shapes(items, xf, flipc, parent):
        out = []
        for w in items:
            sh = pcbnew.PCB_SHAPE(parent)
            sh.SetLayer(pcbnew.Edge_Cuts)
            width = max(_unit(w.get('width', '0')) or 0, 0.05)
            sh.SetWidth(int(round(width * 1e6)))
            if w.tag == 'circle':
                cx, cy = xf(float(w.get('x')), float(w.get('y')))
                r = float(w.get('radius'))
                sh.SetShape(pcbnew.SHAPE_T_CIRCLE)
                sh.SetCenter(V(cx, cy)); sh.SetEnd(V(cx + r, cy))
            else:
                x1, y1 = xf(float(w.get('x1')), float(w.get('y1')))
                x2, y2 = xf(float(w.get('x2')), float(w.get('y2')))
                cv = float(w.get('curve', '0') or 0) * (-1 if flipc else 1)
                if abs(cv) > 1e-6:
                    mx, my = _eagle_arc_mid(x1, y1, x2, y2, cv)
                    sh.SetShape(pcbnew.SHAPE_T_ARC)
                    sh.SetArcGeometry(V(x1, y1), V(mx, my), V(x2, y2))
                else:
                    sh.SetShape(pcbnew.SHAPE_T_SEGMENT)
                    sh.SetStart(V(x1, y1)); sh.SetEnd(V(x2, y2))
            out.append(sh)
        return out

    n_el = n_sh = n_skip = n_slot = 0
    for el in brd.findall('elements/element'):
        key = (el.get('library'), el.get('library_urn', ''), el.get('package'))
        items = pk.get(key) or next((v for k, v in pk.items() if k[0] == key[0] and k[2] == key[2]), None)
        if not items:
            continue
        fp = fps.get(kref(el.get('name')))
        if fp is None:
            continue
        if any(g.GetLayer() == pcbnew.Edge_Cuts for g in fp.GraphicalItems()):
            n_skip += 1; continue                      # already mapped by the user
        mir, ang = _eagle_rot(el.get('rot'))
        ex, ey = float(el.get('x')), float(el.get('y'))
        ca, sa = math.cos(math.radians(ang)), math.sin(math.radians(ang))

        def xf(x, y, mir=mir, ca=ca, sa=sa, ex=ex, ey=ey):
            rx, ry = x * ca - y * sa, x * sa + y * ca     # Eagle: rotate, then mirror
            return ex + (-rx if mir else rx), ey + ry
        new = shapes(items, xf, mir, fp)
        for grp in _clusters(new):
            if _slot_to_pad(pcbnew, fp, grp):
                n_slot += 1; continue
            for sh in grp:
                fp.Add(sh); n_sh += 1
        n_el += 1
    n_plain = n_user = 0
    if plain and not n_skip:          # n_skip > 0: layer 46 was mapped at import -> already there
        # Only a CLOSED milling contour lying INSIDE the board is a cut-out. Open paths and drawings
        # outside the board (Olimex: enclosure front-panel cut-outs drawn on layer 46) would make the
        # KiCad board outline invalid (self-intersecting / not closed) -> kept on Dwgs.User instead.
        bps = pcbnew.SHAPE_POLY_SET()
        have_outline = False
        try:
            have_outline = bool(board.GetBoardPolygonOutlines(bps, False)) and bps.OutlineCount() > 0
        except Exception:
            pass
        for grp in _clusters(shapes(plain, lambda x, y: (x, y), False, board)):
            deg = Counter()
            for sh in grp:
                if sh.GetShape() != pcbnew.SHAPE_T_CIRCLE:
                    for p in (sh.GetStart(), sh.GetEnd()):
                        deg[(round(p.x / 1e4), round(p.y / 1e4))] += 1
            closed = all(v % 2 == 0 for v in deg.values())
            inside = have_outline and all(bps.Contains(p) for sh in grp for p in _shape_pts(sh))
            if not have_outline or (closed and inside):
                for sh in grp:
                    board.Add(sh); n_plain += 1
            else:
                for sh in grp:
                    sh.SetLayer(pcbnew.Dwgs_User); board.Add(sh); n_user += 1
        if n_user:
            R.p(f'{n_user} board-level milling items are open paths or lie outside the board outline '
                f'(e.g. enclosure cut-out drawings) -> Dwgs.User, NOT cut - check the fab notes')
    if n_slot:
        R.p(f'{n_slot} milled slots around plated holes -> plated oval drills (KiCad style, e.g. USB-C shell)')
    R.p(f'{n_sh} milling items in {n_el} footprints, {n_plain} board-level milling items -> Edge.Cuts'
        + (f' ({n_skip} footprints already had Edge.Cuts - left alone)' if n_skip else ''))
    if (n_sh or n_plain or n_slot or n_user) and not dry:
        pcbnew.SaveBoard(pcb_path, board)


def footprint_library(pcb_path, proj_dir, nick, sheets, dry):
    R.h('PCB: project footprint library + re-link')
    try:
        import pcbnew
    except ImportError:
        R.p('pcbnew module not available - run this script with KiCad\'s python.exe to build the footprint '
            'library (or use PCB Editor: File > Export > Export Footprints to New Library).')
        return
    board = pcbnew.LoadBoard(pcb_path)
    sch_fp = {}
    for sh in sheets:
        for inst in sh.instances():
            r = prop_val(inst, 'Reference'); f = prop_val(inst, 'Footprint')
            if r and f: sch_fp[r] = f.split(':', 1)[-1]
    lib_dir = os.path.join(proj_dir, nick + '.pretty')
    plugin = pcbnew.PCB_IO_MGR.FindPlugin(pcbnew.PCB_IO_MGR.KICAD_SEXP)
    if not dry and not os.path.isdir(lib_dir):
        os.makedirs(lib_dir)
    geom = {}           # name -> signature
    variants = Counter()
    ref_to_name = {}
    for fp in board.GetFootprints():
        if fp.IsBoardOnly():
            continue
        ref = fp.GetReference()
        name = str(fp.GetFPID().GetLibItemName())
        m = re.match(r'^(.*)_(\d{5,})$', name)
        if m and m.group(1) == sch_fp.get(ref):
            name = m.group(1)   # Eagle-9 managed library URN suffix (#18515)
        cp = _fp_copy(pcbnew, fp)
        if cp.IsFlipped():
            try:
                cp.Flip(cp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
            except Exception:
                cp.Flip(cp.GetPosition(), False)
        cp.SetOrientationDegrees(0)
        cp.SetPosition(pcbnew.VECTOR2I(0, 0))
        sig = tuple(sorted((str(p.GetNumber()), round(p.GetPosition().x / 1e4), round(p.GetPosition().y / 1e4),
                            _pad_w(pcbnew, p)) for p in cp.Pads()))
        final = name
        if name in geom and geom[name] != sig:
            k = 2
            while f'{name}__v{k}' in geom and geom[f'{name}__v{k}'] != sig:
                k += 1
            final = f'{name}__v{k}'; variants[name] += 1
        if final not in geom:
            geom[final] = sig
            cp.SetReference('REF**'); cp.SetValue(final)
            cp.SetFPID(pcbnew.LIB_ID(nick, final))
            if not dry:
                plugin.FootprintSave(lib_dir, cp)
        fp.SetFPID(pcbnew.LIB_ID(nick, final))
        ref_to_name[ref] = final
    R.p(f'{len(geom)} footprints written to `{nick}.pretty`')
    for k, v in variants.items():
        R.p(f'package `{k}` has {v + 1} different geometries in the board -> variants `{k}__vN`')
    if not dry:
        pcbnew.SaveBoard(pcb_path, board)
    # fp-lib-table
    tp = os.path.join(proj_dir, 'fp-lib-table')
    table = read_sx(tp) if os.path.isfile(tp) else ['fp_lib_table', ['version', '7']]
    if nick not in {str(kid(l, 'name')[1]) for l in kids(table, 'lib')}:
        table.append(['lib', ['name', Q(nick)], ['type', Q('KiCad')], ['uri', Q('${KIPRJMOD}/' + nick + '.pretty')],
                      ['options', Q('')], ['descr', Q('Eagle import (eagle2kicad_fix)')]])
        R.p(f'fp-lib-table: added `{nick}`')
        if not dry: write_sx(tp, table)
    # schematic footprint fields
    n = 0
    for sh in sheets:
        for inst in sh.instances():
            r = prop_val(inst, 'Reference')
            if r in ref_to_name:
                fpp = prop(inst, 'Footprint')
                want = f'{nick}:{ref_to_name[r]}'
                if fpp is not None and str(fpp[2]) != want:
                    fpp[2] = Q(want); sh.dirty = True; n += 1
    R.p(f'{n} schematic Footprint fields re-linked')

# --------------------------------------------------------------------------- design rules

def apply_design_rules(pro_path, brd, dry):
    R.h('PRO: Eagle design rules -> KiCad constraints')
    if not brd:
        R.p('no Eagle .brd given - skipped'); return
    rules, classes = eagle_brd_rules(brd)
    if not rules:
        R.p('no <designrules> in .brd'); return
    g = lambda k: _unit(rules.get(k)) if rules.get(k) else None
    clear = [v for v in (g(k) for k in ('mdWireWire', 'mdWirePad', 'mdWireVia', 'mdPadPad', 'mdPadVia',
                                         'mdViaVia', 'mdSmdPad', 'mdSmdVia', 'mdSmdSmd')) if v]
    with open(pro_path, encoding='utf-8') as f:
        pro = json.load(f)
    ds = pro.setdefault('board', {}).setdefault('design_settings', {})
    r = ds.setdefault('rules', {})
    setv = {}
    if clear: setv['min_clearance'] = min(clear)
    if g('msWidth'): setv['min_track_width'] = g('msWidth')
    if g('msDrill'): setv['min_through_hole_diameter'] = g('msDrill')
    rings = [v for v in (g(k) for k in ('rlMinViaOuter', 'rlMinPadTop', 'rlMinPadBottom')) if v]
    if rings: setv['min_via_annular_width'] = min(rings)   # KiCad checks vias AND PTH pads with this
    if g('msDrill') and g('rlMinViaOuter'): setv['min_via_diameter'] = round(g('msDrill') + 2 * g('rlMinViaOuter'), 4)
    elif g('msDrill') and rings: setv['min_via_diameter'] = round(g('msDrill') + 2 * min(rings), 4)
    if g('mdCopperDimension') is not None: setv['min_copper_edge_clearance'] = g('mdCopperDimension')
    if g('mdDrill'): setv['min_hole_clearance'] = g('mdDrill')   # Eagle Drill/Hole = copper-to-hole
    if g('msMicroVia') and g('msMicroVia') < 5: setv['min_microvia_drill'] = g('msMicroVia')
    for k, v in setv.items():
        old = r.get(k); r[k] = round(v, 4)
        R.p(f'{k}: {old} -> {r[k]} mm')
    # Default net class from Eagle class 0 + wire-wire clearance
    ns = pro.setdefault('net_settings', {})
    cls = ns.setdefault('classes', [])
    dflt = next((c for c in cls if c.get('name') == 'Default'), None)
    if dflt is None:
        dflt = {'name': 'Default'}; cls.insert(0, dflt)
    c0 = classes.get('0', {})
    wc = g('mdWireWire') or (min(clear) if clear else None)
    upd = {}
    if wc: upd['clearance'] = wc
    if c0.get('width'): upd['track_width'] = c0['width']
    elif g('msWidth') and dflt.get('track_width', 0.25) < g('msWidth'): upd['track_width'] = g('msWidth')
    if c0.get('drill'): upd['via_drill'] = c0['drill']
    for k, v in upd.items():
        old = dflt.get(k); dflt[k] = round(v, 4)
        R.p(f'net class Default {k}: {old} -> {dflt[k]} mm')
    named = {c['name']: (num, c) for num, c in classes.items() if c.get('name') and c['name'] != 'default'}
    for kc in cls:
        if kc.get('name') in named:
            num, ec = named[kc['name']]
            # Eagle <clearance class="m"> inside class n is the n:m pair value (written to the
            # .kicad_dru); only n:0 applies against everything = the KiCad class clearance
            own = ec.get('clearance', {}).get('0')
            newc = round(own if own else (wc or kc.get('clearance', 0.2)), 4)
            if kc.get('clearance') != newc:
                R.p(f"net class {kc['name']} clearance: {kc.get('clearance')} -> {newc} mm"
                    + ('' if own else ' (no n:0 clearance in Eagle -> board default; pair values in .kicad_dru)'))
                kc['clearance'] = newc
            if ec.get('width'):
                kc['track_width'] = round(ec['width'], 4)
    missing = [n for n in named if n not in {c.get('name') for c in cls}]
    if missing:
        # kicad-cli's board import does not create the Eagle net classes (and the class:class
        # clearance rules in the .kicad_dru then match nothing): create them from the Eagle class
        # (width, drill, own clearance) on top of the Default class, and assign the nets.
        for n in missing:
            num, ec = named[n]
            kc = {k: v for k, v in dflt.items() if k not in ('name', 'priority')}
            kc['name'] = n
            kc['priority'] = max([c.get('priority', 0) for c in cls if c.get('name') != 'Default'] + [-1]) + 1
            own = ec.get('clearance', {}).get('0')
            kc['clearance'] = round(own if own else (wc or dflt.get('clearance', 0.2)), 4)
            if ec.get('width'):
                kc['track_width'] = round(ec['width'], 4)
            if ec.get('drill'):
                kc['via_drill'] = round(ec['drill'], 4)
                kc['via_diameter'] = round(max(kc.get('via_diameter', 0), ec['drill'] + 2 * (g('rlMinViaOuter') or 0.1)), 4)
            cls.append(kc)
        R.p('Eagle net classes created in KiCad: ' + ', '.join(
            f"{n} (width {next(c for c in cls if c['name'] == n).get('track_width')} mm, clearance "
            f"{next(c for c in cls if c['name'] == n).get('clearance')} mm)" for n in missing))
    sig_cls = eagle_brd_net_classes(brd)
    pats = ns.setdefault('netclass_patterns', [])
    have = {(p_.get('netclass'), p_.get('pattern')) for p_ in pats}
    num2name = {num: c['name'] for num, c in classes.items() if c.get('name') and c['name'] != 'default'}
    added = defaultdict(int)
    for net, num in sorted(sig_cls.items()):
        cname = num2name.get(num)
        if cname and (cname, net) not in have:
            pats.append({'netclass': cname, 'pattern': net}); added[cname] += 1
    if added:
        R.p('nets assigned to Eagle net classes: ' + ', '.join(f'{k}: {v}' for k, v in sorted(added.items())))
    if g('mlMinStopFrame'):
        R.p(f'note: Eagle min solder-mask frame {g("mlMinStopFrame")} mm - KiCad pads default to 0 expansion, '
            'set Board Setup > Solder Mask if your fab expects it')
    if not dry:
        with open(pro_path, 'w', encoding='utf-8') as f:
            json.dump(pro, f, indent=2)

def verify_sch_values(esch, sheets):
    """Eagle part value (parts that have a package) vs KiCad schematic symbol Value."""
    root = eagle_root(esch)
    sch = root.find('drawing/schematic')
    pkg = {}
    for lib in sch.findall('libraries/library'):
        for ds in lib.findall('devicesets/deviceset'):
            for dev in ds.findall('devices/device'):
                pkg[(lib.get('name'), ds.get('name'), dev.get('name', ''))] = dev.get('package')
    kval = {}
    for sh in sheets:
        for inst in sh.instances():
            for r in _inst_refs(inst):
                kval[r] = prop_val(inst, 'Value', '')
    n = n_missing = 0; bad = []
    for p in sch.findall('parts/part'):
        if not pkg.get((p.get('library'), p.get('deviceset'), p.get('device', ''))):
            continue                                # supply / frame symbols (no footprint)
        r = kref(p.get('name'))
        if r not in kval:
            n_missing += 1; bad.append(f'`{r}` missing in KiCad schematic'); continue
        if (p.get('value') or '').strip():          # empty Eagle value -> KiCad shows the device name
            n += 1
            if kval[r].strip() != p.get('value').strip():
                bad.append(f'`{r}` Eagle `{p.get("value")}` KiCad `{kval[r]}`')
    SCH_VAL.update(checked=n, bad=len(bad), missing=n_missing)
    R.p(f'**Eagle schematic values vs KiCad symbols**: {n} parts with a value, {len(bad) - n_missing} differ, '
        f'{n_missing} missing' + ((': ' + ', '.join(bad[:10])) if bad else ''))


def qc_verdict(selftest_ok=None, geom=None):
    """Strict quality-control verdict: anything that is not identical to the Eagle source is a FAIL."""
    R.h('QC VERDICT (strict)')
    fails = []
    for lab, c in LAST_CMP.items():
        if lab.startswith('selftest'):
            continue
        for k in ('short', 'open', 'renamed', 'absent'):
            if c.get(k):
                fails.append(f'{lab}: {c[k]} {k}')
    g = geom
    if g is None:
        fails.append('geometry check did not run')
    else:
        for k, v in g.items():
            if v:
                fails.append(f'geometry: {v} {k}')
    if SCH_VAL.get('bad'):
        fails.append(f"schematic values: {SCH_VAL['bad']} differences")
    if CLASH:
        fails.append('reference collision: ' + ', '.join(f'{n}/{kref(n)}' for n in CLASH))
    if selftest_ok is False:
        fails.append('SELFTEST FAILED - the verifier itself is not trustworthy')
    if fails:
        R.p('**FAIL** - ' + str(len(fails)) + ' finding(s):')
        for f in fails:
            R.p('  ' + f)
    else:
        R.p('**PASS** - netlists, pads, geometry, values identical to the Eagle source; selftest passed')
    return fails


WARNINGS = {}


def check_3d_models(pcb_path):
    """WARNING only (not a QC failure): footprints without a usable 3D model. Eagle designs carry no
    3D models, so after an import this is normally every footprint - the 3D view shows bare pads."""
    R.h('WARNING: 3D models')
    try:
        import pcbnew
    except ImportError:
        R.p('pcbnew module not available - skipped'); return
    board = pcbnew.LoadBoard(pcb_path)
    roots = [v for k, v in os.environ.items() if k.startswith('KICAD') and k.endswith('3DMODEL_DIR')]
    roots += glob.glob(r'C:\Program Files\KiCad\*\share\kicad\3dmodels') + ['/usr/share/kicad/3dmodels',
             '/Applications/KiCad/KiCad.app/Contents/SharedSupport/3dmodels']
    def found(fn):
        f = re.sub(r'\$\{KICAD\d*_3DMODEL_DIR\}', '{root}', fn)
        if '{root}' in f:
            return any(os.path.isfile(f.replace('{root}', r)) for r in roots)
        f = os.path.expandvars(f)
        return os.path.isfile(f if os.path.isabs(f) else os.path.join(os.path.dirname(pcb_path), f))
    none, broken, n = [], [], 0
    for fp in board.GetFootprints():
        if fp.GetAttributes() & getattr(pcbnew, 'FP_BOARD_ONLY', 0):
            continue
        n += 1
        models = [m for m in fp.Models() if getattr(m, 'm_Show', True)]
        if not models:
            none.append(fp.GetReference())
        elif not any(found(m.m_Filename) for m in models):
            broken.append(fp.GetReference())
    WARNINGS['no_3d_model'] = len(none)
    WARNINGS['3d_model_not_found'] = len(broken)
    R.p(f'{len(none)} of {n} footprints have no 3D model, {len(broken)} point to a model file that is not found '
        '(Eagle designs carry no 3D models - assign them in the footprint properties if you need the 3D view / STEP)')
    if none:
        R.p('  without model: ' + ', '.join(sorted(none)[:40]) + (' ...' if len(none) > 40 else ''))
    if broken:
        R.p('  model not found: ' + ', '.join(sorted(broken)[:40]))


IMPORT_META = {}
T_START = __import__('time').time()


def write_metrics(d, proj, esch, ebrd, fails, selftest_ok, mode):
    """Machine-readable record of this run (eaglefix_metrics.json) + one line per run appended to
    eaglefix_history.jsonl -> error counts per iteration for the documentation."""
    import hashlib
    rec = {
        'time': datetime.datetime.now().isoformat(timespec='seconds'),
        'project': proj, 'mode': mode,
        'fixer_md5': hashlib.md5(open(os.path.abspath(__file__), 'rb').read()).hexdigest()[:10],
        'eagle_sch': os.path.basename(esch) if esch else None, 'eagle_brd': os.path.basename(ebrd) if ebrd else None,
        'qc': 'PASS' if not fails else 'FAIL', 'qc_findings': fails,
        'selftest': selftest_ok, 'metrics': METRICS, 'warnings': dict(WARNINGS),
        'timing_s': dict(TIMING), 'total_s': round(time.time() - T_START, 1),
        'import': IMPORT_META,
    }
    try:
        with open(os.path.join(d, 'eaglefix_metrics.json'), 'w', encoding='utf-8') as f:
            json.dump(rec, f, indent=1)
        with open(os.path.join(d, 'eaglefix_history.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec) + '\n')
    except Exception as e:
        R.p(f'metrics not written: {e}')


def render_3d(cli, pcb, d):
    """3D renders of the converted board (documentation): top + bottom, kicad-cli pcb render."""
    if not cli or not os.path.isfile(pcb):
        return
    out = os.path.join(d, 'docs'); os.makedirs(out, exist_ok=True)
    src = pcb
    try:                                   # filled copper pours look like the real board
        import pcbnew
        b = pcbnew.LoadBoard(pcb)
        pcbnew.ZONE_FILLER(b).Fill(b.Zones())
        src = os.path.join(d, '_render_filled.kicad_pcb')
        pcbnew.SaveBoard(src, b)
    except Exception as e:
        R.p(f'zone fill for render skipped: {e}')
    views = [('top', ['--side', 'top', '--quality', 'basic']),
             ('bottom', ['--side', 'bottom', '--quality', 'basic']),
             ('iso', ['--side', 'top', '--rotate', '-40,0,35', '--perspective', '--quality', 'high', '--floor',
                      '--zoom', '0.62'])]
    for name, opts in views:
        png = os.path.join(out, f'render_{name}.png')
        rc, txt = run([cli, 'pcb', 'render'] + opts + ['--width', '1600', '--height', '1100',
                                                      '--background', 'opaque', '-o', png, src])
        if not os.path.isfile(png):
            R.p(f'3D render {name} failed: {txt.strip()[:200]}')
    if src != pcb:
        for f in (src, os.path.splitext(src)[0] + '.kicad_prl'):
            if os.path.isfile(f):
                os.remove(f)
    R.p(f'3D renders: {out}')


def selftest(d, proj, cli, esch, ebrd):
    """Inject known faults into a COPY of the converted project and require the verifier to catch
    every one of them. A verifier that has never been seen failing is not trustworthy."""
    import tempfile
    R.h('SELFTEST (fault injection on a copy)')
    tmp = tempfile.mkdtemp(prefix='eaglefix_selftest_')
    for f in os.listdir(d):
        p = os.path.join(d, f)
        if os.path.isfile(p) and not f.endswith('.lck'):
            shutil.copy2(p, tmp)
        elif os.path.isdir(p) and f.endswith('.pretty'):
            shutil.copytree(p, os.path.join(tmp, f))
    pcb = os.path.join(tmp, proj + '.kicad_pcb')
    results = []
    tree = read_sx(pcb)
    fps = kids(tree, 'footprint')
    def smd_pads(fp):
        out = []
        for p in kids(fp, 'pad'):
            n = kid(p, 'net')
            if str(p[1]) and n is not None:
                out.append(p)
        return out
    used = set()
    # F1: swap the NUMBERS (and nets) of two pads -> geometry identical, numbering flipped
    f1 = None
    for fp in fps:
        ps = smd_pads(fp)
        for i in range(len(ps)):
            for j in range(i + 1, len(ps)):
                if sx_dump(kid(ps[i], 'net')) != sx_dump(kid(ps[j], 'net')) and kid(ps[i], 'size') == kid(ps[j], 'size'):
                    f1 = (fp, ps[i], ps[j]); break
            if f1: break
        if f1: break
    if f1:
        fp, a, b = f1
        a[1], b[1] = b[1], a[1]
        ia, ib = a.index(kid(a, 'net')), b.index(kid(b, 'net'))
        a[ia], b[ib] = b[ib], a[ia]
        used.add(id(fp))
        results.append(('F1 pad numbers swapped (copper identical)', 'padpos',
                        f"{prop_val(fp, 'Reference')}.{b[1]}<->{a[1]}"))
    # F2: swap only the NETS of two pads of another part -> short + open
    f2 = None
    for fp in fps:
        if id(fp) in used: continue
        ps = smd_pads(fp)
        for i in range(len(ps)):
            for j in range(i + 1, len(ps)):
                if sx_dump(kid(ps[i], 'net')) != sx_dump(kid(ps[j], 'net')):
                    f2 = (fp, ps[i], ps[j]); break
            if f2: break
        if f2: break
    if f2:
        fp, a, b = f2
        ia, ib = a.index(kid(a, 'net')), b.index(kid(b, 'net'))
        a[ia], b[ib] = b[ib], a[ia]
        used.add(id(fp))
        results.append(('F2 nets of two pads swapped', 'pcbnet', f"{prop_val(fp, 'Reference')}.{a[1]}/{b[1]}"))
    # F3: footprint shifted by 0.2 mm
    for fp in fps:
        if id(fp) in used or kid(fp, 'attr') and 'board_only' in kid(fp, 'attr'): continue
        at = kid(fp, 'at'); at[1] = f'{float(at[1]) + 0.2:g}'
        used.add(id(fp))
        results.append(('F3 footprint moved 0.2 mm', 'moved', prop_val(fp, 'Reference'))); break
    # F4: value changed (on a part whose Eagle value is not empty - empty values are not compared)
    has_val = {kref(e.get('name')) for e in eagle_root(ebrd).iter('element') if (e.get('value') or '').strip()}
    for fp in fps:
        if id(fp) in used or prop_val(fp, 'Reference') not in has_val: continue
        pv = prop(fp, 'Value')
        if pv is not None and str(pv[2]):
            pv[2] = Q(str(pv[2]) + '_X')
            used.add(id(fp))
            results.append(('F4 part value changed', 'val', prop_val(fp, 'Reference'))); break
    # F5: one via deleted
    vias = kids(tree, 'via')
    if vias:
        tree.remove(vias[0])
        results.append(('F5 via deleted', 'via', ''))
    write_sx(pcb, tree)
    # F6: schematic - a supply net loses its name source -> its pins leave the net / get renamed.
    # Every power symbol of one supply on one sheet is renamed (a single one is often redundant:
    # a second symbol or a label of the same supply on the same wire). Supplies that are ALSO
    # named by a label are avoided; if there is no other choice, those labels are renamed too -
    # otherwise the injection would be a no-op, not a fault.
    sch_done = None
    sheets_st = [Sheet(f) for f in sorted(glob.glob(os.path.join(tmp, '*.kicad_sch')))]
    lab_names = Counter(str(x[1]) for sh in sheets_st for t in ('label', 'global_label', 'hierarchical_label')
                        for x in kids(sh.tree, t))
    def _pw(sh, i):
        l_ = sh.libsyms.get(str(kid(i, 'lib_id')[1]))
        return l_ is not None and is_power(l_) and 'FLG' not in prop_val(i, 'Reference', '')
    cands = []
    for sh in sheets_st:
        cnt = Counter(prop_val(i, 'Value', '') for i in sh.instances() if _pw(sh, i))
        for v, c in cnt.items():
            if v and c >= 2:
                cands.append((lab_names[v] > 0, -c, os.path.basename(sh.path), v, sh))
    if cands:
        has_lab, _, fn, v, sh = min(cands, key=lambda c: c[:4])
        n_ren = n_lab = 0
        for i2 in sh.instances():
            if _pw(sh, i2) and prop_val(i2, 'Value', '') == v:
                prop(i2, 'Value')[2] = Q(v + '_SELFTEST'); n_ren += 1
        if has_lab:
            for sh2 in sheets_st:
                for t in ('label', 'global_label', 'hierarchical_label'):
                    for x in kids(sh2.tree, t):
                        if str(x[1]) == v:
                            x[1] = Q(v + '_SELFTEST'); n_lab += 1; sh2.dirty = True
        sh.dirty = True
        for sh2 in sheets_st:
            sh2.save()
        sch_done = (fn, n_ren, v, n_lab)
    if sch_done:
        results.append(('F6 supply net renamed in the schematic', 'schnet',
                        f'{sch_done[1]}x {sch_done[2]} on {sch_done[0]}'
                        + (f' + {sch_done[3]} label(s)' if sch_done[3] else '')))
    # run the verifiers on the faulty copy, quietly
    saved = list(R.lines)
    GEOM_LOG.clear(); COMPARE_LOG.clear()
    LAST_CMP.pop('selftest pcb', None); LAST_CMP.pop('selftest sch', None)
    global VIEW
    old_view = VIEW
    VIEW = CliView(tmp, proj)
    try:
        truth_b = eagle_brd_truth(ebrd)
        compare_nets(truth_b, kicad_pcb_padnets(read_sx(pcb)), 'selftest pcb')
        verify_geometry(pcb, ebrd)
        if cli and esch:
            net, _ = kicad_sch_netlist(cli, os.path.join(tmp, proj + '.kicad_sch'), tmp)
            if net is not None:
                compare_nets(eagle_sch_truth(esch), net, 'selftest sch')
    finally:
        VIEW.close(); VIEW = old_view
        R.lines[:] = saved
    g = GEOM_LOG[-1] if GEOM_LOG else {}
    cmp = {lab: (m, s_, a_) for lab, m, s_, a_ in COMPARE_LOG}
    caught = {
        'padpos': g.get('padpos', 0) > 0,
        'pcbnet': sum(cmp.get('selftest pcb', (0, 0, 0))[:2]) > 0,
        'moved': g.get('moved', 0) > 0,
        'val': g.get('val', 0) > 0,
        'via': g.get('via', 0) > 0,
        'schnet': any(LAST_CMP.get('selftest sch', {}).get(k) for k in ('short', 'open', 'renamed')),
    }
    ok = True
    for name, key, where in results:
        hit = caught.get(key)
        ok &= bool(hit)
        R.p(f"{'CAUGHT ' if hit else 'MISSED '} {name} {('[' + where + ']') if where else ''}")
    shutil.rmtree(tmp, ignore_errors=True)
    R.p(f'selftest: {sum(1 for _, k, _ in results if caught.get(k))}/{len(results)} injected faults caught'
        + ('' if ok else '  ** VERIFIER NOT TRUSTWORTHY **'))
    return ok


# --------------------------------------------------------------------------- main

TIMING = {}          # stage -> seconds (benchmark / metrics)
_T_LAST = [None, None]


# progress percentages are TIME fractions, measured on the 18-design corpus (run3 stage timings):
# the bar then moves roughly linearly in time and the GUI can extrapolate a remaining-time estimate.
def progress(pct, label):
    """Machine-readable progress line for the plugin window (stripped from the visible log).
    Also closes the timing of the previous stage."""
    import time as _t
    now = _t.time()
    if _T_LAST[0] is not None:
        TIMING[_T_LAST[1]] = round(TIMING.get(_T_LAST[1], 0) + now - _T_LAST[0], 2)
    _T_LAST[0], _T_LAST[1] = now, label
    print(f'@@PROGRESS {int(pct)} {label}', flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('project_dir')
    ap.add_argument('--eagle-sch'); ap.add_argument('--eagle-brd')
    ap.add_argument('--kicad-cli')
    ap.add_argument('--no-pwr-flags', action='store_true')
    ap.add_argument('--no-label-globalize', action='store_true')
    ap.add_argument('--verify-only', action='store_true', help='only compare netlists / ERC / DRC')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--no-render', action='store_true', help='skip the 3D renders')
    ap.add_argument('--selftest', action='store_true', help='inject faults into a copy, verifier must catch all')
    ap.add_argument('--refix', action='store_true',
                    help='run the fix steps again on a project that was already fixed (normally: verify only)')
    ap.add_argument('--import-meta', help='JSON with how/when the KiCad import was made (recorded in the metrics)')
    ap.add_argument('--layers', choices=('auto', 'ask', 'keep', 'trim'), default='auto',
                    help='empty inner copper layers of a board whose Eagle source uses none: auto = remove, '
                         'ask = ask on stdin (@@ASK protocol, used by the plugin GUI), keep = leave, trim = remove')
    a = ap.parse_args()
    global LAYER_MODE
    LAYER_MODE = a.layers
    if a.import_meta:
        try:
            IMPORT_META.update(json.loads(a.import_meta))
        except Exception as ex:
            print(f'--import-meta ignored: {ex}')

    d = os.path.abspath(a.project_dir)
    pros = glob.glob(os.path.join(d, '*.kicad_pro'))
    if not pros:
        sys.exit('no .kicad_pro in ' + d)
    pro = pros[0]; proj = os.path.splitext(os.path.basename(pro))[0]
    root_sch = os.path.join(d, proj + '.kicad_sch')
    pcb = os.path.join(d, proj + '.kicad_pcb')
    has_pcb = os.path.isfile(pcb)
    esch = a.eagle_sch or next((f for f in glob.glob(os.path.join(d, '*.sch')) if eagle_root(f) is not None), None)
    ebrd = a.eagle_brd or next((f for f in glob.glob(os.path.join(d, '*.brd')) if eagle_root(f) is not None), None)
    for f in (esch, ebrd):
        if f and eagle_root(f) is None:
            with open(f, 'rb') as fh:
                head = fh.read(400)
            if b'<eagle' not in head and b'<?xml' not in head:
                sys.exit(f'{os.path.basename(f)}: binary EAGLE file (EAGLE 5.x or older). KiCad and this tool read '
                         'only EAGLE 6+ XML - open it in EAGLE 6...9 (or Fusion Electronics), save it once, retry.')
            sys.exit(f'{os.path.basename(f)}: not a readable EAGLE XML file')
    cli = find_kicad_cli(a.kicad_cli)
    # the fix steps are not idempotent (milling, labels, net ties): a second run would duplicate them
    hist = os.path.join(d, 'eaglefix_history.jsonl')
    if not a.verify_only and not a.refix and os.path.isfile(hist):
        try:
            done = any(json.loads(l).get('mode') == 'fix' for l in open(hist, encoding='utf-8') if l.strip())
        except Exception:
            done = False
        if done:
            print('This project was already fixed by eagle2kicad_fix - running the verification only '
                  '(use --refix to force the fix steps again).')
            a.verify_only = True
            a.selftest = True
    progress(1, 'Reading the project')
    R.h('Project')
    R.p(f'project `{proj}`, sch `{os.path.basename(root_sch)}`, pcb: {has_pcb}')
    R.p(f'Eagle sch: {esch}  |  Eagle brd: {ebrd}  |  kicad-cli: {cli}')
    for m in IMPORT_META.get('kicad_messages') or []:     # message boxes KiCad showed during the import
        R.p(f"KiCad import message ({m.get('action')}): \"{m.get('title')}\" {m.get('text') or '(text not readable)'}")
    tmpdir = d
    global VIEW
    VIEW = CliView(d, proj)
    if VIEW.multi:
        R.p(f'{len(VIEW.tls)} top-level sheets (KiCad 10 multi-root) - kicad-cli runs on a temporary hierarchical copy')

    sheets = [Sheet(p) for p in sorted(glob.glob(os.path.join(d, '*.kicad_sch')))]
    names = set()
    for src, xp in ((esch, 'drawing/schematic/parts/part'), (ebrd, 'drawing/board/elements/element')):
        if src:
            names |= {e.get('name') for e in eagle_root(src).findall(xp)}
    clash = sorted(n for n in names if kref(n) != n and kref(n) in names)
    CLASH[:] = clash
    if clash:
        R.p('**reference collision** (KiCad importer appends "0"): ' +
            ', '.join(f'Eagle `{n}` and `{kref(n)}` both became `{kref(n)}`' for n in clash) +
            ' - rename one of them in the schematic AND the PCB')

    def verify(tag):
        R.h(f'VERIFY ({tag})')
        if not cli:
            R.p('kicad-cli not found - skipped'); return
        truth_s = eagle_sch_truth(esch) if esch else None
        truth_b = eagle_brd_truth(ebrd) if ebrd else None
        net, comps = kicad_sch_netlist(cli, root_sch, tmpdir)
        if net is not None and (truth_s or truth_b):
            compare_nets(truth_s or truth_b, net, 'Eagle schematic vs KiCad schematic')
            if esch and tag != 'before':
                try:
                    verify_sch_values(esch, [Sheet(p) for p in sorted(glob.glob(os.path.join(d, '*.kicad_sch')))])
                except Exception as ex:
                    R.p(f'schematic value check failed: {ex}')
        if has_pcb and truth_b:
            compare_nets(truth_b, kicad_pcb_padnets(read_sx(pcb)), 'Eagle board vs KiCad PCB pads')
            if tag != 'before':
                try:
                    verify_geometry(pcb, ebrd)
                except Exception as ex:
                    R.p(f'geometry check failed: {ex}')
        e = erc_run(cli, root_sch, tmpdir)
        m = METRICS.setdefault(tag, {})
        m['compare'] = {k: dict(v) for k, v in LAST_CMP.items() if not k.startswith('selftest')}
        if e:
            c = erc_summary(e); print_counter('ERC', c)
            m['erc'] = {f'{t}/{sv}': n for (t, sv), n in c.items()}
        if has_pcb:
            dr = drc_run(cli, pcb, tmpdir)
            if dr:
                c = drc_summary(dr); print_counter('DRC (+schematic parity)', c)
                m['drc'] = {f'{t}/{sv}': n for (t, sv), n in c.items()}
        if tag != 'before' and GEOM_LOG:
            m['geometry'] = dict(GEOM_LOG[-1])
        if tag != 'before' and SCH_VAL:
            m['sch_values'] = dict(SCH_VAL)
        return comps

    progress(2, 'Quality control: comparing the project with the Eagle source' if a.verify_only
             else 'Measuring the native KiCad import')
    comps = verify('current' if a.verify_only else 'before')
    if a.verify_only:
        geom = dict(GEOM_LOG[-1]) if GEOM_LOG else None
        if a.selftest and has_pcb and ebrd:
            progress(40, 'Quality control: self-check')
        st = selftest(d, proj, cli, esch, ebrd) if (a.selftest and has_pcb and ebrd) else None
        progress(50, 'Writing the report')
        if has_pcb:
            try:
                check_3d_models(pcb)
            except Exception as ex:
                R.p(f'3D model check failed: {ex}')
        fails = qc_verdict(st, geom)
        if has_pcb and not a.no_render:
            progress(51, '3D renders (about a minute)')
            render_3d(cli, pcb, d)
        progress(100, 'Done')
        write_metrics(d, proj, esch, ebrd, fails, st, 'verify')
        VIEW.close(); R.save(os.path.join(d, 'eaglefix_report.md'))
        sys.exit(3 if st is False else (1 if fails else 0))

    if not a.dry_run:
        bk = os.path.join(d, '_eaglefix_backup', datetime.datetime.now().strftime('%Y%m%d_%H%M%S'))
        os.makedirs(bk)
        for f in glob.glob(os.path.join(d, '*')):
            if os.path.isfile(f) and not f.endswith(('.sch', '.brd')):
                shutil.copy2(f, bk)
        R.h('Backup'); R.p(bk)

    progress(9, 'Fixing the schematic')
    fix_power_values(sheets, esch)
    if not a.no_label_globalize:
        fix_local_labels(sheets)
    fix_pinless(sheets)
    fix_nc_pins(d, sheets, eagle_sch_truth(esch) if esch else None, a.dry_run)
    if not a.dry_run:
        for s in sheets: s.save()
    ensure_symbol_lib(d, proj, sheets, a.dry_run)

    if has_pcb:
        progress(10, 'Fixing the PCB')
        fix_pcb_sexpr(pcb, comps, a.dry_run, ebrd)
        promote_fp_edge_cuts(pcb, a.dry_run)          # first: milling is classified against the outline
        if ebrd:
            trim_empty_inner_layers(pcb, ebrd, a.dry_run)
            add_milling(pcb, ebrd, a.dry_run)
        assign_orphan_pads(pcb, a.dry_run)
        nicks = Counter()
        for sh in sheets:
            for inst in sh.instances():
                f = prop_val(inst, 'Footprint', '')
                if ':' in f: nicks[f.split(':', 1)[0]] += 1
        nick = nicks.most_common(1)[0][0] if nicks else proj + '-eagle-import'
        if a.dry_run:
            R.p('dry run: footprint library step skipped')
        else:
            progress(14, 'Building the project footprint library')
            footprint_library(pcb, d, nick, sheets, a.dry_run)
            for s in sheets: s.save()
        progress(15, 'Design rules and net classes')
        apply_design_rules(pro, ebrd, a.dry_run)
        if ebrd:
            eagle_dru_rules(d, proj, ebrd, a.dry_run)

    if cli and not a.dry_run and os.path.isfile(root_sch):
        sheets = [Sheet(p) for p in sorted(glob.glob(os.path.join(d, '*.kicad_sch')))]
        progress(16, 'Schematic clean-up (ERC passes)')
        fix_stub_wires(cli, root_sch, sheets, tmpdir)
        if not a.no_pwr_flags:
            sheets = [Sheet(p) for p in sorted(glob.glob(os.path.join(d, '*.kicad_sch')))]
            progress(22, 'Power flags')
            add_pwr_flags(cli, root_sch, sheets, tmpdir)
            for s in sheets: s.save()
        sheets = [Sheet(p) for p in sorted(glob.glob(os.path.join(d, '*.kicad_sch')))]
        progress(30, 'Restoring Eagle net names')
        restore_net_names(cli, root_sch, sheets, tmpdir, eagle_sch_truth(esch) if esch else None)
        sheets = [Sheet(p) for p in sorted(glob.glob(os.path.join(d, '*.kicad_sch')))]
        progress(33, 'No-connect flags')
        add_no_connects(cli, root_sch, sheets, tmpdir, eagle_sch_truth(esch) if esch else None)
        for s in sheets: s.save()

    if not a.dry_run:
        progress(39, 'Quality control: comparing the result with the Eagle source')
        verify('after')
    st = None
    geom = dict(GEOM_LOG[-1]) if GEOM_LOG else None
    if (a.selftest or not a.dry_run) and has_pcb and ebrd:
        progress(63, 'Quality control: self-check')
        st = selftest(d, proj, cli, esch, ebrd)
    if has_pcb and not a.dry_run:
        try:
            check_3d_models(pcb)
        except Exception as ex:
            R.p(f'3D model check failed: {ex}')
    fails = qc_verdict(st, geom) if not a.dry_run else []
    if not a.dry_run:
        progress(69, 'Writing the report')
        if has_pcb and not a.no_render:
            progress(70, '3D renders (about a minute)')
            render_3d(cli, pcb, d)
    progress(100, 'Done')
    if not a.dry_run:
        write_metrics(d, proj, esch, ebrd, fails, st, 'fix')
    VIEW.close()
    R.save(os.path.join(d, 'eaglefix_report.md'))
    print('\nReport: ' + os.path.join(d, 'eaglefix_report.md'))
    print('Next: open project, Tools > Update PCB from Schematic (F8), B (refill zones), ERC/DRC.')
    sys.exit(3 if st is False else (1 if fails else 0))

if __name__ == '__main__':
    main()
