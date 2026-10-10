# Binary EAGLE file format (EAGLE 4.x / 5.x)

EAGLE 6 switched to XML. Older versions (3.x, 4.x, 5.x) stored boards, schematics and libraries
in a proprietary binary format. KiCad 10 reads only the XML format. Eagle Exhumer converts binary
files to EAGLE 6 XML first (`plugins/eagle_bin.py`), then runs the normal import, repair and
quality control.

This document records the format as implemented and verified in `eagle_bin.py`.

**Sources.** The starting points were:
- the format notes of pcb-rnd's io_eagle plugin (T. Palinkas, E. S. Heinzle, GPL-2.0+);
- its 2026 KiCad port, `common/io/eagle/eagle_bin_parser.cpp`, which is not in KiCad 10.0.x;
- the pyeagle field notes.

Every field below was checked against real designs. Where those notes were wrong, that is marked
**(corrected)**.

## Verification

The test corpus is the Olimex OLinuXino hardware repository: 227 binary files (113 boards,
113 schematics, 1 library). All of them were saved by EAGLE 4.16.

| check | result |
|---|---|
| all 227 files decode, the record tree closes exactly, every string-table entry is consumed | 227 / 227 |
| library objects vs. the same libraries saved by EAGLE 6/7 as XML (same library + name) | packages 707/720, symbols 103/104, devicesets incl. pin-pad connects 99/103 identical; the rest are real library edits between revisions |
| converted binary board vs. EAGLE's own XML schematic of the same revision (A10-OLinuXino-LIME rev B, shield template) | 1606 + 180 pads, identical nets and net names |
| every binary schematic/board pair (111 designs), converted netlists compared | 147 117 pads, 0 split, 0 merged, 0 renamed |
| board geometry, binary rev B vs. XML rev A of an otherwise unchanged board (A10-OLinuXino-LIME) | 11 503 signal wires/vias/polygons/contacts, 356 placements, 678 name/value texts identical; only edited texts and part values differ |
| schematic geometry, binary vs. XML of neighbouring revisions (iMX233 MAXI C2/D, Micro E/D) | instances, attribute texts, net wires/junctions/labels, busses identical apart from real edits |

No Gerber or drill files exist for the binary designs. The ground truth is EAGLE's own XML output
for the same libraries and boards.

## File layout

All integers are little endian.

```
records   N x 24 bytes          record 0 = start record, N at offset 4
strings   13 12 99 19 | u32 len | NUL-separated strings, empty string ends | u32 checksum
rules     (boards) repeated: u32 tag | u32 len (counts itself) | payload | u32 end sentinel | u32 checksum
          99 99 99 99 00 00 00 00  ends the file
```

Byte 0 of a record is its type. Byte 1 is 0x80 or 0x00 in the start record and is not used.
The start record holds the version at bytes 8/9 (4, 16 = EAGLE 4.16). A file is binary EAGLE when
it starts with `10 00` or `10 80`.

**Units.** Coordinates are signed 32-bit in 1/10 micrometre (0.1 um = 0.0001 mm). Widths, drills,
diameters, SMD dx/dy and text sizes are stored as **half** values. Net class width/drill/clearance
and the design-rule distances are stored as full values.

**Angles.**
- Elements, texts, pads, SMDs and rectangles store a 12-bit angle where 4096 = 360 degrees.
  EAGLE 6 rounds it to 0.1 degree when it converts.
- Pins and gate instances store quarter turns.
- In 16-bit angle words, 0x1000 = mirrored and 0x4000 = spin.

## String references

A string field that starts with `0x7F` refers to the string table. References are consumed in
record order, and within a record in this field order:

| record | field order |
|---|---|
| package | name, description |
| packages section | library, description |
| deviceset | name, description, prefix |
| device variant | name, technology table |
| part (sheet) | value, name |
| element (board) | value, name |
| attribute value | attribute, symbol |

The 4 bytes after `0x7F` are a heap pointer. Within long runs it equals base + offset in the
table, but the base changes from time to time (about 3 % of the references). Sequential order is
the reliable method. The pointer is only used as a consistency hint.

## Record tree

Containers say how many records belong to them:
- **D** = number of direct children; each child brings its own subtree.
- **R** = number of records in the subtree.

