"""PRD REQ-COORD-01: occupation compatibility, stacking and independent release."""
from contextlib import ExitStack

import pytest

from app.domain.errors import (
    TestCaseBusy as CaseBusy,
    TestCaseHasActiveRun as CaseHasActiveRun,
    TestCasePlanning as CasePlanning,
)
from app.test_case_lock import TestCaseLock as CaseLock, TestCaseOccupation as Occupation


# Rows and columns follow the product's occupation compatibility table.
OCCUPATIONS: tuple[Occupation, ...] = (
    "archiving", "editing", "revising", "pre_planning", "planning", "pre_running", "running",
)
COMPATIBILITY = (
    "0000000",
    "0010111",
    "0101001",
    "0010111",
    "0101001",
    "0101001",
    "0111110",
)
CONFLICTS = (CaseBusy, CasePlanning, CaseHasActiveRun)


@pytest.mark.parametrize(("held", "requested", "compatible"), [
    (held, requested, COMPATIBILITY[row][column] == "1")
    for row, held in enumerate(OCCUPATIONS)
    for column, requested in enumerate(OCCUPATIONS)
])
def test_occupation_compatibility(
    held: Occupation, requested: Occupation, compatible: bool,
) -> None:
    case_lock = CaseLock()
    with case_lock.hold("case", held):
        if compatible:
            with case_lock.hold("case", requested):
                pass
        else:
            with pytest.raises(CONFLICTS):
                with case_lock.hold("case", requested):
                    pytest.fail("conflicting occupation was accepted")
        # A rejected request or a completed peer cannot release its owner.
        with pytest.raises(CONFLICTS):
            case_lock.acquire("case", held)
        with case_lock.hold("other-case", requested):
            pass
    with case_lock.hold("case", "archiving"):
        pass


def test_planning_preparation_adds_and_then_releases_editing_constraint() -> None:
    case_lock = CaseLock()
    with case_lock.hold("case", "planning"):
        with case_lock.hold("case", "pre_planning"):
            with pytest.raises(CasePlanning):
                case_lock.acquire("case", "editing")
            with pytest.raises(CaseBusy):
                case_lock.acquire("case", "revising")
        with case_lock.hold("case", "editing"):
            with pytest.raises(CaseBusy):
                case_lock.acquire("case", "planning")
        with pytest.raises(CaseBusy):
            case_lock.acquire("case", "revising")


def test_failed_run_preparation_admission_releases_only_its_main_occupation() -> None:
    case_lock = CaseLock()
    with case_lock.hold("case", "planning"):
        with pytest.raises(CaseBusy):
            with case_lock.hold("case", "running"):
                with case_lock.hold("case", "pre_running"):
                    pytest.fail("planning must prevent a new run preparation")
        with case_lock.hold("case", "running"):
            pass
        with pytest.raises(CaseBusy):
            case_lock.acquire("case", "planning")


def test_run_creation_releases_preparation_but_retains_main_occupation() -> None:
    case_lock = CaseLock()
    with case_lock.hold("case", "running"):
        with case_lock.hold("case", "pre_running"):
            for operation in ("planning", "revising"):
                with pytest.raises(CaseBusy):
                    case_lock.acquire("case", operation)
            with case_lock.hold("case", "editing"):
                pass
        with case_lock.hold("case", "planning"), case_lock.hold("case", "pre_planning"):
            pass
        with case_lock.hold("case", "revising"):
            pass
        with pytest.raises(CaseHasActiveRun):
            case_lock.acquire("case", "archiving")


def test_preparation_failure_preserves_an_existing_edit() -> None:
    case_lock = CaseLock()
    with case_lock.hold("case", "editing"):
        with pytest.raises(CaseBusy):
            with ExitStack() as occupations:
                occupations.enter_context(case_lock.hold("case", "planning"))
                occupations.enter_context(case_lock.hold("case", "pre_planning"))
        with case_lock.hold("case", "revising"):
            pass
        with pytest.raises(CaseBusy):
            case_lock.acquire("case", "editing")
