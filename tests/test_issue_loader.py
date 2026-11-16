"""Tests for reading acceptance criteria out of a real-looking issue body."""

from pathlib import Path

import pytest

from src.issue_loader import extract_acceptance_criteria, load_issue_from_fixture

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "issue_rfqa.json"


def test_fixture_parses_five_criteria_with_ids():
    issue = load_issue_from_fixture(FIXTURE)
    assert issue.number == 42
    assert issue.column == "Ready for QA"
    assert [ac.id for ac in issue.acceptance_criteria] == ["AC1", "AC2", "AC3", "AC4", "AC5"]
    assert "counter" in issue.acceptance_criteria[2].text


def test_checkbox_bullets():
    body = """## Acceptance Criteria
- [ ] Users can add a todo
- [x] Users can delete a todo
"""
    criteria = extract_acceptance_criteria(body)
    assert [c.text for c in criteria] == ["Users can add a todo", "Users can delete a todo"]
    assert [c.id for c in criteria] == ["AC1", "AC2"]


def test_numbered_and_starred_bullets():
    body = """### Acceptance criteria
1. Login succeeds with valid credentials
2. Login fails with a bad password
* The error message is visible
"""
    assert len(extract_acceptance_criteria(body)) == 3


def test_explicit_ids_are_preserved():
    body = """## Acceptance Criteria
- [ ] AC1: counter shows one item
- [ ] AC-2 — counter shows two items
"""
    criteria = extract_acceptance_criteria(body)
    assert [c.id for c in criteria] == ["AC1", "AC2"]
    assert criteria[0].text == "counter shows one item"


def test_stops_at_the_next_heading():
    body = """## Acceptance Criteria
- [ ] Only this one

## Out of scope
- Not this one
"""
    criteria = extract_acceptance_criteria(body)
    assert len(criteria) == 1


def test_falls_back_to_bullets_without_a_heading():
    body = """We should check:
- the page loads
- the button works
"""
    assert len(extract_acceptance_criteria(body)) == 2


def test_empty_body_returns_nothing():
    assert extract_acceptance_criteria("") == []
    assert extract_acceptance_criteria("## Notes\nNothing testable here.") == []


def test_missing_fixture_raises_helpful_error():
    with pytest.raises(FileNotFoundError, match="fixtures/issue_rfqa.json"):
        load_issue_from_fixture("does/not/exist.json")
