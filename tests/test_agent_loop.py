"""End-to-end tests of the execution loop, with the browser and LLM faked.

These tests exist to prove the two properties the assignment cares about most:

* a scenario can only pass when its verdict quotes something the agent really
  observed (no hallucinated passes);
* the loop always terminates, whether the model cooperates or not.

Nothing here touches the network or launches a browser.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

from src.agent import UITestAgent
from src.config import Settings
from src.evidence import RunRecorder
from src.models import AcceptanceCriterion, Issue, Status

# A realistic accessibility snapshot, of the sort Playwright MCP returns.
SNAPSHOT_ONE = """- heading "todos" [level=1] [ref=e3]
- textbox "What needs to be done?" [ref=e5]
- list [ref=e8]
- listitem:
  - checkbox "Toggle Todo" [ref=e12]
  - text "Buy milk" [ref=e13]
- contentinfo:
  - text "1 item left" [ref=e20]"""


@dataclass
class FakeOutcome:
    """Stand-in for src.browser.ToolOutcome (avoids importing the MCP SDK)."""

    text: str
    is_error: bool = False
    screenshot: str = ""
    observation: str = ""


@dataclass
class FakeBrowser:
    """Minimal browser that replays a fixed snapshot."""

    snapshot: str = SNAPSHOT_ONE
    calls: list[tuple] = field(default_factory=list)
    fail_tools: set[str] = field(default_factory=set)

    async def __aenter__(self) -> "FakeBrowser":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def openai_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {"name": "browser_click", "description": "Click", "parameters": {}},
            }
        ]

    async def call(self, name: str, arguments: dict, step: int = 0) -> FakeOutcome:
        self.calls.append((name, arguments, step))
        if name in self.fail_tools:
            return FakeOutcome("### Error\nError: element not found", is_error=True)
        return FakeOutcome(text=self.snapshot, observation=self.snapshot)


class FakeLLM:
    """Replays scripted replies, then repeats the last one."""

    def __init__(self, replies: list[dict], plan: dict | None = None) -> None:
        self.replies = replies
        self.plan = plan or {
            "scenarios": [
                {
                    "id": "S1",
                    "title": "Add a todo",
                    "ac_ids": ["AC1", "AC2"],
                    "steps": ["add a todo", "check the counter"],
                    "rationale": "both criteria describe one flow",
                }
            ]
        }
        self.index = 0
        self.call_count = 0
        self.total_tokens = 0

    async def chat(self, messages, tools=None, **kwargs) -> dict:
        self.call_count += 1
        reply = self.replies[min(self.index, len(self.replies) - 1)]
        self.index += 1
        return reply

    async def json_completion(self, messages, **kwargs) -> dict:
        return self.plan

    async def aclose(self) -> None:
        return None


def tool_reply(name: str, arguments: dict, call_id: str = "call_1") -> dict:
    return {
        "content": f"I will now {name}.",
        "tool_calls": [
            {"id": call_id, "name": name, "arguments": arguments, "parse_error": ""}
        ],
        "finish_reason": "tool_calls",
        "usage": {},
    }


def verdict_reply(ac_results: list[dict], status: str = "PASS", call_id: str = "call_v") -> dict:
    return tool_reply(
        "submit_verdict",
        {
            "scenario_id": "S1",
            "status": status,
            "summary": "Checked the todo flow.",
            "ac_results": ac_results,
        },
        call_id,
    )


def make_issue() -> Issue:
    return Issue(
        title="Persist todos",
        body="- [ ] AC1: adding a todo shows it\n- [ ] AC2: the counter is accurate",
        number=1,
        repo="owner/repo",
        acceptance_criteria=[
            AcceptanceCriterion("AC1", "adding a todo shows it"),
            AcceptanceCriterion("AC2", "the counter is accurate"),
        ],
    )


def make_agent(tmp_path: Path, llm: FakeLLM, browser: FakeBrowser, **overrides) -> UITestAgent:
    settings = Settings(
        provider="openrouter",
        model="fake/model",
        api_key="test",
        max_steps=overrides.pop("max_steps", 10),
        **overrides,
    )
    recorder = RunRecorder(tmp_path, run_name="run_test")
    return UITestAgent(
        settings, llm, recorder, make_issue(), browser_factory=lambda: browser
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_verified_verdict_passes_the_run(tmp_path):
    llm = FakeLLM(
        [
            tool_reply("browser_click", {"target": "e5"}),
            verdict_reply(
                [
                    {"ac_id": "AC1", "status": "PASS", "evidence_quote": "Buy milk",
                     "reasoning": "the item appears in the list"},
                    {"ac_id": "AC2", "status": "PASS", "evidence_quote": "1 item left",
                     "reasoning": "the footer counter matches"},
                ]
            ),
        ]
    )
    browser = FakeBrowser()
    report = asyncio.run(make_agent(tmp_path, llm, browser).run())

    assert report.overall_status is Status.PASS
    assert [s.status for s in report.scenarios] == [Status.PASS]
    assert report.scenarios[0].steps_used >= 1
    assert all(r.verified for r in report.scenarios[0].ac_results)


def test_failing_criterion_fails_the_run(tmp_path):
    llm = FakeLLM(
        [
            tool_reply("browser_click", {"target": "e5"}),
            verdict_reply(
                [
                    {"ac_id": "AC1", "status": "PASS", "evidence_quote": "Buy milk",
                     "reasoning": "present"},
                    {"ac_id": "AC2", "status": "FAIL", "evidence_quote": "1 item left",
                     "reasoning": "counter should have said 2"},
                ],
                status="FAIL",
            ),
        ]
    )
    report = asyncio.run(make_agent(tmp_path, llm, FakeBrowser()).run())

    assert report.overall_status is Status.FAIL
    assert report.scenarios[0].status is Status.FAIL
    assert report.ac_status_map()["AC2"].status is Status.FAIL


# ---------------------------------------------------------------------------
# Anti-hallucination
# ---------------------------------------------------------------------------

def test_fabricated_evidence_is_rejected_then_run_blocks(tmp_path):
    fabricated = verdict_reply(
        [
            {"ac_id": "AC1", "status": "PASS", "evidence_quote": "99 items left",
             "reasoning": "made up"},
            {"ac_id": "AC2", "status": "PASS", "evidence_quote": "Buy milk",
             "reasoning": "present"},
        ]
    )
    llm = FakeLLM([tool_reply("browser_click", {"target": "e5"}), fabricated])

    recorder_dir = tmp_path
    report = asyncio.run(make_agent(recorder_dir, llm, FakeBrowser(), max_steps=8).run())

    scenario = report.scenarios[0]
    assert scenario.status is Status.BLOCKED
    assert scenario.stop_reason == "unverifiable_verdict"
    assert report.overall_status is Status.FAIL
    # The model must have been told why, in the transcript.
    transcript = (tmp_path / "run_test" / "transcript.jsonl").read_text()
    assert "verdict_rejected" in transcript
    assert "99 items left" not in [
        r.evidence_quote for r in scenario.ac_results
    ]


def test_verdict_missing_an_ac_is_rejected(tmp_path):
    llm = FakeLLM(
        [
            tool_reply("browser_click", {"target": "e5"}),
            verdict_reply(
                [
                    {"ac_id": "AC1", "status": "PASS", "evidence_quote": "Buy milk",
                     "reasoning": "present"},
                ]
            ),
            verdict_reply(
                [
                    {"ac_id": "AC1", "status": "PASS", "evidence_quote": "Buy milk",
                     "reasoning": "present"},
                    {"ac_id": "AC2", "status": "PASS", "evidence_quote": "1 item left",
                     "reasoning": "counter correct"},
                ]
            ),
        ]
    )
    report = asyncio.run(make_agent(tmp_path, llm, FakeBrowser()).run())
    assert report.scenarios[0].status is Status.PASS
    transcript = (tmp_path / "run_test" / "transcript.jsonl").read_text()
    assert "no verdict for AC2" in transcript


def test_unknown_ac_is_rejected(tmp_path):
    llm = FakeLLM(
        [
            tool_reply("browser_click", {"target": "e5"}),
            verdict_reply(
                [
                    {"ac_id": "AC9", "status": "PASS", "evidence_quote": "Buy milk",
                     "reasoning": "not an AC of this scenario"},
                ]
            ),
        ]
    )
    report = asyncio.run(make_agent(tmp_path, llm, FakeBrowser(), max_steps=4).run())
    assert report.scenarios[0].status is Status.BLOCKED
    assert "AC9" in (tmp_path / "run_test" / "transcript.jsonl").read_text()


# ---------------------------------------------------------------------------
# Termination
# ---------------------------------------------------------------------------

def test_step_budget_always_terminates(tmp_path):
    # A model that only ever clicks, with fresh arguments so repetition
    # detection does not fire first.
    replies = [tool_reply("browser_click", {"target": f"e{i}"}) for i in range(1, 40)]
    report = asyncio.run(make_agent(tmp_path, FakeLLM(replies), FakeBrowser(), max_steps=5).run())

    scenario = report.scenarios[0]
    assert scenario.status is Status.BLOCKED
    assert scenario.stop_reason == "step_budget"
    assert scenario.steps_used == 5


def test_repeated_action_aborts_early(tmp_path):
    reply = tool_reply("browser_click", {"target": "e5"})
    report = asyncio.run(make_agent(tmp_path, FakeLLM([reply]), FakeBrowser(), max_steps=20).run())

    scenario = report.scenarios[0]
    assert scenario.status is Status.BLOCKED
    assert scenario.stop_reason == "guardrail_abort"
    assert scenario.steps_used < 20


def test_text_only_reply_is_nudged_then_blocked(tmp_path):
    chatty = {
        "content": "I think the page is fine.",
        "tool_calls": [],
        "finish_reason": "stop",
        "usage": {},
    }
    report = asyncio.run(make_agent(tmp_path, FakeLLM([chatty]), FakeBrowser(), max_steps=10).run())
    scenario = report.scenarios[0]
    assert scenario.status is Status.BLOCKED
    assert scenario.stop_reason == "no_tool_calls"


# ---------------------------------------------------------------------------
# Evidence on disk
# ---------------------------------------------------------------------------

def test_evidence_folder_is_complete(tmp_path):
    llm = FakeLLM(
        [
            tool_reply("browser_click", {"target": "e5"}),
            verdict_reply(
                [
                    {"ac_id": "AC1", "status": "PASS", "evidence_quote": "Buy milk",
                     "reasoning": "present"},
                    {"ac_id": "AC2", "status": "PASS", "evidence_quote": "1 item left",
                     "reasoning": "counter correct"},
                ]
            ),
        ]
    )
    asyncio.run(make_agent(tmp_path, llm, FakeBrowser()).run())

    run_dir = tmp_path / "run_test"
    for name in ("agent_trace.log", "transcript.jsonl", "plan.json"):
        assert (run_dir / name).exists(), name
    assert "Buy milk" in (run_dir / "agent_trace.log").read_text()
    plan = json.loads((run_dir / "plan.json").read_text())
    assert plan["scenarios"][0]["id"] == "S1"
