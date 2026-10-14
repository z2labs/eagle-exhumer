"""Minimal KiCad s-expression reader used by the kraken symbol tools.

The KiCad 10 schematic / symbol files are plain s-expressions with quoted
strings.  A tiny hand written parser is enough for our purposes and keeps the
tooling dependency free (no ``pcbnew`` import required).

Every list node is registered in a *span table* mapping ``id(node)`` to the
``(start, end)`` character offsets of that node inside the source text.  The
tools use those spans to perform surgical text edits, which keeps the original
formatting (tab indentation, ``(pts (xy ..) (xy ..))`` one-liners, ...) intact
instead of re-serialising whole files.

Tokens are kept as ``str`` (atoms) or ``list`` (sub lists).  Quoted atoms carry
a leading ``"`` marker so ``foo`` and ``"foo"`` stay distinguishable; use
:func:`unquote` to get the plain value.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple, Union

Sexp = Union[str, list]
Spans = Dict[int, Tuple[int, int]]

_ESCAPES = {'n': '\n', 't': '\t', 'r': '\r', '\\': '\\', '"': '"'}


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def parse_sexp(text: str, spans: Optional[Spans] = None) -> list:
    """Parse one s-expression (``( token ... )``) into nested lists.

    When *spans* is given it is filled with ``id(node) -> (start, end)`` for
    every list node found (``end`` is exclusive).
    """
    pos = 0
    n = len(text)
    stack: List[list] = []
    starts: List[int] = []
    root: Optional[list] = None
    while pos < n:
        ch = text[pos]
        if ch.isspace():
            pos += 1
            continue
        if ch == '(':
            node: list = []
            stack.append(node)
            starts.append(pos)
            pos += 1
            continue
        if ch == ')':
            done = stack.pop()
            start = starts.pop()
            if spans is not None:
                spans[id(done)] = (start, pos + 1)
            if not stack:
                if root is not None:
                    raise ValueError('trailing data after the top level expression')
                root = done
            else:
                stack[-1].append(done)
            pos += 1
            continue
        if ch == '"':
            buf: List[str] = []
            pos += 1
            while pos < n:
                c = text[pos]
                if c == '\\':
                    nxt = text[pos + 1] if pos + 1 < n else ''
                    buf.append(_ESCAPES.get(nxt, nxt))
                    pos += 2
                    continue
                if c == '"':
                    pos += 1
                    break
                buf.append(c)
                pos += 1
            stack[-1].append('"' + ''.join(buf))
            continue
        start = pos
        while pos < n and not text[pos].isspace() and text[pos] not in '()"':
            pos += 1
        stack[-1].append(text[start:pos])
    if root is None:
        raise ValueError('no top level expression found')
    return root


def unquote(atom: str) -> str:
    """Return the value of a token, dropping the internal marker quote."""
    if atom.startswith('"'):
        return atom[1:]
    return atom


def is_quoted(atom: str) -> bool:
    return atom.startswith('"')


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def find(node: list, name: str) -> Optional[list]:
    """First direct child list whose head == *name*."""
    for item in node[1:]:
        if isinstance(item, list) and item and unquote(item[0]) == name:
            return item
    return None


def find_all(node: list, name: str) -> List[list]:
    """All direct child lists whose head == *name*."""
    return [item for item in node[1:]
            if isinstance(item, list) and item and unquote(item[0]) == name]


def text_of(text: str, spans: Spans, node: list) -> str:
    """The verbatim source slice of *node*."""
    start, end = spans[id(node)]
    return text[start:end]


# --------------------------------------------------------------------------- #
# surgical editing
# --------------------------------------------------------------------------- #
def apply_edits(text: str, edits: List[Tuple[int, int, str]]) -> str:
    """Apply non overlapping ``(start, end, replacement)`` edits to *text*."""
    ordered = sorted(edits, key=lambda e: e[0])
    out: List[str] = []
    cursor = 0
    for start, end, replacement in ordered:
        if start < cursor:
            raise ValueError('overlapping edits at offset %d' % start)
        out.append(text[cursor:start])
        out.append(replacement)
        cursor = end
    out.append(text[cursor:])
    return ''.join(out)


def dedent_block(block: str, level: int = 1, unit: str = '\t') -> str:
    """Remove *level* indentation units from every line of a verbatim block."""
    strip = unit * level
    lines = block.split('\n')
    return '\n'.join(ln[len(strip):] if ln.startswith(strip) else ln.lstrip(unit)
                     for ln in lines)



def indent_block(block: str, level: int = 1, unit: str = '\t') -> str:
    """Add *level* indentation units to every line but the first of a block."""
    pad = unit * level
    lines = block.split('\n')
    return '\n'.join([lines[0]] + [(pad + ln) if ln else ln for ln in lines[1:]])


# --------------------------------------------------------------------------- #
# name handling
# --------------------------------------------------------------------------- #
# KiCad symbol names live in quoted strings, but a schematic stores them as
# ``lib:name`` inside another quoted string, so quotes, parentheses, colons and
# whitespace must not appear in a symbol name.  Everything else that shows up in
# real values ('/', '.', '%', '+', ',') is kept verbatim.
_ILLEGAL_IN_NAME = re.compile(r'[\s"\'()\\\[\]{}|:;<>]')


def sanitize_symbol_name(name: str, replacement: str = '_') -> str:
    """Make *name* usable as a KiCad symbol name (no stripping, no reordering)."""
    cleaned = _ILLEGAL_IN_NAME.sub(replacement, name)
    while replacement * 2 in cleaned:
        cleaned = cleaned.replace(replacement * 2, replacement)
    return cleaned or name



def split_lib_id(lib_id: str) -> Tuple[str, str]:
    """``'lib:name'`` -> ``('lib', 'name')`` (no prefix -> ``('', name)``)."""
    if ':' in lib_id:
        lib, _, name = lib_id.partition(':')
        return lib, name
    return '', lib_id


def footprint_leaf(footprint: str) -> str:
    """``'KrakenSDR_3v3:C0603_DWS'`` -> ``'C0603_DWS'``."""
    return footprint.rsplit(':', 1)[-1].strip()

