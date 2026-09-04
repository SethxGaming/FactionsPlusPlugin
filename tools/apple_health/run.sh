#!/usr/bin/env bash
# Build the health dashboard from an Apple Health export in one step.
#   ./run.sh ~/Downloads/export.zip [output.html]
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "usage: $0 <export.zip|export.xml> [output.html]" >&2
  exit 1
fi

EXPORT="$1"
OUT="${2:-dashboard.html}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JSON="$(dirname "$OUT")/health.json"

python3 "$DIR/parse_export.py" "$EXPORT" -o "$JSON"
python3 "$DIR/build_dashboard.py" "$JSON" -o "$OUT"

echo "Open $OUT in a browser."
