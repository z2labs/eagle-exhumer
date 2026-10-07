#!/usr/bin/env python3
"""Build the KiCad PCM package zip and the repository metadata entry for submission.

    python tools/build_pcm.py [--download-url URL]

dist/eagle-exhumer-<version>-pcm.zip        -> attach to a GitHub release
dist/metadata-submission/<identifier>/metadata.json  -> merge request to gitlab.com/kicad/addons/metadata
"""
import argparse, hashlib, json, os, shutil, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP = ('__pycache__', '.pyc')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--download-url', help='public URL of the release zip')
    a = ap.parse_args()
    meta = json.load(open(os.path.join(ROOT, 'metadata.json'), encoding='utf-8'))
    ver = meta['versions'][0]['version']
    dist = os.path.join(ROOT, 'dist'); os.makedirs(dist, exist_ok=True)
    zpath = os.path.join(dist, f"eagle-exhumer-{ver}-pcm.zip")
    install = 0
    with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED) as z:
        for sub in ('plugins', 'resources'):
            for dp, dn, fn in os.walk(os.path.join(ROOT, sub)):
                dn[:] = [x for x in dn if x not in SKIP]
                for f in sorted(fn):
                    if f.endswith(SKIP):
                        continue
                    p = os.path.join(dp, f)
                    z.write(p, os.path.relpath(p, ROOT).replace(os.sep, '/'))
                    install += os.path.getsize(p)
        pkg_meta = json.loads(json.dumps(meta))          # package copy: no download fields
        z.writestr('metadata.json', json.dumps(pkg_meta, indent=2, ensure_ascii=False))
        install += len(json.dumps(pkg_meta, indent=2, ensure_ascii=False).encode())
    data = open(zpath, 'rb').read()
    sub = json.loads(json.dumps(meta))
    v = sub['versions'][0]
    v['download_sha256'] = hashlib.sha256(data).hexdigest()
    v['download_size'] = len(data)
    v['install_size'] = install
    v['download_url'] = a.download_url or (
        f"https://github.com/z2labs/eagle-exhumer/releases/download/v{ver}/eagle-exhumer-{ver}-pcm.zip")
    out = os.path.join(dist, 'metadata-submission', meta['identifier'])
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, 'metadata.json'), 'w', encoding='utf-8') as f:
        json.dump(sub, f, indent=2, ensure_ascii=False)
    shutil.copy2(os.path.join(ROOT, 'resources', 'icon.png'), os.path.join(out, 'icon.png'))
    print(zpath, len(data), 'bytes, sha256', v['download_sha256'])
    print(os.path.join(out, 'metadata.json'))


if __name__ == '__main__':
    main()
