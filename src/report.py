"""Report generation.

Two artefacts, one source of truth: ``report.json`` for machines and CI,
``report.md`` for a human opening the evidence folder. The markdown is written
to answer three questions in order — did it pass, which criterion failed, and
what did the agent actually see.
"""

from __future__ import annotations

from pathlib import Path

from src.evidence import RunRecorder
from src.models import RunReport, Status

BADGE = {
    Status.PASS: "PASS",
    Status.FAIL: "FAIL",
    Status.BLOCKED: "BLOCKED",
    Status.ERROR: "ERROR",
    Status.NOT_RUN: "NOT RUN",
}


def _escape(text: str) -> str:
    """Keep quotes from breaking a markdown table cell."""
    return " ".join((text or "").split()).replace("|", "\\|")


def render_markdown(report: RunReport, recorder: RunRecorder) -> str:
    lines: list[str] = []
    add = lines.append

    add("# Test Execution Report")
    add("")
    add(f"**Overall result: {BADGE[report.overall_status]}**")
    add("")
    add("| | |")
    add("|---|---|")
    add(f"| Issue | {_escape(report.issue_ref)} — {_escape(report.issue_title)} |")
    if report.issue_url:
        add(f"| Issue URL | {report.issue_url} |")
    add(f"| Application under test | {report.target_url} |")
    add(f"| Agent model | `{report.provider}` / `{report.model}` |")
    add(f"| Started | {report.started_at} |")
    add(f"| Finished | {report.finished_at} |")
    add(f"| Duration | {report.duration_s:.1f}s |")
    add(f"| Evidence folder | `{recorder.run_dir.name}/` |")
    add("")

    # --- per-AC summary -------------------------------------------------
    add("## Acceptance criteria")
    add("")
    statuses = report.ac_status_map()
    add("| AC | Criterion | Status | Evidence the agent cited |")
    add("|----|-----------|--------|--------------------------|")
    for criterion in report.acceptance_criteria:
        result = statuses.get(criterion.id)
        if result is None:
            status, evidence = "NOT SETTLED", "—"
        else:
            status = BADGE[result.status]
            evidence = _escape(result.evidence_quote) or "—"
        add(f"| {criterion.id} | {_escape(criterion.text)} | **{status}** | {evidence} |")
    add("")

    # --- scenario detail ------------------------------------------------
    add("## Scenarios")
    add("")
    for scenario in report.scenarios:
        add(f"### {scenario.scenario_id} — {_escape(scenario.title)}")
        add("")
        add(
            f"**{BADGE[scenario.status]}** · {scenario.steps_used} steps · "
            f"{scenario.duration_s:.1f}s · stopped by `{scenario.stop_reason or 'n/a'}`"
        )
        add("")
        if scenario.reason:
            add(f"> {_escape(scenario.reason)}")
            add("")
        if scenario.ac_results:
            add("| AC | Status | Reasoning | Verified |")
            add("|----|--------|-----------|----------|")
            for result in scenario.ac_results:
                verified = "yes" if result.verified else "no"
                add(
                    f"| {result.ac_id} | {BADGE[result.status]} | "
                    f"{_escape(result.reasoning) or '—'} | {verified} |"
                )
            add("")
        if scenario.steps:
            add("<details><summary>Step log</summary>")
            add("")
            add("| # | Tool | Result |")
            add("|---|------|--------|")
            for step in scenario.steps:
                mark = "" if step.ok else "**error** "
                add(
                    f"| {step.step} | `{step.tool}` | {mark}"
                    f"{_escape(step.result_excerpt)[:160]} |"
                )
            add("")
            add("</details>")
            add("")

    # --- notes ----------------------------------------------------------
    if report.notes:
        add("## Notes")
        add("")
        for note in report.notes:
            add(f"- {note}")
        add("")

    # --- evidence index -------------------------------------------------
    add("## Evidence in this folder")
    add("")
    add("| File | What it is |")
    add("|------|------------|")
    add("| `agent_trace.log` | Timestamped, human-readable log of the agent's decisions |")
    add("| `transcript.jsonl` | The same run as structured events, one JSON object per line |")
    add("| `plan.json` | The test plan the agent generated from the issue |")
    add("| `screenshots/` | Screenshots the agent captured during execution |")
    add("| `mcp_server.log` | Playwright MCP server stderr, for debugging the browser layer |")
    add("| `report.md` / `report.json` | This report |")
    add("")
    return "\n".join(lines)


def write_reports(report: RunReport, recorder: RunRecorder) -> tuple[Path, Path]:
    """Write report.json and report.md into the run folder."""
    json_path = recorder.write_json("report.json", report.to_dict())
    md_path = recorder.write_text("report.md", render_markdown(report, recorder))
    return json_path, md_path
