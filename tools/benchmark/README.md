# Benchmark harness

Runs the importer with the strong quality control over a whole corpus of EAGLE designs and
compares the KiCad manufacturing files with a reference set. Runs on Windows (KiCad's import
dialogs driven through the Win32 API) and on Linux (a private virtual display per batch, dialogs
read over the accessibility bus - see `plugins/linux_gui.py`).

## Machine setup

1. **KiCad 10.0.6** (default install path). Check `kicad-cli version`.
2. **Plugin**: install the plugin from this repository (PCM zip from `tools/build_pcm.py`, or copy
   `plugins/*` into `%USERPROFILE%\Documents\KiCad\10.0\3rdparty\plugins\com_github_z2labs_eagle-exhumer`).
3. **Python extras for KiCad's python** (raster comparison and crops):
   `"C:\Program Files\KiCad\10.0\bin\python.exe" -m pip install numpy pillow`
4. **Corpus and references** (outside this repository):
   - `git clone https://github.com/OLIMEX/OLINUXINO` (public EAGLE designs, CC BY-SA)
   - the private reference library repository (EAGLE 7.7 CAM output of every corpus board,
     `eagle77_cam/<set>/<board>/`) - access on request; never copied into this repository
   - private / client design sets: copied by hand, anonymous folder names only
5. Build the Olimex corpus folder (sources are only read):
   ```
   python tools\benchmark\prep_olimex_corpus.py <testdata repo> <OLINUXINO clone> D:\bench\olimex_full
   ```

## Run

```
powershell -ExecutionPolicy Bypass -File tools\benchmark\run_benchmark.ps1 `
  -Corpus D:\bench\olimex_full, D:\bench\private_sets `
  -MfgRoot <testdata repo>\eagle77_cam -Priority BelowNormal -Logs D:\bench\logs
```

- Every design gets `<design>_kicad\` with `eaglefix_report.md`, `eaglefix_metrics.json` and the
  QC verdict; `logs\summary.md` lists the counts, the finding categories and every failing design.
- Manufacturing-file reference: files next to the design (factory CAM output) first, otherwise the
  reference library (`EAGLE_EXHUMER_MFG_ROOT`).
- About 2-5 minutes per design on an idle 8-core machine; on a busy machine use `-Priority Idle`
  and expect 5-10x longer. Keep the KiCad windows visible (not minimised).

## Investigating a difference

```
"C:\Program Files\KiCad\10.0\bin\python.exe" tools\benchmark\fill_export.py <design>_kicad D:\tmp\g
"C:\Program Files\KiCad\10.0\bin\python.exe" plugins\gerber_compare.py <reference dir> D:\tmp\g --brd <design.brd> --backend raster
"C:\Program Files\KiCad\10.0\bin\python.exe" tools\benchmark\spot_png.py <reference dir> D:\tmp\g spot.png mask:top+cu:top <x> <y> 10
```

`spot_png.py` draws EAGLE left, KiCad right (red: only EAGLE, green: only KiCad), coordinates as
`gerber_compare` reports them.

## Linux

1. KiCad 10 from the distribution or the KiCad PPA (`kicad`, `kicad-cli`, `python3` with `pcbnew`).
   Start KiCad once and finish the first-run wizard (library tables).
2. Packages: `sudo apt install xvfb xdotool dbus-daemon at-spi2-core python3-gi gir1.2-atspi-2.0 python3-numpy python3-pil`
3. Plugin: copy `plugins/*` into `~/.local/share/kicad/10.0/3rdparty/plugins/com_github_z2labs_eagle-exhumer`
4. Corpus and references as above, then:
   ```
   tools/benchmark/run_benchmark.sh --mfg-root <testdata repo>/eagle77_cam --logs ~/bench/logs \
       --jobs 2 --nice 10 ~/bench/olimex_full/* ~/bench/private_sets
   ```
   Each argument runs as one batch with its own virtual display; `--jobs` batches run at the same
   time (about one per two CPU cores). Nothing appears on the desktop, the machine stays usable.
