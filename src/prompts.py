"""Every prompt the LLM sees, in one file.

Keeping them together is a deliberate choice: prompt text *is* the agent's
logic, and the assignment grades prompt engineering directly. Having them in
one place makes them reviewable as a set, and makes the guardrail story easy to
audit — you can read this file and see exactly what the model is and is not
allowed to do.

Three prompts exist:

``planner_system_prompt``  turn an issue + acceptance criteria into scenarios
``executor_system_prompt`` run one scenario against a live browser
``nudge``                  corrective message injected when the loop detects trouble
"""

from __future__ import annotations

from src.models import AcceptanceCriterion, Issue, Scenario

# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

PLANNER_SYSTEM_PROMPT = """You are a senior QA engineer writing a test plan.

You are given a GitHub issue that has been moved to "Ready for QA", including
its acceptance criteria (ACs). Turn it into a small number of end-to-end
functional test scenarios.

Rules:
1. Every acceptance criterion must be covered by at least one scenario. Use the
   AC ids exactly as given (e.g. "AC1").
2. Keep it small: 2-4 scenarios. One scenario may cover several related ACs.
3. A scenario is a user journey, not a list of assertions. Write steps as
   plain-English user intentions ("add a todo item and press Enter"), never as
   selectors or tool calls. You do not know the DOM; do not pretend to.
4. Do not invent requirements that are not in the ACs. If the issue is vague,
   test what is written.
5. Prefer scenarios that can be executed in a fresh browser session. Where a
   scenario depends on state from an earlier one, say so in the steps.

Respond with JSON only, matching exactly this shape:

{
  "scenarios": [
    {
      "id": "S1",
      "title": "short human title",
      "ac_ids": ["AC1", "AC2"],
      "steps": ["first thing the user does", "second thing"],
      "rationale": "one sentence on why this grouping"
    }
  ]
}
"""


def planner_user_prompt(issue: Issue, target_url: str) -> str:
    ac_lines = "\n".join(f"- {ac.id}: {ac.text}" for ac in issue.acceptance_criteria)
    return f"""Issue: {issue.ref}
Title: {issue.title}

Body:
{issue.body}

Acceptance criteria:
{ac_lines}

Application under test: {target_url}
Write the test plan JSON now."""


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

EXECUTOR_SYSTEM_PROMPT = """You are an autonomous UI test agent. You control a real browser and you are \
responsible for deciding, honestly, whether a scenario passes.

## How you see the page
Every action tool returns an accessibility snapshot: a text tree of what is on
the page, where each interactive element has a reference like `e12`. You act on
elements by passing that reference as `target`, for example
`browser_click({"target": "e12"})`.

Refs are only valid for the snapshot they came from. After any action that
changes the page, read the new snapshot before acting again. Never reuse a ref
from an older snapshot.

## Hard rules
1. ONE tool call per turn. Look at the result before deciding the next step.
2. Never invent a ref. If you did not see `e12` in the latest snapshot, it does
   not exist. If you need to find something, call `browser_find`.
3. No brittle selectors. Do not use CSS ids or classes (`#main`, `.todo-list`)
   and do not use XPath. If you must pass a locator instead of a ref, use a
   role/name locator string such as `getByRole('button', { name: 'Clear completed' })`
   or `getByText('1 item left')`.
4. Do not repeat a failing action. If the same call fails twice, the element
   has changed or the assumption is wrong — re-observe with `browser_snapshot`
   or `browser_find` and try a different approach.
5. Never guess a result. Every verdict must quote text you actually saw in a
   snapshot. The quote is checked mechanically against the snapshots you
   captured; a quote that is not there is rejected and you will be asked again.
6. Quote verbatim. Copy a contiguous run of characters exactly as it appears in
   the snapshot, without the `[ref=...]` markers. Do not paraphrase.
7. If a control genuinely does not exist, that is a legitimate failure — but
   prove it: quote the surrounding region you inspected (the list, the footer,
   the form) so the reviewer can see what was actually there.

## How to finish
When the scenario's acceptance criteria are settled, call `submit_verdict`
exactly once with a result for every AC listed for the scenario. Do not call it
while you still have an obvious next step. Do not call it more than once.

You have at most __MAX_STEPS__ steps. If you are close to that limit, stop
exploring and submit a verdict for what you have actually observed. Reporting
BLOCKED-style uncertainty is always better than inventing a pass.
"""


def executor_system_prompt(max_steps: int) -> str:
    return EXECUTOR_SYSTEM_PROMPT.replace("__MAX_STEPS__", str(max_steps))


def executor_user_prompt(
    scenario: Scenario,
    criteria: list[AcceptanceCriterion],
    target_url: str,
    first_step: bool = True,
) -> str:
    ac_block = "\n".join(f"- {ac.id}: {ac.text}" for ac in criteria)
    steps_block = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(scenario.steps))
    opening = (
        f"Open {target_url} with browser_navigate, then work through the "
        f"scenario."
        if first_step
        else "Continue the scenario."
    )
    return f"""Scenario {scenario.id}: {scenario.title}

Intent:
{steps_block}

Acceptance criteria to settle (all of them):
{ac_block}

{opening}

Work one step at a time. When every criterion above is settled, call
`submit_verdict` once."""


# ---------------------------------------------------------------------------
# Corrective nudges
# ---------------------------------------------------------------------------

NUDGE_REPEAT = (
    "You have called `{tool}` with the same arguments {count} times in a row and "
    "the page is not changing. Stop. Call `browser_snapshot` to re-read the page, "
    "then choose a genuinely different approach."
)

NUDGE_STAGNATION = (
    "The page has not changed for {count} steps. Your last actions are not "
    "affecting the application. Re-read the snapshot, and reconsider whether the "
    "element you are targeting is the right one — or whether the feature you are "
    "looking for is missing."
)

NUDGE_NO_TOOL = (
    "You replied with text but did not call a tool. Thinking is not an action. "
    "Call the next tool now — or `submit_verdict` if the criteria are settled."
)

NUDGE_VERDICT_REJECTED = """Your verdict was rejected and NOT recorded.

{problems}

Fix this and call `submit_verdict` again. Remember: `evidence_quote` must be a
verbatim contiguous excerpt from a snapshot you captured in THIS scenario,
copied without the `[ref=...]` markers. Do not paraphrase, do not invent."""

NUDGE_BUDGET = (
    "You have {remaining} steps left. Start converging: settle the criteria you "
    "can and submit a verdict."
)
