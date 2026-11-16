"""The execution loop.

This is the heart of the agent: one scenario in, one verdict out, with every
turn logged to the evidence folder.

Shape of a turn:

    LLM decides -> calls a browser tool -> wrapper returns a snapshot
                -> loop records it as evidence -> guardrail checks the pattern
                -> back to the LLM

The loop ends in exactly one of four ways, and it always ends:

    1. the model submits a verdict whose evidence checks out  -> PASS/FAIL
    2. a guardrail aborts it (repeat / stagnation)            -> BLOCKED
    3. the step budget runs out                               -> BLOCKED
    4. the LLM or browser raises                              -> ERROR

``submit_verdict`` is the only way to produce a result, and it is not a browser
tool — it is a local function whose output is verified against the snapshots
captured during that scenario. That is the structural answer to "does the agent
have guardrails against hallucinating test results".
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any, Callable

from src.config import Settings
from src.evidence import RunRecorder
from src.guardrails import LoopGuard, verify_quote
from src.llm import LLMClient, LLMError
from src.models import (
    ACResult,
    Issue,
    RunReport,
    Scenario,
    ScenarioResult,
    Status,
    StepRecord,
)
from src.planner import build_plan, criteria_for
from src.prompts import (
    NUDGE_BUDGET,
    NUDGE_NO_TOOL,
    NUDGE_VERDICT_REJECTED,
    executor_system_prompt,
    executor_user_prompt,
)

log = logging.getLogger("agent.executor")

# The one tool that is not a browser tool.
SUBMIT_VERDICT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_verdict",
        "description": (
            "Submit the final result for this scenario. Call this exactly once, "
            "when every acceptance criterion listed for the scenario has been "
            "settled. Each result must quote text you actually observed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scenario_id": {"type": "string", "description": "The scenario id, e.g. S1."},
                "status": {
                    "type": "string",
                    "enum": ["PASS", "FAIL"],
                    "description": "Your overall verdict for the scenario.",
                },
                "summary": {
                    "type": "string",
                    "description": "One or two sentences a reviewer can act on.",
                },
                "ac_results": {
                    "type": "array",
                    "description": "One entry per acceptance criterion.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ac_id": {"type": "string", "description": "e.g. AC2"},
                            "status": {"type": "string", "enum": ["PASS", "FAIL"]},
                            "evidence_quote": {
                                "type": "string",
                                "description": (
                                    "A verbatim, contiguous excerpt from a snapshot "
                                    "you captured in this scenario, copied without "
                                    "the [ref=...] markers. For a FAIL, quote what "
                                    "you actually saw instead."
                                ),
                            },
                            "reasoning": {
                                "type": "string",
                                "description": "Why this quote proves the verdict.",
                            },
                        },
                        "required": ["ac_id", "status", "evidence_quote", "reasoning"],
                    },
                },
            },
            "required": ["scenario_id", "status", "summary", "ac_results"],
        },
    },
}

# A browser factory is anything that returns an async context manager exposing
# ``openai_tools()`` and ``call()``. Injecting it keeps the loop testable
# without a browser or a network.
BrowserFactory = Callable[[], Any]


class UITestAgent:
    """Runs a plan against a live application and produces a report."""

    def __init__(
        self,
        settings: Settings,
        llm: LLMClient,
        recorder: RunRecorder,
        issue: Issue,
        browser_factory: BrowserFactory | None = None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.recorder = recorder
        self.issue = issue
        self._browser_factory = browser_factory
        self.plan: list[Scenario] = []
        self.plan_source = ""

    # ------------------------------------------------------------------
    def _make_browser(self) -> Any:
        if self._browser_factory is not None:
            return self._browser_factory()
        # Imported lazily so tests (and --dry-run) never need the MCP server.
        from src.browser import BrowserSession

        return BrowserSession(self.settings, self.recorder.run_dir)

    # ------------------------------------------------------------------
    async def run(self) -> RunReport:
        started = datetime.now()
        self.recorder.log("INFO", f"Agent starting — {self.settings.describe()}")
        self.recorder.log("INFO", f"Issue: {self.issue.ref} ({self.issue.source})")

        self.plan, self.plan_source = await build_plan(
            self.issue, self.llm, self.settings.target_url
        )
        self.recorder.write_json(
            "plan.json",
            {
                "source": self.plan_source,
                "issue": self.issue.to_dict(),
                "scenarios": [s.to_dict() for s in self.plan],
            },
        )
        for scenario in self.plan:
            self.recorder.log(
                "INFO",
                f"Plan: {scenario.id} {scenario.title} -> {', '.join(scenario.ac_ids)}",
            )

        results: list[ScenarioResult] = []
        async with self._make_browser() as browser:
            for scenario in self.plan:
                results.append(await self._run_scenario(browser, scenario))

        report = self._build_report(results, started)
        return report

    # ------------------------------------------------------------------
    async def _run_scenario(self, browser: Any, scenario: Scenario) -> ScenarioResult:
        """Execute one scenario until it produces a verdict or runs out of rope."""
        criteria = criteria_for(scenario, self.issue)
        result = ScenarioResult(scenario_id=scenario.id, title=scenario.title)
        started = time.monotonic()

        self.recorder.begin_scenario(scenario.id)
        self.recorder.log(
            "INFO", f"--- {scenario.id}: {scenario.title} ({', '.join(scenario.ac_ids)}) ---"
        )

        await self._reset_app_state(browser)

        guard = LoopGuard(
            repeat_nudge=self.settings.repeat_nudge,
            repeat_abort=self.settings.repeat_abort,
            stagnation_nudge=self.settings.stagnation_nudge,
            stagnation_abort=self.settings.stagnation_abort,
        )

        tools = list(browser.openai_tools()) + [SUBMIT_VERDICT_TOOL]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": executor_system_prompt(self.settings.max_steps)},
            {
                "role": "user",
                "content": executor_user_prompt(
                    scenario, criteria, self.settings.target_url
                ),
            },
        ]

        rejections = 0
        no_tool_turns = 0
        budget_nudged = False

        for step in range(1, self.settings.max_steps + 1):
            try:
                reply = await self.llm.chat(messages, tools=tools)
            except LLMError as exc:
                result.status = Status.ERROR
                result.reason = f"LLM failure at step {step}: {exc}"
                self.recorder.log("ERROR", result.reason)
                break

            self.recorder.event(
                "assistant",
                step=step,
                content=reply["content"],
                tool_calls=[
                    {"name": c["name"], "arguments": c["arguments"]}
                    for c in reply["tool_calls"]
                ],
                usage=reply.get("usage", {}),
            )
            if reply["content"]:
                self.recorder.log("INFO", f"[{scenario.id} step {step}] {reply['content']}")

            if not reply["tool_calls"]:
                no_tool_turns += 1
                self.recorder.log("WARN", f"[{scenario.id} step {step}] no tool call")
                if no_tool_turns > self.settings.max_no_tool_turns:
                    result.status = Status.BLOCKED
                    result.reason = "The agent stopped calling tools and produced no verdict."
                    result.stop_reason = "no_tool_calls"
                    break
                messages.append({"role": "assistant", "content": reply["content"]})
                messages.append({"role": "user", "content": NUDGE_NO_TOOL})
                continue

            no_tool_turns = 0
            messages.append(self._assistant_message(reply))
            verdict: ScenarioResult | None = None

            for call in reply["tool_calls"]:
                if call["name"] == "submit_verdict":
                    accepted, problems, verdict = self._evaluate_verdict(
                        scenario, call["arguments"], criteria
                    )
                    if accepted and verdict is not None:
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "content": "Verdict accepted and recorded.",
                            }
                        )
                        break
                    rejections += 1
                    self.recorder.log(
                        "WARN",
                        f"[{scenario.id}] verdict rejected ({rejections}): {'; '.join(problems)}",
                    )
                    self.recorder.event(
                        "verdict_rejected", problems=problems, arguments=call["arguments"]
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": NUDGE_VERDICT_REJECTED.format(
                                problems="\n".join(f"- {p}" for p in problems)
                            ),
                        }
                    )
                    if rejections > self.settings.max_verdict_rejections:
                        result.status = Status.BLOCKED
                        result.reason = (
                            "The agent could not support its verdict with evidence after "
                            f"{rejections} attempts: {'; '.join(problems)}"
                        )
                        result.stop_reason = "unverifiable_verdict"
                    continue

                outcome = await self._run_tool(browser, call, step, scenario, result)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": self._truncate(outcome.text),
                    }
                )

                decision = guard.record(call["name"], call["arguments"], outcome.observation)
                if decision.should_abort:
                    result.status = Status.BLOCKED
                    result.reason = f"Guardrail: {decision.reason}"
                    result.stop_reason = "guardrail_abort"
                    self.recorder.log("WARN", f"[{scenario.id}] {decision.reason}")
                elif decision.should_nudge:
                    messages.append({"role": "user", "content": decision.message})
                    self.recorder.log("WARN", f"[{scenario.id}] nudge: {decision.reason}")

            if verdict is not None:
                verdict.steps = result.steps  # keep the audit trail with the verdict
                result = verdict
                break
            if result.status in {Status.BLOCKED, Status.ERROR}:
                break

            remaining = self.settings.max_steps - step
            if remaining <= 3 and not budget_nudged:
                budget_nudged = True
                messages.append(
                    {"role": "user", "content": NUDGE_BUDGET.format(remaining=remaining)}
                )
        else:
            result.status = Status.BLOCKED
            result.reason = (
                f"Step budget exhausted ({self.settings.max_steps} steps) without a "
                f"verdict. The agent did not converge on the criteria."
            )
            result.stop_reason = "step_budget"

        result.duration_s = time.monotonic() - started
        self.recorder.log(
            "INFO",
            f"--- {scenario.id} result: {result.status.value} "
            f"({result.steps_used} steps, {result.duration_s:.1f}s) ---",
        )
        self.recorder.event("scenario_end", **result.to_dict())
        return result

    # ------------------------------------------------------------------
    async def _run_tool(
        self,
        browser: Any,
        call: dict[str, Any],
        step: int,
        scenario: Scenario,
        result: ScenarioResult,
    ) -> Any:
        """Execute one browser tool call and record it as evidence."""
        name = call["name"]
        arguments = call["arguments"]
        if call.get("parse_error"):
            self.recorder.log("WARN", f"Malformed arguments for {name}: {call['parse_error']}")

        outcome = await browser.call(name, arguments, step=step)

        record = StepRecord(
            step=step,
            tool=name,
            arguments=arguments,
            ok=not outcome.is_error,
            result_excerpt=outcome.text[:400],
            screenshot=outcome.screenshot,
        )
        result.steps.append(record)
        self.recorder.event("tool_call", **record.to_dict())

        status = "ERROR" if outcome.is_error else "INFO"
        self.recorder.log(
            status,
            f"[{scenario.id} step {step}] {name}({self._brief(arguments)}) -> "
            f"{self._brief_text(outcome.text)}",
        )

        if outcome.observation:
            index = self.recorder.add_snapshot(outcome.observation, step, name)
            self.recorder.event(
                "observation", step=step, tool=name, snapshot_index=index,
                chars=len(outcome.observation),
            )
        return outcome

    # ------------------------------------------------------------------
    def _evaluate_verdict(
        self,
        scenario: Scenario,
        arguments: dict[str, Any],
        criteria: list[Any],
    ) -> tuple[bool, list[str], ScenarioResult | None]:
        """Check a verdict before it is allowed to become the result.

        A verdict is accepted only if it covers every AC the scenario owns and
        every cited quote is really in a snapshot from this scenario.
        """
        problems: list[str] = []

        raw_results = arguments.get("ac_results")
        if not isinstance(raw_results, list) or not raw_results:
            return False, ["ac_results must be a non-empty array"], None

        expected = {ac.id for ac in criteria}
        ac_results: list[ACResult] = []
        seen: set[str] = set()

        for item in raw_results:
            if not isinstance(item, dict):
                problems.append("each ac_results entry must be an object")
                continue
            ac_id = str(item.get("ac_id", "")).strip()
            status_raw = str(item.get("status", "")).upper()
            quote = str(item.get("evidence_quote", "") or "")
            reasoning = str(item.get("reasoning", "") or "")

            if ac_id not in expected:
                problems.append(f"{ac_id or '(missing ac_id)'} is not an AC of {scenario.id}")
                continue
            if ac_id in seen:
                problems.append(f"{ac_id} appears twice")
                continue
            if status_raw not in {"PASS", "FAIL"}:
                problems.append(f"{ac_id} status must be PASS or FAIL, got {status_raw!r}")
                continue

            seen.add(ac_id)
            check = verify_quote(quote, self.recorder.snapshots)
            if not check.ok:
                problems.append(
                    f"{ac_id}: evidence_quote could not be verified — {check.detail}. "
                    f"Quote a contiguous excerpt you actually saw in a snapshot."
                )
                continue

            ac_results.append(
                ACResult(
                    ac_id=ac_id,
                    status=Status(status_raw),
                    evidence_quote=quote.strip(),
                    reasoning=reasoning.strip(),
                    snapshot_index=check.snapshot_index,
                    verified=True,
                )
            )

        missing = expected - seen
        if missing:
            problems.append(
                f"no verdict for {', '.join(sorted(missing))} — every AC must be settled"
            )

        if problems:
            return False, problems, None

        failed = [r.ac_id for r in ac_results if r.status is Status.FAIL]
        scenario_status = Status.FAIL if failed else Status.PASS
        claimed = str(arguments.get("status", "")).upper()
        if claimed and claimed != scenario_status.value:
            self.recorder.log(
                "WARN",
                f"{scenario.id}: model claimed {claimed} but per-AC verdicts say "
                f"{scenario_status.value}; using the per-AC verdicts",
            )

        summary = str(arguments.get("summary", "")).strip()
        result = ScenarioResult(
            scenario_id=scenario.id,
            title=scenario.title,
            status=scenario_status,
            ac_results=ac_results,
            reason=summary,
            stop_reason="verdict",
        )
        self.recorder.log(
            "INFO",
            f"{scenario.id} verdict: {scenario_status.value}"
            + (f" (failed: {', '.join(failed)})" if failed else ""),
        )
        return True, [], result

    # ------------------------------------------------------------------
    async def _reset_app_state(self, browser: Any) -> None:
        """Give each scenario a clean slate.

        Test isolation, not a shortcut: the agent still has to create every
        piece of state it asserts on, it just does not inherit state from the
        previous scenario. Storage is cleared with JS because that is a
        test-harness concern, not a UI interaction.
        """
        try:
            await browser.call(
                "browser_navigate", {"url": self.settings.target_url}, step=0
            )
            await browser.call(
                "browser_evaluate",
                {
                    "function": (
                        "() => { try { localStorage.clear(); sessionStorage.clear(); } "
                        "catch (e) {} return 'storage cleared'; }"
                    )
                },
                step=0,
            )
            await browser.call(
                "browser_navigate", {"url": self.settings.target_url}, step=0
            )
            self.recorder.log("INFO", "App state reset (navigation + storage cleared)")
        except Exception as exc:  # noqa: BLE001 - never let cleanup kill a run
            self.recorder.log("WARN", f"State reset failed (continuing anyway): {exc}")

    # ------------------------------------------------------------------
    @staticmethod
    def _assistant_message(reply: dict[str, Any]) -> dict[str, Any]:
        """Rebuild the assistant turn in OpenAI wire format."""
        return {
            "role": "assistant",
            "content": reply["content"] or None,
            "tool_calls": [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call["arguments"]),
                    },
                }
                for call in reply["tool_calls"]
            ],
        }

    def _truncate(self, text: str) -> str:
        """Keep tool results inside the context budget without losing the ends.

        A snapshot's head shows the page structure and its tail usually holds
        the item count / footer, so head+tail is a better cut than head alone.
        """
        limit = self.settings.snapshot_char_limit
        if len(text) <= limit:
            return text
        head = text[: int(limit * 0.7)]
        tail = text[-int(limit * 0.25) :]
        return f"{head}\n\n… [{len(text) - limit} characters omitted] …\n\n{tail}"

    @staticmethod
    def _brief(arguments: dict[str, Any]) -> str:
        rendered = ", ".join(f"{k}={v!r}" for k, v in (arguments or {}).items())
        return rendered[:160]

    @staticmethod
    def _brief_text(text: str) -> str:
        flat = " ".join((text or "").split())
        return flat[:180] + ("…" if len(flat) > 180 else "")

    # ------------------------------------------------------------------
    def _build_report(self, results: list[ScenarioResult], started: datetime) -> RunReport:
        finished = datetime.now()
        all_ac: dict[str, Status] = {}
        for scenario in results:
            for ac_result in scenario.ac_results:
                all_ac.setdefault(ac_result.ac_id, ac_result.status)

        expected = [ac.id for ac in self.issue.acceptance_criteria]
        notes: list[str] = []

        passed = (
            results
            and all(s.status is Status.PASS for s in results)
            and all(all_ac.get(ac_id) is Status.PASS for ac_id in expected)
        )
        overall = Status.PASS if passed else Status.FAIL

        unsettled = [ac_id for ac_id in expected if ac_id not in all_ac]
        if unsettled:
            notes.append(
                f"Not settled (no verified verdict): {', '.join(unsettled)}."
            )
        blocked = [s.scenario_id for s in results if s.status is Status.BLOCKED]
        if blocked:
            notes.append(
                f"Blocked scenarios: {', '.join(blocked)} — see each scenario's "
                f"reason in report.json."
            )
        errored = [s.scenario_id for s in results if s.status is Status.ERROR]
        if errored:
            notes.append(f"Scenarios that hit an infrastructure error: {', '.join(errored)}.")
        if self.plan_source == "fallback":
            notes.append("The test plan was produced by the deterministic fallback, not the LLM.")

        return RunReport(
            issue_ref=self.issue.ref,
            issue_title=self.issue.title,
            issue_url=self.issue.url,
            target_url=self.settings.target_url,
            provider=self.settings.provider,
            model=self.settings.model,
            started_at=started.isoformat(timespec="seconds"),
            finished_at=finished.isoformat(timespec="seconds"),
            duration_s=(finished - started).total_seconds(),
            overall_status=overall,
            scenarios=results,
            acceptance_criteria=list(self.issue.acceptance_criteria),
            notes=notes,
        )
