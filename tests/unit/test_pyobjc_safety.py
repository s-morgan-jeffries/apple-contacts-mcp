"""Tests for scripts/check_pyobjc_safety.sh — the five PyObjC anti-pattern
checks ship a deterministic regression gate (issue #31).

Each pattern gets two fixture-driven cases (one violating, one safe) plus
one smoke test that the real src/ passes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_pyobjc_safety.sh"


def _run(target: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the script against `target` (a directory of .py fixtures)."""
    return subprocess.run(
        [str(SCRIPT), str(target)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


def _write(tmp_path: Path, name: str, body: str) -> None:
    (tmp_path / name).write_text(body)


# ---------------------------------------------------------------------------
# Smoke: the real source tree must be clean.
# ---------------------------------------------------------------------------


class TestRealSourcePasses:
    def test_real_src_passes(self) -> None:
        result = subprocess.run(
            [str(SCRIPT)],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        assert result.returncode == 0, (
            f"check_pyobjc_safety.sh failed on real src/:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


# ---------------------------------------------------------------------------
# Pattern 1: KVC dynamic-key calls
# ---------------------------------------------------------------------------


class TestPattern1KVCDynamicKeys:
    def test_violation_setValue_forKey_is_flagged(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "bad.py",
            'def update(obj, k): obj.setValue_forKey_("x", k)\n',
        )
        result = _run(tmp_path)
        assert result.returncode == 1
        assert "setValue_forKey_" in result.stdout

    def test_violation_valueForKey_is_flagged(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "bad.py",
            "def read(obj, k): return obj.valueForKey_(k)\n",
        )
        result = _run(tmp_path)
        assert result.returncode == 1
        assert "valueForKey_" in result.stdout

    def test_safe_explicit_setters_are_clean(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "ok.py",
            'def update(obj): obj.setGivenName_("Alice")\n',
        )
        result = _run(tmp_path)
        assert result.returncode == 0, result.stdout


# ---------------------------------------------------------------------------
# Pattern 2: dataWithContacts_error_ without descriptorForRequiredKeys
# ---------------------------------------------------------------------------


class TestPattern2VCardDescriptorMissing:
    def test_violation_export_without_descriptor_is_flagged(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "export.py",
            "def export(contacts):\n"
            "    data, err = CNContactVCardSerialization.dataWithContacts_error_(contacts, None)\n"
            "    return data\n",
        )
        result = _run(tmp_path)
        assert result.returncode == 1
        assert "pattern-2" in result.stdout
        assert "export" in result.stdout

    def test_safe_export_with_descriptor_is_clean(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "export.py",
            "def export(contacts):\n"
            "    desc = CNContactVCardSerialization.descriptorForRequiredKeys()\n"
            "    _ = [c for c in contacts if desc]\n"
            "    data, err = CNContactVCardSerialization.dataWithContacts_error_(contacts, None)\n"
            "    return data\n",
        )
        result = _run(tmp_path)
        assert result.returncode == 0, result.stdout


# ---------------------------------------------------------------------------
# Pattern 3: imageData() without imageDataAvailable() guard
# ---------------------------------------------------------------------------


class TestPattern3PhotoGuardMissing:
    def test_violation_imageData_without_guard_is_flagged(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "photo.py",
            "def read(contact):\n    return contact.imageData()\n",
        )
        result = _run(tmp_path)
        assert result.returncode == 1
        assert "pattern-3" in result.stdout

    def test_safe_imageData_with_guard_is_clean(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "photo.py",
            "def read(contact):\n"
            "    if not contact.imageDataAvailable():\n"
            "        return None\n"
            "    return contact.imageData()\n",
        )
        result = _run(tmp_path)
        assert result.returncode == 0, result.stdout

    def test_safe_no_imageData_call_is_clean(self, tmp_path: Path) -> None:
        """Variable named `image_data` is not an imageData() method call."""
        _write(
            tmp_path,
            "photo.py",
            "def read():\n    image_data = b''\n    return image_data\n",
        )
        result = _run(tmp_path)
        assert result.returncode == 0, result.stdout


# ---------------------------------------------------------------------------
# Pattern 4: @mcp.tool() touches connector without TCC pre-check
# ---------------------------------------------------------------------------


class TestPattern4MissingTCCGate:
    def test_violation_mcp_tool_without_auth_gate_is_flagged(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "tool.py",
            "import mcp\n"
            "\n"
            "@mcp.tool()\n"
            "def my_tool():\n"
            "    return connector._run_cn_enumerate_contacts(0, 50)\n",
        )
        result = _run(tmp_path)
        assert result.returncode == 1
        assert "pattern-4" in result.stdout
        assert "my_tool" in result.stdout

    def test_safe_mcp_tool_with_auth_gate_is_clean(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "tool.py",
            "import mcp\n"
            "\n"
            "@mcp.tool()\n"
            "def my_tool():\n"
            "    err = _require_contacts_authorization()\n"
            "    if err is not None:\n"
            "        return err\n"
            "    return connector._run_cn_enumerate_contacts(0, 50)\n",
        )
        result = _run(tmp_path)
        assert result.returncode == 0, result.stdout

    def test_check_authorization_exception_is_allowed(
        self, tmp_path: Path
    ) -> None:
        """check_authorization IS the TCC status getter and is exempt."""
        _write(
            tmp_path,
            "tool.py",
            "import mcp\n"
            "\n"
            "@mcp.tool()\n"
            "def check_authorization():\n"
            "    return connector._run_cn_authorization_status()\n",
        )
        result = _run(tmp_path)
        assert result.returncode == 0, result.stdout

    def test_non_mcp_tool_function_is_ignored(self, tmp_path: Path) -> None:
        """Plain helper functions aren't subject to pattern 4."""
        _write(
            tmp_path,
            "helper.py",
            "def helper():\n"
            "    return connector._run_cn_enumerate_contacts(0, 50)\n",
        )
        result = _run(tmp_path)
        assert result.returncode == 0, result.stdout


# ---------------------------------------------------------------------------
# Pattern 5: destructive @mcp.tool() missing check_test_mode_safety
# ---------------------------------------------------------------------------


class TestPattern5MissingTestModeGate:
    def test_violation_destructive_tool_without_safety_is_flagged(
        self, tmp_path: Path
    ) -> None:
        # delete_contact is in DESTRUCTIVE_OPERATIONS — flag if missing gate.
        _write(
            tmp_path,
            "tool.py",
            "import mcp\n"
            "\n"
            "@mcp.tool()\n"
            "def delete_contact(identifier):\n"
            "    err = _require_contacts_authorization()\n"
            "    return connector._run_cn_delete_contact(identifier)\n",
        )
        result = _run(tmp_path)
        assert result.returncode == 1
        assert "pattern-5" in result.stdout
        assert "delete_contact" in result.stdout

    def test_safe_destructive_with_safety_gate_is_clean(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "tool.py",
            "import mcp\n"
            "\n"
            "@mcp.tool()\n"
            "def delete_contact(identifier, group_identifier=None):\n"
            "    err = _require_contacts_authorization()\n"
            "    safety = check_test_mode_safety('delete_contact', group=group_identifier)\n"
            "    if safety is not None:\n"
            "        return safety\n"
            "    return connector._run_cn_delete_contact(identifier)\n",
        )
        result = _run(tmp_path)
        assert result.returncode == 0, result.stdout

    def test_non_destructive_tool_does_not_require_safety_gate(
        self, tmp_path: Path
    ) -> None:
        """list_contacts is a read; pattern 5 doesn't apply."""
        _write(
            tmp_path,
            "tool.py",
            "import mcp\n"
            "\n"
            "@mcp.tool()\n"
            "def list_contacts():\n"
            "    err = _require_contacts_authorization()\n"
            "    return connector._run_cn_enumerate_contacts(0, 50)\n",
        )
        result = _run(tmp_path)
        assert result.returncode == 0, result.stdout


# ---------------------------------------------------------------------------
# Misc: bad source dir
# ---------------------------------------------------------------------------


class TestMissingSourceDir:
    def test_nonexistent_dir_returns_exit_2(self, tmp_path: Path) -> None:
        result = subprocess.run(
            [str(SCRIPT), str(tmp_path / "does_not_exist")],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        assert result.returncode == 2
