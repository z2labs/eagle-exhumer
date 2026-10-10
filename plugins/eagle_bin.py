#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Eagle Exhumer - binary EAGLE (3.x/4.x/5.x) reader and Eagle-XML writer.

KiCad 10 and Eagle Exhumer read EAGLE 6+ XML only. Older EAGLE versions stored
boards, schematics and libraries in a proprietary binary format. This module
decodes that format and writes an equivalent EAGLE 6 XML file, so the normal
import + repair + quality-control chain can run on old designs unchanged.

Format knowledge
  * The record layout follows the format notes of pcb-rnd's io_eagle plugin
    (Tibor 'Igor2' Palinkas, Erich S. Heinzle, GPL-2.0-or-later), its KiCad port
    (common/io/eagle/eagle_bin_parser.cpp, KiCad Developers, GPL-2.0-or-later)
    and the pyeagle field notes (Alexander Pollak et al.).
  * Every field used here was re-checked against real designs (the Olimex
    OLinuXino hardware repository, EAGLE 4.16 binary files, compared with the
    same libraries / boards saved by EAGLE 6/7 as XML). Several fields differ
    from the earlier notes; see docs/eagle-binary-format.md.

File layout (all integers little endian)
  records    N x 24 byte records; record 0 (0x10) holds N at offset 4.
             Byte 0 is the record type. Containers announce the size of their
             subtrees (direct child count or recursive record count).
  strings    13 12 99 19, u32 length, NUL separated strings, u32 checksum.
             A string field that starts with 0x7F is a reference into this
             table; references are consumed in record/field order.
  rules      (boards only) tagged blocks: tag u32, length u32 (counts itself),
             payload, end sentinel u32, checksum u32. 0x20000410 design rules,
             0x20000425 net class, 0x20000523 autorouter; 99 99 99 99 ends.

Units: coordinates are 1/10 micrometre (0.1 um). Most sizes are stored as half
values (width, drill, diameter, smd dx/dy, text size).

