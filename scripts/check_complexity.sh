#!/bin/bash
# Check cyclomatic complexity of Python source files using radon.
# Threshold: CC <= 20 for new code. Documented exceptions allowed.
#
# Always invoked via `uv run` so radon executes under the project's pinned
# Python (3.10+) — earlier interpreters can't parse `match` statements
# introduced in #53.
set -euo pipefail

THRESHOLD=20
SRC_DIR="src/apple_contacts_mcp"

if ! command -v uv &> /dev/null; then
    echo "Error: uv not found. Install from https://docs.astral.sh/uv/."
    exit 1
fi

echo "Checking cyclomatic complexity (threshold: CC <= $THRESHOLD)..."
echo ""

# Visibility report — show every function ranked C or worse (CC >= 11) so
# we surface things drifting upward before they breach the gate.
REPORT=$(uv run radon cc "$SRC_DIR" -n C -s)

if [ -z "$REPORT" ]; then
    echo "All functions have complexity <= B (acceptable)."
    exit 0
fi

echo "$REPORT"
echo ""

# Gate. `-n A` emits every function — we then apply $THRESHOLD honestly in
# Python. (The previous `-n F` filter only emitted CC>=41, which let
# CC=21..40 functions slip through despite the documented threshold.)
uv run radon cc "$SRC_DIR" -n A -j | uv run python -c "
import json, sys
data = json.load(sys.stdin)
failures = []
for filepath, functions in data.items():
    for func in functions:
        if func['complexity'] > $THRESHOLD:
            failures.append(
                f\"  {filepath}:{func['lineno']} {func['name']} (CC={func['complexity']})\"
            )
if failures:
    print('Functions exceeding threshold:')
    for f in failures:
        print(f)
    sys.exit(1)
print('All functions within threshold.')
"
