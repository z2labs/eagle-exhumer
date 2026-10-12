"""spot_png.py <eagle mfg dir> <kicad gerber dir> <out.png> <layer[+layer..]> <x> <y> [window mm]
Zoomed EAGLE | KiCad crop of one difference spot (red = only EAGLE, green = only KiCad), one row per layer.
Coordinates in the EAGLE CAM frame, as gerber_compare reports them."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'plugins'))
import gerber_compare as gc
from PIL import Image
ed, kd, out, layer, x, y = sys.argv[1:7]; x, y = float(x), float(y)
win = float(sys.argv[7]) if len(sys.argv) > 7 else 8
le, he, _ = gc.collect(ed, None); lk, hk, _ = gc.collect(kd, None)
(dx, dy), _ = gc.align(he, hk)
geo = gc.RasterGeo((x - win / 2, y - win / 2, x + win / 2, y + win / 2), px=win / 400)
np = geo.np
rows = []
for L in layer.split('+'):
    a = le[L].build(geo); b = lk[L].build(geo, dx, dy)
    def pane(m, o, c):
        im = np.zeros(m.shape + (3,), np.uint8); im[:] = (18, 22, 28); im[m] = (200, 170, 90); im[m & ~o] = c; return im
    rows.append(np.concatenate([pane(a, b, (235, 60, 60)), np.full((a.shape[0], 6, 3), 255, np.uint8),
                                pane(b, a, (60, 210, 90))], 1))
Image.fromarray(np.concatenate(rows, 0)).save(out); print('ok', out)
