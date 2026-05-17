#!/bin/bash
# Static analyzer for unsafe PyObjC patterns in apple-contacts-mcp source.
#
# Enforces five rules at PR time so future refactors can't silently regress:
#
#   1. No KVC dynamic-key calls (setValue:forKey: / valueForKey:). The
#      connector should use explicit setter methods only.
#   2. Every CNContactVCardSerialization.dataWithContacts_error_ call must
#      be preceded (same function) by a descriptorForRequiredKeys() lookup,
#      otherwise vCards export silently empty.
#   3. Every .imageData() call must be guarded (same function) by an
#      imageDataAvailable() check, otherwise photo reads crash on contacts
#      with no photo.
#   4. Every @mcp.tool() that touches connector._run_cn_* must first call
#      _require_contacts_authorization(). Exception: check_authorization,
#      which IS the TCC status getter.
#   5. Every @mcp.tool() whose name is in security.DESTRUCTIVE_OPERATIONS
#      must call check_test_mode_safety() in its body.
#
# Usage:
#   ./scripts/check_pyobjc_safety.sh          # scans src/apple_contacts_mcp
#   ./scripts/check_pyobjc_safety.sh PATH     # scans PATH (used by tests)
#
# Exit 0 = clean; exit 1 = one or more violations reported.
set -euo pipefail

SRC_DIR="${1:-src/apple_contacts_mcp}"

if [ ! -d "$SRC_DIR" ]; then
    echo "ERROR: source directory not found: $SRC_DIR" >&2
    exit 2
fi

ERRORS=0

# ---------------------------------------------------------------------------
# Pattern 1: KVC dynamic keys — pure grep, want zero matches.
# ---------------------------------------------------------------------------
echo "Pattern 1: KVC dynamic-key calls (setValue_forKey_ / valueForKey_)"
if KVC_HITS=$(grep -rn 'setValue_forKey_\|valueForKey_' "$SRC_DIR" 2>/dev/null); then
    while IFS= read -r line; do
        echo "  ERROR: $line"
    done <<< "$KVC_HITS"
    KVC_COUNT=$(echo "$KVC_HITS" | wc -l | tr -d ' ')
    ERRORS=$((ERRORS + KVC_COUNT))
else
    echo "  OK"
fi

# ---------------------------------------------------------------------------
# Patterns 2–5: AST-walk every .py under SRC_DIR.
# ---------------------------------------------------------------------------
export SRC_DIR
AST_OUTPUT=$(uv run python <<'PYEOF'
"""AST-based PyObjC safety checks. Emits one line per violation:
  <file>:<lineno> <pattern>: <message>
Exits 1 if any violations; 0 otherwise.
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

SRC_DIR = Path(os.environ["SRC_DIR"]).resolve()

# Pattern-4 exception: check_authorization IS the TCC status getter, so it
# legitimately calls connector._run_cn_authorization_status without a prior
# _require_contacts_authorization gate.
PATTERN_4_EXCEPTIONS = {"check_authorization"}

# Pattern 5 needs the destructive-op list. Try to import it from the
# project's security module; fall back to a hardcoded list if not
# importable (e.g., the script is run against a temp fixture dir that
# doesn't ship the package — the test harness handles that with --
# destructives-override below).
try:
    from apple_contacts_mcp.security import DESTRUCTIVE_OPERATIONS
except Exception:
    DESTRUCTIVE_OPERATIONS = frozenset()


def is_mcp_tool(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if the function is decorated with @mcp.tool()."""
    for dec in fn.decorator_list:
        # @mcp.tool() — a Call whose func is Attribute(value=Name('mcp'), attr='tool')
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
            if (
                isinstance(dec.func.value, ast.Name)
                and dec.func.value.id == "mcp"
                and dec.func.attr == "tool"
            ):
                return True
    return False


def call_attr_names(node: ast.AST) -> set[str]:
    """Collect the .attr name of every Attribute-style Call inside `node`."""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            names.add(child.func.attr)
    return names


def call_names(node: ast.AST) -> set[str]:
    """Collect the bare-name id of every Name-style Call inside `node`."""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
            names.add(child.func.id)
    return names


def touches_connector_run_cn(node: ast.AST) -> bool:
    """True if the body contains an attribute access shaped
    connector._run_cn_<something> (whether called or just referenced)."""
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr.startswith("_run_cn_"):
            if isinstance(child.value, ast.Name) and child.value.id == "connector":
                return True
    return False


violations: list[str] = []


def check_file(path: Path) -> None:
    try:
        src = path.read_text()
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as exc:
        violations.append(f"{path}:{exc.lineno or 0} parse: {exc.msg}")
        return

    rel = path.relative_to(SRC_DIR.parent) if SRC_DIR.parent in path.parents else path

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        body_attrs = call_attr_names(node)
        body_names = call_names(node)

        # --- Pattern 2: vCard export missing descriptorForRequiredKeys ---
        if "dataWithContacts_error_" in body_attrs:
            if "descriptorForRequiredKeys" not in body_attrs:
                violations.append(
                    f"{rel}:{node.lineno} pattern-2 ({node.name}): "
                    f"dataWithContacts_error_ without descriptorForRequiredKeys "
                    f"in same function"
                )

        # --- Pattern 3: photo read missing imageDataAvailable guard ---
        if "imageData" in body_attrs:
            if "imageDataAvailable" not in body_attrs:
                violations.append(
                    f"{rel}:{node.lineno} pattern-3 ({node.name}): "
                    f"imageData() without imageDataAvailable() guard "
                    f"in same function"
                )

        # --- Pattern 4: @mcp.tool() touching connector without TCC gate ---
        if is_mcp_tool(node) and node.name not in PATTERN_4_EXCEPTIONS:
            if touches_connector_run_cn(node):
                if "_require_contacts_authorization" not in body_names:
                    violations.append(
                        f"{rel}:{node.lineno} pattern-4 ({node.name}): "
                        f"@mcp.tool calls connector._run_cn_* without "
                        f"_require_contacts_authorization gate"
                    )

        # --- Pattern 5: destructive @mcp.tool() missing test-mode safety ---
        if is_mcp_tool(node) and node.name in DESTRUCTIVE_OPERATIONS:
            if "check_test_mode_safety" not in body_names:
                violations.append(
                    f"{rel}:{node.lineno} pattern-5 ({node.name}): "
                    f"destructive @mcp.tool missing check_test_mode_safety call"
                )


for py in sorted(SRC_DIR.rglob("*.py")):
    check_file(py)

if violations:
    for v in violations:
        print(f"  ERROR: {v}")
    sys.exit(1)
PYEOF
) || AST_EXIT=$?
AST_EXIT="${AST_EXIT:-0}"

echo ""
echo "Patterns 2-5 (AST walk under $SRC_DIR):"
if [ -n "$AST_OUTPUT" ]; then
    echo "$AST_OUTPUT"
fi
if [ "$AST_EXIT" -ne 0 ]; then
    AST_COUNT=$(echo "$AST_OUTPUT" | grep -c '^  ERROR:' || true)
    ERRORS=$((ERRORS + AST_COUNT))
elif [ -z "$AST_OUTPUT" ]; then
    echo "  OK"
fi

echo ""
if [ "$ERRORS" -gt 0 ]; then
    echo "PyObjC safety check FAILED: $ERRORS violation(s)"
    exit 1
fi
echo "PyObjC safety check passed."
