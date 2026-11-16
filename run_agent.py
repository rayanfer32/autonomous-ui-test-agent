#!/usr/bin/env python3
"""Command-line entry point.

    python run_agent.py                                  # fixture issue, TodoMVC
    python run_agent.py --dry-run                        # plan only, no browser
    python run_agent.py --repo owner/name --number 42    # live GitHub issue
    python run_agent.py --headed                         # watch it work

Exit codes: 0 = all criteria passed, 1 = something failed or was blocked,
2 = configuration/setup problem (nothing was tested).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from src.agent import UITestAgent
from src.config import ConfigError, Settings
from src.evidence import RunRecorder
from src.issue_loader import load_issue_from_fixture, load_issue_from_github
from src.llm import LLMClient
from src.models import Issue, Status
from src.planner import build_plan, deterministic_plan
from src.report import write_reports

DEFAULT_FIXTURE = Path("fixtures/issue_rfqa.json")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Autonomous UI test agent: GitHub issue in, pass/fail out.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    source = parser.add_argument_group("what to test")
    source.add_argument(
        "--issue",
        type=Path,
        default=DEFAULT_FIXTURE,
        help=f"local issue fixture (default: {DEFAULT_FIXTURE})",
    )
    source.add_argument("--repo", help="live GitHub repo, e.g. owner/name")
    source.add_argument("--number", type=int, help="live GitHub issue number")
    source.add_argument("--url", help="application under test (overrides TARGET_URL)")

    agent = parser.add_argument_group("agent")
    agent.add_argument("--provider", choices=["openrouter", "groq"], help="LLM provider")
    agent.add_argument("--model", help="model id (overrides the provider default)")
    agent.add_argument("--max-steps", type=int, help="step budget per scenario")
    agent.add_argument("--out", type=Path, help="evidence root directory")
    agent.add_argument("--headed", action="store_true", help="show the browser window")
    agent.add_argument(
        "--dry-run",
        action="store_true",
        help="generate the test plan and stop — no browser, no execution",
    )
    agent.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore", "openai", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def load_issue(args: argparse.Namespace) -> Issue:
    if args.repo:
        if not args.number:
            raise ConfigError("--repo also needs --number")
        import os

        return load_issue_from_github(args.repo, args.number, token=os.getenv("GITHUB_TOKEN"))
    return load_issue_from_fixture(args.issue)


async def main_async(args: argparse.Namespace) -> int:
    try:
        settings = Settings.from_env(
            provider=args.provider,
            model=args.model,
            target_url=args.url,
            max_steps=args.max_steps,
            output_root=args.out,
            headless=False if args.headed else None,
        )
        settings.validate(require_api_key=not args.dry_run)
    except ConfigError as exc:
        print(f"\nConfiguration error: {exc}\n", file=sys.stderr)
        return 2

    try:
        issue = load_issue(args)
    except (ConfigError, FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"\nCould not load the issue: {exc}\n", file=sys.stderr)
        return 2

    if not issue.acceptance_criteria:
        print(
            "\nThe issue contains no acceptance criteria. Add an "
            "'## Acceptance Criteria' section with bullet points.\n",
            file=sys.stderr,
        )
        return 2

    recorder = RunRecorder(settings.output_root)
    print(f"\nIssue:    {issue.ref} — {issue.title}")
    print(f"Criteria: {', '.join(ac.id for ac in issue.acceptance_criteria)}")
    print(f"Target:   {settings.target_url}")
    print(f"Agent:    {settings.provider} / {settings.model}")
    print(f"Evidence: {recorder.run_dir}\n")

    if args.dry_run:
        if not settings.api_key:
            print("No API key set — showing the deterministic fallback plan.\n")
            plan, source = deterministic_plan(issue), "fallback"
        else:
            llm = LLMClient(settings)
            try:
                plan, source = await build_plan(issue, llm, settings.target_url)
            finally:
                await llm.aclose()
        recorder.write_json(
            "plan.json",
            {"source": source, "issue": issue.to_dict(), "scenarios": [s.to_dict() for s in plan]},
        )
        print(f"Test plan ({source}):")
        for scenario in plan:
            print(f"  {scenario.id}  {scenario.title}  [{', '.join(scenario.ac_ids)}]")
            for step in scenario.steps:
                print(f"        - {step}")
        print(f"\nPlan written to {recorder.run_dir / 'plan.json'}")
        print("Dry run: no browser was launched and nothing was executed.")
        return 0

    llm = LLMClient(settings)
    try:
        agent = UITestAgent(settings, llm, recorder, issue)
        report = await agent.run()
    finally:
        await llm.aclose()

    json_path, md_path = write_reports(report, recorder)

    print("\n" + "=" * 66)
    print(f"RESULT: {report.overall_status.value}  ({report.duration_s:.1f}s, "
          f"{llm.call_count} LLM calls)")
    print("=" * 66)
    statuses = report.ac_status_map()
    for criterion in report.acceptance_criteria:
        result = statuses.get(criterion.id)
        mark = result.status.value if result else "NOT SETTLED"
        print(f"  {criterion.id:<5} {mark:<12} {criterion.text[:70]}")
    for scenario in report.scenarios:
        if scenario.status is not Status.PASS and scenario.reason:
            print(f"\n  {scenario.scenario_id} {scenario.status.value}: {scenario.reason}")
    print(f"\nReport:   {md_path}")
    print(f"Evidence: {recorder.run_dir}\n")

    return 0 if report.overall_status is Status.PASS else 1


def main() -> None:
    args = parse_args()
    setup_logging(args.verbose)
    try:
        sys.exit(asyncio.run(main_async(args)))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