This file is part of Eagle Exhumer (GPL-3.0). Not affiliated with Autodesk.
"""

import math
import struct
import sys
import xml.etree.ElementTree as ET

__all__ = ['is_binary_eagle', 'EagleBinaryError', 'read_binary', 'to_xml', 'convert_file']

FORMAT_VERSION = '1.0'


class EagleBinaryError(Exception):
    pass


def is_binary_eagle(path_or_bytes):
    if isinstance(path_or_bytes, (bytes, bytearray)):
        head = bytes(path_or_bytes[:2])
    else:
        with open(path_or_bytes, 'rb') as f:
            head = f.read(2)
    return len(head) == 2 and head[0] == 0x10 and head[1] in (0x00, 0x80)


# --------------------------------------------------------------------------------------------
# record tree
# --------------------------------------------------------------------------------------------
# type -> list of subsection counters (offset, length, mode); mode 'D' = number of direct
# children, 'R' = number of records in the subtree, 'T' = whole file (start record)
SUBSECTIONS = {
    0x10: [(4, 4, 'T')],
    0x14: [(4, 4, 'R'), (8, 4, 'R')],                         # schema: libraries, sheets
    0x15: [(4, 4, 'R'), (8, 4, 'R'), (12, 4, 'R')],           # library: devices, symbols, packages
    0x17: [(4, 4, 'R')],
    0x18: [(4, 4, 'R')],
    0x19: [(4, 4, 'R')],
    0x1a: [(2, 2, 'D'), (12, 4, 'R'), (16, 4, 'R'), (20, 4, 'R')],  # sheet: plain, parts, busses, nets
    0x1b: [(12, 4, 'R'), (2, 2, 'D'), (16, 4, 'R'), (20, 4, 'R')],  # board: libs, plain, elements, signals
    0x1c: [(2, 2, 'R')],
    0x1d: [(2, 2, 'R')],
    0x1e: [(2, 2, 'R')],
    0x1f: [(2, 2, 'R')],
    0x20: [(2, 2, 'R')],
    0x21: [(2, 2, 'D')],
    0x2e: [(2, 2, 'D')],
    0x30: [(2, 2, 'D')],
    0x36: [(2, 2, 'D')],
    0x37: [(4, 2, 'R'), (2, 2, 'R')],                         # deviceset: variants, gates
    0x38: [(2, 2, 'R')],
    0x3a: [(2, 2, 'R')],
}

KNOWN_TYPES = set(range(0x10, 0x15)) | {0x15, 0x17, 0x18, 0x19, 0x1a, 0x1b, 0x1c, 0x1d, 0x1e, 0x1f, 0x20,
                                         0x21, 0x22, 0x24, 0x25, 0x26, 0x27, 0x28, 0x29, 0x2a, 0x2b, 0x2c,
                                         0x2d, 0x2e, 0x2f, 0x30, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37,
                                         0x38, 0x3a, 0x3c, 0x3d, 0x3e, 0x3f, 0x40, 0x41, 0x42, 0x43, 0x44}


class Rec:
    __slots__ = ('t', 'idx', 'raw', 'groups', 'f')

    def __init__(self, t, idx, raw):
        self.t = t
        self.idx = idx
        self.raw = raw
        self.groups = []
        self.f = {}

    @property
    def kids(self):
        out = []
        for g in self.groups:
            out.extend(g)
        return out

    # raw field helpers
    def u8(self, o):
        return self.raw[o]

    def u16(self, o):
        return struct.unpack_from('<H', self.raw, o)[0]

    def i16(self, o):
        return struct.unpack_from('<h', self.raw, o)[0]

    def u32(self, o):
        return struct.unpack_from('<I', self.raw, o)[0]

    def i32(self, o):
        return struct.unpack_from('<i', self.raw, o)[0]

    def dbl(self, o):
        return struct.unpack_from('<d', self.raw, o)[0]


class Strings:
    """The trailing string table. 0x7F string fields are consumed in order; the
    pointer stored after the 0x7F byte is checked against the table offsets."""

    def __init__(self, blob):
        self.items = []
        self.offsets = []
        pos = 0
        while pos < len(blob):
            end = blob.find(b'\0', pos)
            if end < 0:
                end = len(blob)
            if end == pos:          # empty string terminates the table
                break
            self.items.append(blob[pos:end])
            self.offsets.append(pos)
            pos = end + 1
        self.cursor = 0
        self.pointers = []          # (pointer, cursor index) for the consistency check

    def take(self, ptr):
        if self.cursor >= len(self.items):
            raise EagleBinaryError('string table exhausted (more 0x7F references than strings)')
        self.pointers.append((ptr, self.cursor))
        s = self.items[self.cursor]
        self.cursor += 1
        return s

    def pointer_check(self):
        """All references must share one base: pointer - offset == const."""
        bases = {}
        for ptr, i in self.pointers:
            b = ptr - self.offsets[i]
            bases[b] = bases.get(b, 0) + 1
        return bases


def _dec(b):
    try:
        return b.decode('utf-8')
    except UnicodeDecodeError:
        return b.decode('cp1252', errors='replace')


class BinaryDrawing:
    """Parsed binary EAGLE file."""

    def __init__(self, data):
        if not is_binary_eagle(data):
            raise EagleBinaryError('not a binary EAGLE file')
        if len(data) < 24:
            raise EagleBinaryError('file too short')
        self.data = data
        self.nrec = struct.unpack_from('<I', data, 4)[0]
        if self.nrec * 24 + 8 > len(data):
            raise EagleBinaryError('record count exceeds file size')
        self.major = data[8]
        self.minor = data[9]
        self.warnings = []
        pos = self.nrec * 24
        if data[pos:pos + 4] != b'\x13\x12\x99\x19':
            raise EagleBinaryError('string table sentinel missing at offset %d' % pos)
        slen = struct.unpack_from('<I', data, pos + 4)[0]
        self.strings = Strings(data[pos + 8:pos + 8 + slen])
        self.tail_pos = pos + 8 + slen + 4
        self.root = self._read_tree()
        self._decode(self.root)
        if self.strings.cursor != len(self.strings.items):
            self.warnings.append('%d unreferenced strings in the string table'
                                 % (len(self.strings.items) - self.strings.cursor))
        bases = self.strings.pointer_check()
        self.string_pointer_bases = bases
        self.rules = self._read_rules()

    # ---------------------------------------------------------------- tree
    def _read_tree(self):
        d = self.data
        n = self.nrec
        pos = [0]

        def rd():
            i = pos[0]
            if i >= n:
                raise EagleBinaryError('record tree runs past the last record')
            raw = d[i * 24:i * 24 + 24]
            t = raw[0]
            if t not in KNOWN_TYPES:
                raise EagleBinaryError('unknown record type 0x%02x at record %d' % (t, i))
            node = Rec(t, i, raw)
            pos[0] += 1
            for (o, ln, mode) in SUBSECTIONS.get(t, ()):
                cnt = int.from_bytes(raw[o:o + ln], 'little')
                grp = []
                if mode == 'T':
                    cnt = n - 1
                    mode = 'R'
                if mode == 'D':
                    for _ in range(cnt):
                        grp.append(rd())
                else:
                    end = pos[0] + cnt
                    if end > n:
                        raise EagleBinaryError('record %d (0x%02x) subtree exceeds the file' % (i, t))
                    while pos[0] < end:
                        grp.append(rd())
                    if pos[0] != end:
                        raise EagleBinaryError('record %d (0x%02x) subtree size mismatch' % (i, t))
                node.groups.append(grp)
            return node

        root = rd()
        if root.t != 0x10:
            raise EagleBinaryError('first record is not a start record')
        if pos[0] != n:
            raise EagleBinaryError('record tree ended at %d of %d records' % (pos[0], n))
        return root

    # ---------------------------------------------------------------- strings
    def _s(self, r, off, ln):
        raw = r.raw[off:off + ln]
        if raw[:1] == b'\x7f':
            ptr = struct.unpack_from('<I', r.raw, off + 1)[0] if off + 5 <= 24 else None
            return _dec(self.strings.take(ptr))
        z = raw.find(b'\0')
        if z >= 0:
            raw = raw[:z]
        return _dec(raw)

    # ---------------------------------------------------------------- field decode
    def _decode(self, r):
        t = r.t
        f = r.f
        s = self._s
        if t == 0x12:
            f.update(display=r.u8(2) & 1, style=(r.u8(2) >> 1) & 1, unit=r.u8(3) & 0x0f,
                     altunit=r.u8(3) >> 4, multiple=r.u32(4) & 0xffffff, size=r.dbl(8), altsize=r.dbl(16))
        elif t == 0x13:
            fl = r.u8(2)
            f.update(number=r.u8(3), other=r.u8(4), fill=r.u8(5) & 0x0f, color=r.u8(6) & 0x3f,
                     visible=bool(fl & 0x0c), active=bool(fl & 0x02), side=bool(fl & 0x10),
                     name=s(r, 15, 9))
        elif t == 0x14:
            f.update(xref_format=s(r, 19, 5))
        elif t == 0x15:
            f.update(name=s(r, 16, 8))
        elif t in (0x17, 0x18):
            f.update(library=s(r, 16, 8))
        elif t == 0x19:
            f.update(library=s(r, 16, 8))
            f.update(desc=s(r, 10, 6))
        elif t == 0x1c:
            f.update(airwireshidden=bool(r.u8(12) & 0x02), netclass=r.u8(13) & 0x07, name=s(r, 16, 8))
        elif t == 0x1d:
            f.update(name=s(r, 16, 8))
        elif t == 0x1e:
            f.update(name=s(r, 18, 6))
            f.update(desc=s(r, 13, 5))
        elif t == 0x1f:
            f.update(netclass=r.u8(13) & 0x07, name=s(r, 16, 8))
        elif t == 0x21:
            b19 = r.u8(19)
            f.update(width=2 * r.u16(12), spacing=2 * r.u16(14), isolate=2 * r.u16(16), layer=r.u8(18),
                     hatch=bool(b19 & 0x01), rank=(b19 >> 1) & 0x07, thermals=bool(b19 & 0x80),
                     orphans=bool(b19 & 0x40))
        elif t == 0x22:
            self._decode_wire(r)
        elif t == 0x25:
            f.update(layer=r.u8(3), x=r.i32(4), y=r.i32(8), radius=r.i32(12), width=2 * r.u32(20))
        elif t == 0x26:
            f.update(layer=r.u8(3), x1=r.i32(4), y1=r.i32(8), x2=r.i32(12), y2=r.i32(16),
                     angle=r.u16(20) & 0x0fff)
        elif t == 0x27:
            f.update(layer=r.u8(3), x=r.i32(4), y=r.i32(8))
        elif t == 0x28:
            f.update(x=r.i32(4), y=r.i32(8), drill=2 * r.u32(12))
        elif t == 0x29:
            lay = r.u8(16)
            f.update(shape=r.u8(2) & 0x03, x=r.i32(4), y=r.i32(8), drill=2 * r.u16(12),
                     diameter=2 * r.u16(14), layer_from=(lay & 0x0f) + 1, layer_to=(lay >> 4) + 1,
                     alwaysstop=bool(r.u8(17) & 0x01))
        elif t == 0x2a:
            fl = r.u8(18)
            f.update(shape=r.u8(2) & 0x07, x=r.i32(4), y=r.i32(8), drill=2 * r.u16(12),
                     diameter=2 * r.u16(14), angle=r.u16(16) & 0x0fff, stop=not (fl & 0x01),
                     thermals=not (fl & 0x04), first=bool(fl & 0x08), name=s(r, 19, 5))
        elif t == 0x2b:
            fl = r.u8(18)
            f.update(roundness=r.u8(2), layer=r.u8(3), x=r.i32(4), y=r.i32(8), dx=2 * r.u16(12),
                     dy=2 * r.u16(14), angle=r.u16(16) & 0x0fff, stop=not (fl & 0x01),
                     cream=not (fl & 0x02), thermals=not (fl & 0x04), name=s(r, 19, 5))
        elif t == 0x2c:
            b2, b12 = r.u8(2), r.u8(12)
            f.update(function=b2 & 0x03, visible=(b2 >> 6) & 0x03, x=r.i32(4), y=r.i32(8),
                     direction=b12 & 0x0f, length=(b12 >> 4) & 0x03, angle=((b12 >> 6) & 0x03) * 1024,
                     swaplevel=r.u8(13), name=s(r, 14, 10))
        elif t == 0x2d:
            f.update(x=r.i32(4), y=r.i32(8), addlevel=r.u8(12), swaplevel=r.u8(13), symno=r.u16(14),
                     name=s(r, 16, 8))
        elif t == 0x2e:
            a = r.u16(16)
            f.update(x=r.i32(4), y=r.i32(8), libno=r.u16(12), pacno=r.u16(14), angle=a & 0x0fff,
                     mirror=bool(a & 0x1000), spin=bool(a & 0x4000))
        elif t == 0x2f:
            f.update(value=s(r, 10, 14))
            f.update(name=s(r, 2, 8))
        elif t == 0x30:
            a = r.u16(16)
            f.update(x=r.i32(4), y=r.i32(8), placed=r.i16(12), gateno=r.u16(14),
                     angle=a & 0x0c00, mirror=bool(a & 0x1000), smashed=bool(r.u8(18) & 0x01))
        elif t in (0x31, 0x33, 0x34, 0x35, 0x3f, 0x40, 0x41, 0x44):
            a = r.u16(16)
            f.update(font=r.u8(2) & 0x03, layer=r.u8(3), x=r.i32(4), y=r.i32(8), size=2 * r.u16(12),
                     ratio=(r.u8(14) & 0x7c) >> 2, angle=a & 0x0fff, mirror=bool(a & 0x1000),
                     spin=bool(a & 0x4000))
            if t in (0x31, 0x41):
                f['text'] = s(r, 18, 6)
        elif t == 0x32:
            f.update(text=_dec(r.raw[2:24].split(b'\0')[0]))
        elif t == 0x36:
            f.update(pacno=r.u16(4))
            f.update(name=s(r, 19, 5))
            f.update(table=s(r, 6, 13))
        elif t == 0x37:
            b6, b7 = r.u8(6), r.u8(7)
            f.update(value_on=bool(b6 & 0x01), con_byte=bool(b7 & 0x80), pin_bits=b7 & 0x0f)
            f.update(name=s(r, 18, 6))
            f.update(desc=s(r, 13, 5))
            f.update(prefix=s(r, 8, 5))
        elif t == 0x38:
            f.update(libno=r.u16(4), devno=r.u16(6), variant=r.u8(8), technology=r.u8(9))
            f.update(value=s(r, 16, 8))
            f.update(name=s(r, 11, 5))
        elif t == 0x3a:
            f.update(name=s(r, 4, 20))
        elif t == 0x3d:
            f.update(partno=r.u16(4), gateno=r.u16(6), pinno=r.u16(8))
        elif t == 0x3e:
            f.update(elemno=r.u16(4), padno=r.u16(6))
        elif t == 0x42:
            f.update(attribute=s(r, 7, 17))
            f.update(symbol=s(r, 2, 5))
        elif t == 0x43:
            f.update(layer=r.u8(3), x1=r.i32(4), y1=r.i32(8), x2=r.i32(12), y2=r.i32(16),
                     columns=struct.unpack_from('<b', r.raw, 20)[0], rows=struct.unpack_from('<b', r.raw, 21)[0],
                     borders=r.u8(22) & 0x0f)
        for g in r.groups:
            for k in g:
                self._decode(k)

    def _decode_wire(self, r):
        f = r.f
        lt = r.u8(23)
        st = r.u8(22)
        f.update(layer=r.u8(3), width=2 * r.u16(20), linetype=lt)
        if lt == 0x01:
            st = 0
        # bit 0x20 set = counter-clockwise arc (verified against EAGLE 6 XML of the same libraries)
        f.update(style=st & 0x03, flatcap=bool(st & 0x10), clockwise=not (st & 0x20))
        if lt == 0x81:
            neg = r.u8(19) & 0x1f
            raw = r.raw

            def i24(b, negbit):
                v = b[0] | (b[1] << 8) | (b[2] << 16)
                return v - 0x1000000 if negbit else v

            c = i24(bytes((raw[7], raw[11], raw[15])), neg & 0x01)
            x1 = i24(raw[4:7], neg & 0x02)
            y1 = i24(raw[8:11], neg & 0x04)
            x2 = i24(raw[12:15], neg & 0x08)
            y2 = i24(raw[16:19], neg & 0x10)
            f.update(x1=x1, y1=y1, x2=x2, y2=y2, arc_c=c)
        else:
            f.update(x1=r.i32(4), y1=r.i32(8), x2=r.i32(12), y2=r.i32(16))
        f['curve'] = wire_curve(f)

    # ---------------------------------------------------------------- rules (boards)
    def _read_rules(self):
        d = self.data
        pos = self.tail_pos
        rules = {'drc': None, 'classes': [], 'unknown': []}
        guard = 0
        while pos + 8 <= len(d) and guard < 1000:
            guard += 1
            tag = d[pos:pos + 4]
            if tag == b'\x99\x99\x99\x99':
                break
            ln = struct.unpack_from('<I', d, pos + 4)[0]
            if ln < 4 or pos + 4 + ln + 8 > len(d):
                self.warnings.append('truncated rules block at %d' % pos)
                break
            payload = d[pos + 8:pos + 4 + ln]
            if tag == b'\x10\x04\x00\x20':
                rules['drc'] = self._parse_drc(payload)
            elif tag == b'\x25\x04\x00\x20':
                c = self._parse_netclass(payload)
                if c:
                    rules['classes'].append(c)
            else:
                rules['unknown'].append(tag.hex())
            pos += 4 + ln + 8
        return rules

    def _parse_netclass(self, p):
        if len(p) > 20 and len(p) not in (20, 48):
            z = p.index(b'\0')
            name, rest = _dec(p[:z]), p[z + 1:]
        elif len(p) in (20, 48) and p[4:8] != b'\x21\x43\x65\x87':
            z = p.index(b'\0')
            name, rest = _dec(p[:z]), p[z + 1:]
        else:
            name, rest = '', p
        if len(rest) < 20 or rest[4:8] != b'\x21\x43\x65\x87':
            self.warnings.append('unrecognised net class block (%d bytes)' % len(p))
            return None
        num, _, width, drill = struct.unpack_from('<IIii', rest, 0)
        if len(rest) >= 48:
            clear = list(struct.unpack_from('<8i', rest, 16))
        else:
            clear = {num: struct.unpack_from('<i', rest, 16)[0]}
            clear = [clear.get(i, 0) for i in range(8)]
        return {'number': num, 'name': name, 'width': width, 'drill': drill, 'clearance': clear}

    def _parse_drc(self, p):
        i1 = p.index(b'\0')
        i2 = p.index(b'\0', i1 + 1)
        name, desc = _dec(p[:i1]), _dec(p[i1 + 1:i2])
        rest = p[i2 + 1:]
        stack = None
        if rest[:4] != b'\x78\x56\x34\x12':
            i3 = rest.index(b'\0')
            stack, rest = _dec(rest[:i3]), rest[i3 + 1:]
        if rest[:4] != b'\x78\x56\x34\x12':
            self.warnings.append('design rules: magic not found')
            return {'name': name, 'description': desc, 'layerSetup': stack, 'params': []}
        b = rest[4:]
        I = lambda o: struct.unpack_from('<i', b, o)[0]
        D = lambda o: struct.unpack_from('<d', b, o)[0]
        P = []

        def dist(nm, o):
            P.append((nm, _mm(I(o)) + 'mm'))

        v5 = len(b) >= 426 - 4
        for k, nm in enumerate(('mdWireWire', 'mdWirePad', 'mdWireVia', 'mdPadPad', 'mdPadVia', 'mdViaVia',
                                'mdSmdPad', 'mdSmdVia', 'mdSmdSmd')):
            dist(nm, 4 * k)
        dist('mdViaViaSameLayer', 36)
        P.append(('mnLayersViaInSmd', str(I(40))))
        dist('mdCopperDimension', 44)
        dist('mdDrill', 52)
        dist('mdSmdStop', 56)
        dist('msWidth', 64)
        dist('msDrill', 68)
        if v5:
            dist('msMicroVia', 72)
            P.append(('msBlindViaRatio', _num(D(76))))
            names = ('PadTop', 'PadInner', 'PadBottom', 'ViaOuter', 'ViaInner', 'MicroViaOuter', 'MicroViaInner')
            for k, nm in enumerate(names):
                P.append(('rv' + nm, _num(D(84 + 8 * k))))
            o = 140
            for nm in names:
                dist('rlMin' + nm, o)
                dist('rlMax' + nm, o + 4)
                o += 8
            o = 196
        else:
            names = ('PadTop', 'PadInner', 'PadBottom', 'ViaOuter', 'ViaInner')
            for k, nm in enumerate(names):
                P.append(('rv' + nm, _num(D(72 + 8 * k))))
            o = 112
            for nm in names:
                dist('rlMin' + nm, o)
                dist('rlMax' + nm, o + 4)
                o += 8
        P.append(('psTop', str(I(o))))
        P.append(('psBottom', str(I(o + 4))))
        P.append(('psFirst', str(I(o + 8))))
        o += 12
        P.append(('mvStopFrame', _num(D(o))))
        P.append(('mvCreamFrame', _num(D(o + 8))))
        dist('mlMinStopFrame', o + 16)
        dist('mlMaxStopFrame', o + 20)
        dist('mlMinCreamFrame', o + 24)
        dist('mlMaxCreamFrame', o + 28)
        dist('mlViaStopLimit', o + 32)
        o += 36
        P.append(('srRoundness', _num(D(o))))
        dist('srMinRoundness', o + 8)
        dist('srMaxRoundness', o + 12)
        o += 16
        # supply gap (percentage, min, max) and supply annulus / thermal widths (EAGLE 4/5 only,
        # no XML counterpart except the thermal isolation)
        o += 16
        dist('slThermalIsolate', o + 4)
        o += 8
        P.append(('slThermalsForVias', str(b[o + 2])))
        P.append(('checkGrid', str(b[o + 3])))
        P.append(('checkAngle', str(b[o + 4])))
        if v5:
            P.append(('checkFont', str(b[o + 9])))
            P.append(('checkRestrict', str(b[o + 10])))
            P.append(('useDiameter', str(b[o + 11])))
            P.append(('psElongationLong', str(b[o + 12])))
            P.append(('psElongationOffset', str(b[o + 13])))
            o2 = o + 14
            cop = struct.unpack_from('<16i', b, o2)
            iso = struct.unpack_from('<15i', b, o2 + 64)
            P.insert(0, ('mtIsolate', ' '.join(_mm(x) + 'mm' for x in iso)))
            P.insert(0, ('mtCopper', ' '.join(_mm(x) + 'mm' for x in cop)))
        P.append(('maxErrors', str(struct.unpack_from('<i', b, o + 5)[0])))
        return {'name': name, 'description': desc, 'layerSetup': stack, 'params': P}

    # ---------------------------------------------------------------- helpers
    def find(self, t):
        for k in self.root.kids:
            if k.t == t:
                return k
        return None

    @property
    def kind(self):
        if self.find(0x1b):
            return 'board'
        if self.find(0x14):
            return 'schematic'
        if self.find(0x15):
            return 'library'
        return 'unknown'


# --------------------------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------------------------
QUADRANT_ARCS = {0x78: (90, 'minmin'), 0x79: (90, 'maxmin'), 0x7a: (90, 'maxmax'), 0x7b: (90, 'minmax'),
                 0x7c: (180, 'mid'), 0x7d: (180, 'mid'), 0x7e: (180, 'mid'), 0x7f: (180, 'mid')}


def arc_center(f):
    lt = f['linetype']
    x1, y1, x2, y2 = f['x1'], f['y1'], f['x2'], f['y2']
    if lt == 0x81:
        c = f['arc_c']
        x3, y3 = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        if x1 == x2 and y1 == y2:
            return float(x1), float(y1)
        if abs(x2 - x1) < abs(y2 - y1):
            cx = float(c)
            cy = (x3 - cx) * (x2 - x1) / float(y2 - y1) + y3
        else:
            cy = float(c)
            cx = (y3 - cy) * (y2 - y1) / float(x2 - x1) + x3
        return cx, cy
    if lt in QUADRANT_ARCS:
        kind = QUADRANT_ARCS[lt][1]
        if kind == 'mid':
            return (x1 + x2) / 2.0, (y1 + y2) / 2.0
        cx = min(x1, x2) if kind[:3] == 'min' else max(x1, x2)
        cy = min(y1, y2) if kind[3:] == 'min' else max(y1, y2)
        return float(cx), float(cy)
    return None


def wire_curve(f):
    """Signed sweep angle (degrees, CCW positive) for an EAGLE XML wire, 0 for straight."""
    lt = f['linetype']
    if lt in (0x00, 0x01) or lt not in QUADRANT_ARCS and lt != 0x81:
        return 0.0
    cen = arc_center(f)
    if cen is None:
        return 0.0
    cx, cy = cen
    a1 = math.atan2(f['y1'] - cy, f['x1'] - cx)
    a2 = math.atan2(f['y2'] - cy, f['x2'] - cx)
    ccw = (a2 - a1) % (2 * math.pi)            # CCW sweep from start to end
    if lt in QUADRANT_ARCS:
        mag = QUADRANT_ARCS[lt][0]
        return -float(mag) if f['clockwise'] else float(mag)
    deg = math.degrees(ccw)
    if f['clockwise']:
        deg = deg - 360.0
    if abs(deg) < 1e-9:
        deg = 360.0 if not f['clockwise'] else -360.0
    return deg


def _mm(v):
    """0.1 um integer -> mm string with up to 4 decimals, trailing zeros trimmed."""
    s = '%.4f' % (v / 10000.0)
    s = s.rstrip('0').rstrip('.')
    return '0' if s in ('-0', '') else s


def _num(x):
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    s = ('%.6f' % x).rstrip('0').rstrip('.')
    return s


def _rot(angle4096, mirror=False, spin=False):
    # EAGLE 6 writes angles with 0.1 degree resolution when it converts old files
    deg = round(angle4096 * 360.0 / 4096.0, 1)
    s = ('%.1f' % deg).rstrip('0').rstrip('.')
    pre = ('S' if spin else '') + ('M' if mirror else '')
    if s == '0' and not pre:
        return None
    return pre + 'R' + s


def _default_value(dsname, tech, dev):
    v = dsname.replace('*', tech) if '*' in dsname else dsname + tech
    return v.replace('?', dev) if '?' in v else v + dev


def _yn(b):
    return 'yes' if b else 'no'


GRID_UNITS = {0: 'mic', 5: 'mm', 10: 'mil', 15: 'inch'}
PIN_LENGTH = ('point', 'short', 'middle', 'long')
PIN_DIRECTION = ('nc', 'in', 'out', 'io', 'oc', 'pwr', 'pas', 'hiz', 'sup')
PIN_VISIBLE = ('off', 'pad', 'pin', 'both')
PIN_FUNCTION = ('none', 'dot', 'clk', 'dotclk')
PAD_SHAPE = ('square', 'round', 'octagon', 'long', 'offset')
VIA_SHAPE = ('square', 'round', 'octagon')
WIRE_STYLE = ('continuous', 'longdash', 'shortdash', 'dashdot')
FONTS = ('vector', 'proportional', 'fixed')
ADDLEVEL = ('must', 'can', 'next', 'request', 'always')


# --------------------------------------------------------------------------------------------
# XML writer
# --------------------------------------------------------------------------------------------
class XmlWriter:
    def __init__(self, drw):
        self.d = drw
        self.notes = []

    def E(self, parent, tag, attrs=(), text=None):
        el = ET.SubElement(parent, tag)
        for k, v in attrs:
            if v is not None:
                el.set(k, v if isinstance(v, str) else str(v))
        if text is not None:
            el.text = text
        return el

    # ---------- primitives
    def wire(self, parent, r, layer=None):
        f = r.f
        a = [('x1', _mm(f['x1'])), ('y1', _mm(f['y1'])), ('x2', _mm(f['x2'])), ('y2', _mm(f['y2'])),
             ('width', _mm(f['width'])), ('layer', str(layer if layer is not None else f['layer']))]
        if f['curve']:
            a.append(('curve', _num(round(f['curve'], 6))))
        if f['style']:
            a.append(('style', WIRE_STYLE[f['style']]))
        if f['flatcap'] and f['curve']:
            a.append(('cap', 'flat'))
        return self.E(parent, 'wire', a)

    def text(self, parent, r, tag='text', extra=()):
        f = r.f
        a = list(extra) + [('x', _mm(f['x'])), ('y', _mm(f['y'])), ('size', _mm(f['size'])),
                           ('layer', str(f['layer']))]
        # font code: 0 vector, 1 proportional (the XML default), 2 fixed
        if f.get('font', 0) != 1:
            a.append(('font', FONTS[f['font']] if f['font'] < 3 else 'vector'))
        if f.get('ratio', 8) != 8:
            a.append(('ratio', str(f['ratio'])))
        a.append(('rot', _rot(f['angle'], f['mirror'], f['spin'])))
        return self.E(parent, tag, a, f.get('text') if tag == 'text' else None)

    def circle(self, parent, r):
        f = r.f
        return self.E(parent, 'circle', [('x', _mm(f['x'])), ('y', _mm(f['y'])), ('radius', _mm(f['radius'])),
                                         ('width', _mm(f['width'])), ('layer', str(f['layer']))])

    def rect(self, parent, r):
        f = r.f
        return self.E(parent, 'rectangle', [('x1', _mm(f['x1'])), ('y1', _mm(f['y1'])), ('x2', _mm(f['x2'])),
                                            ('y2', _mm(f['y2'])), ('layer', str(f['layer'])),
                                            ('rot', _rot(f['angle']))])

    def polygon(self, parent, r, signal=False):
        f = r.f
        a = [('width', _mm(f['width'])), ('layer', str(f['layer']))]
        if f['spacing'] and f['spacing'] != 12700:
            a.append(('spacing', _mm(f['spacing'])))
        if f['hatch']:
            a.append(('pour', 'hatch'))
        # EAGLE keeps an isolate value on every polygon, but it only means something on copper;
        # EAGLE 6 drops it for package/plain polygons on other layers
        if f['isolate'] and (signal or 1 <= f['layer'] <= 16):
            a.append(('isolate', _mm(f['isolate'])))
        if not f['orphans'] and False:
            pass
        if f['orphans']:
            a.append(('orphans', 'yes'))
        if not f['thermals']:
            a.append(('thermals', 'no'))
        if signal and f['rank'] not in (0, 1):
            a.append(('rank', str(f['rank'])))
        el = self.E(parent, 'polygon', a)
        for w in r.kids:
            if w.t != 0x22:
                continue
            va = [('x', _mm(w.f['x1'])), ('y', _mm(w.f['y1']))]
            if w.f['curve']:
                va.append(('curve', _num(round(w.f['curve'], 6))))
            self.E(el, 'vertex', va)
        return el

    def hole(self, parent, r):
        f = r.f
        return self.E(parent, 'hole', [('x', _mm(f['x'])), ('y', _mm(f['y'])), ('drill', _mm(f['drill']))])

    def frame(self, parent, r):
        f = r.f
        b = f['borders']
        a = [('x1', _mm(f['x1'])), ('y1', _mm(f['y1'])), ('x2', _mm(f['x2'])), ('y2', _mm(f['y2'])),
             ('columns', str(f['columns'])), ('rows', str(f['rows'])), ('layer', str(f['layer']))]
        for bit, nm in ((1, 'border-bottom'), (2, 'border-right'), (4, 'border-top'), (8, 'border-left')):
            if not b & bit:
                a.append((nm, 'no'))
        return self.E(parent, 'frame', a)

    def drawable(self, parent, r, signal=False):
        t = r.t
        if t == 0x22:
            return self.wire(parent, r)
        if t == 0x31:
            return self.text(parent, r)
        if t == 0x25:
            return self.circle(parent, r)
        if t == 0x26:
            return self.rect(parent, r)
        if t == 0x21:
            return self.polygon(parent, r, signal)
        if t == 0x28:
            return self.hole(parent, r)
        if t == 0x43:
            return self.frame(parent, r)
        if t == 0x32:
            return None
        self.notes.append('dropped record 0x%02x in plain/drawing' % t)
        return None

    # ---------- library parts
    def package(self, parent, pk):
        el = self.E(parent, 'package', [('name', pk.f['name'])])
        if pk.f.get('desc'):
            self.E(el, 'description', (), pk.f['desc'])
        for r in pk.kids:
            t = r.t
            f = r.f
            if t == 0x2a:
                a = [('name', f['name']), ('x', _mm(f['x'])), ('y', _mm(f['y'])), ('drill', _mm(f['drill']))]
                if f['diameter']:
                    a.append(('diameter', _mm(f['diameter'])))
                if f['shape'] != 1:
                    a.append(('shape', PAD_SHAPE[f['shape']] if f['shape'] < 5 else 'round'))
                a.append(('rot', _rot(f['angle'])))
                if not f['stop']:
                    a.append(('stop', 'no'))
                if not f['thermals']:
                    a.append(('thermals', 'no'))
                if f['first']:
                    a.append(('first', 'yes'))
                self.E(el, 'pad', a)
            elif t == 0x2b:
                a = [('name', f['name']), ('x', _mm(f['x'])), ('y', _mm(f['y'])), ('dx', _mm(f['dx'])),
                     ('dy', _mm(f['dy'])), ('layer', str(f['layer']))]
                if f['roundness']:
                    a.append(('roundness', str(f['roundness'])))
                a.append(('rot', _rot(f['angle'])))
                if not f['stop']:
                    a.append(('stop', 'no'))
                if not f['thermals']:
                    a.append(('thermals', 'no'))
                if not f['cream']:
                    a.append(('cream', 'no'))
                self.E(el, 'smd', a)
            else:
                self.drawable(el, r)
        return el

    def symbol(self, parent, sy):
        el = self.E(parent, 'symbol', [('name', sy.f['name'])])
        for r in sy.kids:
            if r.t == 0x2c:
                f = r.f
                a = [('name', f['name']), ('x', _mm(f['x'])), ('y', _mm(f['y']))]
                if f['visible'] != 3:
                    a.append(('visible', PIN_VISIBLE[f['visible']]))
                if f['length'] != 3:
                    a.append(('length', PIN_LENGTH[f['length']]))
                if f['direction'] != 3:
                    a.append(('direction', PIN_DIRECTION[f['direction']] if f['direction'] < 9 else 'io'))
                if f['function']:
                    a.append(('function', PIN_FUNCTION[f['function']]))
                if f['swaplevel']:
                    a.append(('swaplevel', str(f['swaplevel'])))
                a.append(('rot', _rot(f['angle'])))
                self.E(el, 'pin', a)
            else:
                self.drawable(el, r)
        return el

    # ---------- board
    def board(self, eagle_drawing):
        d = self.d
        b = d.find(0x1b)
        libs_g, plain_g, elems_g, sigs_g = b.groups
        board = self.E(eagle_drawing, 'board')
        plain = self.E(board, 'plain')
        for r in plain_g:
            self.drawable(plain, r)
        # libraries: one 0x19 record per library in board files
        libs = self.E(board, 'libraries')
        lib_names = []
        used = {}
        lib_pkgs = []
        for L in libs_g:
            name = L.f.get('library') or 'lib%d' % (len(lib_names) + 1)
            if name in used:
                used[name] += 1
                name = '%s_%d' % (name, used[name])
                self.notes.append('duplicate library name renamed to %s' % name)
            else:
                used[name] = 1
            lib_names.append(name)
            le = self.E(libs, 'library', [('name', name)])
            if L.f.get('desc'):
                self.E(le, 'description', (), L.f['desc'])
            pe = self.E(le, 'packages')
            pk_list = [k for k in L.kids if k.t == 0x1e]
            lib_pkgs.append(pk_list)
            for pk in pk_list:
                self.package(pe, pk)
        self.E(board, 'attributes')
        self.E(board, 'variantdefs')
        classes = self.E(board, 'classes')
        cl = sorted(d.rules['classes'], key=lambda c: c['number'])
        if not any(c['number'] == 0 for c in cl):
            cl.insert(0, {'number': 0, 'name': 'default', 'width': 0, 'drill': 0, 'clearance': [0] * 8})
        for c in cl:
            if c['number'] != 0 and not c['name'] and not c['width'] and not c['drill'] and not any(c['clearance']):
                continue
            ce = self.E(classes, 'class', [('number', str(c['number'])), ('name', c['name'] or str(c['number'])),
                                           ('width', _mm(c['width'])), ('drill', _mm(c['drill']))])
            for k, v in enumerate(c['clearance']):
                if v:
                    self.E(ce, 'clearance', [('class', str(k)), ('value', _mm(v))])
        drc = d.rules['drc']
        if drc:
            de = self.E(board, 'designrules', [('name', drc['name'] or 'default')])
            if drc['description']:
                self.E(de, 'description', [('language', 'en')], drc['description'])
            if drc['layerSetup']:
                self.E(de, 'param', [('name', 'layerSetup'), ('value', drc['layerSetup'])])
            for k, v in drc['params']:
                self.E(de, 'param', [('name', k), ('value', v)])
        self.E(board, 'autorouter')
        # elements
        elems = self.E(board, 'elements')
        elem_list = []
        for r in elems_g:
            if r.t != 0x2e:
                continue
            f = r.f
            e2 = next((k for k in r.kids if k.t == 0x2f), None)
            name = e2.f['name'] if e2 else 'E%d' % (len(elem_list) + 1)
            value = e2.f['value'] if e2 else ''
            li = f['libno'] - 1
            pi = f['pacno'] - 1
            if not (0 <= li < len(lib_names)) or not (0 <= pi < len(lib_pkgs[li])):
                raise EagleBinaryError('element %s references a missing package (%d/%d)'
                                       % (name, f['libno'], f['pacno']))
            pk = lib_pkgs[li][pi]
            elem_list.append((name, pk))
            smashed = any(k.t in (0x34, 0x35) for k in r.kids)
            a = [('name', name), ('library', lib_names[li]), ('package', pk.f['name']), ('value', value),
                 ('x', _mm(f['x'])), ('y', _mm(f['y'])), ('rot', _rot(f['angle'], f['mirror'], f['spin']))]
            if smashed:
                a.append(('smashed', 'yes'))
            ee = self.E(elems, 'element', a)
            for k in r.kids:
                if k.t == 0x34:
                    self.text(ee, k, 'attribute', [('name', 'NAME')])
                elif k.t == 0x35:
                    self.text(ee, k, 'attribute', [('name', 'VALUE')])
                elif k.t == 0x3f:
                    self.text(ee, k, 'attribute', [('name', 'PART')])
        # signals
        sigs = self.E(board, 'signals')
        self._signals(sigs, sigs_g, elem_list)
        return board

    def _signals(self, sigs, group, elem_list):
        for s in group:
            if s.t != 0x1c:
                continue
            f = s.f
            a = [('name', f['name'])]
            if f['netclass']:
                a.append(('class', str(f['netclass'])))
            if f['airwireshidden']:
                a.append(('airwireshidden', 'yes'))
            se = self.E(sigs, 'signal', a)
            nested = []
            for r in s.kids:
                t = r.t
                if t == 0x3e:
                    ei = r.f['elemno'] - 1
                    if not 0 <= ei < len(elem_list):
                        raise EagleBinaryError('contact reference to missing element %d in %s'
                                               % (r.f['elemno'], f['name']))
                    ename, pk = elem_list[ei]
                    pads = [k for k in pk.kids if k.t in (0x2a, 0x2b)]
                    pi = r.f['padno'] - 1
                    if not 0 <= pi < len(pads):
                        raise EagleBinaryError('contact reference to missing pad %d of %s'
                                               % (r.f['padno'], ename))
                    self.E(se, 'contactref', [('element', ename), ('pad', pads[pi].f['name'])])
                elif t == 0x22:
                    self.wire(se, r)
                elif t == 0x29:
                    vf = r.f
                    va = [('x', _mm(vf['x'])), ('y', _mm(vf['y'])),
                          ('extent', '%d-%d' % (vf['layer_from'], vf['layer_to'])),
                          ('drill', _mm(vf['drill']))]
                    if vf['diameter']:
                        va.append(('diameter', _mm(vf['diameter'])))
                    if vf['shape'] != 1:
                        va.append(('shape', VIA_SHAPE[vf['shape']]))
                    if vf['alwaysstop']:
                        va.append(('alwaysstop', 'yes'))
                    self.E(se, 'via', va)
                elif t == 0x21:
                    self.polygon(se, r, signal=True)
                elif t == 0x1c:
                    nested.append(r)
                else:
                    self.notes.append('dropped record 0x%02x in signal %s' % (t, f['name']))
            if nested:
                self.notes.append('nested signals flattened in %s' % f['name'])
                self._signals(sigs, nested, elem_list)

    # ---------- schematic
    def schematic(self, eagle_drawing):
        d = self.d
        sch = d.find(0x14)
        libs_g, sheets_g = sch.groups
        se = self.E(eagle_drawing, 'schematic', [('xreflabel', sch.f.get('xref_format') or None)])
        self.E(se, 'description')
        libs = self.E(se, 'libraries')
        self.libinfo = []
        used = {}
        for L in libs_g:
            if L.t != 0x15:
                continue
            self.libinfo.append(self.library(libs, L, used))
        self.E(se, 'attributes')
        self.E(se, 'variantdefs')
        cls = self.E(se, 'classes')
        self.E(cls, 'class', [('number', '0'), ('name', 'default'), ('width', '0'), ('drill', '0')])
        maxcls = 0
        for sh in sheets_g:
            for n in (sh.groups[3] if len(sh.groups) > 3 else []):
                if n.t == 0x1f:
                    maxcls = max(maxcls, n.f['netclass'])
        for k in range(1, maxcls + 1):
            self.E(cls, 'class', [('number', str(k)), ('name', str(k)), ('width', '0'), ('drill', '0')])
        parts = self.E(se, 'parts')
        sheets = self.E(se, 'sheets')
        seen = {}
        for sh in sheets_g:
            if sh.t != 0x1a:
                continue
            self.sheet(sheets, sh, parts, seen)
        return se

    def library(self, libs, L, used):
        devs_g, syms_g, pkgs_g = L.groups
        dev_sec = devs_g[0] if devs_g else None
        sym_sec = syms_g[0] if syms_g else None
        pkg_sec = pkgs_g[0] if pkgs_g else None
        name = L.f.get('name') or (dev_sec.f.get('library') if dev_sec else '') or 'lib%d' % (len(self.libinfo) + 1)
        if name in used:
            used[name] += 1
            name = '%s_%d' % (name, used[name])
            self.notes.append('duplicate library name renamed to %s' % name)
        else:
            used[name] = 1
        le = self.E(libs, 'library', [('name', name)])
        if pkg_sec is not None and pkg_sec.f.get('desc'):
            self.E(le, 'description', (), pkg_sec.f['desc'])
        pk_list = [k for k in (pkg_sec.kids if pkg_sec else []) if k.t == 0x1e]
        sy_list = [k for k in (sym_sec.kids if sym_sec else []) if k.t == 0x1d]
        ds_list = [k for k in (dev_sec.kids if dev_sec else []) if k.t == 0x37]
        pe = self.E(le, 'packages')
        for pk in pk_list:
            self.package(pe, pk)
        ye = self.E(le, 'symbols')
        for sy in sy_list:
            self.symbol(ye, sy)
        de = self.E(le, 'devicesets')
        info = {'name': name, 'devicesets': []}
        for ds in ds_list:
            info['devicesets'].append(self.deviceset(de, ds, sy_list, pk_list))
        return info

    def deviceset(self, parent, ds, sy_list, pk_list):
        f = ds.f
        variants_g, gates_g = ds.groups
        gates = [g for g in gates_g if g.t == 0x2d]
        a = [('name', f['name']), ('prefix', f['prefix'] or None)]
        if f['value_on']:
            a.append(('uservalue', 'yes'))
        el = self.E(parent, 'deviceset', a)
        if f.get('desc'):
            self.E(el, 'description', (), f['desc'])
        ge = self.E(el, 'gates')
        gate_info = []
        for g in gates:
            gf = g.f
            sy = sy_list[gf['symno'] - 1] if 1 <= gf['symno'] <= len(sy_list) else None
            if sy is None:
                raise EagleBinaryError('gate %s of %s references a missing symbol' % (gf['name'], f['name']))
            ga = [('name', gf['name']), ('symbol', sy.f['name']), ('x', _mm(gf['x'])), ('y', _mm(gf['y']))]
            if gf['addlevel'] != 2:
                ga.append(('addlevel', ADDLEVEL[gf['addlevel']] if gf['addlevel'] < 5 else 'next'))
            if gf['swaplevel']:
                ga.append(('swaplevel', str(gf['swaplevel'])))
            self.E(ge, 'gate', ga)
            gate_info.append((gf['name'], [p.f['name'] for p in sy.kids if p.t == 0x2c]))
        dve = self.E(el, 'devices')
        dev_info = []
        for v in variants_g:
            if v.t != 0x36:
                continue
            vf = v.f
            vname = '' if vf['name'] in ("''", '') else vf['name']
            va = [('name', vname)]
            pk = None
            if vf['pacno']:
                if not 1 <= vf['pacno'] <= len(pk_list):
                    raise EagleBinaryError('device %s%s references a missing package' % (f['name'], vname))
                pk = pk_list[vf['pacno'] - 1]
                va.append(('package', pk.f['name']))
            de = self.E(dve, 'device', va)
            techs = [x for x in (vf.get('table') or '').split() if x != "''"] or ['']
            if pk is not None:
                ce = self.E(de, 'connects')
                pads = [k for k in pk.kids if k.t in (0x2a, 0x2b)]
                conns = self.connections(ds, v, len(pads))
                for pad, (gi, pin) in zip(pads, conns):
                    if gi == 0:
                        continue
                    if not 1 <= gi <= len(gate_info) or not 1 <= pin <= len(gate_info[gi - 1][1]):
                        raise EagleBinaryError('bad connection %d/%d in %s%s' % (gi, pin, f['name'], vname))
                    gname, pins = gate_info[gi - 1]
                    self.E(ce, 'connect', [('gate', gname), ('pin', pins[pin - 1]), ('pad', pad.f['name'])])
            te = self.E(de, 'technologies')
            for tch in techs:
                self.E(te, 'technology', [('name', tch)])
            dev_info.append((vname, techs))
        return {'name': f['name'], 'gates': gate_info, 'devices': dev_info, 'value_on': f['value_on']}

    def connections(self, ds, v, npads):
        f = ds.f
        data = b''.join(c.raw[2:24] for c in v.kids if c.t == 0x3c)
        if f['con_byte']:
            vals = list(data)
        else:
            vals = [struct.unpack_from('<H', data, i)[0] for i in range(0, len(data) - 1, 2)]
        vals = (vals + [0] * npads)[:npads]
        sh = f['pin_bits']
        mask = (1 << sh) - 1
        return [(x >> sh, x & mask) for x in vals]

    def sheet(self, sheets, sh, parts_el, seen):
        plain_g, parts_g, bus_g, nets_g = sh.groups
        se = self.E(sheets, 'sheet')
        pe = self.E(se, 'plain')
        for r in plain_g:
            self.drawable(pe, r)
        inst = self.E(se, 'instances')
        sheet_parts = []
        for p in parts_g:
            if p.t != 0x38:
                continue
            f = p.f
            li = f['libno'] - 1
            if not 0 <= li < len(self.libinfo):
                raise EagleBinaryError('part %s references missing library %d' % (f['name'], f['libno']))
            lib = self.libinfo[li]
            di = f['devno'] - 1
            if not 0 <= di < len(lib['devicesets']):
                raise EagleBinaryError('part %s references missing deviceset' % f['name'])
            ds = lib['devicesets'][di]
            vi = f['variant'] - 1
            if not 0 <= vi < len(ds['devices']):
                raise EagleBinaryError('part %s references missing device variant %d' % (f['name'], f['variant']))
            dev, techs = ds['devices'][vi]
            ti = f['technology'] - 1
            tech = techs[ti] if 0 <= ti < len(techs) else ''
            sheet_parts.append((f['name'], ds))
            if f['name'] not in seen:
                seen[f['name']] = True
                a = [('name', f['name']), ('library', lib['name']), ('deviceset', ds['name']), ('device', dev)]
                if tech:
                    a.append(('technology', tech))
                # EAGLE 6 omits the value when it equals the generated default (deviceset name with
                # technology and device variant filled in) and keeps an empty value as value=""
                if f['value'] != _default_value(ds['name'], tech, dev):
                    a.append(('value', f['value']))
                self.E(parts_el, 'part', a)
            for k in p.kids:
                if k.t != 0x30:
                    continue
                kf = k.f
                gi = kf['gateno'] - 1
                if not 0 <= gi < len(ds['gates']):
                    raise EagleBinaryError('instance of %s references missing gate' % f['name'])
                ia = [('part', f['name']), ('gate', ds['gates'][gi][0]), ('x', _mm(kf['x'])), ('y', _mm(kf['y']))]
                smashed = kf['smashed'] or any(t.t in (0x34, 0x35, 0x3f, 0x40) for t in k.kids)
                if smashed:
                    ia.append(('smashed', 'yes'))
                ia.append(('rot', _rot(kf['angle'], kf['mirror'])))
                ie = self.E(inst, 'instance', ia)
                for t in k.kids:
                    nm = {0x34: 'NAME', 0x35: 'VALUE', 0x3f: 'PART', 0x40: 'GATE'}.get(t.t)
                    if nm:
                        self.text(ie, t, 'attribute', [('name', nm)])
        be = self.E(se, 'busses')
        for b in bus_g:
            if b.t != 0x3a:
                continue
            bb = self.E(be, 'bus', [('name', b.f['name'])])
            for seg in b.kids:
                if seg.t == 0x20:
                    self.segment(bb, seg, sheet_parts)
        ne = self.E(se, 'nets')
        for n in nets_g:
            if n.t != 0x1f:
                continue
            a = [('name', n.f['name']), ('class', str(n.f['netclass']))]
            nn = self.E(ne, 'net', a)
            for seg in n.kids:
                if seg.t == 0x20:
                    self.segment(nn, seg, sheet_parts)
        return se

    def segment(self, parent, seg, sheet_parts):
        el = self.E(parent, 'segment')
        for r in seg.kids:
            t = r.t
            if t == 0x3d:
                f = r.f
                pi = f['partno'] - 1
                if not 0 <= pi < len(sheet_parts):
                    raise EagleBinaryError('pin reference to missing part %d' % f['partno'])
                pname, ds = sheet_parts[pi]
                gi = f['gateno'] - 1
                if not 0 <= gi < len(ds['gates']):
                    raise EagleBinaryError('pin reference to missing gate of %s' % pname)
                gname, pins = ds['gates'][gi]
                if not 1 <= f['pinno'] <= len(pins):
                    raise EagleBinaryError('pin reference to missing pin of %s' % pname)
                self.E(el, 'pinref', [('part', pname), ('gate', gname), ('pin', pins[f['pinno'] - 1])])
            elif t == 0x22:
                self.wire(el, r)
            elif t == 0x27:
                self.E(el, 'junction', [('x', _mm(r.f['x'])), ('y', _mm(r.f['y']))])
            elif t == 0x33:
                self.text(el, r, 'label')
            else:
                self.notes.append('dropped record 0x%02x in net segment' % t)
        return el

    # ---------- library file (.lbr)
    def library_file(self, eagle_drawing):
        L = self.d.find(0x15)
        self.libinfo = []
        le = self.E(eagle_drawing, 'library')
        tmp = ET.Element('x')
        info = self.library(tmp, L, {})
        inner = tmp.find('library')
        for ch in list(inner):
            le.append(ch)
        return le

    # ---------- document
    def document(self):
        d = self.d
        root = ET.Element('eagle', {'version': '6.0.0'})
        drawing = self.E(root, 'drawing')
        st = self.E(drawing, 'settings')
        self.E(st, 'setting', [('alwaysvectorfont', 'no')])
        self.E(st, 'setting', [('verticaltext', 'up')])
        g = next((k for k in d.root.kids if k.t == 0x12), None)
        if g is not None:
            gf = g.f
            unit = GRID_UNITS.get(gf['unit'], 'mil')
            aunit = GRID_UNITS.get(gf['altunit'], 'mil')
            self.E(drawing, 'grid', [('distance', _num(gf['size'])), ('unitdist', unit), ('unit', unit),
                                     ('style', 'dots' if gf['style'] else 'lines'),
                                     ('multiple', str(max(1, gf['multiple']))), ('display', _yn(gf['display'])),
                                     ('altdistance', _num(gf['altsize'])), ('altunitdist', aunit),
                                     ('altunit', aunit)])
        le = self.E(drawing, 'layers')
        for r in d.root.kids:
            if r.t == 0x13:
                f = r.f
                self.E(le, 'layer', [('number', str(f['number'])), ('name', f['name']), ('color', str(f['color'])),
                                     ('fill', str(f['fill'])), ('visible', _yn(f['visible'])),
                                     ('active', _yn(f['active']))])
        kind = d.kind
        if kind == 'board':
            self.board(drawing)
        elif kind == 'schematic':
            self.schematic(drawing)
        elif kind == 'library':
            self.library_file(drawing)
        else:
            raise EagleBinaryError('unknown drawing kind')
        return root


def read_binary(path_or_bytes):
    if isinstance(path_or_bytes, (bytes, bytearray)):
        data = bytes(path_or_bytes)
    else:
        with open(path_or_bytes, 'rb') as f:
            data = f.read()
    return BinaryDrawing(data)


def to_xml(drw):
    w = XmlWriter(drw)
    root = w.document()
    _indent(root)
    body = ET.tostring(root, encoding='unicode')
    head = ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE eagle SYSTEM "eagle.dtd">\n'
            '<!-- converted from binary EAGLE %d.%d by Eagle Exhumer eagle_bin %s -->\n'
            % (drw.major, drw.minor, FORMAT_VERSION))
    return head + body + '\n', w.notes


def _indent(el, level=0):
    pad = '\n' + level * ''
    if len(el):
        if not el.text or not el.text.strip():
            el.text = '\n'
        for ch in el:
            _indent(ch, level + 1)
            if not ch.tail or not ch.tail.strip():
                ch.tail = '\n'
    return pad


def convert_file(src, dst):
    drw = read_binary(src)
    xml, notes = to_xml(drw)
    with open(dst, 'w', encoding='utf-8', newline='\n') as f:
        f.write(xml)
    return {'kind': drw.kind, 'version': '%d.%d' % (drw.major, drw.minor), 'records': drw.nrec,
            'warnings': drw.warnings, 'notes': notes}


# --------------------------------------------------------------------------------------------
# converter self-check: the converted schematic and board must describe the same netlist
# --------------------------------------------------------------------------------------------
def _sch_netlist(path):
    import collections
    sch = ET.parse(path).getroot().find('drawing/schematic')
    devs, gsym, pdir = {}, {}, {}
    for lib in sch.iter('library'):
        ln = lib.get('name')
        for sy in lib.iter('symbol'):
            for pn in sy.iter('pin'):
                pdir[(ln, sy.get('name'), pn.get('name'))] = pn.get('direction', 'io')
        for ds in lib.iter('deviceset'):
            for g in ds.iter('gate'):
                gsym[(ln, ds.get('name'), g.get('name'))] = g.get('symbol')
            for dv in ds.iter('device'):
                m = collections.defaultdict(list)
                for c in dv.iter('connect'):
                    for pad in c.get('pad').split():
                        m[(c.get('gate'), c.get('pin'))].append(pad)
                devs[(ln, ds.get('name'), dv.get('name', ''))] = m
    parts = {p.get('name'): (p.get('library'), p.get('deviceset'), p.get('device', '')) for p in sch.iter('part')}
    nets, used = {}, set()
    for net in sch.iter('net'):
        s = nets.setdefault(net.get('name'), set())
        for pr in net.iter('pinref'):
            key = (pr.get('part'), pr.get('gate'), pr.get('pin'))
            used.add(key)
            for pad in devs.get(parts.get(pr.get('part')), {}).get(key[1:], []):
                s.add((pr.get('part'), pad))
    # EAGLE implicit supply: a 'pwr' pin that is not wired joins the net named after the pin
    for pname, key in parts.items():
        for (gate, pin), pads in devs.get(key, {}).items():
            if (pname, gate, pin) in used:
                continue
            if pdir.get((key[0], gsym.get((key[0], key[1], gate)), pin)) == 'pwr':
                for pad in pads:
                    nets.setdefault(pin, set()).add((pname, pad))
    return {k: v for k, v in nets.items() if v}


def _brd_netlist(path):
    nets = {}
    for sig in ET.parse(path).getroot().iter('signal'):
        s = nets.setdefault(sig.get('name'), set())
        for c in sig.iter('contactref'):
            s.add((c.get('element'), c.get('pad')))
    return {k: v for k, v in nets.items() if v}


def crosscheck(sch_xml, brd_xml):
    """Compare the netlists of a converted schematic/board pair. Returns a dict with counts and
    examples; 'ok' is True when every pad sits in the same net (same name) on both sides."""
    a, b = _sch_netlist(sch_xml), _brd_netlist(brd_xml)
    pa = {p: k for k, v in a.items() for p in v}
    pb = {p: k for k, v in b.items() for p in v}
    only_sch = sorted(p for p in pa if p not in pb)
    only_brd = sorted(p for p in pb if p not in pa)
    renamed = sorted(p for p in pa if p in pb and pa[p] != pb[p])
    split = sorted(k for k, v in a.items() if len({pb.get(p) for p in v if p in pb}) > 1)
    merged = sorted(k for k, v in b.items() if len({pa.get(p) for p in v if p in pa}) > 1)
    ok = not (only_sch or only_brd or renamed or split or merged)
    return {'ok': ok, 'pins': len(set(pa) | set(pb)), 'nets_sch': len(a), 'nets_brd': len(b),
            'only_sch': only_sch[:20], 'only_brd': only_brd[:20], 'renamed': renamed[:20],
            'split': split[:20], 'merged': merged[:20],
            'counts': {'only_sch': len(only_sch), 'only_brd': len(only_brd), 'renamed': len(renamed),
                       'split': len(split), 'merged': len(merged)}}


def prepare_sources(paths, outdir, log=print):
    """Convert every binary EAGLE file of a design (same base name, .sch/.brd) into EAGLE XML in
    outdir. XML inputs are copied unchanged. Returns (mapping original -> xml path, info dict)."""
    import os
    import shutil
    os.makedirs(outdir, exist_ok=True)
    out, info = {}, {'converted': [], 'converter': 'eagle_bin ' + FORMAT_VERSION}
    for p in paths:
        if not p or not os.path.isfile(p):
            continue
        dst = os.path.join(outdir, os.path.basename(p))
        if os.path.abspath(dst) == os.path.abspath(p):
            dst = os.path.join(outdir, 'xml_' + os.path.basename(p))
        if is_binary_eagle(p):
            r = convert_file(p, dst)
            r['source'] = p
            r['xml'] = dst
            info['converted'].append(r)
            log('  binary EAGLE %s %s: %d records -> EAGLE XML %s' % (r['version'], r['kind'], r['records'],
                                                                       os.path.basename(dst)))
            for w in r['warnings'] + r['notes']:
                log('    note: ' + w)
        else:
            shutil.copy2(p, dst)
        out[p] = dst
    sch = next((v for k, v in out.items() if k.lower().endswith('.sch')), None)
    brd = next((v for k, v in out.items() if k.lower().endswith('.brd')), None)
    if info['converted'] and sch and brd:
        cc = crosscheck(sch, brd)
        info['crosscheck'] = cc
        if cc['ok']:
            log('  converter cross-check: schematic and board netlists identical (%d pads, %d nets)'
                % (cc['pins'], cc['nets_brd']))
        else:
            log('  converter cross-check: schematic and board netlists DIFFER %s' % cc['counts'])
    return out, info


def summary(info):
    """Compact, JSON-serialisable record of a prepare_sources() run (for the import metadata)."""
    import os
    cc = info.get('crosscheck')
    return {'files': [{'file': os.path.basename(c['source']), 'eagle': c['version'], 'kind': c['kind'],
                       'records': c['records']} for c in info.get('converted', [])],
            'converter': info.get('converter'),
            'crosscheck': {k: cc[k] for k in ('ok', 'pins', 'counts')} if cc else None}


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description='Convert binary EAGLE (<= 5.x) .brd/.sch/.lbr to EAGLE 6 XML')
    ap.add_argument('src', help='binary EAGLE file')
    ap.add_argument('dst', nargs='?', help='output XML file (default: <name>.xml.<ext> next to the source)')
    ap.add_argument('--check', metavar='OTHER', help='converted counterpart (.sch for a .brd or vice versa) '
                                                       'for the netlist cross-check')
    a = ap.parse_args(argv)
    import os
    dst = a.dst or os.path.splitext(a.src)[0] + '.xml' + os.path.splitext(a.src)[1]
    info = convert_file(a.src, dst)
    print('%s %s: %d records -> %s' % (info['kind'], info['version'], info['records'], dst))
    for w in info['warnings'] + info['notes']:
        print('  note:', w)
    if a.check:
        s, b = (dst, a.check) if dst.lower().endswith('.sch') else (a.check, dst)
        print(crosscheck(s, b))


if __name__ == '__main__':
    main(sys.argv[1:])
