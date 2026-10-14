#!/usr/bin/env python3
"""Task #2 - point every R/L/C instance at its own unique symbol.

For every ``R*`` / ``C*`` / ``L*`` instance of every ``*.kicad_sch`` in
``CAD/KrakenSDR_3v4`` the ``(lib_id ...)`` is replaced by the unique symbol
created by task #1 (``<base>_<Value>_<Footprint>``), and the sheet's
``lib_symbols`` cache is kept consistent:

* the definition of the new symbol is added to the sheet cache,
* the definition of the old generic symbol is removed once no instance on that
  sheet refers to it any more.

Run task #1 first.  Re-running this script is a no-op: instances that already
point at a symbol carrying their own ``MPN`` are left alone.

Usage::

    python tools/task2_replace_rc_symbols.py [--dry-run] [--no-backup]
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kicad_sexp import apply_edits, find, indent_block, parse_sexp      # noqa: E402
from kraken_symbols import (LIB_NAME, SymbolLib, backup_files,          # noqa: E402
                            build_rc_plan, is_rc_reference,
                            iter_instances, quote_string, sheet_files,
                            write_report)

CACHE_INDENT = '\t\t'


def to_cached_form(block: str, name: str) -> str:
    """Prefix the top level name of a library block for a sheet cache.

    Only the first ``(symbol "NAME" ...)`` is prefixed; the ``NAME_1_0`` unit
    sub-symbols stay unprefixed, exactly like KiCad writes them.
    """
    pattern = re.compile(r'(symbol\s+)"' + re.escape(name) + r'"')
    return pattern.sub(lambda m: m.group(1) + '"%s:%s"' % (LIB_NAME, name),
                       block, count=1)


def append_to_cache(text: str, blocks: list) -> str:
    """Append every already indented *block* at the end of ``lib_symbols``."""
    spans = {}
    root = parse_sexp(text, spans)
    ls = find(root, 'lib_symbols')
    if ls is None:
        raise ValueError('sheet has no lib_symbols section')
    ls_start, ls_end = spans[id(ls)]
    close = ls_end - 1                        # ')' closing lib_symbols
    prev = text.rindex(')', ls_start, close)  # ')' of the last cached symbol
    return (text[:prev + 1]
            + ''.join('\n' + CACHE_INDENT + b for b in blocks)
            + text[prev + 1:])


def process_sheet(path: str, lib: SymbolLib, plan, log: list, dry_run: bool):
    """Re-point one sheet; returns ``(replaced, added, removed)``."""
    sfile, instances, cached = iter_instances(path)
    name = os.path.basename(path)

    edits, wanted, users_after, users_before = [], {}, {}, {}
    for inst in instances:
        users_before[inst.base] = users_before.get(inst.base, 0) + 1
        if not is_rc_reference(inst.reference) or inst.lib != LIB_NAME:
            users_after[inst.base] = users_after.get(inst.base, 0) + 1
            continue
        new_name = plan.lookup(inst.base, inst.value, inst.footprint, inst.partnr)
        if new_name is None or new_name == inst.base:
            users_after[inst.base] = users_after.get(inst.base, 0) + 1
            continue
        edits.append((inst.lib_id_span[0], inst.lib_id_span[1],
                      '(lib_id %s)' % quote_string('%s:%s' % (LIB_NAME, new_name))))
        wanted[new_name] = wanted.get(new_name, 0) + 1
        log.append('REPL  %s: %s  %s -> %s'
                   % (name, inst.reference, inst.lib_id, '%s:%s' % (LIB_NAME, new_name)))

    # drop cached definitions that no instance on this sheet uses any more
    removals = []
    for cached_id, node in cached.items():
        base = cached_id.split(':', 1)[-1]
        if base in users_after or base in wanted:
            continue
        if base not in users_before:
            continue          # stale cache we did not cause - leave it alone
        start, end = sfile.span(node)
        removals.append((sfile.text.rfind('\n', 0, start) + 1, end, ''))
        log.append('DROP  %s: cached %s is no longer referenced' % (name, cached_id))

    # add the definitions the re-pointed instances now need
    additions = []
    for new_name in sorted(wanted):
        if '%s:%s' % (LIB_NAME, new_name) in cached:
            continue
        node = lib.node(new_name)
        if node is None:
            raise SystemExit('symbol %r is missing from the library - run task #1 first'
                             % new_name)
        additions.append(to_cached_form(indent_block(lib.file.block(node), 1), new_name))

    if not edits:
        return 0, 0, 0

    text = apply_edits(sfile.text, edits + removals)
    text = append_to_cache(text, additions)
    if not dry_run:
        sfile.save(text)
    return len(edits), len(additions), len(removals)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--dry-run', action='store_true', help='report only, write nothing')
    ap.add_argument('--no-backup', action='store_true', help='skip the tools/_backup copy')
    args = ap.parse_args(argv)

    log = ['# task 2 - replace R/L/C instances with their unique symbols', '']
    lib = SymbolLib()
    sheets = sheet_files()
    plan, picked, notes = build_rc_plan(sheets, lib)
    log.append('library : %s (%d symbols)' % (lib.path, len(lib.symbols)))
    log.append('R/L/C instances to re-point : %d' % len(picked))
    log += ['NOTE  ' + n for n in notes]
    log += ['WARN  ' + w for w in plan.warnings]
    log.append('')

    if not args.dry_run and not args.no_backup:
        log.append('backup: %s' % backup_files(sheets, 'task2'))

    replaced = added = removed = 0
    for sheet in sheets:
        c, a, r = process_sheet(sheet, lib, plan, log, args.dry_run)
        replaced, added, removed = replaced + c, added + a, removed + r
        if c:
            log.append('----  %s: %d instances re-pointed, %d cached defs added, '
                       '%d removed' % (os.path.basename(sheet), c, a, r))

    log += ['', 'lib_id replaced       : %d' % replaced,
            'cache entries added   : %d' % added,
            'cache entries removed : %d' % removed]
    if args.dry_run:
        log.append('DRY RUN - nothing written')
    print(write_report('task2_replace_rc_symbols', log))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

