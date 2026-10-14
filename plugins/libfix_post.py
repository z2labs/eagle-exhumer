#!/usr/bin/env python3
"""Post-processing orchestrator for eagle-exhumer.

Library clean-up of a converted project (opt-in, by Robert Katona): runs the
scripts in plugins/libfix/ in order on the project folder. Eagle Exhumer calls
it from eagle2kicad_fix.py (--libfix) before its quality control, so the QC
checks the cleaned-up files.

Execution order:
    1. post_process_unique_rc_symbols.py  - create unique R/L/C symbols
    2. post_process_set_value.py          - set Value fields on library symbols
    3. post_process_replace_rc.py         - replace R/L/C instances with unique symbols
    4. post_process_add_mpn.py            - add MPN and supplier fields
    5. verify_kicad_cli.py                - validate with kicad-cli (final check)

Usage::

    python post_process.py <project_dir> [--dry-run] [--no-backup] [--no-verify]

Output is appended to the eaglefix_console.log in the project directory.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import datetime

# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #
PLUGINS_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.join(PLUGINS_DIR, 'libfix')


SCRIPTS = [
    ('unique_rc_symbols', 'Task 1 - create unique R/L/C symbols'),
    ('set_value',         'Task 1b - set Value fields'),
    ('replace_rc',        'Task 2 - replace R/L/C instances'),
    ('add_mpn',           'Task 3 - add MPN and supplier fields'),
]

VERIFY_SCRIPT = 'verify_kicad_cli'


def run_script(project_dir: str, name: str, extra_args: list) -> int:
    """Run a tools script in the given project directory and return its exit code."""
    script_path = os.path.join(TOOLS_DIR, name + '.py')
    if not os.path.isfile(script_path):
        print(f'  SKIP {name}: script not found at {script_path}')
        return 0
    cmd = [sys.executable, script_path] + extra_args
    print(f'  >>> {name}: {extra_args if extra_args else ""}')
    env = dict(os.environ, EXHUMER_PROJECT=os.path.abspath(project_dir), PYTHONIOENCODING='utf-8')
    kw = {}
    if os.name == 'nt':
        kw['creationflags'] = 0x08000000          # CREATE_NO_WINDOW
    result = subprocess.run(cmd, cwd=project_dir, capture_output=True, text=True, env=env,
                            encoding='utf-8', errors='replace', **kw)
    stdout_lines = result.stdout.strip().splitlines() if result.stdout else []
    stderr_lines = result.stderr.strip().splitlines() if result.stderr else []
    for line in stdout_lines:
        print(f'      {line}')
    for line in stderr_lines:
        print(f'      [ERR] {line}')
    print(f'      exit code: {result.returncode}')
    return result.returncode


def append_to_console_log(project_dir: str, tag: str, lines: list) -> None:
    """Append lines to the eaglefix_console.log in the project directory."""
    log_path = os.path.join(project_dir, 'eaglefix_console.log')
    try:
        with open(log_path, 'a', encoding='utf-8') as fh:
            fh.write(f'\n\n{"=" * 70}\n')
            fh.write(f'  POST-PROCESSING ({tag}) - {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n')
            fh.write(f'{"=" * 70}\n')
            for line in lines:
                fh.write(line + '\n')
            fh.write(f'\n{"=" * 70}\n')
            fh.write(f'  POST-PROCESSING ({tag}) COMPLETE\n')
            fh.write(f'{"=" * 70}\n\n')
    except Exception as ex:
        print(f'  WARNING: could not append to console log: {ex}')


def run_pipeline(project_dir: str, dry_run: bool, no_backup: bool) -> tuple[int, list[str]]:
    """Execute the post-processing pipeline. Returns (exit_code, log_lines)."""
    log = [
        f'# post-processing pipeline',
        f'# timestamp: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
        f'# project: {project_dir}',
        '',
    ]

    extra = ['--dry-run'] if dry_run else []
    if no_backup:
        extra.append('--no-backup')

    overall_rc = 0

    for name, desc in SCRIPTS:
        log.append(f'--- {name}: {desc} ---')
        rc = run_script(project_dir, name, extra)
        log.append(f'  exit code: {rc}')
        if rc != 0:
            log.append(f'  WARNING: {name} returned non-zero exit code {rc}')
            overall_rc = rc
        log.append('')

    return overall_rc, log


def run_verify(project_dir: str, tag: str, dry_run: bool) -> tuple[int, list[str]]:
    """Run the kicad-cli verification with the given tag. Returns (exit_code, log_lines)."""
    log = [f'=== VERIFICATION ({tag}) ===']
    extra = [tag]
    if dry_run:
        extra.append('--dry-run')
    rc = run_script(project_dir, VERIFY_SCRIPT, extra)
    log.append(f'  exit code: {rc}')
    log.append(f'=== END VERIFICATION ({tag}) ===')
    return rc, log


def post_process(project_dir: str, dry_run: bool = False, no_backup: bool = False,
                 no_verify: bool = False) -> int:
    """Run the full post-processing pipeline on the given project.

    Returns the overall exit code (0 = success).
    """
    log_lines = [
        f'# post-processing run',
        f'# timestamp: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
        f'# project: {project_dir}',
        f'# dry_run: {dry_run}, no_backup: {no_backup}, no_verify: {no_verify}',
        '',
    ]

    overall_rc = 0

    # Run post-processing pipeline
    log_lines.append('=== POST-PROCESSING PIPELINE ===')
    rc, pipeline_log = run_pipeline(project_dir, dry_run, no_backup)
    log_lines.extend(pipeline_log)
    log_lines.append('')
    overall_rc = max(overall_rc, rc)

    # Run verification
    if not no_verify:
        log_lines.append('=== VERIFICATION AFTER ===')
        rc, verify_log = run_verify(project_dir, 'after_post_process', dry_run)
        log_lines.extend(verify_log)
        log_lines.append('')
        overall_rc = max(overall_rc, rc)

    log_lines.append('=== SUMMARY ===')
    log_lines.append(f'  overall exit code: {overall_rc}')

    # Append to the project's console log
    append_to_console_log(project_dir, 'post_process', log_lines)

    # Also print to stdout
    for line in log_lines:
        print(line)

    return overall_rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument('project_dir', help='KiCad project directory to process')
    ap.add_argument('--dry-run', action='store_true',
                    help='report only, no writes (passed to all scripts)')
    ap.add_argument('--no-backup', action='store_true',
                    help='skip backup copies (passed to all scripts)')
    ap.add_argument('--no-verify', action='store_true',
                    help='skip verification step after pipeline')
    args = ap.parse_args(argv)

    return post_process(args.project_dir, dry_run=args.dry_run,
                        no_backup=args.no_backup, no_verify=args.no_verify)


if __name__ == '__main__':
    raise SystemExit(main())