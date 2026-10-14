#!/usr/bin/env python3
"""Unified runner for the KrakenSDR post-processing pipeline.

This script orchestrates the execution of the symbol library post-processing
scripts in the correct order, as described in the eagle-exhumer post-processing
integration plan.

Execution order:
    1. post_process_unique_rc_symbols.py  - create unique R/L/C symbols
    2. post_process_set_value.py          - set Value fields on library symbols
    3. post_process_replace_rc.py         - replace R/L/C instances with unique symbols
    4. post_process_add_mpn.py            - add MPN and supplier fields
    5. verify_kicad_cli.py                - validate with kicad-cli (final check)

Usage::

    # Full pipeline (before and after verification)
    python tools/run_post_process.py --run

    # Verification only (before pipeline)
    python tools/run_post_process.py --verify-before

    # Verification only (after pipeline)
    python tools/run_post_process.py --verify-after

    # Dry-run mode (report only, no writes)
    python tools/run_post_process.py --dry-run

    # Skip verification steps
    python tools/run_post_process.py --run --no-verify

    # Pass --no-backup to all scripts
    python tools/run_post_process.py --run --no-backup

Logs from each step are written to tools/<script_name>.log.
A combined pipeline log is written to tools/run_post_process.log.
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
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))

SCRIPTS = [
    ('post_process_unique_rc_symbols', 'Task 1 - create unique R/L/C symbols'),
    ('post_process_set_value',       'Task 1b - set Value fields'),
    ('post_process_replace_rc',      'Task 2 - replace R/L/C instances'),
    ('post_process_add_mpn',         'Task 3 - add MPN and supplier fields'),
]

VERIFY_SCRIPT = 'verify_kicad_cli'


def run_script(name: str, extra_args: list) -> int:
    """Run a tools script and return its exit code."""
    script_path = os.path.join(TOOLS_DIR, name + '.py')
    cmd = [sys.executable, script_path] + extra_args
    print(f'\n>>> Running: {name} {" ".join(extra_args) if extra_args else ""}')
    print(f'    Script: {script_path}')
    proc = subprocess.run(cmd, cwd=TOOLS_DIR)
    return proc.returncode


def write_log(tag: str, lines: list) -> str:
    """Write a log file and return its path."""
    path = os.path.join(TOOLS_DIR, tag + '.log')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')
    return path


def run_pipeline(dry_run: bool, no_backup: bool) -> int:
    """Execute the full post-processing pipeline."""
    log = [
        f'# post-processing pipeline run',
        f'# timestamp: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
        '',
    ]

    extra = ['--dry-run'] if dry_run else []
    if no_backup:
        extra.append('--no-backup')

    overall_rc = 0

    for name, desc in SCRIPTS:
        log.append(f'--- {name}: {desc} ---')
        rc = run_script(name, extra)
        log.append(f'  exit code: {rc}')
        if rc != 0:
            log.append(f'  WARNING: {name} returned non-zero exit code {rc}')
            overall_rc = rc
        log.append('')

    return overall_rc


def run_verify(tag: str, dry_run: bool) -> int:
    """Run the kicad-cli verification with the given tag."""
    extra = [tag]
    if dry_run:
        extra.append('--dry-run')
    return run_script(VERIFY_SCRIPT, extra)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument('--run', action='store_true',
                    help='run the full post-processing pipeline')
    ap.add_argument('--verify-before', action='store_true',
                    help='run verification before pipeline (tag=before)')
    ap.add_argument('--verify-after', action='store_true',
                    help='run verification after pipeline (tag=after)')
    ap.add_argument('--dry-run', action='store_true',
                    help='report only, no writes (passed to all scripts)')
    ap.add_argument('--no-backup', action='store_true',
                    help='skip backup copies (passed to all scripts)')
    ap.add_argument('--no-verify', action='store_true',
                    help='skip verification steps after pipeline')
    args = ap.parse_args(argv)

    has_run = args.run
    has_verify_before = args.verify_before
    has_verify_after = args.verify_after

    if not has_run and not has_verify_before and not has_verify_after:
        has_verify_before = True
        has_run = True
        has_verify_after = True

    log_lines = [
        f'# post-processing pipeline run',
        f'# timestamp: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
        f'# mode: {"verify-before" if has_verify_before else ""}'
             f'{"+run" if has_run else ""}'
             f'{"+verify-after" if has_verify_after else ""}',
        '',
    ]

    overall_rc = 0

    if has_verify_before:
        log_lines.append('=== VERIFICATION BEFORE ===')
        rc = run_verify('before', args.dry_run)
        log_lines.append(f'  exit code: {rc}')
        log_lines.append('')
        overall_rc = max(overall_rc, rc)

    if has_run:
        log_lines.append('=== POST-PROCESSING PIPELINE ===')
        rc = run_pipeline(args.dry_run, args.no_backup)
        log_lines.append(f'  pipeline exit code: {rc}')
        log_lines.append('')
        overall_rc = max(overall_rc, rc)

    if has_verify_after:
        log_lines.append('=== VERIFICATION AFTER ===')
        rc = run_verify('after', args.dry_run)
        log_lines.append(f'  exit code: {rc}')
        log_lines.append('')
        overall_rc = max(overall_rc, rc)

    log_lines.append('=== SUMMARY ===')
    log_lines.append(f'  overall exit code: {overall_rc}')

    log_path = write_log('run_post_process', log_lines)
    print(f'\nCombined log: {log_path}')

    return overall_rc


if __name__ == '__main__':
    raise SystemExit(main())