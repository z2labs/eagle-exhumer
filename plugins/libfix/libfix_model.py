"""Shared model of a converted KiCad project for the library clean-up tools.

Everything the three task scripts need:

* locating the project folder / symbol library / schematic sheets,
* a span aware view of the symbol library (name -> node),
* iterating the symbol *instances* of every sheet with their fields,
* the deterministic ``base_Value_Footprint`` unique-symbol naming scheme,
* verbatim block helpers (copy a symbol definition, rename it, add a field).

The tools never re-serialise a whole file: they parse it to *understand* it and
then patch the original text through the character spans recorded by
:func:`libfix_sexp.parse_sexp`, so the on-disk formatting stays byte-for-byte
KiCad-like.
"""

from __future__ import annotations

import datetime
import os
import re
import shutil
from typing import Dict, List, Optional, Tuple

from libfix_sexp import (Spans, apply_edits, dedent_block, find, find_all,
                        footprint_leaf, parse_sexp, sanitize_symbol_name,
                        split_lib_id, text_of, unquote)

# --------------------------------------------------------------------------- #
# project layout
# --------------------------------------------------------------------------- #
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
# The project to work on: EXHUMER_PROJECT (set by Eagle Exhumer) or the current folder.
PROJECT_DIR = os.path.abspath(os.environ.get('EXHUMER_PROJECT') or os.getcwd())


def _project_stem(project_dir: str) -> str:
    pros = sorted(f for f in os.listdir(project_dir) if f.endswith('.kicad_pro')
                  and not f.startswith('_'))
    return os.path.splitext(pros[0])[0] if pros else os.path.basename(project_dir.rstrip(os.sep))


def _project_lib(project_dir: str) -> str:
    """The project symbol library: <project>-eagle-import as KiCad's EAGLE importer names it,
    else the only *.kicad_sym of the project."""
    stem = _project_stem(project_dir)
    if os.path.isfile(os.path.join(project_dir, stem + '-eagle-import.kicad_sym')):
        return stem + '-eagle-import'
    syms = sorted(f for f in os.listdir(project_dir) if f.endswith('.kicad_sym'))
    return os.path.splitext(syms[0])[0] if len(syms) == 1 else stem + '-eagle-import'


PROJECT_STEM = _project_stem(PROJECT_DIR)
LIB_NAME = os.environ.get('EXHUMER_PROJECT_LIB') or _project_lib(PROJECT_DIR)
LIB_PATH = os.path.join(PROJECT_DIR, LIB_NAME + '.kicad_sym')
LOG_DIR = os.path.join(PROJECT_DIR, '_eaglefix_backup', 'libfix')
BACKUP_ROOT = LOG_DIR

# R / C / L references: a single R, C or L followed by digits (KiCad style).
# Deliberately strict so that RFL* (BAV99 diodes), CKO* (test points),
# CON_* (connectors), FB* (ferrite beads), NTC*, GDT*, SJ*, Z*, LED* ... are
# *not* treated as plain R/L/C parts.
REF_RC_RE = re.compile(r'^[RCL][0-9]+\??$')

MPN_FIELD = 'MPN'
PARTNR_FIELD = 'PARTNR'
VALUE_FIELD = 'Value'

# Eagle carried the distributor stock codes as free-form instance fields.  They
# are folded into the structured supplier block below, in this priority order,
# compacted so the first populated slot is always SUPPLIER1.
ORDER_CODE_FIELDS = ('OC_DIGIKEY', 'OC_FARNELL', 'OC_NEWARK')
SUPPLIER_OF_ORDER_CODE = {'OC_DIGIKEY': 'Digi-Key',
                          'OC_FARNELL': 'Farnell',
                          'OC_NEWARK': 'Newark'}
# Eagle wrote a bare dash where a part was not stocked at that distributor, so
# "-" is a placeholder rather than an order code and must not become a supplier.
ORDER_CODE_PLACEHOLDERS = {'-', '--', 'n/a', 'na', 'none', 'tbd'}
MAX_SUPPLIERS = 3
SUPPLIER_FIELD = 'SUPPLIER%d'
SUPPLIER_PART_NUMBER_FIELD = 'SUPPLIER_PART_NUMBER%d'


