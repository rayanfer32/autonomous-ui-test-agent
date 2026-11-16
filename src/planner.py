"""Turn an issue into a test plan.

The LLM proposes scenarios; Python validates them. That split matters: the plan
is where a weak free-tier model is most likely to drift (invent requirements,
drop an AC, emit prose instead of JSON), so the validation is strict and there
is a deterministic fallback that still produces a runnable plan if the model
fails entirely.

The fallback is intentionally dumb — chunk the ACs into pairs, one scenario
each. It is worse than a good LLM plan and better than no plan at all.
"""

from __future__ import annotations

import logging
from typing import Any

from src.llm import LLMClient, LLMError
from src.models import AcceptanceCriterion, Issue, Scenario
from src.prompts import PLANNER_SYSTEM_PROMPT, planner_user_prompt

log = logging.getLogger("agent.planner")

MAX_SCENARIOS = 5


def deterministic_plan(issue: Issue, chunk_size: int = 2) -> list[Scenario]:
    """A plan that needs no model at all.

    One scenario per pair of acceptance criteria, executed in document order.
    Used when the model is unavailable or returns something unusable, so a run
    always has a plan to execute.
    """
    criteria = issue.acceptance_criteria
    scenarios: list[Scenario] = []
    for index in range(0, len(criteria), chunk_size):
        chunk = criteria[index : index + chunk_size]
        number = len(scenarios) + 1
        scenarios.append(
            Scenario(
                id=f"S{number}",
                title=f"Verify {', '.join(ac.id for ac in chunk)}",
                ac_ids=[ac.id for ac in chunk],
                steps=[f"Verify that: {ac.text}" for ac in chunk],
                rationale="Deterministic fallback plan (LLM planning unavailable).",
            )
        )
    return scenarios


def _coerce_scenarios(payload: dict[str, Any], issue: Issue) -> list[Scenario]:
    """Validate the model's plan and repair what can be repaired."""
    known = {ac.id for ac in issue.acceptance_criteria}
    raw = payload.get("scenarios")
    if not isinstance(raw, list) or not raw:
        raise ValueError("plan JSON has no 'scenarios' array")

    scenarios: list[Scenario] = []
    for index, item in enumerate(raw[:MAX_SCENARIOS], start=1):
        if not isinstance(item, dict):
            continue

        ac_ids = [str(a) for a in item.get("ac_ids", []) if str(a) in known]
        if not ac_ids:
            log.warning("Dropping plan scenario %s: no valid AC ids", item.get("id"))
            continue

        steps = item.get("steps") or []
        if isinstance(steps, str):
            steps = [steps]
        steps = [str(s) for s in steps if str(s).strip()]
        if not steps:
            steps = [f"Verify {ac_id}" for ac_id in ac_ids]

        scenarios.append(
            Scenario(
                id=str(item.get("id") or f"S{index}"),
                title=str(item.get("title") or f"Scenario {index}"),
                ac_ids=ac_ids,
                steps=steps,
                rationale=str(item.get("rationale") or ""),
            )
        )

    if not scenarios:
        raise ValueError("no usable scenarios in plan JSON")

    # Any AC the model forgot gets its own scenario rather than silently
    # dropping out of the run.
    covered = {ac_id for scenario in scenarios for ac_id in scenario.ac_ids}
    for criterion in issue.acceptance_criteria:
        if criterion.id not in covered:
            log.warning("AC %s was not covered by the plan; adding a scenario", criterion.id)
            scenarios.append(
                Scenario(
                    id=f"S{len(scenarios) + 1}",
                    title=f"Verify {criterion.id}",
                    ac_ids=[criterion.id],
                    steps=[f"Verify that: {criterion.text}"],
                    rationale="Auto-added: the generated plan missed this criterion.",
                )
            )
    return scenarios


async def build_plan(issue: Issue, llm: LLMClient, target_url: str) -> tuple[list[Scenario], str]:
    """Ask the model for a plan; fall back deterministically if it fails.

    Returns ``(scenarios, how_it_was_produced)`` so the report can state whether
    the plan was model-generated or a fallback.
    """
    if not issue.acceptance_criteria:
        raise ValueError(
            "The issue has no acceptance criteria. Add an '## Acceptance Criteria' "
            "section with bullet points, or an explicit acceptance_criteria array "
            "in the fixture."
        )

    messages = [
        {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
        {"role": "user", "content": planner_user_prompt(issue, target_url)},
    ]

    try:
        payload = await llm.json_completion(messages, temperature=0.0)
        scenarios = _coerce_scenarios(payload, issue)
        log.info("Planned %d scenario(s) from the model", len(scenarios))
        return scenarios, "llm"
    except (LLMError, ValueError) as exc:
        log.warning("LLM planning failed (%s); using deterministic fallback", exc)
        return deterministic_plan(issue), "fallback"


def criteria_for(scenario: Scenario, issue: Issue) -> list[AcceptanceCriterion]:
    """The AC objects a scenario is responsible for."""
    by_id = {ac.id: ac for ac in issue.acceptance_criteria}
    return [by_id[ac_id] for ac_id in scenario.ac_ids if ac_id in by_id]
