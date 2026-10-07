"""kicad-cli board import (shared by the plugin and the batch tool) - no wx, no pcbnew needed."""
import glob, json, os, subprocess, sys, time


def kicad_cli():
    cands = [os.path.join(os.path.dirname(sys.executable), 'kicad-cli.exe'),
             os.path.join(os.path.dirname(sys.executable), 'kicad-cli')]
    cands += sorted(glob.glob(r'C:\Program Files\KiCad\*\bin\kicad-cli.exe'), reverse=True)
    cands += ['/usr/bin/kicad-cli', '/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli']
    return next((c for c in cands if os.path.isfile(c)), None)


def cli_import_pcb(brd, out_pcb, log=print):
    """KiCad 10's own board importer from the command line: no windows, no layer-mapping dialog,
    about a second. Returns (ok, seconds, report_dict)."""
    cli = kicad_cli()
    if not cli or not brd or not os.path.isfile(brd):
        return False, 0.0, {}
    rep = out_pcb + '.import.json'
    t0 = time.time()
    flags = 0x08000000 if os.name == 'nt' else 0
    r = subprocess.run([cli, 'pcb', 'import', '--format', 'eagle', '--report-format', 'json',
                        '--report-file', rep, '-o', out_pcb, brd],
                       capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=flags)
    dt = time.time() - t0
    report = {}
    try:
        report = json.load(open(rep, encoding='utf-8'))
    except Exception:
        pass
    ok = r.returncode == 0 and os.path.isfile(out_pcb) and os.path.getsize(out_pcb) > 1000
    log(f'  kicad-cli pcb import: {"ok" if ok else "FAILED"} in {dt:.1f} s'
        + (f', {len(report.get("errors") or [])} errors, {len(report.get("warnings") or [])} warnings' if report else '')
        + ('' if ok else ' - ' + (r.stderr or r.stdout).strip()[-300:]))
    for w in (report.get('warnings') or [])[:20]:
        log(f'    [import warning] {w}')
    return ok, dt, report
