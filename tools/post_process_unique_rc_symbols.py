#!/usr/bin/env python3
"""Task #1 - create one unique R/L/C symbol per part in the project library.

The imported Eagle library only carries generic symbols (``RES-SMD-NEW``,
``C-EUC0603_DWS``, ``COILCRAFT_0603LS0603`` ...) reused by dozens of parts with
different values and part numbers.  This script walks every ``*.kicad_sch`` of
``CAD/KrakenSDR_3v4``, collects the ``R*`` / ``C*`` / ``L*`` instances and
creates one library symbol per unique
``(base symbol, Value, Footprint, PARTNR)`` combination, named::

    <base symbol>_<Value>_<Footprint without library prefix>

e.g. ``dzo2_RES-SMD-NEW_200k_0402``, ``C-EUC0603_DWS_22uF/6.3V_C0603_DWS``,
``COILCRAFT_0603LS0603_0R_WE-KI_0603_B``.

The definition is copied verbatim from the sheet's ``lib_symbols`` cache ("copy
the symbol from schematic to the lib"), the ``NAME_1_0`` unit sub-symbols are
renamed along with it, and the part number is stored as ``MPN`` (an existing
``PARTNR`` field is renamed, otherwise a new hidden ``MPN`` field is added -
the imported symbols carry no ``PARTNR`` of their own).

Existing symbols are left untouched, so the script is safe to re-run.

Usage::

    python tools/task1_make_unique_rc_symbols.py [--dry-run] [--no-backup]
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kicad_sexp import dedent_block                                    # noqa: E402
from kraken_symbols import (LIB_NAME, MPN_FIELD, PARTNR_FIELD, SymbolLib,   # noqa: E402
                            backup_files, build_rc_plan, iter_instances,
                            rename_symbol_block, set_field_in_symbol,
                            sheet_files, write_report)


def collect_cached_blocks(sheets, log):
    """``lib:NAME`` -> verbatim definition block, dedented to library level."""
    blocks = {}
    for sheet in sheets:
        sfile, _instances, cached = iter_instances(sheet)
        for lib_id, node in cached.items():
            if lib_id not in blocks:
                blocks[lib_id] = (dedent_block(sfile.block(node), 1),
                                  os.path.basename(sheet))
    log.append('cached symbol definitions collected: %d' % len(blocks))
    return blocks


def build_new_blocks(lib, plan, cached_blocks, log):
    """Render every planned symbol that is still missing from the library."""
    new_blocks, created = [], []
    for (base, value, fp_leaf, partnr), name in sorted(plan.names.items(),
                                                       key=lambda kv: kv[1]):
        if name in lib.symbols:
            log.append('SKIP   %s (already in library, %s=%s)'
                       % (name, MPN_FIELD, lib.field(name, MPN_FIELD)))
            continue
        cached = cached_blocks.get('%s:%s' % (LIB_NAME, base))
        if cached:
            block, origin = cached
        else:
            block, origin = lib.file.block(lib.node(base)), 'library definition'
        block = rename_symbol_block(block,
                                    old_prefixed='%s:%s' % (LIB_NAME, base),
                                    old_bare=base, new_prefixed=name, new_bare=name)
        block = set_field_in_symbol(block, MPN_FIELD, partnr, rename_from=PARTNR_FIELD)
        new_blocks.append(block)
        created.append('%s  <- %s  [%s=%s]  (from %s)'
                       % (name, base, MPN_FIELD, partnr, origin))
    log += ['CREATE ' + c for c in created]
    return new_blocks


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--dry-run', action='store_true', help='report only, write nothing')
    ap.add_argument('--no-backup', action='store_true', help='skip the tools/_backup copy')
    args = ap.parse_args(argv)

    log = ['# task 1 - unique R/L/C symbols', '']
    lib = SymbolLib()
    sheets = sheet_files()
    log.append('library : %s (%d symbols)' % (lib.path, len(lib.symbols)))
    log.append('sheets  : %d' % len(sheets))

    plan, picked, notes = build_rc_plan(sheets, lib)
    log.append('R/L/C instances scanned : %d' % len(picked))
    log.append('unique parts planned    : %d' % len(plan.names))
    log += ['NOTE  ' + n for n in notes]
    log += ['WARN  ' + w for w in plan.warnings]
    log.append('')

    cached_blocks = collect_cached_blocks(sheets, log)
    new_blocks = build_new_blocks(lib, plan, cached_blocks, log)
    log += ['', 'symbols to create : %d' % len(new_blocks)]

    if args.dry_run:
        log.append('DRY RUN - nothing written')
        print(write_report('task1_make_unique_rc_symbols', log))
        return 0
    if not new_blocks:
        log.append('library already complete - nothing written')
        print(write_report('task1_make_unique_rc_symbols', log))
        return 0

    if not args.no_backup:
        log.append('backup: %s' % backup_files([lib.path], 'task1'))

    end = lib.file.span(lib.file.root)[1]
    insert_at = lib.file.text.rindex(')', 0, end)
    lib.file.save(lib.file.text[:insert_at]
                  + ''.join('\t' + b.rstrip('\n') + '\n' for b in new_blocks)
                  + lib.file.text[insert_at:])
    log.append('library now holds %d symbols (was %d)'
               % (len(lib.symbols) + len(new_blocks), len(lib.symbols)))
    print(write_report('task1_make_unique_rc_symbols', log))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
