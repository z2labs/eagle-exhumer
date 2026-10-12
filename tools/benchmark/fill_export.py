"""fill_export.py <kicad project dir> <out dir>
Gerbers + Excellon of a converted project WITH filled zones. The project itself is not changed:
the zones are filled on a temporary copy next to it, exported, and the copy is deleted.
Run with KiCad's python (pcbnew). kicad-cli: KICAD_CLI or the default KiCad 10 location."""
import glob, os, subprocess, sys
import pcbnew
CLI = os.environ.get('KICAD_CLI', r'C:\Program Files\KiCad\10.0\bin\kicad-cli.exe')
proj_dir, out = sys.argv[1], sys.argv[2]
pcb = [p for p in glob.glob(os.path.join(proj_dir, '*.kicad_pcb')) if not os.path.basename(p).startswith('_')][0]
b = pcbnew.LoadBoard(pcb)
pcbnew.ZONE_FILLER(b).Fill(b.Zones())
tmp = os.path.join(proj_dir, '_gerber_filled.kicad_pcb')
pcbnew.SaveBoard(tmp, b)
os.makedirs(out, exist_ok=True)
n = b.GetCopperLayerCount()
cu = ['F.Cu'] + [f'In{i}.Cu' for i in range(1, n - 1)] + ['B.Cu']
r1 = subprocess.run([CLI, 'pcb', 'export', 'gerbers', '--output', out + os.sep, '--layers',
                     ','.join(cu + ['F.Mask', 'B.Mask', 'F.Paste', 'B.Paste', 'F.Silkscreen', 'B.Silkscreen', 'Edge.Cuts']),
                     '--no-protel-ext', tmp], capture_output=True, text=True)
r2 = subprocess.run([CLI, 'pcb', 'export', 'drill', '--output', out + os.sep, '--format', 'excellon',
                     '--excellon-units', 'mm', '--excellon-separate-th', tmp], capture_output=True, text=True)
for f in glob.glob(os.path.join(proj_dir, '_gerber_filled.*')):
    os.remove(f)
print('gerbers rc', r1.returncode, 'drill rc', r2.returncode, (r1.stderr + r2.stderr).strip()[:300])