def is_rc_reference(reference: str) -> bool:
    """True for plain resistor / capacitor / inductor references."""
    return bool(REF_RC_RE.match(reference.strip()))


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
class SexpFile:
    """A KiCad s-expression file kept as raw text + parsed tree + spans."""

    def __init__(self, path: str):
        self.path = path
        self.text = open(path, encoding='utf-8').read()
        self.spans: Spans = {}
        self.root = parse_sexp(self.text, self.spans)

    def span(self, node: list) -> Tuple[int, int]:
        return self.spans[id(node)]

    def block(self, node: list) -> str:
        return text_of(self.text, self.spans, node)

    def save(self, new_text: str) -> None:
        with open(self.path, 'w', encoding='utf-8', newline='') as fh:
            fh.write(new_text)


def sheet_files(project_dir: str = PROJECT_DIR) -> List[str]:
    """All ``*.kicad_sch`` of the project, root sheet first, then sorted."""
    names = sorted(f for f in os.listdir(project_dir) if f.endswith('.kicad_sch'))
    root = PROJECT_STEM + '.kicad_sch'
    if root in names:
        names.remove(root)
        names.insert(0, root)
    return [os.path.join(project_dir, n) for n in names]


# --------------------------------------------------------------------------- #
# symbol library
# --------------------------------------------------------------------------- #
class SymbolLib:
    """The project symbol library: name -> node, plus raw text access."""

    def __init__(self, path: str = LIB_PATH):
        self.file = SexpFile(path)
        self.path = path
        self.symbols: Dict[str, list] = {}
        for node in find_all(self.file.root, 'symbol'):
            self.symbols[unquote(node[1])] = node

    def __contains__(self, name: str) -> bool:
        return name in self.symbols

    def node(self, name: str) -> Optional[list]:
        return self.symbols.get(name)

    def field(self, name: str, field: str) -> Optional[str]:
        """Value of *field* of library symbol *name* (``None`` if absent)."""
        node = self.symbols.get(name)
        if node is None:
            return None
        props = instance_fields(node)
        return props.get(field)

    def append_symbol(self, block: str) -> Tuple[str]:
        """Return the new text of the lib file with *block* added last."""
        end = self.file.span(self.file.root)[1]
        insert_at = self.file.text.rindex(')', 0, end)
        return (self.file.text[:insert_at] + '\t' + block.rstrip('\n') + '\n'
                + self.file.text[insert_at:])


# --------------------------------------------------------------------------- #
# instances
# --------------------------------------------------------------------------- #
def instance_fields(node: list) -> Dict[str, str]:
    """``{property "Name" "value" ...}`` -> ``{'Name': 'value'}``."""
    out: Dict[str, str] = {}
    for prop in find_all(node, 'property'):
        if len(prop) >= 2:
            out[unquote(prop[1])] = unquote(prop[2]) if len(prop) > 2 else ''
    return out


class Instance:
    """One symbol instance placed on a schematic sheet."""

    def __init__(self, sheet: str, node: list, spans: Spans):
        self.sheet = sheet
        self.node = node
        self.lib_id_span = spans[id(find(node, 'lib_id'))]
        lid = find(node, 'lib_id')
        self.lib_id = unquote(lid[1]) if len(lid) > 1 else ''
        self.lib, self.base = split_lib_id(self.lib_id)
        self.fields = instance_fields(node)
        self.reference = self.fields.get('Reference', '')
        self.value = self.fields.get('Value', '')
        self.footprint = self.fields.get('Footprint', '')
        self.partnr = self.fields.get(PARTNR_FIELD, '')

    @property
    def in_bom(self) -> bool:
        """True when this instance can actually reach the generated BOM.

        KiCad silently drops symbols whose reference starts with ``#`` - the
        anonymous power/global symbols the Eagle import produced (``#U$70``,
        ``#VDD66``, ...) - and it honours an explicit ``exclude_from_bom`` or
        ``in_bom no`` flag.  Everything else shows up as a BOM row.
        """
        if self.reference.startswith('#'):
            return False
        flag = find(self.node, 'in_bom')
        if flag is not None and len(flag) > 1 \
                and unquote(flag[1]).lower() in ('no', 'false', '0'):
            return False
        return find(self.node, 'exclude_from_bom') is None


