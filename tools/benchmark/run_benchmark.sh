#!/bin/bash
# run_benchmark.sh - Eagle Exhumer benchmark on Linux: convert + strong quality control on every
# design of the given corpus roots, manufacturing files compared with the reference library.
#
#   run_benchmark.sh [--mfg-root DIR] [--logs DIR] [--jobs N] [--nice N] [--plugin DIR] CORPUS...
#
# Each corpus root runs as one batch (own virtual display, see plugins/linux_gui.py); --jobs runs
# that many corpus roots at the same time. Needs: KiCad 10 (kicad, kicad-cli, python3 with pcbnew),
# Xvfb, xdotool, dbus-daemon, at-spi2-core, python3-gi, python3-numpy, python3-pil.
set -u
MFG=''; LOGS="$(pwd)/bench_logs"; JOBS=1; NICE=10
PLUGIN="${XDG_DATA_HOME:-$HOME/.local/share}/kicad/10.0/3rdparty/plugins/com_github_z2labs_eagle-exhumer"
while [ $# -gt 0 ]; do
  case "$1" in
    --mfg-root) MFG="$2"; shift 2 ;;
    --logs) LOGS="$2"; shift 2 ;;
    --jobs) JOBS="$2"; shift 2 ;;
    --nice) NICE="$2"; shift 2 ;;
    --plugin) PLUGIN="$2"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) break ;;
  esac
done
[ $# -gt 0 ] || { echo "no corpus given"; exit 2; }
mkdir -p "$LOGS"
[ -n "$MFG" ] && export EAGLE_EXHUMER_MFG_ROOT="$MFG"
BATCH="$PLUGIN/eagle2kicad_batch.py"
[ -f "$BATCH" ] || { echo "plugin not found: $BATCH"; exit 2; }
HERE="$(cd "$(dirname "$0")" && pwd)"
for c in "$@"; do
  while [ "$(jobs -rp | wc -l)" -ge "$JOBS" ]; do sleep 5; done
  name="$(basename "$c")"
  echo "== $c ($(date +%T))"
  ( cd "$c" && nice -n "$NICE" python3 -u "$BATCH" . --force > "$LOGS/$name.log" 2> "$LOGS/$name.err" ) &
done
wait
python3 "$HERE/summarize.py" "$@" --md "$LOGS/summary.md"
