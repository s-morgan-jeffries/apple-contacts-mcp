"""Pytest configuration and fixtures."""

import pytest

# Register the integration-test session fixtures so benchmarks can reuse them.
# Pytest 9 requires `pytest_plugins` to live in the top-level conftest, not in
# subdirectory conftests. The fixtures only fire when requested — unit tests
# don't reference them, so they remain a no-op for `make test`.
pytest_plugins = ["tests.integration.conftest"]


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add custom command line options."""
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run integration tests (requires Apple Contacts setup)",
    )
    parser.addoption(
        "--run-benchmark",
        action="store_true",
        default=False,
        help="Run benchmark tests (requires Apple Contacts setup; produces timings)",
    )
    parser.addoption(
        "--capture-baseline",
        action="store_true",
        default=False,
        help=(
            "When running benchmarks, write observed timings to baseline.json "
            "instead of comparing against it. Use after intentional perf changes."
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest."""
    config.addinivalue_line(
        "markers", "integration: mark test as integration test (requires --run-integration)"
    )
    config.addinivalue_line("markers", "e2e: mark test as end-to-end test (full MCP stack)")
    config.addinivalue_line("markers", "benchmark: mark test as performance benchmark")
    config.addinivalue_line("markers", "slow: mark test as slow-running")