# --------------------------------------------------------------------------- #
# unique symbol naming
# --------------------------------------------------------------------------- #
def base_symbol_name(base: str, value: str, footprint: str) -> str:
    """``dzo2_RES-SMD-NEW`` + ``200k`` + ``Project:0402`` -> ``..._200k_0402``."""
    return sanitize_symbol_name('%s_%s_%s' % (base, value, footprint_leaf(footprint)))


def iter_instances(sheet_path: str) -> Tuple[SexpFile, List[Instance], Dict[str, list]]:
    """Parse *sheet_path* and return it plus its instances and cached symbols."""
    sfile = SexpFile(sheet_path)
    instances: List[Instance] = []
    for node in find_all(sfile.root, 'symbol'):
        if find(node, 'lib_id') is None:
            continue                      # a cached lib definition, not an instance
        instances.append(Instance(sheet_path, node, sfile.spans))
    lib_symbols = find(sfile.root, 'lib_symbols')
    cached: Dict[str, list] = {}
    if lib_symbols is not None:
        for node in find_all(lib_symbols, 'symbol'):
            cached[unquote(node[1])] = node
    return sfile, instances, cached


# --------------------------------------------------------------------------- #
# unique symbol naming
# --------------------------------------------------------------------------- #
class NamePlan:
    """Deterministic ``base/value/footprint`` -> unique symbol name mapping.

    The plain ``base_Value_Footprint`` name is used whenever a single part
    number lives behind that combination.  When two different part numbers share
    the combination (a real case here: ``RES-SMD-NEW`` / ``1k`` / ``0402`` with
    ``RC0402FR-071KL`` and ``RC1005J102CS``) the alphabetically first part
    number keeps the plain name and the others get ``_<MPN>`` appended, so no
    part number is silently merged into another one.
    """

    def __init__(self) -> None:
        # (base, value, footprint_leaf) -> list of part numbers
        self.groups: Dict[Tuple[str, str, str], List[str]] = {}
        # (base, value, footprint_leaf, partnr) -> resolved unique name
        self.names: Dict[Tuple[str, str, str, str], str] = {}
        self.warnings: List[str] = []

    def add(self, base: str, value: str, footprint: str, partnr: str) -> None:
        key = (base, value, footprint_leaf(footprint))
        group = self.groups.setdefault(key, [])
        if partnr not in group:
            group.append(partnr)

    def resolve(self, lib: SymbolLib) -> None:
        """Turn the collected groups into unique names (idempotent)."""
        for key in sorted(self.groups):
            base, value, fp_leaf = key
            partnrs = sorted(self.groups[key])
            plain = sanitize_symbol_name('%s_%s_%s' % (base, value, fp_leaf))
            if len(partnrs) > 1:
                self.warnings.append(
                    '%s / %s / %s carries %d different %s values: %s'
                    % (base, value, fp_leaf, len(partnrs), PARTNR_FIELD,
                       ', '.join(partnrs)))
            for index, partnr in enumerate(partnrs):
                name = plain if index == 0 else '%s_%s' % (plain, sanitize_symbol_name(partnr))
                self.names[key + (partnr,)] = self._avoid_clash(name, partnr, lib)

    def _avoid_clash(self, name: str, partnr: str, lib: SymbolLib) -> str:
        """Reuse *name* when it already exists carrying the same MPN, else suffix."""
        candidate = name
        counter = 1
        while candidate in lib.symbols:
            if lib.field(candidate, MPN_FIELD) == partnr:
                return candidate          # already created by a previous run
            counter += 1
            candidate = '%s_%d' % (name, counter)
        if candidate != name:
            self.warnings.append('symbol name %r was taken, using %r for %s'
                                 % (name, candidate, partnr))
        return candidate

    def lookup(self, base: str, value: str, footprint: str, partnr: str) -> Optional[str]:
        return self.names.get((base, value, footprint_leaf(footprint), partnr))


def multi_value_bases(sheets: List[str]) -> Dict[str, set]:
    """Project-library symbols used by BOM parts with more than one distinct ``Value``
    (``base -> {values}``). A library symbol can carry only one ``Value``, so such a
    symbol has to be split one-per-value like the R/L/C ones - otherwise its library
    ``Value`` stays empty and *Update Symbols from Library* empties the parts' values."""
    seen: Dict[str, set] = {}
    for sheet in sheets:
        _, instances, _ = iter_instances(sheet)
        for inst in instances:
            if inst.lib == LIB_NAME and inst.in_bom:
                seen.setdefault(inst.base, set()).add(inst.value)
    return {b: v for b, v in seen.items() if len(v) > 1}


