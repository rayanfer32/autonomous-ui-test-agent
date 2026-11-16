# Test Scenarios

## Source ticket

**Mock GitHub issue:** `rayan-fernandes/todomvc-qa-demo#42`
**Title:** [Feature] Persist todos across reloads and keep the counter honest
**Column:** Ready for QA (RFQA) · **Labels:** `RFQA`, `feature`, `frontend`
**Fixture:** [`fixtures/issue_rfqa.json`](fixtures/issue_rfqa.json)
**Application under test:** <https://demo.playwright.dev/todomvc>

The issue is a mock, but it is shaped like a real one: a user story, context on
what is changing (state moves from memory to browser storage), an explicit scope
boundary, and acceptance criteria written as checkbox bullets — the format the
agent's parser actually reads. Nothing about the ACs is hardcoded in the test
code; `src/issue_loader.py` extracts them from the issue body, and the planner
maps them to scenarios at runtime.

## Acceptance criteria

| ID | Criterion |
|----|-----------|
| **AC1** | On a first load with empty storage, the todo list is empty and the footer (item counter and filter links) is not visible. |
| **AC2** | Typing a todo into the "What needs to be done?" input and pressing Enter adds it to the list, and the footer shows exactly "1 item left". |
| **AC3** | Adding a second todo updates the counter to "2 items left"; marking one of them complete brings the counter back to "1 item left". |
| **AC4** | After a full page reload, the todos are still listed and a completed todo is still shown as completed. |
| **AC5** | Clicking "Clear completed" removes the completed todos and leaves the remaining count correct. |

## Scenarios

The agent generates these from the ACs at runtime (`plan.json`); this is the plan
a good run produces. Steps are user intentions, not selectors — the agent decides
*how* to do each one from the live page.

| Scenario | Title | ACs | Why grouped |
|---|---|---|---|
| **S1** | Empty state and the first todo | AC1, AC2 | Both are about the app's initial state; AC2's counter assertion only makes sense if AC1's empty state was established first |
| **S2** | Counter accuracy and completion | AC3 | Needs two items in the list, so it depends on the add flow being sound |
| **S3** | Persistence across reload and cleanup | AC4, AC5 | Both operate on the same two-item state; a reload is the natural midpoint between persisting and clearing |

---

### S1 — Empty state and the first todo

**Covers:** AC1, AC2
**Preconditions:** Browser navigated to the app; `localStorage` and
`sessionStorage` cleared by the harness.

| # | Step (intent) | Expected observation |
|---|---|---|
| 1 | Open the app | Heading "todos" and the "What needs to be done?" textbox are present |
| 2 | Confirm the list is empty | No list items in the accessibility tree |
| 3 | Confirm the footer is hidden | No "item left" counter, no All/Active/Completed filter links |
| 4 | Type `Buy milk` into the input and press Enter | `Buy milk` appears as a list item |
| 5 | Read the footer | Counter reads exactly `1 item left` (singular, not "items") |

**Evidence the agent must cite:** the literal string `1 item left` from a
snapshot, and the item text `Buy milk`.
**Typical failure signals:** counter says "1 items left" (pluralisation bug),
footer visible with zero items, or Enter does not submit the form.

---

### S2 — Counter accuracy and completion

**Covers:** AC3
**Preconditions:** App loaded; state created within the scenario.

| # | Step (intent) | Expected observation |
|---|---|---|
| 1 | Add `Buy milk`, then add `Walk dog` | Two list items visible |
| 2 | Read the counter | Exactly `2 items left` (plural) |
| 3 | Mark the first todo complete | That item's checkbox is checked; the label is struck through |
| 4 | Read the counter | Back to exactly `1 item left` |

**Evidence the agent must cite:** `2 items left` after step 2, and `1 item left`
after step 4.
**Typical failure signals:** counter not decrementing on completion, completion
toggle not rendering, or the counter showing the total rather than the remaining.

---

### S3 — Persistence across reload and cleanup

**Covers:** AC4, AC5
**Preconditions:** Two todos exist and one is completed (created within the
scenario, not inherited).

| # | Step (intent) | Expected observation |
|---|---|---|
| 1 | Add two todos and complete one | Two items, one completed |
| 2 | Reload the page | Both items are still listed |
| 3 | Inspect the previously completed item | Still shown as completed |
| 4 | Read the counter after reload | Still `1 item left` |
| 5 | Click `Clear completed` | Only the open todo remains |
| 6 | Read the counter | Still `1 item left`, and the cleared item is gone |

**Evidence the agent must cite:** the completed item still present after the
reload, and the cleared item absent after `Clear completed`.
**Typical failure signals:** list empties on reload (state still in memory),
completion flag lost while items persist, or `Clear completed` removing open
items too.

---

## Coverage matrix

| AC | S1 | S2 | S3 |
|----|:--:|:--:|:--:|
| AC1 — empty state, footer hidden | ● | | |
| AC2 — first todo, counter = "1 item left" | ● | | |
| AC3 — two items, completion decrements counter | | ● | |
| AC4 — survives reload, completion preserved | | | ● |
| AC5 — Clear completed, count correct | | | ● |

Every criterion is covered by at least one scenario; no scenario is orphaned.

## Out of scope

Taken from the ticket, and deliberately not tested:

* Editing a todo (double-click to edit, Escape to cancel, Enter to save)
* Drag-and-drop reordering
* The All / Active / Completed filter links as a feature (their *presence* is
  asserted in AC1; their behaviour is not)
* The toggle-all checkbox
* Cross-browser behaviour (the run uses one browser)
* Accessibility conformance beyond "the accessibility tree exposes the control"

Keeping the flow to three scenarios and a handful of state transitions is a
deliberate choice: a bounded flow that runs reliably is more informative than a
broad one that times out.

## How state is handled between scenarios

The agent runs all scenarios in one browser session, but each scenario starts
with a navigation and a storage clear performed by the harness
(`UITestAgent._reset_app_state`). Two reasons:

1. **Determinism.** AC1 ("first load with empty storage") is only meaningful if
   the previous scenario's todos are gone.
2. **Honesty.** The reset does not do any of the work being tested — the agent
   still has to add, complete and reload by itself. It only removes state the
   agent did not create in that scenario.

This is stated in the report's notes whenever it matters, and it is the one place
where the harness touches the application directly.

## Running these scenarios

```bash
python run_agent.py --dry-run     # print the generated plan, no browser
python run_agent.py --headed      # execute, with the browser visible
python run_agent.py               # execute headless
```

Results land in `evidence/run_<timestamp>/report.md`, with the per-criterion
verdict, the evidence the agent cited for each, and the full step log.
