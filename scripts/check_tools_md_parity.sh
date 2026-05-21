#!/bin/bash
# Verify every @mcp.tool() in server.py has a TOOLS.md entry, and vice versa.
set -euo pipefail

SERVER="src/apple_contacts_mcp/server.py"
DOC="docs/reference/TOOLS.md"

echo "Checking TOOLS.md <-> @mcp.tool() parity..."

SERVER_TOOLS=$(grep -A1 '@mcp.tool' "$SERVER" 2>/dev/null \
  | grep 'def ' \
  | sed 's/.*def \([a-z_]*\)(.*/\1/' \
  | sort || true)

DOC_TOOLS=$(grep -E '^### [a-z_]+$' "$DOC" 2>/dev/null \
  | sed 's/^### //' \
  | sort || true)

echo ""
echo "Server tools (@mcp.tool):"
echo "$SERVER_TOOLS" | sed 's/^/  /'
echo ""
echo "Documented tools (TOOLS.md):"
echo "$DOC_TOOLS" | sed 's/^/  /'
echo ""

UNDOCUMENTED=$(comm -23 <(echo "$SERVER_TOOLS") <(echo "$DOC_TOOLS"))
ORPHANED=$(comm -13 <(echo "$SERVER_TOOLS") <(echo "$DOC_TOOLS"))

EXIT=0
if [ -n "$UNDOCUMENTED" ]; then
    echo "ERROR: @mcp.tool() functions missing from TOOLS.md:"
    echo "$UNDOCUMENTED" | sed 's/^/  - /'
    echo ""
    EXIT=1
fi
if [ -n "$ORPHANED" ]; then
    echo "ERROR: TOOLS.md entries with no matching @mcp.tool() in server.py:"
    echo "$ORPHANED" | sed 's/^/  - /'
    echo ""
    EXIT=1
fi

if [ "$EXIT" -eq 0 ]; then
    echo "TOOLS.md and @mcp.tool() registrations are in parity."
fi
exit $EXIT
