#!/usr/bin/env python3
"""Validate ``CAD/KrakenSDR_3v4`` with ``kicad-cli`` and summarise the result.

Run it before and after the task scripts and compare the two reports::

    python tools/verify_with_kicad_cli.py baseline
    python tools/task1_make_unique_rc_symbols.py
    python tools/task2_replace_rc_symbols.py
    python tools/task3_add_mpn_to_lib.py
    python tools/verify_with_kicad_cli.py after

Checks performed on every ``*.kicad_sch`` of the project:

* ``kicad-cli sch erc``            - electrical rules / broken symbol references
* ``kicad-cli sch export netlist`` - the whole hierarchy still elaborates
* ``kicad-cli sym list``           - the symbol library still parses

Outputs land in ``tools/_verify/<tag>/``; a short summary is written to
``tools/verify_<tag>.log``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from libfix_model import LIB_PATH, PROJECT_DIR, sheet_files, write_report

KICAD_CLI = os.environ.get('KICAD_CLI', r'C:\Program Files\KiCad\10.0\bin\kicad-cli.exe')


def run(args, out_path):
    cmd = [KICAD_CLI] + args
    proc = subprocess.run(cmd, capture_output=True, text=True)
    with open(out_path + '.cmd.log', 'w', encoding='utf-8') as fh:
        fh.write(' '.join(cmd) + '\n--- stdout ---\n' + (proc.stdout or '') +
                 '\n--- stderr ---\n' + (proc.stderr or ''))
    return proc


def count_erc(path: str) -> tuple:
    """Return ``(violations, unconnected)`` from an ERC JSON report."""
    if not os.path.exists(path):
        return -1, -1
    try:
        data = json.load(open(path, encoding='utf-8-sig'))
    except (ValueError, OSError):
        return -1, -1
    counts = {}
    for sheet in data.get('sheets', []):
        for item in sheet.get('violations', []):
            counts[item.get('type', '?')] = counts.get(item.get('type', '?'), 0) + 1
    return sum(counts.values()), counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('tag', nargs='?', default='check', help='name for this run')
    ap.add_argument('--root-only', action='store_true', default=None,
                    help='only run ERC + netlist + BOM on the root sheet')
    args = ap.parse_args(argv)

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_verify', args.tag)
    os.makedirs(out_dir, exist_ok=True)
    log = ['# kicad-cli verification (%s)' % args.tag,
           'kicad-cli : %s' % KICAD_CLI, '']

    if not os.path.exists(KICAD_CLI):
        log.append('ERROR   kicad-cli not found at %s' % KICAD_CLI)
        print(write_report('verify_' + args.tag, log))
        return 1

    # --- symbol library: KiCad round-trip parse (original file untouched) ----
    lib_dir = os.path.join(out_dir, 'lib_roundtrip')
    os.makedirs(lib_dir, exist_ok=True)
    proc = run(['sym', 'upgrade', '--force', '-o', lib_dir, LIB_PATH],
               os.path.join(out_dir, 'sym_upgrade.txt'))
    log.append('sym upgrade --force : rc=%d (library parses + resaves in KiCad %s)'
               % (proc.returncode, 'OK' if proc.returncode == 0 else (proc.stderr or '').strip()))

    # --- every sheet ---------------------------------------------------------
    total_violations = 0
    per_sheet_types = {}
    targets = sheet_files()[:1] if args.root_only else sheet_files()
    totals = [0, 0, 0]                       # netlist comps, BOM rows, BOM rows w/ MPN
    for sheet in targets:
        base = os.path.splitext(os.path.basename(sheet))[0]
        erc_json = os.path.join(out_dir, base + '_erc.json')
        run(['sch', 'erc', sheet, '-o', erc_json, '--format', 'json',
             '--severity-all'], erc_json)
        violations, types = count_erc(erc_json)
        if violations < 0:
            log.append('ERC     %-22s : no report produced' % base)
            continue
        total_violations += violations
        for kind, num in (types or {}).items():
            per_sheet_types[kind] = per_sheet_types.get(kind, 0) + num
        net = os.path.join(out_dir, base + '.net')
        proc_net = run(['sch', 'export', 'netlist', sheet, '-o', net], net)
        comps = 0
        if os.path.exists(net):
            comps = sum(1 for line in open(net, encoding='utf-8', errors='replace')
                        if line.strip() == '(comp')
        bom = os.path.join(out_dir, base + '_bom.csv')
        proc_bom = run(['sch', 'export', 'bom', sheet, '-o', bom,
                        '--fields', 'Reference,Value,Footprint,MPN,PARTNR,DNP',
                        '--labels', 'Refs,Value,Footprint,MPN,PARTNR,DNP',
                        '--sort-field', 'Reference'], bom)
        with_mpn = rows = 0
        if os.path.exists(bom):
            import csv as _csv
            with open(bom, newline='', encoding='utf-8-sig') as fh:
                for row in _csv.DictReader(fh):
                    rows += 1
                    if (row.get('MPN') or '').strip():
                        with_mpn += 1
        log.append('%-22s : ERC %4d viol, netlist rc=%d comps=%3d, '
                   'bom rc=%d %3d/%3d rows with MPN'
                   % (base, violations, proc_net.returncode, comps,
                      proc_bom.returncode, with_mpn, rows))
        totals[0] += comps
        totals[1] += rows
        totals[2] += with_mpn

    log += ['', 'TOTAL ERC violations : %d' % total_violations,
            'TOTAL netlist components : %d' % totals[0],
            'TOTAL BOM rows           : %d  (%d with MPN)' % (totals[1], totals[2])]
    log += ['  %-40s %s' % (kind, num) for kind, num in sorted(per_sheet_types.items())]
    print(write_report('verify_' + args.tag, log))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