def needs_unique(inst: Instance, multi: Dict[str, set]) -> bool:
    """R / L / C parts, and every part whose symbol is used with several values."""
    return is_rc_reference(inst.reference) or (inst.in_bom and inst.base in multi)


def build_rc_plan(sheets: List[str], lib: SymbolLib,
                  skip_converted: bool = True) -> Tuple[NamePlan, List[Instance], List[str]]:
    """Scan every sheet for R/L/C and multi-value instances and build the unique-name plan."""
    plan = NamePlan()
    picked: List[Instance] = []
    notes: List[str] = []
    multi = multi_value_bases(sheets)
    for b in sorted(multi):
        notes.append('%s is used with %d values (%s) - one symbol per value'
                     % (b, len(multi[b]), ', '.join(sorted(v or '(empty)' for v in multi[b]))[:120]))
    for sheet in sheets:
        _, instances, _ = iter_instances(sheet)
        for inst in instances:
            if not needs_unique(inst, multi):
                continue
            if inst.lib != LIB_NAME:
                notes.append('%s: %s uses %s which is not the project library - skipped'
                             % (os.path.basename(sheet), inst.reference, inst.lib_id))
                continue
            if skip_converted and inst.partnr and already_converted(inst, lib):
                continue                  # already a unique part (tool already ran)
            plan.add(inst.base, inst.value, inst.footprint, inst.partnr)
            picked.append(inst)
    plan.resolve(lib)
    return plan, picked, notes


def already_converted(inst: Instance, lib: SymbolLib) -> bool:
    """True when the instance already points at a symbol carrying its MPN."""
    if not inst.partnr:
        return False
    return lib.field(inst.base, MPN_FIELD) == inst.partnr



# --------------------------------------------------------------------------- #
# verbatim block helpers
# --------------------------------------------------------------------------- #
def quote_string(value: str) -> str:
    """Quote *value* the way KiCad writes string tokens."""
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def rename_symbol_block(block: str, old_prefixed: str, old_bare: str,
                        new_prefixed: str, new_bare: str) -> str:
    """Rename a symbol definition *and* its ``NAME_1_0`` unit sub-symbols.

    In a schematic cache the top level name is ``lib:NAME`` while the unit
    sub-symbols are plain ``NAME_1_0``; in the library both are plain.  The
    lookahead ``(?=["_])`` keeps sibling names such as ``RES-SMD-NEW0402-SHORT``
    untouched.

    Both names are replaced in a *single* regex pass: doing one pass per name
    would let the second pass re-match what the first one just wrote and double
    the suffix (``RES-SMD-NEW_1k_0402_1k_0402``).
    """
    mapping: Dict[str, str] = {}
    for old, new in ((old_prefixed, new_prefixed), (old_bare, new_bare)):
        if old and new and old != new:
            mapping[old] = new
    if not mapping:
        return block
    # longest alternative first so ``lib:NAME`` always wins over ``NAME``
    pattern = re.compile('|'.join('"' + re.escape(old) + '(?=["_])'
                                  for old in sorted(mapping, key=len, reverse=True)))
    return pattern.sub(lambda m: '"' + mapping[m.group(0)[1:]], block)


def property_block(name: str, value: str, indent: str = '\t', hide: bool = True) -> str:
    """A KiCad ``(property ...)`` node rendered at *indent*."""
    lines = [indent + '(property ' + quote_string(name) + ' ' + quote_string(value),
             indent + '\t(at 0 0 0)']
    if hide:
        lines.append(indent + '\t(hide yes)')
    lines += [indent + '\t(show_name no)',
              indent + '\t(do_not_autoplace no)',
              indent + '\t(effects',
              indent + '\t\t(font',
              indent + '\t\t\t(size 1.27 1.27)',
              indent + '\t\t)',
              indent + '\t)',
              indent + ')']
    return '\n'.join(lines)


def _leading_ws(text: str, pos: int) -> str:
    out: List[str] = []
    while pos < len(text) and text[pos] in ' \t':
        out.append(text[pos])
        pos += 1
    return ''.join(out)


