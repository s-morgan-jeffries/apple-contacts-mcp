"""Benchmark conftest — gates the suite on --run-benchmark and wires the
baseline-comparison vs. baseline-capture switch.

Reuses the session fixtures from tests/integration/conftest.py
(real_connector, test_group, integration_env, tmp_contact). The benchmarks
are read-mostly against the real CN store with writes scoped to the
MCP-Test group via the test-mode safety gate.

Run via `make benchmark` (compare) or `make benchmark-baseline` (capture).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

# Integration session fixtures (real_connector, test_group, tmp_contact,
# integration_env) are registered globally via the top-level tests/conftest.py
# `pytest_plugins` directive. They only fire when requested, so unit tests
# remain unaffected.

logger = logging.getLogger(__name__)


BASELINE_PATH = Path(__file__).parent / "baseline.json"
TOLERANCE = 3.0  # measured / baseline must be <= this multiplier


@pytest.fixture(autouse=True)
def _skip_unless_benchmark_flag(request: pytest.FixtureRequest) -> None:
    """Skip every benchmark unless --run-benchmark is set. Mirrors the
    integration-test gate pattern. Without this, `make test` would
    accidentally run benchmarks via the package conftest pickup."""
    if not request.config.getoption("--run-benchmark"):
        pytest.skip("benchmark requires --run-benchmark")


@pytest.fixture(scope="session")
def capture_baseline(pytestconfig: pytest.Config) -> bool:
    """True when --capture-baseline was passed."""
    return bool(pytestconfig.getoption("--capture-baseline"))


@pytest.fixture(scope="session")
def baseline_data() -> dict[str, Any]:
    """Load the committed baseline.json (or empty dict if missing)."""
    if not BASELINE_PATH.exists():
        return {}
    try:
        return json.loads(BASELINE_PATH.read_text())
    except json.JSONDecodeError as exc:
        pytest.fail(f"baseline.json is malformed: {exc}")
    return {}


@pytest.fixture(scope="session")
def _baseline_writer(capture_baseline: bool) -> Iterator[dict[str, Any]]:
    """When capturing, collect all entries and flush at session end so a
    partial run doesn't truncate the file."""
    pending: dict[str, Any] = {}
    yield pending
    if capture_baseline and pending:
        existing = (
            json.loads(BASELINE_PATH.read_text())
            if BASELINE_PATH.exists()
            else {}
        )
        existing.update(pending)
        BASELINE_PATH.write_text(json.dumps(existing, indent=2, sort_keys=True))
        logger.info(
            "Wrote %d baseline entries to %s", len(pending), BASELINE_PATH
        )


@pytest.fixture
def record_or_assert(
    capture_baseline: bool,
    baseline_data: dict[str, Any],
    _baseline_writer: dict[str, Any],
) -> Any:
    """Return the function tests call after timing their op.

    Capture mode → buffer the median into _baseline_writer (flushed at
    session end).
    Compare mode → assert the median is within TOLERANCE × the baseline,
    or skip when no baseline exists yet.
    """
    import datetime

    def _impl(name: str, median_ms: float) -> None:
        print(f"\n[bench] {name}: median={median_ms:.2f} ms")
        if capture_baseline:
            _baseline_writer[name] = {
                "median_ms": round(median_ms, 2),
                "captured": datetime.date.today().isoformat(),
            }
            return
        entry = baseline_data.get(name)
        if entry is None:
            pytest.skip(
                f"no baseline for {name!r} — run with --capture-baseline"
            )
        baseline_ms = float(entry["median_ms"])
        limit_ms = TOLERANCE * baseline_ms
        assert median_ms <= limit_ms, (
            f"{name}: measured {median_ms:.2f} ms > "
            f"{TOLERANCE:.1f}× baseline {baseline_ms:.2f} ms "
            f"(limit {limit_ms:.2f} ms)"
        )

    return _impl
