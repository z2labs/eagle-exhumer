"""prep_olimex_corpus.py <testdata repo> <OLINUXINO clone> <out dir>
Build the Olimex benchmark corpus: one folder per board of the reference library
(<testdata>/MANIFEST.csv, set 'olimex'), with the .brd/.sch copied from a local clone of
github.com/OLIMEX/OLINUXINO. The clone is only read."""
import csv, os, shutil, sys
td, olx, out = sys.argv[1:4]
seen, n, miss = set(), 0, []
with open(os.path.join(td, 'MANIFEST.csv'), encoding='utf-8', errors='replace') as f:
    for r in csv.DictReader(f, delimiter=';'):
        if r['set'] != 'olimex' or r['board'] in seen:
            continue
        seen.add(r['board'])
        rel = r['source'].split(' : ', 1)[1]
        rel = rel.split('OLINUXINO/HARDWARE/', 1)[1] if 'OLINUXINO/HARDWARE/' in rel else rel
        brd = os.path.join(olx, 'HARDWARE', *rel.split('/'))
        if not os.path.isfile(brd):
            miss.append(brd); continue
        dst = os.path.join(out, r['board'])
        os.makedirs(dst, exist_ok=True)
        for p in (brd, os.path.splitext(brd)[0] + '.sch'):
            if os.path.isfile(p):
                shutil.copy2(p, os.path.join(dst, r['board'] + os.path.splitext(p)[1]))
        n += 1
print(n, 'boards copied,', len(miss), 'missing')
for m in miss:
    print('  missing', m)
