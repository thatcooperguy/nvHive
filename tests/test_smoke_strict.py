"""Tests that the smoke-test report distinguishes soft-pass from real pass.

Why this exists: before this change, a 429 from every provider silently
re-labeled the smoke test ``passed=True``, masking real outages behind a
green CI signal. The contract this file pins:

* ``TestResult.soft_pass`` is set only when the result is environmentally
  transient (rate limit / quota), so callers can tell true green from
  amber-green-counted-as-green.
* ``SmokeTestReport.strict_failed()`` counts both hard failures and
  soft-passes, so ``nvh test --strict`` actually fails the run.
"""

from __future__ import annotations

from nvh.core.smoke_test import SmokeTestReport, TestResult


def test_test_result_defaults_are_not_soft() -> None:
    """A normal pass must not be marked soft_pass."""
    r = TestResult(name="x", category="Core", passed=True)
    assert r.soft_pass is False
    assert r.soft_reason == ""


def test_report_strict_failed_includes_soft_passes() -> None:
    """A report with N soft-passes and M hard fails fails strict-mode N+M."""
    report = SmokeTestReport()
    report.results = [
        TestResult(name="a", category="Core", passed=True),
        TestResult(
            name="b", category="Providers", passed=True,
            soft_pass=True, soft_reason="rate limited",
        ),
        TestResult(
            name="c", category="Providers", passed=True,
            soft_pass=True, soft_reason="rate limited",
        ),
        TestResult(name="d", category="API", passed=False, error="boom"),
    ]
    assert report.passed == 3            # legacy count: counts soft as passing
    assert report.failed == 1            # legacy count: only hard fails
    assert report.soft_passed == 2
    assert report.strict_failed() == 3   # soft (2) + hard (1)


def test_report_strict_failed_zero_when_truly_green() -> None:
    report = SmokeTestReport()
    report.results = [
        TestResult(name="a", category="Core", passed=True),
        TestResult(name="b", category="Core", passed=True),
    ]
    assert report.failed == 0
    assert report.soft_passed == 0
    assert report.strict_failed() == 0
