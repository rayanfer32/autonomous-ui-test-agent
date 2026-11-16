"""Tests for the report artefacts."""

from pathlib import Path

from src.evidence import RunRecorder
from src.models import ACResult, AcceptanceCriterion, RunReport, ScenarioResult, Status
from src.report import render_markdown, write_reports


def make_report(overall: Status = Status.PASS) -> RunReport:
    return RunReport(
        issue_ref="owner/repo#42",
        issue_title="Persist todos",
        issue_url="https://github.com/owner/repo/issues/42",
        target_url="https://demo.playwright.dev/todomvc",
        provider="openrouter",
        model="test/model",
        started_at="2026-11-16T10:00:00",
        finished_at="2026-11-16T10:01:00",
        duration_s=60.0,
        overall_status=overall,
        acceptance_criteria=[
            AcceptanceCriterion("AC1", "adding a todo shows it"),
            AcceptanceCriterion("AC2", "the counter is accurate"),
        ],
        scenarios=[
            ScenarioResult(
                scenario_id="S1",
                title="Add a todo",
                status=Status.PASS if overall is Status.PASS else Status.FAIL,
                reason="Checked the flow.",
                stop_reason="verdict",
                duration_s=12.5,
                ac_results=[
                    ACResult("AC1", Status.PASS, "Buy milk", "the item is listed",
                             snapshot_index=0, verified=True),
                    ACResult("AC2", overall, "1 item left", "counter matches",
                             snapshot_index=1, verified=True),
                ],
            )
        ],
        notes=["Blocked scenarios: none"],
    )


def test_markdown_contains_the_verdict_and_every_criterion(tmp_path):
    recorder = RunRecorder(tmp_path, run_name="run_test")
    markdown = render_markdown(make_report(), recorder)

    assert "# Test Execution Report" in markdown
    assert "**Overall result: PASS**" in markdown
    assert "AC1" in markdown and "AC2" in markdown
    assert "1 item left" in markdown
    assert "agent_trace.log" in markdown  # evidence index


def test_markdown_flags_a_failed_criterion(tmp_path):
    recorder = RunRecorder(tmp_path, run_name="run_test")
    markdown = render_markdown(make_report(Status.FAIL), recorder)
    assert "**Overall result: FAIL**" in markdown
    assert "| **FAIL** |" in markdown


def test_unsettled_criterion_is_visible(tmp_path):
    report = make_report()
    report.scenarios[0].ac_results = report.scenarios[0].ac_results[:1]
    recorder = RunRecorder(tmp_path, run_name="run_test")
    assert "NOT SETTLED" in render_markdown(report, recorder)


def test_write_reports_creates_both_files(tmp_path):
    recorder = RunRecorder(tmp_path, run_name="run_test")
    json_path, md_path = write_reports(make_report(), recorder)

    assert Path(json_path).exists() and Path(md_path).exists()
    assert '"overall_status": "PASS"' in Path(json_path).read_text()
    assert "Test Execution Report" in Path(md_path).read_text()


def test_pipe_characters_do_not_break_tables(tmp_path):
    report = make_report()
    report.acceptance_criteria[0] = AcceptanceCriterion("AC1", "a | b in the text")
    recorder = RunRecorder(tmp_path, run_name="run_test")
    assert "a \\| b" in render_markdown(report, recorder)
