"""Shared pytest fixtures for the sensortap test suite.

Requirement 16.8: the default test run must complete within a 60 second
wall-clock budget, and the run must fail when that budget is exceeded.
"""

from __future__ import annotations

import time

import pytest

DEFAULT_RUN_BUDGET_SECONDS = 60.0


@pytest.fixture(scope="session", autouse=True)
def _enforce_wall_clock_budget():
    """Record the session start time and fail the run past the budget.

    The check runs at session teardown, after every test has executed,
    so a slow suite is reported as a budget violation rather than a
    silent pass.
    """
    start = time.monotonic()
    yield
    elapsed = time.monotonic() - start
    if elapsed > DEFAULT_RUN_BUDGET_SECONDS:
        pytest.fail(
            f"Default test run exceeded the {DEFAULT_RUN_BUDGET_SECONDS:.0f}s "
            f"wall-clock budget (took {elapsed:.2f}s). See Requirement 16.8."
        )
