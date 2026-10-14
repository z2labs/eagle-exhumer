# Library clean-up (optional)

Contributed by Robert Katona. Eagle Exhumer runs these steps when the library clean-up is
switched on (the question after choosing the Eagle file, or `--libfix` for
`eagle2kicad_fix.py` / `eagle2kicad_batch.py`). They run on the converted project **before** the
quality control, so the QC checks the cleaned-up files against the Eagle source.

| Step | Script | What it does |
|---|---|---|
| pins | `eagle2kicad_fix.py` | Every pin of a regular symbol becomes **bidirectional** (Eagle libraries carry many wrong input / output / power types). Power symbols, passive, not-connected and hidden power pins stay. Runs before the PWR_FLAG pass, so the flags go where the supply comes from. |
| 1 | `unique_rc_symbols.py` | One symbol per R / L / C part: `<symbol>_<value>_<footprint>`, the Eagle `PARTNR` becomes its `MPN` |
| 1b | `set_value.py` | Every BOM symbol gets its `Value` field |
| 2 | `replace_rc.py` | Every R / L / C instance points at its own symbol |
| 3 | `add_mpn.py` | `MPN` and `SUPPLIER1..3` / `SUPPLIER_PART_NUMBER1..3` fields from the Eagle order codes (`OC_DIGIKEY`, `OC_FARNELL`, `OC_NEWARK`) |
| check | `verify_kicad_cli.py` | Optional stand-alone check with kicad-cli (ERC, netlist, symbol library) |

The project library is `<project>-eagle-import.kicad_sym` (as KiCad's EAGLE importer names it).
Logs and backups go to `<project>/_eaglefix_backup/libfix/`.

Run stand-alone on a converted project:

```bash
cd <project folder>
python <plugin>/libfix_post.py .            # all steps
python <plugin>/libfix/add_mpn.py --dry-run # one step, report only
```

Every script supports `--dry-run` and `--no-backup`.
