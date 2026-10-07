"""Eagle Exhumer - diagnostic bundle + pre-filled GitHub issue.

Used by the plugin's "Report a problem..." button and from the command line:

    python diag.py <kicad_project_dir> [--include-design] [--out DIR] [--no-browser]

The bundle (a .zip) contains the run report and metrics, the console log and the environment
(OS, Python, KiCad, plugin version). Design files - the KiCad project and the Eagle source - are
added ONLY with --include-design (the GUI asks). Nothing is uploaded automatically: the issue page
opens in the browser and the user drags the zip into it.
"""
import argparse, datetime, glob, json, os, platform, sys, urllib.parse, webbrowser, zipfile

VERSION = '0.9.6'
REPO = 'https://github.com/z2labs/eagle-exhumer'

RUN_FILES = ('eaglefix_report.md', 'eaglefix_metrics.json', 'eaglefix_history.jsonl',
             'eaglefix_console.log', 'batch_summary.md')
DESIGN_GLOBS = ('*.kicad_pro', '*.kicad_sch', '*.kicad_pcb', '*.kicad_dru', '*.kicad_sym',
                'sym-lib-table', 'fp-lib-table', '*.pretty/*.kicad_mod', 'eagle_source/*')


def environment():
    env = {'plugin': VERSION, 'os': f'{platform.system()} {platform.release()} ({platform.version()})',
           'machine': platform.machine(), 'python': sys.version.split()[0]}
    try:
        import pcbnew
        env['kicad'] = pcbnew.GetBuildVersion()
    except Exception:
        env['kicad'] = 'unknown (pcbnew not importable here)'
    return env


def summary(project_dir):
    """Short, non-design facts for the issue text: verdict, findings, check counts."""
    s = {'verdict': 'no run found', 'selftest': None, 'findings': [], 'mode': None}
    m = os.path.join(project_dir, 'eaglefix_metrics.json')
    if os.path.isfile(m):
        try:
            d = json.load(open(m, encoding='utf-8'))
            s.update(verdict=d.get('qc') or d.get('dtw') or '?', selftest=d.get('selftest'),
                     findings=(d.get('qc_findings') or d.get('dtw_findings') or [])[:15], mode=d.get('mode'),
                     eagle=f"{d.get('eagle_sch')} / {d.get('eagle_brd')}")
        except Exception as e:
            s['verdict'] = f'metrics unreadable: {e}'
    log = os.path.join(project_dir, 'eaglefix_console.log')
    if os.path.isfile(log):
        tail = open(log, encoding='utf-8', errors='replace').read().splitlines()
        tb = [i for i, l in enumerate(tail) if l.startswith('Traceback')]
        s['error'] = '\n'.join(tail[tb[-1]:tb[-1] + 25]) if tb else ''
    return s


def make_bundle(project_dir, out_dir=None, include_design=False, extra_log=None):
    project_dir = os.path.abspath(project_dir)
    out_dir = out_dir or project_dir
    name = os.path.basename(project_dir.rstrip('\\/')) or 'project'
    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    zpath = os.path.join(out_dir, f'eagle-exhumer-diag_{name}_{stamp}.zip')
    env, summ = environment(), summary(project_dir)
    with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('environment.json', json.dumps(env, indent=1))
        z.writestr('summary.json', json.dumps(summ, indent=1, default=str))
        if extra_log:
            z.writestr('plugin_window.log', extra_log)
        for f in RUN_FILES:
            p = os.path.join(project_dir, f)
            if os.path.isfile(p):
                z.write(p, 'run/' + f)
        if include_design:
            for g in DESIGN_GLOBS:
                for p in glob.glob(os.path.join(project_dir, g)):
                    if os.path.isfile(p) and os.path.getsize(p) < 50e6:
                        z.write(p, 'design/' + os.path.relpath(p, project_dir).replace(os.sep, '/'))
    return zpath, env, summ


def issue_url(env, summ, include_design=False):
    """New-issue URL; the issue form fields are pre-filled through query parameters."""
    body = []
    if summ.get('findings'):
        body.append('Findings:\n' + '\n'.join('- ' + str(f) for f in summ['findings']))
    if summ.get('error'):
        body.append('Error:\n```\n' + summ['error'][:1500] + '\n```')
    q = {
        'template': 'problem_report.yml',
        'title': f"[{summ.get('verdict')}] ",
        'plugin': env['plugin'], 'kicad': env['kicad'], 'os': env['os'][:120],
        'verdict': f"{summ.get('verdict')} (self-check: {summ.get('selftest')}, mode: {summ.get('mode')})",
        'details': '\n\n'.join(body)[:3000],
        'design': 'yes - design files are in the zip' if include_design else 'no - report and logs only',
    }
    return REPO + '/issues/new?' + urllib.parse.urlencode(q)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('project_dir')
    ap.add_argument('--include-design', action='store_true',
                    help='also pack the KiCad project and the Eagle source (only if you may share them)')
    ap.add_argument('--out')
    ap.add_argument('--no-browser', action='store_true')
    a = ap.parse_args()
    zpath, env, summ = make_bundle(a.project_dir, a.out, a.include_design)
    url = issue_url(env, summ, a.include_design)
    print('diagnostic bundle:', zpath)
    print('open a new issue and drag the zip into it:\n' + url)
    if not a.no_browser:
        webbrowser.open(url)


if __name__ == '__main__':
    main()
