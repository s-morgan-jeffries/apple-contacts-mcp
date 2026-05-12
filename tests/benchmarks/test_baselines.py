"""Per-tool performance baselines for the Apple Contacts MCP connector.

Opt-in via ``--run-benchmark`` (gated in tests/benchmarks/conftest.py).
Each benchmark times the operation N times after a warm-up run, takes the
median, and either captures it into baseline.json (``--capture-baseline``)
or asserts it stays within TOLERANCE × baseline.

Reads run against the user's real CN store. Writes are scoped to
CONTACTS_TEST_GROUP=MCP-Test via the test-mode safety gate (set up by
tests/integration/conftest.py's session fixtures).
"""

from __future__ import annotations

import logging
import statistics
import time
from collections.abc import Callable
from typing import Any

import pytest

from apple_contacts_mcp.contacts_connector import ContactsConnector

logger = logging.getLogger(__name__)


_DEFAULT_RUNS = 7


def _time_op(
    name: str,
    op: Callable[[], Any],
    record_or_assert: Callable[[str, float], None],
    *,
    runs: int = _DEFAULT_RUNS,
) -> float:
    """Run ``op`` once for warm-up, then ``runs`` more times. Returns the
    median in milliseconds and dispatches it to ``record_or_assert``."""
    op()  # warm-up
    samples_ms: list[float] = []
    for _ in range(runs):
        t0 = time.perf_counter()
        op()
        samples_ms.append((time.perf_counter() - t0) * 1000.0)
    median_ms = statistics.median(samples_ms)
    record_or_assert(name, median_ms)
    return median_ms


pytestmark = [pytest.mark.benchmark]


# ---------------------------------------------------------------------------
# Read paths — whole-store
# ---------------------------------------------------------------------------


def test_list_contacts_page(
    real_connector: ContactsConnector,
    record_or_assert: Callable[[str, float], None],
) -> None:
    _time_op(
        "test_list_contacts_page",
        lambda: real_connector._run_cn_enumerate_contacts(offset=0, limit=50),
        record_or_assert,
    )


def test_list_contacts_full_first_page(
    real_connector: ContactsConnector,
    record_or_assert: Callable[[str, float], None],
) -> None:
    _time_op(
        "test_list_contacts_full_first_page",
        lambda: real_connector._run_cn_enumerate_contacts(offset=0, limit=200),
        record_or_assert,
    )


def test_search_contacts_name(
    real_connector: ContactsConnector,
    record_or_assert: Callable[[str, float], None],
) -> None:
    _time_op(
        "test_search_contacts_name",
        lambda: real_connector._run_cn_search_contacts(
            field="name", value="a", limit=200
        ),
        record_or_assert,
    )


def test_search_contacts_phone(
    real_connector: ContactsConnector,
    record_or_assert: Callable[[str, float], None],
) -> None:
    _time_op(
        "test_search_contacts_phone",
        lambda: real_connector._run_cn_search_contacts(
            field="phone", value="555", limit=200
        ),
        record_or_assert,
    )


def test_search_contacts_email(
    real_connector: ContactsConnector,
    record_or_assert: Callable[[str, float], None],
) -> None:
    _time_op(
        "test_search_contacts_email",
        lambda: real_connector._run_cn_search_contacts(
            field="email", value="@", limit=200
        ),
        record_or_assert,
    )


def test_list_groups(
    real_connector: ContactsConnector,
    record_or_assert: Callable[[str, float], None],
) -> None:
    _time_op(
        "test_list_groups",
        lambda: real_connector._run_cn_list_groups(),
        record_or_assert,
    )


def test_list_containers(
    real_connector: ContactsConnector,
    record_or_assert: Callable[[str, float], None],
) -> None:
    _time_op(
        "test_list_containers",
        lambda: real_connector._run_cn_list_containers(),
        record_or_assert,
    )


# ---------------------------------------------------------------------------
# Per-contact reads
# ---------------------------------------------------------------------------


def test_get_contact(
    real_connector: ContactsConnector,
    tmp_contact: str,
    record_or_assert: Callable[[str, float], None],
) -> None:
    _time_op(
        "test_get_contact",
        lambda: real_connector._run_cn_unified_contact(tmp_contact),
        record_or_assert,
    )


def test_get_contact_with_niche(
    real_connector: ContactsConnector,
    tmp_contact: str,
    record_or_assert: Callable[[str, float], None],
) -> None:
    _time_op(
        "test_get_contact_with_niche",
        lambda: real_connector._run_cn_unified_contact(
            tmp_contact, include_niche=True
        ),
        record_or_assert,
    )


def test_export_vcard_single(
    real_connector: ContactsConnector,
    tmp_contact: str,
    record_or_assert: Callable[[str, float], None],
) -> None:
    _time_op(
        "test_export_vcard_single",
        lambda: real_connector._run_cn_export_vcard([tmp_contact]),
        record_or_assert,
    )


def test_read_photo_no_photo_set(
    real_connector: ContactsConnector,
    tmp_contact: str,
    record_or_assert: Callable[[str, float], None],
) -> None:
    """Fixture contact has no photo → exercises the fast path
    (imageDataAvailable=False, imageData() never called)."""
    _time_op(
        "test_read_photo_no_photo_set",
        lambda: real_connector._run_cn_read_photo(tmp_contact),
        record_or_assert,
    )


# ---------------------------------------------------------------------------
# Write paths (scoped to MCP-Test via integration_env)
# ---------------------------------------------------------------------------


def test_update_contact(
    real_connector: ContactsConnector,
    tmp_contact: str,
    record_or_assert: Callable[[str, float], None],
) -> None:
    """Single update round-trip on the fixture contact. Same field each run
    so we measure CN's save cost, not field-diff cost."""
    _time_op(
        "test_update_contact",
        lambda: real_connector._run_cn_update_contact(
            identifier=tmp_contact, fields={"nickname": "bench"}
        ),
        record_or_assert,
    )


def test_create_then_delete_contact(
    real_connector: ContactsConnector,
    test_group: str,
    integration_env: None,
    record_or_assert: Callable[[str, float], None],
) -> None:
    """Full create + delete cycle in MCP-Test. The pair is the unit of work
    so we don't leak intermediate contacts."""

    def _cycle() -> None:
        new_id = real_connector._run_cn_create_contact(
            fields={"given_name": "Bench", "family_name": "Cycle"},
            group_identifier=test_group,
        )
        real_connector._run_cn_delete_contact(new_id)

    _time_op(
        "test_create_then_delete_contact",
        _cycle,
        record_or_assert,
    )


# ---------------------------------------------------------------------------
# AppleScript path — read_note. Expect 200–400 ms / call (subprocess cost).
# ---------------------------------------------------------------------------


def test_read_note_applescript(
    real_connector: ContactsConnector,
    tmp_contact: str,
    record_or_assert: Callable[[str, float], None],
) -> None:
    _time_op(
        "test_read_note_applescript",
        lambda: real_connector._run_applescript_read_note(tmp_contact),
        record_or_assert,
        # AppleScript subprocess cost is high; fewer runs to keep wall-time sane.
        runs=5,
    )
