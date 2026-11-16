"""Unit tests for the guardrails — the two things that must not be vibes.

Run with:  python -m pytest tests -q
"""

from src.guardrails import LoopGuard, normalise, snapshot_fingerprint, verify_quote

SNAPSHOT_A = """- heading "todos" [level=1] [ref=e3]
- textbox "What needs to be done?" [ref=e5]
- list [ref=e8]
- listitem:
  - checkbox "Toggle Todo" [ref=e12]
  - text "Buy milk" [ref=e13]
- contentinfo:
  - text "1 item left" [ref=e20]"""

SNAPSHOT_B = SNAPSHOT_A.replace("1 item left", "2 items left").replace(
    'text "Buy milk"', 'text "Buy milk"\n  - text "Walk dog"'
)


# ---------------------------------------------------------------------------
# Evidence verification
# ---------------------------------------------------------------------------

def test_verbatim_quote_is_accepted():
    check = verify_quote("1 item left", [SNAPSHOT_A])
    assert check.ok
    assert check.similarity == 1.0
    assert check.snapshot_index == 0


def test_quote_survives_ref_markers_being_dropped():
    # A model that copies the snapshot including [ref=...] must still match.
    check = verify_quote('text "Buy milk" [ref=e13]', [SNAPSHOT_A])
    assert check.ok


def test_quote_matches_the_right_snapshot():
    check = verify_quote("2 items left", [SNAPSHOT_A, SNAPSHOT_B])
    assert check.ok
    assert check.snapshot_index == 1


def test_fabricated_quote_is_rejected():
    check = verify_quote("99 items left", [SNAPSHOT_A, SNAPSHOT_B])
    assert not check.ok
    assert "not found" in check.detail


def test_too_short_quote_is_rejected():
    check = verify_quote("todos", [SNAPSHOT_A])
    assert not check.ok
    assert "too short" in check.detail


def test_quote_with_no_snapshots_is_rejected():
    check = verify_quote("1 item left", [])
    assert not check.ok


def test_reordered_quote_still_matches_on_tokens():
    # Copied across lines in a different order: tokens are all present, so the
    # fuzzy pass accepts it rather than punishing formatting.
    check = verify_quote("Buy milk checkbox Toggle Todo", [SNAPSHOT_A])
    assert check.ok
    assert "partial" in check.detail


def test_normalise_strips_accessibility_noise():
    assert "[ref" not in normalise(SNAPSHOT_A)
    assert normalise("  A   B  ") == "a b"


def test_fingerprint_ignores_whitespace_and_refs():
    assert snapshot_fingerprint(SNAPSHOT_A) == snapshot_fingerprint(SNAPSHOT_A.replace("\n", " "))
    assert snapshot_fingerprint(SNAPSHOT_A) != snapshot_fingerprint(SNAPSHOT_B)


# ---------------------------------------------------------------------------
# Loop detection
# ---------------------------------------------------------------------------

def test_repeated_action_nudges_then_aborts():
    # Snapshots vary so stagnation detection stays out of the way: this test is
    # about repetition only.
    guard = LoopGuard(repeat_nudge=3, repeat_abort=5)
    decisions = [
        guard.record("browser_click", {"target": "e12"}, f"{SNAPSHOT_A}\n- tick {i}")
        for i in range(5)
    ]
    assert decisions[1].action == "continue"
    assert decisions[2].action == "nudge"      # 3rd identical call
    assert decisions[3].action == "continue"   # already nudged, not yet fatal
    assert decisions[4].action == "abort"      # 5th identical call


def test_nudge_fires_once_not_every_step():
    guard = LoopGuard(repeat_nudge=3, repeat_abort=9)
    nudges = [
        guard.record("browser_click", {"target": "e1"}, f"{SNAPSHOT_A}\n- tick {i}").action
        for i in range(6)
    ]
    assert nudges.count("nudge") == 1


def test_different_arguments_reset_the_repeat_counter():
    guard = LoopGuard(repeat_nudge=2, repeat_abort=3)
    guard.record("browser_click", {"target": "e1"}, SNAPSHOT_A)
    decision = guard.record("browser_click", {"target": "e2"}, SNAPSHOT_B)
    assert decision.action == "continue"


def test_stagnation_nudges_then_aborts():
    guard = LoopGuard(stagnation_nudge=4, stagnation_abort=8, repeat_abort=99, repeat_nudge=99)
    decisions = [
        guard.record("browser_snapshot", {"n": i}, SNAPSHOT_A) for i in range(8)
    ]
    assert decisions[3].action == "nudge"
    assert decisions[7].action == "abort"


def test_changing_page_resets_stagnation():
    guard = LoopGuard(stagnation_nudge=2, stagnation_abort=5)
    guard.record("browser_click", {"t": "e1"}, SNAPSHOT_A)
    guard.record("browser_click", {"t": "e2"}, SNAPSHOT_A)
    decision = guard.record("browser_click", {"t": "e3"}, SNAPSHOT_B)
    assert decision.action == "continue"


def test_guard_reset_clears_state():
    guard = LoopGuard(repeat_nudge=2, repeat_abort=3)
    guard.record("browser_click", {"target": "e1"}, SNAPSHOT_A)
    guard.record("browser_click", {"target": "e1"}, SNAPSHOT_A)
    guard.reset()
    assert guard.record("browser_click", {"target": "e1"}, SNAPSHOT_A).action == "continue"