| type | record | subsections |
|---|---|---|
| 0x10 | start | whole file |
| 0x11 | unknown (2x) | - |
| 0x12 | grid | - |
| 0x13 | layer | - |
| 0x14 | schematic | R@4 libraries, R@8 sheets |
| 0x15 | library (sch/lbr) | R@4 devices, R@8 symbols, R@12 packages |
| 0x17 / 0x18 / 0x19 | devices / symbols / packages section | R@4 |
| 0x1a | sheet | D@2 plain, R@12 parts, R@16 busses, R@20 nets |
| 0x1b | board | R@12 libraries (0x19 each), D@2 plain, R@16 elements, R@20 signals |
| 0x1c | signal | R@2 |
| 0x1d | symbol | **R@2** (corrected: direct count breaks on symbols with polygons) |
| 0x1e | package | R@2 |
| 0x1f / 0x20 | net / segment | R@2 |
| 0x21 | polygon | D@2 (vertex wires) |
| 0x2e | element | D@2 (0x2f, smashed texts) |
| 0x30 | instance | D@2 (smashed texts) |
| 0x36 | device variant | D@2 (0x3c connection records) |
| 0x37 | deviceset | R@4 variants, then R@2 gates |
| 0x38 | part | R@2 (instances) |
| 0x3a | bus | R@2 (segments) |

## Record fields

Offsets are in bytes from the start of the 24-byte record.

**Primitives**

| type | fields |
|---|---|
| 0x22 wire | @3 layer, @4..19 x1 y1 x2 y2 (int32), @20 u16 half width, @22 flags, @23 line type. Flags: bits 0-1 style (continuous, longdash, shortdash, dashdot), 0x10 flat cap, **0x20 = counter-clockwise** (corrected: pyeagle had it as clockwise). |
| 0x25 circle | @3 layer, @4 x, @8 y, @12 radius, @20 u32 half width |
| 0x26 rectangle | @3 layer, @4..19 corners, @20 angle (12 bit) |
| 0x27 junction | @4 x, @8 y |
| 0x28 hole | @4 x, @8 y, @12 u32 half drill |
| 0x29 via | @2 bits 0-1 shape (square, round, octagon), @4 x, @8 y, @12 u16 half drill, @14 u16 half diameter (0 = auto), @16 layers (low nibble +1 .. high nibble +1), @17 bit0 always stop |
| 0x2a pad | @2 bits 0-2 shape (square, round, octagon, long, offset), @4 x, @8 y, @12 half drill, @14 half diameter, @16 angle, @18 flags, @19 name (5). Flags: 0x01 **no** stop, 0x04 **no** thermals, 0x08 first (corrected: these are negative flags). |
| 0x2b smd | @2 roundness, @3 layer, @4 x, @8 y, @12 half dx, @14 half dy, @16 angle, @18 flags, @19 name (5). Flags: 0x01 no stop, 0x02 **no** cream, 0x04 no thermals (corrected). |
| 0x21 polygon | @12 half width, @14 half spacing, @16 half isolate, @18 layer, @19 flags. Flags: 0x01 hatch, bits 1-3 rank, 0x40 orphans, 0x80 thermals. Vertices are the child wires (start point + arc). EAGLE 6 keeps isolate only on copper polygons. |

**Wire line types (@23)**

| value | meaning |
|---|---|
| 0x00 | straight line |
| 0x01 | airwire |
| 0x78..0x7b | 90 degree arc; the centre is the min/max corner of the endpoints |
| 0x7c..0x7f | 180 degree arc; the centre is the midpoint |
| 0x81 | general arc, see below |

For 0x81 the four coordinates are 24-bit at @4/@8/@12/@16. A fifth 24-bit value c is formed from
bytes 7, 11 and 15. Byte 19 holds the sign bits: c, x1, y1, x2, y2 = bits 0..4. c is the centre x
when |dx| < |dy|, otherwise the centre y. The other centre coordinate lies on the perpendicular
bisector of the chord.

**Texts**

0x31 text, 0x33 label, 0x34 smashed NAME, 0x35 smashed VALUE, 0x3f PART, 0x40 GATE,
0x41 attribute.

| field | content |
|---|---|
| @2 bits 0-1 | font: **0 vector, 1 proportional, 2 fixed** (corrected) |
| @3 | layer |
| @4, @8 | x, y |
| @12 | half size |
| @14 bits 2-6 | ratio |
| @16 | angle + mirror/spin |
| @18 | text (6 bytes; 0x31/0x41 only) |

**Schematic and library**

| type | fields |
|---|---|
| 0x2c pin | @2 bits 0-1 function (none, dot, clk, dotclk), bits 6-7 visible (off, pad, pin, both); @4 x, @8 y; @12 bits 0-3 direction (nc, in, out, io, oc, pwr, pas, hiz, sup), bits 4-5 length (point, short, middle, long), bits 6-7 quarter turns; @13 swap level; @14 name (10) |
| 0x2d gate | @4 x, @8 y, @12 add level (must, can, next, request, always), @13 swap level, @14 symbol index (1-based), @16 name |
| 0x37 deviceset | @6 bit0 user value; @7 connection encoding (see below); @8 prefix (5), @13 description (5), @18 name (6) |
| 0x36 device variant | @4 package index (1-based; 0 = no package), @6 technology table (13), @19 name (5); `''` = empty name |
| 0x3c connections | @2..23 one slot per package pad, in pad/smd record order. Slots past the last record are 0 (unconnected). |
| 0x38 part | @4 library index, @6 deviceset index, @8 u8 variant index, @9 technology index, @11 name (5), @16 value (8) |
| 0x30 instance | @4 x, @8 y, @14 gate index, @16 quarter turns in bits 10-11, mirror 0x1000, @18 bit0 smashed |
| 0x3d pinref | @4 part index (in the sheet's part list), @6 gate index, @8 pin index (in the gate symbol's pin order) |
| 0x1f net | @13 bits 0-2 net class, @16 name |
| 0x3a bus | @4 name (20) |

