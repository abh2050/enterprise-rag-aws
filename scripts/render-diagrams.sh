#!/usr/bin/env bash
# Regenerate docs/diagrams/*.drawio, validate them, and export PNGs (embedded XML, re-editable) with headless draw.io.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 docs/diagrams/build_diagrams.py "$@"
python3 .claude/skills/aws-architecture-diagram/scripts/validate_drawio.py docs/diagrams/*.drawio
for f in docs/diagrams/*.drawio; do
  n=$(basename "$f")
  if [ $# -gt 0 ] && [[ ! " $* " == *" ${n%.drawio} "* ]]; then continue; fi
  docker run --rm --shm-size=1g -v "$PWD/docs/diagrams:/data" -w /data rlespinasse/drawio-desktop-headless \
    -x -f png -e -b 10 -o "/data/$n.png" "/data/$n" >/dev/null 2>&1 || echo "export failed: $n"
done
ls -la docs/diagrams/*.png
