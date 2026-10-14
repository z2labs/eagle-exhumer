#!/usr/bin/env python3
"""Task #1b - give every BOM symbol its ``Value`` field.

Task #1 named the unique R/L/C symbols after the instance value but copied the
*library* definition, whose ``Value`` is empty - the value only ever lived on the
schematic instance.  KiCad therefore showed a blank Value for every part, both in
the editor and in the BOM.

This script copies the ``Value`` of the schematic instances onto the library
symbol they point at, for **every component that appears in the project BOM**:

* the symbol definition in ``KrakenSDR_3v3-eagle-import.kicad_sym``, and
* the ``lib_symbols`` cache of every sheet that holds a copy of it,

so library and schematics stay in sync.  A symbol whose instances disagree on the
value cannot be resolved from the library alone and is reported and left alone
unless ``--force`` picks the most common one.

Usage::

    python tools/task1b_set_value_in_lib_symbols.py [--dry-run] [--no-backup] [--force]
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kicad_sexp import apply_edits                                    # noqa: E402
from kraken_symbols import (LIB_NAME, VALUE_FIELD, SymbolLib,         # noqa: E402
                            backup_files, iter_instances, is_rc_reference,
                            set_field_in_symbol, sheet_files, write_report)


def collect_values(sheets, log):
    """``base symbol name`` -> ``Counter({Value: how many instances})``.

    Only instances that actually reach the BOM are considered, and only those of
    the project library - symbols of the global libraries are not ours to edit.
    Also returns the symbols reached by an ``R*``/``C*``/``L*`` reference, which
    is only used to split the report.
    """
    per_symbol = defaultdict(Counter)
    rc_symbols = set()
    for sheet in sheets:
        _sfile, instances, _cached = iter_instances(sheet)
        for inst in instances:
            if inst.lib != LIB_NAME or not inst.in_bom:
                continue
            per_symbol[inst.base][inst.value] += 1
            if is_rc_reference(inst.reference):
                rc_symbols.add(inst.base)
    return per_symbol, rc_symbols


def patch_lib(lib: SymbolLib, value_of: dict, dry_run: bool) -> int:
    """Set ``Value`` on every library symbol named in *value_of*."""
    edits = []
    for name, value in value_of.items():
        node = lib.node(name)
        if node is None:
            continue
        start, end = lib.file.span(node)
        old = lib.file.text[start:end]
        new = set_field_in_symbol(old, VALUE_FIELD, value)
        if new != old:
            edits.append((start, end, new))
    if edits and not dry_run:
        lib.file.save(apply_edits(lib.file.text, edits))
    return len(edits)


def patch_sheet_caches(sheets, value_of: dict, dry_run: bool) -> int:
    """Mirror the same ``Value`` into the ``lib_symbols`` cache of each sheet."""
    patched = 0
    for sheet in sheets:
        sfile, _instances, cached = iter_instances(sheet)
        edits = []
        for cached_id, node in cached.items():
            base = cached_id.split(':', 1)[-1]
            if base not in value_of:
                continue
            start, end = sfile.span(node)
            old = sfile.text[start:end]
            new = set_field_in_symbol(old, VALUE_FIELD, value_of[base])
            if new != old:
                edits.append((start, end, new))
        if edits:
            patched += len(edits)
            if not dry_run:
                sfile.save(apply_edits(sfile.text, edits))
    return patched


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--dry-run', action='store_true', help='report only, write nothing')
    ap.add_argument('--no-backup', action='store_true', help='skip the tools/_backup copy')
    ap.add_argument('--force', action='store_true',
                    help='also patch symbols whose instances disagree on Value')
    args = ap.parse_args(argv)

    log = ['# task 1b - Value field on the BOM symbols', '']
    lib = SymbolLib()
    sheets = sheet_files()
    per_symbol, rc_symbols = collect_values(sheets, log)
    log.append('library : %s (%d symbols)' % (lib.path, len(lib.symbols)))
    log.append('symbols referenced by BOM components : %d' % len(per_symbol))

    value_of, skipped = {}, []
    for name in sorted(per_symbol):
        variants = per_symbol[name]
        if len(variants) > 1:
            best, _n = variants.most_common(1)[0]
            log.append('%s  %s has %d different %s values (%s)%s'
                       % ('WARN ' if args.force else 'SKIP ', name, len(variants),
                          VALUE_FIELD, ', '.join(sorted(v for v in variants if v)),
                          ' -> using %s' % best if args.force else
                          ' -> SKIPPED (not in the BOM, or genuinely ambiguous)'))
            if not args.force:
                skipped.append(name)
                continue
            value_of[name] = best
        else:
            value_of[name] = next(iter(variants))

    rc = sum(1 for n in value_of if n in rc_symbols)
    log += ['', 'R/L/C symbols : %d, other symbols : %d' % (rc, len(value_of) - rc)]
    for name in sorted(value_of):
        log.append('VALUE %-40s %s' % (name, value_of[name] or '(empty)'))
    log += ['', 'symbols to patch : %d' % len(value_of),
            'symbols skipped  : %d' % len(skipped)]

    if not args.dry_run and not args.no_backup:
        log.append('backup: %s' % backup_files([lib.path] + sheets, 'task1b'))

    lib_patched = patch_lib(lib, value_of, args.dry_run)
    cache_patched = patch_sheet_caches(sheets, value_of, args.dry_run)
    log += ['library symbols patched : %d' % lib_patched,
            'sheet cache copies patched : %d' % cache_patched]
    if args.dry_run:
        log.append('DRY RUN - nothing written')
    print(write_report('task1b_set_value_in_lib_symbols', log))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
