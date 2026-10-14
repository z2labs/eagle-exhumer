#!/usr/bin/env python3
"""Task #3 - give the library symbols their ``MPN`` and supplier fields.

Two jobs, both driven by what the schematic instances already carry:

**MPN.**  The instances hold the manufacturer part number in an Eagle ``PARTNR``
field, the library symbols hold nothing.  The ``PARTNR`` of every non
``R*``/``C*``/``L*`` instance (the R/L/C parts are covered by task #1, which
creates one symbol per part number) becomes an ``MPN`` field.

**Suppliers.**  Eagle carried the distributor stock codes as loose instance
fields ``OC_DIGIKEY`` / ``OC_FARNELL`` / ``OC_NEWARK``.  Those are folded into a
structured block on the symbol::

    SUPPLIER1             SUPPLIER_PART_NUMBER1
    SUPPLIER2             SUPPLIER_PART_NUMBER2
    SUPPLIER3             SUPPLIER_PART_NUMBER3

The codes are read in that Digi-Key, Farnell, Newark priority order and then
packed from slot 1 upwards, so the first populated slot is always SUPPLIER1 -
a part stocked only at Newark still lands in SUPPLIER1, but keeps "Newark" as its
supplier name.  Unused slots are written empty so every patched symbol carries
the same six fields; ``--no-empty-slots`` stops after the last populated one, and
``--all-symbols`` writes the empty block onto symbols that have no order code.

Both jobs write to the symbol definition in ``KrakenSDR_3v3-eagle-import.kicad_sym``
and to the ``lib_symbols`` cache of every sheet that holds a copy, so library and
schematics stay in sync.  A symbol that would need two different values for the
same field is reported and left alone unless ``--force`` picks the most common.

Usage::

    python tools/task3_add_mpn_to_lib.py [--dry-run] [--no-backup] [--force]
                                         [--no-empty-slots] [--all-symbols]
                                         [--mpn-only] [--supplier-only]
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from libfix_sexp import apply_edits                                    # noqa: E402
from libfix_model import (LIB_NAME, MPN_FIELD, ORDER_CODE_FIELDS,   # noqa: E402
                            PARTNR_FIELD, SUPPLIER_FIELD, SymbolLib,
                            SUPPLIER_PART_NUMBER_FIELD, backup_files,
                            is_rc_reference, iter_instances, set_field_in_symbol,
                            set_supplier_fields, sheet_files, supplier_slots,
                            write_report)


def collect_partnr(sheets, log):
    """``base symbol name`` -> ``Counter({PARTNR: how many instances})``."""
    per_symbol = defaultdict(Counter)
    rc_bases = set()
    for sheet in sheets:
        _sfile, instances, _cached = iter_instances(sheet)
        for inst in instances:
            if inst.lib != LIB_NAME:
                continue
            if is_rc_reference(inst.reference):
                rc_bases.add(inst.base)
                continue
            if inst.partnr:
                per_symbol[inst.base][inst.partnr] += 1
    clash = sorted(rc_bases & set(per_symbol))
    if clash:
        log.append('WARN  %s is used by both R/L/C and other parts - MPN taken '
                   'from the non R/L/C instances only' % ', '.join(clash))
    return per_symbol


def collect_order_codes(sheets):
    """``base symbol name`` -> ``{OC field: Counter({code: instances})}``.

    Unlike ``PARTNR`` the order codes also matter on the unique R/L/C symbols,
    so every project-library instance is considered here.
    """
    per_symbol = defaultdict(lambda: defaultdict(Counter))
    for sheet in sheets:
        _sfile, instances, _cached = iter_instances(sheet)
        for inst in instances:
            if inst.lib != LIB_NAME:
                continue
            for field in ORDER_CODE_FIELDS:
                value = (inst.fields.get(field) or '').strip()
                if value:
                    per_symbol[inst.base][field][value] += 1
    return per_symbol


def resolve(counter, name, label, args, log, skipped):
    """Pick one value out of *counter*, honouring the ``--force`` rule."""
    if len(counter) == 1:
        return next(iter(counter))
    best, _n = counter.most_common(1)[0]
    log.append('%s %s has %d different %s values (%s)%s'
               % ('WARN ' if args.force else 'SKIP ', name, len(counter), label,
                  ', '.join(sorted(counter)),
                  ' -> using %s' % best if args.force else ' -> SKIPPED'))
    if args.force:
        return best
    skipped.append(name)
    return None


def build_mpn_of(per_symbol, args, log, skipped):
    mpn_of = {}
    for name in sorted(per_symbol):
        value = resolve(per_symbol[name], name, PARTNR_FIELD, args, log, skipped)
        if value is not None:
            mpn_of[name] = value
    return mpn_of


def build_supplier_of(per_codes, args, log, skipped, all_symbols):
    """``base symbol name`` -> ``[(supplier, part_number), ...]``."""
    supplier_of = {}
    for name in sorted(per_codes):
        codes, dropped = {}, False
        for field in ORDER_CODE_FIELDS:
            counter = per_codes[name].get(field)
            if not counter:
                continue
            value = resolve(counter, name, field, args, log, skipped)
            if value is None:
                dropped = True
                continue
            codes[field] = value
        if dropped:
            continue
        slots = supplier_slots(codes)
        if slots or all_symbols:
            supplier_of[name] = slots
    return supplier_of


def _patch_block(text, base, mpn_of, supplier_of, write_empty):
    """Apply both field groups to one verbatim symbol definition."""
    new = text
    if base in mpn_of:
        new = set_field_in_symbol(new, MPN_FIELD, mpn_of[base],
                                  rename_from=PARTNR_FIELD)
    if base in supplier_of:
        new = set_supplier_fields(new, supplier_of[base], write_empty)
    return new


def patch_lib(lib: SymbolLib, mpn_of: dict, supplier_of: dict,
              write_empty: bool, dry_run: bool) -> int:
    """Add/refresh the fields on the library symbol definitions."""
    edits = []
    for name in sorted(set(mpn_of) | set(supplier_of)):
        node = lib.node(name)
        if node is None:
            continue
        start, end = lib.file.span(node)
        old = lib.file.text[start:end]
        new = _patch_block(old, name, mpn_of, supplier_of, write_empty)
        if new != old:
            edits.append((start, end, new))
    if edits and not dry_run:
        lib.file.save(apply_edits(lib.file.text, edits))
    return len(edits)


def patch_sheet_caches(sheets, mpn_of: dict, supplier_of: dict,
                       write_empty: bool, dry_run: bool) -> int:
    """Mirror the same fields into the ``lib_symbols`` cache of each sheet."""
    patched = 0
    for sheet in sheets:
        sfile, _instances, cached = iter_instances(sheet)
        edits = []
        for cached_id, node in cached.items():
            base = cached_id.split(':', 1)[-1]
            if base not in mpn_of and base not in supplier_of:
                continue
            start, end = sfile.span(node)
            old = sfile.text[start:end]
            new = _patch_block(old, base, mpn_of, supplier_of, write_empty)
            if new != old:
                edits.append((start, end, new))
        if edits:
            patched += len(edits)
            if not dry_run:
                sfile.save(apply_edits(sfile.text, edits))
    return patched


def describe(slots) -> str:
    """One-line rendering of a supplier block for the report."""
    return ' | '.join('%s=%s / %s'
                      % (SUPPLIER_FIELD % (i + 1), slots[i][0],
                         SUPPLIER_PART_NUMBER_FIELD % (i + 1))
                      for i in range(len(slots))) or '(none)'


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--dry-run', action='store_true', help='report only, write nothing')
    ap.add_argument('--no-backup', action='store_true', help='skip the tools/_backup copy')
    ap.add_argument('--force', action='store_true',
                    help='also patch symbols whose instances disagree on a field')
    ap.add_argument('--no-empty-slots', action='store_true',
                    help='do not write the unused SUPPLIERn slots as empty')
    ap.add_argument('--all-symbols', action='store_true',
                    help='write an empty supplier block on symbols with no order code')
    ap.add_argument('--mpn-only', action='store_true', help='skip the supplier block')
    ap.add_argument('--supplier-only', action='store_true', help='skip the MPN field')
    args = ap.parse_args(argv)
    if args.mpn_only and args.supplier_only:
        ap.error('--mpn-only and --supplier-only cancel each other out')

    log = ['# task 3 - MPN and supplier fields on the library symbols', '']
    lib = SymbolLib()
    sheets = sheet_files()
    log.append('library : %s (%d symbols)' % (lib.path, len(lib.symbols)))

    skipped, mpn_of, supplier_of = [], {}, {}
    if not args.supplier_only:
        per_symbol = collect_partnr(sheets, log)
        mpn_of = build_mpn_of(per_symbol, args, log, skipped)
        log.append('symbols carrying a PARTNR on their instances : %d' % len(per_symbol))
    if not args.mpn_only:
        per_codes = collect_order_codes(sheets)
        supplier_of = build_supplier_of(per_codes, args, log, skipped,
                                        args.all_symbols)
        with_codes = sum(1 for codes in per_codes.values()
                         if any(codes[f] for f in ORDER_CODE_FIELDS))
        counts = Counter(len(slots) for slots in supplier_of.values())
        log.append('symbols with an order code on their instances : %d' % with_codes)
        log.append('supplier slots per patched symbol : %s'
                   % ', '.join('%d slot%s: %d' % (n, '' if n == 1 else 's', counts[n])
                               for n in sorted(counts)))

    log.append('')
    for name in sorted(mpn_of):
        log.append('MPN      %-38s %s' % (name, mpn_of[name]))
    for name in sorted(supplier_of):
        log.append('SUPPLIER %-38s %s' % (name, describe(supplier_of[name])))
        for i, (_supplier, part) in enumerate(supplier_of[name]):
            log.append('         %-38s %s = %s'
                       % ('', SUPPLIER_PART_NUMBER_FIELD % (i + 1), part))
    log += ['', 'symbols to patch (MPN)      : %d' % len(mpn_of),
            'symbols to patch (supplier) : %d' % len(supplier_of),
            'symbols skipped             : %d' % len(skipped)]

    if not args.dry_run and not args.no_backup:
        log.append('backup: %s' % backup_files([lib.path] + sheets, 'task3'))

    write_empty = not args.no_empty_slots
    lib_patched = patch_lib(lib, mpn_of, supplier_of, write_empty, args.dry_run)
    cache_patched = patch_sheet_caches(sheets, mpn_of, supplier_of,
                                       write_empty, args.dry_run)
    log += ['library symbols patched : %d' % lib_patched,
            'sheet cache copies patched : %d' % cache_patched]
    if args.dry_run:
        log.append('DRY RUN - nothing written')
    print(write_report('task3_add_mpn_to_lib', log))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

