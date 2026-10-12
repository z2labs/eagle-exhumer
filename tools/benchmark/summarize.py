"""summarize.py <corpus root>... [--md out.md] [--since YYYY-MM-DDTHH:MM]
QC / manufacturing-file verdicts of every converted design (<board>_kicad/eaglefix_metrics.json)
under the given roots: counts, finding categories and the failing designs."""
import collections, datetime, glob, json, os, re, sys
args = sys.argv[1:]
md = since = None
roots = []
while args:
    a = args.pop(0)
    if a == '--md':
        md = args.pop(0)
    elif a == '--since':
        since = datetime.datetime.fromisoformat(args.pop(0)).timestamp()
    else:
        roots.append(a)
rows = []
for r in roots:
    for m in glob.glob(os.path.join(r, '**', '*_kicad', 'eaglefix_metrics.json'), recursive=True):
        if since and os.path.getmtime(m) < since:
            continue
        j = json.load(open(m, encoding='utf-8'))
        rows.append((os.path.relpath(os.path.dirname(os.path.dirname(m)), r), j.get('qc'), j.get('selftest'),
                     j.get('qc_findings') or []))
cnt = collections.Counter(q for _, q, _, _ in rows)
cats = collections.Counter()
for _, _, _, f in rows:
    for x in f:
        m = re.match(r'(geometry: \d+ \w+|manufacturing files: [\w:]+|Eagle \w+ vs KiCad \w+|[^:]+)', x)
        cats[re.sub(r'\d+ ', '', m.group(1)) if m else x[:40]] += 1
out = [f'# Benchmark summary ({datetime.datetime.now():%Y-%m-%d %H:%M})', '',
       f'designs: {len(rows)} - ' + ', '.join(f'{k}: {v}' for k, v in cnt.most_common()), '',
       '| finding | designs |', '|---|---|'] + [f'| {k} | {v} |' for k, v in cats.most_common()] + \
      ['', '| design | QC | selftest | findings |', '|---|---|---|---|'] + \
      [f'| {d} | {q} | {s} | {"; ".join(f)[:200]} |' for d, q, s, f in sorted(rows) if q != 'PASS']
txt = '\n'.join(out)
print(txt)
if md:
    open(md, 'w', encoding='utf-8').write(txt + '\n')
