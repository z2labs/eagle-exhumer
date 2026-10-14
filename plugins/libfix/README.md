# KrakenSDR Post-Processing Scripts

This directory contains the symbol library post-processing scripts for the KrakenSDR
EAGLE-to-KiCad migration project. These scripts run **after** the eagle-exhumer main
import pipeline has processed a project.

## Files

### Support Scripts

| File | Purpose |
|------|---------|
| `kicad_sexp.py` | Minimal KiCad s-expression parser (dependency-free) |
| `kraken_symbols.py` | Shared model of the KrakenSDR_3v3 KiCad project |

### Post-Processing Scripts (run in order)

| File | Task | Description |
|------|------|-------------|
| `post_process_unique_rc_symbols.py` | #1 | Create one unique R/L/C symbol per part in the project library |
| `post_process_set_value.py` | #1b | Give every BOM symbol its `Value` field |
| `post_process_replace_rc.py` | #2 | Point every R/L/C instance at its own unique symbol |
| `post_process_add_mpn.py` | #3 | Give the library symbols their `MPN` and supplier fields |

### Verification

| File | Purpose |
|------|---------|
| `verify_kicad_cli.py` | Validate the project with `kicad-cli` (ERC, netlist, BOM) |

### Runner

| File | Purpose |
|------|---------|
| `run_post_process.py` | Unified runner that orchestrates the full pipeline |

## Usage

### Full Pipeline (default)

Runs verification before + all 4 post-processing scripts + verification after:

```bash
cd eagle-exhumer
python tools/run_post_process.py
```

### Run Pipeline Only (no verification)

```bash
python tools/run_post_process.py --run --no-verify
```

### Dry-Run (report only, no writes)

```bash
python tools/run_post_process.py --dry-run
```

### Skip Backups

```bash
python tools/run_post_process.py --run --no-backup
```

### Individual Scripts

Each script can also be run directly:

```bash
python tools/post_process_unique_rc_symbols.py
python tools/post_process_set_value.py
python tools/post_process_replace_rc.py
python tools/post_process_add_mpn.py
python tools/verify_kicad_cli.py before
python tools/verify_kicad_cli.py after
```

All individual scripts support `--dry-run` and `--no-backup` flags.

## Execution Order

The scripts **must** be run in this order:

1. **post_process_unique_rc_symbols** - Creates unique symbols for each R/L/C part
2. **post_process_set_value** - Sets Value fields on the library symbols
3. **post_process_replace_rc** - Re-points schematic instances to unique symbols
4. **post_process_add_mpn** - Adds MPN and supplier fields to non-R/L/C symbols
5. **verify_kicad_cli** - Validates the final result

## Project Layout

These scripts target the following project structure:

```
CAD/KrakenSDR_3v4/
    KrakenSDR_3v3-eagle-import.kicad_sym   (project symbol library)
    *.kicad_sch                             (schematic sheets)
```

The project library name is `KrakenSDR_3v3-eagle-import` and is defined in
`kraken_symbols.py` as `LIB_NAME`.

## Logs

Each script writes its own log to `tools/<script_name>.log`.
The runner also writes a combined log to `tools/run_post_process.log`.

Backups are saved to `tools/_backup/<timestamp>_<tag>/`.

## License

These scripts are part of the eagle-exhumer project, licensed under GPL-3.0.
