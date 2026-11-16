"""Typed data structures.

These are deliberately plain dataclasses with ``to_dict`` helpers: they get
serialised into the evidence folder, so the shape has to be stable and
readable by a human opening the JSON six months later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Status(str, Enum):
    """Outcome of a scenario, an acceptance criterion, or the whole run."""

    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"  # agent ran out of budget / could not produce verifiable evidence
    ERROR = "ERROR"      # infrastructure problem (LLM down, browser crashed)
    NOT_RUN = "NOT_RUN"

    @property
    def is_terminal_success(self) -> bool:
        return self is Status.PASS


@dataclass
class AcceptanceCriterion:
    """One testable statement pulled out of the GitHub issue body."""

    id: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text}


@dataclass
class Issue:
    """A GitHub issue, in the shape the agent needs."""

    title: str
    body: str
    number: int | None = None
    repo: str = ""
    url: str = ""
    labels: list[str] = field(default_factory=list)
    column: str = ""
    source: str = "fixture"
    acceptance_criteria: list[AcceptanceCriterion] = field(default_factory=list)

    @property
    def ref(self) -> str:
        if self.repo and self.number is not None:
            return f"{self.repo}#{self.number}"
        return self.title or "untitled issue"

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "body": self.body,
            "number": self.number,
            "repo": self.repo,
            "url": self.url,
            "labels": self.labels,
            "column": self.column,
            "source": self.source,
            "acceptance_criteria": [ac.to_dict() for ac in self.acceptance_criteria],
        }


@dataclass
class Scenario:
    """A functional test the agent will execute end to end."""

    id: str
    title: str
    ac_ids: list[str]
    steps: list[str] = field(default_factory=list)
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "ac_ids": self.ac_ids,
            "steps": self.steps,
            "rationale": self.rationale,
        }


@dataclass
class ACResult:
    """A per-criterion verdict, with the evidence the agent cited for it."""

    ac_id: str
    status: Status
    evidence_quote: str = ""
    reasoning: str = ""
    snapshot_index: int | None = None
    verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ac_id": self.ac_id,
            "status": self.status.value,
            "evidence_quote": self.evidence_quote,
            "reasoning": self.reasoning,
            "snapshot_index": self.snapshot_index,
            "verified": self.verified,
        }


@dataclass
class StepRecord:
    """One tool call the agent made."""

    step: int
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    result_excerpt: str = ""
    screenshot: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "tool": self.tool,
            "arguments": self.arguments,
            "ok": self.ok,
            "result_excerpt": self.result_excerpt,
            "screenshot": self.screenshot,
        }


@dataclass
class ScenarioResult:
    """Everything that happened while running one scenario."""

    scenario_id: str
    title: str
    status: Status = Status.NOT_RUN
    ac_results: list[ACResult] = field(default_factory=list)
    steps: list[StepRecord] = field(default_factory=list)
    reason: str = ""
    stop_reason: str = ""
    duration_s: float = 0.0

    @property
    def steps_used(self) -> int:
        return len(self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "title": self.title,
            "status": self.status.value,
            "reason": self.reason,
            "stop_reason": self.stop_reason,
            "steps_used": self.steps_used,
            "duration_s": round(self.duration_s, 2),
            "ac_results": [r.to_dict() for r in self.ac_results],
            "steps": [s.to_dict() for s in self.steps],
        }


@dataclass
class RunReport:
    """The final artefact: what was tested, against what, and how it went."""

    issue_ref: str
    issue_title: str
    issue_url: str
    target_url: str
    provider: str
    model: str
    started_at: str
    finished_at: str = ""
    duration_s: float = 0.0
    overall_status: Status = Status.NOT_RUN
    scenarios: list[ScenarioResult] = field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def ac_status_map(self) -> dict[str, ACResult]:
        """Flatten per-AC verdicts across scenarios (first verdict wins)."""
        seen: dict[str, ACResult] = {}
        for scenario in self.scenarios:
            for result in scenario.ac_results:
                seen.setdefault(result.ac_id, result)
        return seen

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue": {
                "ref": self.issue_ref,
                "title": self.issue_title,
                "url": self.issue_url,
            },
            "target_url": self.target_url,
            "agent": {"provider": self.provider, "model": self.model},
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": round(self.duration_s, 2),
            "overall_status": self.overall_status.value,
            "notes": self.notes,
            "acceptance_criteria": [ac.to_dict() for ac in self.acceptance_criteria],
            "scenarios": [s.to_dict() for s in self.scenarios],
        }