**Connection encoding** (from deviceset byte 7):
- bit 0x80 set: one byte per slot (22 slots per record); clear: u16 per slot (11 per record);
- low nibble = number of pin bits;
- slot = (gate index << pin bits) | pin index, both 1-based.

The corpus uses pin bits 1, 4, 5, 6 and 7 with bytes, and 8 and 12 with u16.

**Board**

| type | fields |
|---|---|
| 0x1c signal | @12 0x02 airwires hidden, @13 bits 0-2 net class, @16 name |
| 0x2e element | @4 x, @8 y, @12 library index, @14 package index, @16 angle + mirror/spin |
| 0x2f element 2 | @2 name (8), @10 value (14) |
| 0x3e contactref | @4 element index, @6 pad index (pad/smd order in the package) |

All indexes are 1-based ordinals into the corresponding lists.

## Rules blocks (boards)

| tag | payload |
|---|---|
| `10 04 00 20` design rules | name\0 description\0 [layer setup\0] 78 56 34 12 + 426 bytes (layout below); end `98 ba dc fe` |
| `25 04 00 20` net class | [name\0] u32 number, 21 43 65 87, i32 width, i32 drill, then i32 clearance (20-byte form, EAGLE 4.16) or 8 x i32 clearance row (48-byte form); end `ef cd ab 89` |
| `23 05 00 20` | autorouter parameters (not converted); end `64 00 00 00` |

**Design-rule layout** (offsets after the 0x12345678 magic):

| offset | content |
|---|---|
| 0..32 | mdWireWire, mdWirePad, mdWireVia, mdPadPad, mdPadVia, mdViaVia, mdSmdPad, mdSmdVia, mdSmdSmd |
| 36 / 40 / 44 | mdViaViaSameLayer / mnLayersViaInSmd / mdCopperDimension |
| 52 / 56 | mdDrill / mdSmdStop |
| 64 / 68 / 72 | msWidth / msDrill / msMicroVia |
| 76 | double msBlindViaRatio |
| 84..132 | 7 doubles rv*: PadTop, PadInner, PadBottom, ViaOuter, ViaInner, MicroViaOuter, MicroViaInner |
| 140..192 | rlMin/rlMax pairs in the same order |
| 196..204 | psTop / psBottom / psFirst |
| 208 / 216 | double mvStopFrame / double mvCreamFrame |
| 224..240 | mlMin/MaxStopFrame, mlMin/MaxCreamFrame, mlViaStopLimit |
| 244 | double srRoundness, then srMin/srMax |
| 260 | supply gap (double + 2 x int) |
| 276 / 280 | supply annulus / thermal isolation |
| 284.. | bytes: thermals for vias, check grid, check angle, u32 max errors, check font, check restrict, use diameter, elongation long/offset |
| 298 | 16 x copper thickness |
| 362 | 15 x isolation thickness |

All distances are 0.1 um. The values were confirmed against the XML design rules of the same
boards.

## XML conventions used by the writer

These conventions follow what EAGLE 6 itself writes, so the output matches EAGLE's own conversion:

- **Part value.** Omitted when it equals the generated default (deviceset name with technology
  and variant filled in). An empty value is written as `value=""` (supply symbols).
- **Library names.** Board library names come from the 0x19 section records. Duplicates get a
  `_2`, `_3` suffix.
- **Airwires.** Kept as layer 19 wires, as in EAGLE XML.

## Known limits

- Every corpus file is EAGLE 4.16 with a single sheet. Multi-sheet schematics (part numbering
  across sheets), the 48-byte EAGLE 5 net class form, attribute records (0x41/0x42), frames (0x43)
  and EAGLE 3.x short pad records are decoded per the notes above but are not verified on real
  files.
- Autorouter settings are not converted.
- KiCad rewrites part names that do not start with a letter (for example `#CE_NAND_E` becomes
  `UNK#CE_NAND_E0`). This is KiCad behaviour, not a conversion issue.

## Command line

```
python plugins/eagle_bin.py old.brd new.brd                      # convert one file
python plugins/eagle_bin.py old.sch new.sch --check new.brd      # + netlist cross-check
```
