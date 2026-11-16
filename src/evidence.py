"""Evidence collection.

Every run gets its own folder so two runs never overwrite each other:

    evidence/run_20261116_142530/
        agent_trace.log        human-readable "I see X, I will click it" log
        transcript.jsonl       one JSON object per event, for machines
        plan.json              the test plan the LLM generated
        screenshots/           step_01_....png
        report.json            the pass/fail summary
        report.md              the same summary, readable

The recorder is the single writer. Nothing else opens files in the evidence
folder, which keeps the on-disk story consistent and makes the trace easy to
follow during a demo.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("agent.evidence")


class RunRecorder:
    """Owns one evidence folder for the lifetime of a run."""

    def __init__(self, root: Path | str, run_name: str | None = None) -> None:
        root = Path(root)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir = root / (run_name or f"run_{stamp}")
        self.screenshots_dir = self.run_dir / "screenshots"
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

        self.trace_path = self.run_dir / "agent_trace.log"
        self.transcript_path = self.run_dir / "transcript.jsonl"
        self.trace_path.write_text("", encoding="utf-8")
        self.transcript_path.write_text("", encoding="utf-8")

        self._snapshots: list[dict[str, Any]] = []
        self._scenario_id = ""
        self._started = datetime.now()

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    def log(self, level: str, message: str, **fields: Any) -> None:
        """Write one line to the trace log and mirror it to stderr."""
        stamp = datetime.now().strftime("%H:%M:%S")
        suffix = ""
        if fields:
            rendered = " ".join(f"{k}={v}" for k, v in fields.items() if v not in (None, ""))
            suffix = f"  [{rendered}]" if rendered else ""
        line = f"{stamp} {level:<5} {message}{suffix}\n"
        with self.trace_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        log.log(getattr(logging, level.upper(), logging.INFO), message)

    def event(self, kind: str, **payload: Any) -> None:
        """Append a structured event to the JSONL transcript."""
        record = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "scenario": self._scenario_id,
            "event": kind,
            **payload,
        }
        with self.transcript_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")

    # ------------------------------------------------------------------
    # Snapshots (the evidence base for verdict verification)
    # ------------------------------------------------------------------
    def begin_scenario(self, scenario_id: str) -> None:
        """Clear the snapshot buffer so verdicts can only cite this scenario."""
        self._scenario_id = scenario_id
        self._snapshots = []
        self.event("scenario_start", scenario_id=scenario_id)

    def add_snapshot(self, text: str, step: int, tool: str) -> int:
        """Store a page observation and return its index."""
        self._snapshots.append({"step": step, "tool": tool, "text": text or ""})
        return len(self._snapshots) - 1

    @property
    def snapshots(self) -> list[str]:
        return [entry["text"] for entry in self._snapshots]

    # ------------------------------------------------------------------
    # Artefacts
    # ------------------------------------------------------------------
    def write_json(self, name: str, data: Any) -> Path:
        path = self.run_dir / name
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        return path

    def write_text(self, name: str, text: str) -> Path:
        path = self.run_dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def relative(self, path: Path | str) -> str:
        """Path relative to the run folder, for logs and reports."""
        try:
            return str(Path(path).relative_to(self.run_dir))
        except ValueError:
            return str(path)

    def summary(self) -> dict[str, Any]:
        return {
            "run_dir": str(self.run_dir),
            "duration_s": (datetime.now() - self._started).total_seconds(),
            "snapshots_captured": len(self._snapshots),
        }
