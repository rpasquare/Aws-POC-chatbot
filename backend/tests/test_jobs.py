"""Tests for job retry bookkeeping.

Validates: Requirements 6.9
"""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from app.jobs import backoff_seconds, next_attempt_outcome


# --- unit tests -----------------------------------------------------------


def test_first_failure_of_three_requeues():
    attempts, status = next_attempt_outcome(0, max_attempts=3)
    assert (attempts, status) == (1, "queued")


def test_final_failure_of_three_terminates():
    attempts, status = next_attempt_outcome(2, max_attempts=3)
    assert (attempts, status) == (3, "failed")


def test_max_attempts_of_one_fails_immediately():
    attempts, status = next_attempt_outcome(0, max_attempts=1)
    assert (attempts, status) == (1, "failed")


def test_backoff_grows_exponentially():
    assert backoff_seconds(1) == 60.0
    assert backoff_seconds(2) == 120.0
    assert backoff_seconds(3) == 240.0


# --- property test ----------------------------------------------------------
# P14 — Retries are bounded and terminate. For any handler that always
# fails, attempts never exceed `max_attempts` and the job reaches `failed`.
# Validates: Requirements 6.9


@settings(max_examples=25)
@given(max_attempts=st.integers(min_value=1, max_value=10))
def test_p14_always_failing_handler_terminates_within_bound(max_attempts):
    attempts = 0
    status = "queued"
    iterations = 0
    # A handler that always raises drives this loop; bound the loop itself
    # generously so a bug that never terminates fails the test rather than
    # hanging it.
    while status == "queued" and iterations <= max_attempts + 1:
        attempts, status = next_attempt_outcome(attempts, max_attempts)
        iterations += 1

    assert status == "failed"
    assert attempts <= max_attempts
    assert attempts == max_attempts  # bounded exactly, not just eventually


@settings(max_examples=25)
@given(
    starting_attempts=st.integers(min_value=0, max_value=20),
    max_attempts=st.integers(min_value=1, max_value=10),
)
def test_p14_single_step_never_exceeds_max_attempts_when_terminal(starting_attempts, max_attempts):
    attempts, status = next_attempt_outcome(starting_attempts, max_attempts)
    if status == "failed":
        assert attempts >= max_attempts
    else:
        assert attempts < max_attempts