def set_field_in_symbol(symbol_text: str, field: str, value: str,
                        rename_from: Optional[str] = None) -> str:
    """Add, refresh or rename *field* inside a verbatim symbol definition block.

    ``rename_from`` renames an existing field (keeping its placement and
    formatting) instead of appending a new one - that is the ``PARTNR`` ->
    ``MPN`` case of task #1.
    """
    spans: Spans = {}
    node = parse_sexp(symbol_text, spans)
    props = find_all(node, 'property')

    # 1. rename an existing field
    if rename_from:
        for prop in props:
            if unquote(prop[1]) == rename_from:
                start, end = spans[id(prop)]
                old = symbol_text[start:end]
                renamed = re.sub(r'"' + re.escape(rename_from) + r'"',
                                 quote_string(field), old, count=1)
                # the library's own PARTNR is the deviceset default, not this part's number:
                # keep the field's placement, but give it the requested value
                return set_field_in_symbol(symbol_text[:start] + renamed + symbol_text[end:], field, value)

    # 2. refresh an existing field
    for prop in props:
        if unquote(prop[1]) == field:
            start, end = spans[id(prop)]
            head, _, tail = symbol_text[start:end].partition('\n')
            new_head = re.sub(r'(' + quote_string(field) + r')\s+".*"',
                              lambda m: m.group(1) + ' ' + quote_string(value),
                              head, count=1)
            return symbol_text[:start] + new_head + ('\n' + tail if tail else '') + symbol_text[end:]

    # 3. append a new field after the last property of the symbol
    if not props:
        raise ValueError('symbol block has no property to anchor the new field')
    _, end = spans[id(props[-1])]
    line_start = symbol_text.rfind('\n', 0, end) + 1
    indent = _leading_ws(symbol_text, line_start)
    return symbol_text[:end] + '\n' + property_block(field, value, indent) + symbol_text[end:]


def supplier_slots(order_codes) -> List[tuple]:
    """Fold Eagle order-code fields into ``[(supplier, part_number), ...]``.

    *order_codes* maps ``OC_*`` field names to values; empty values and Eagle's
    ``-`` "not stocked here" placeholder are ignored.  The remaining codes are
    taken in ``ORDER_CODE_FIELDS`` priority order and then packed from slot 1
    upwards, so a part stocked only at Newark still lands in SUPPLIER1 - while
    keeping "Newark" as the supplier name.
    """
    slots = []
    for field in ORDER_CODE_FIELDS:
        value = (order_codes.get(field) or '').strip()
        if value and value.lower() not in ORDER_CODE_PLACEHOLDERS:
            slots.append((SUPPLIER_OF_ORDER_CODE[field], value))
    return slots


def set_supplier_fields(block: str, slots, write_empty: bool = True) -> str:
    """Write the ``SUPPLIERn`` / ``SUPPLIER_PART_NUMBERn`` block into *block*.

    Unused slots are written empty by default so every patched symbol carries
    the same six-field structure; pass ``write_empty=False`` to stop after the
    last populated slot.
    """
    for i in range(MAX_SUPPLIERS):
        if i >= len(slots):
            if not write_empty:
                break
            supplier, part_number = '', ''
        else:
            supplier, part_number = slots[i]
        block = set_field_in_symbol(block, SUPPLIER_FIELD % (i + 1), supplier)
        block = set_field_in_symbol(block, SUPPLIER_PART_NUMBER_FIELD % (i + 1),
                                    part_number)
    return block


# --------------------------------------------------------------------------- #
# safety / reporting
# --------------------------------------------------------------------------- #
def backup_files(paths: List[str], tag: str) -> str:
    """Copy *paths* into ``<project>/_eaglefix_backup/libfix/<stamp>_<tag>/`` and return that folder."""
    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    target = os.path.join(BACKUP_ROOT, '%s_%s' % (stamp, tag))
    os.makedirs(target, exist_ok=True)
    for path in paths:
        shutil.copy2(path, os.path.join(target, os.path.basename(path)))
    return target


def write_report(tag: str, lines: List[str]) -> str:
    """Write ``<project>/_eaglefix_backup/libfix/<tag>.log`` with *lines* and return the path."""
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, tag + '.log')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')
    return path

