#!/usr/bin/env python3
"""Regression check of the binary EAGLE converter on a folder of designs.

Converts every binary EAGLE .sch/.brd/.lbr found below the given folders into a scratch folder and
cross-checks the netlists of each converted schematic/board pair (EAGLE implicit 'pwr' pins
included). Exit code 0 when every file converts and every pair agrees.

  python tools/check_eagle_bin.py <folder> [...] [--out DIR]
"""
import argparse, os, sys, tempfile, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'plugins'))
import eagle_bin  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('paths', nargs='+')
    ap.add_argument('--out', help='folder for the converted XML (default: a temporary folder)')
    a = ap.parse_args()
    out = a.out or tempfile.mkdtemp(prefix='eagle_bin_check_')
    os.makedirs(out, exist_ok=True)
    files = []
    for p in a.paths:
        for r, _, fs in os.walk(p):
            for n in fs:
                f = os.path.join(r, n)
                if n.lower().endswith(('.sch', '.brd', '.lbr')) and eagle_bin.is_binary_eagle(f):
                    files.append(f)
    t0, conv, errors, pins = time.time(), {}, [], 0
    for f in sorted(files):
        rel = os.path.relpath(f, os.path.commonpath(a.paths) if len(a.paths) > 1 else a.paths[0])
        dst = os.path.join(out, rel.replace(os.sep, '__'))
        try:
            eagle_bin.convert_file(f, dst)
            conv[f] = dst
        except Exception as ex:
            errors.append(f'{rel}: {ex}')
    pairs = bad = 0
    for f, dst in conv.items():
        if not f.lower().endswith('.brd'):
            continue
        sch = next((conv[c] for c in (f[:-4] + '.sch', f[:-4] + '.SCH') if c in conv), None)
        if not sch:
            continue
        cc = eagle_bin.crosscheck(sch, dst)
        pairs += 1
        pins += cc['pins']
        if not cc['ok']:
            bad += 1
            errors.append(f'{os.path.basename(f)}: netlist cross-check {cc["counts"]}')
    print(f'{len(conv)}/{len(files)} binary files converted, {pairs} schematic/board pairs, {pins} pads, '
          f'{bad} pair(s) differ, {time.time() - t0:.0f} s, output in {out}')
    for e in errors:
        print('  ' + e)
    sys.exit(1 if errors else 0)


if __name__ == '__main__':
    main()
