"""Tests for the document status state machine.

Validates: Requirements 6.2
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings, strategies as st

from app.documents import (
    STATUSES,
    TRANSITIONS,
    IllegalTransitionError,
    is_legal_transition,
    validate_transition,
)

ALL_STATUS_PAIRS = [(c, t) for c in STATUSES for t in STATUSES]
DECLARED_EDGES = [(c, t) for c, targets in TRANSITIONS.items() for t in targets]
UNDECLARED_EDGES = [pair for pair in ALL_STATUS_PAIRS if pair not in DECLARED_EDGES]


# --- unit tests -------------------------------------------------------------


@pytest.mark.parametrize("current,target", DECLARED_EDGES)
def test_declared_edges_are_accepted(current, target):
    assert validate_transition(current, target) == target


@pytest.mark.parametrize("current,target", UNDECLARED_EDGES)
def test_undeclared_edges_are_rejected(current, target):
    with pytest.raises(IllegalTransitionError):
        validate_transition(current, target)


def test_purged_is_terminal():
    assert TRANSITIONS["purged"] == frozenset()


def test_unknown_status_is_rejected():
    with pytest.raises(IllegalTransitionError):
        validate_transition("ready", "not_a_real_status")
    with pytest.raises(IllegalTransitionError):
        validate_transition("not_a_real_status", "ready")


# --- property test -----------------------------------------------------------
# P9 — Status transitions are legal. For any sequence of pipeline events, the
# document's status only ever follows a declared transition, and no
# undeclared state is reachable. Validates: Requirements 6.2

status_st = st.sampled_from(sorted(STATUSES))


@settings(max_examples=25)
@given(current=status_st, target=status_st)
def test_p9_validation_agrees_with_declared_table(current, target):
    declared = target in TRANSITIONS.get(current, frozenset())
    assert is_legal_transition(current, target) == declared
    if declared:
        assert validate_transition(current, target) == target
    else:
        with pytest.raises(IllegalTransitionError):
            validate_transition(current, target)


@settings(max_examples=25)
@given(path=st.lists(status_st, min_size=1, max_size=12))
def test_p9_only_declared_edges_survive_a_walk(path):
    """Drive a sequence of proposed transitions from `awaiting_upload` and
    keep only the ones that validate. Every retained hop must be a declared
    edge, and the running state must never leave the declared status set.
    """
    current = "awaiting_upload"
    visited = {current}
    for target in path:
        try:
            current = validate_transition(current, target)
        except IllegalTransitionError:
            continue
        assert current in STATUSES
        visited.add(current)
    assert visited <= STATUSES
