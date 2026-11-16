"""Load the ticket the agent is testing.

Two sources, one interface:

* a local fixture (``fixtures/issue_rfqa.json``) — default, works offline and
  keeps the demo reproducible for a reviewer who does not have repo access;
* a live GitHub issue via the public REST API — no token needed for public
  repositories, an optional token raises the rate limit and works for private
  ones.

Acceptance criteria are parsed out of the issue body, not hardcoded, because
the whole premise of the exercise is that the agent reads the ticket like a
human would.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from src.models import AcceptanceCriterion, Issue

log = logging.getLogger("agent.issue")

GITHUB_API = "https://api.github.com"

# "## Acceptance Criteria", "### AC", "**Acceptance criteria:**" ...
_AC_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s*|\*\*\s*)?(acceptance criteria|acceptance criterion|"
    r"\bac\b|definition of done|success criteria)\s*:?\s*(?:\*\*)?\s*:?\s*$",
    re.IGNORECASE,
)
_ANY_HEADING = re.compile(r"^\s*#{1,6}\s+")

# "- [ ] text", "- [x] text", "* text", "1. text", "1) text"
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s*)?(?P<text>.+?)\s*$")
# A leading id like "AC1:", "AC-2 —", "[AC3]", "AC4."
_ID_PREFIX = re.compile(
    r"^\s*\[?(?P<id>AC[-_ ]?\d+)\]?\s*[:.)\-–—]?\s*(?P<text>.+)$", re.IGNORECASE
)


def _clean(text: str) -> str:
    text = text.strip()
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)  # bold
    text = re.sub(r"`(.+?)`", r"\1", text)  # inline code
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .;:-")


def extract_acceptance_criteria(body: str) -> list[AcceptanceCriterion]:
    """Pull acceptance criteria out of a markdown issue body.

    Strategy: find the AC heading and read the block under it until the next
    heading of any level. If no heading exists, fall back to "every bullet in
    the body", which is what a lot of real issues look like.

    Ids are taken from the text when the author wrote one (``AC1: ...``);
    otherwise they are generated as ``AC1..ACn`` in document order.
    """
    if not body or not body.strip():
        return []

    lines = body.splitlines()
    block: list[str] = []

    start = next((i for i, line in enumerate(lines) if _AC_HEADING.match(line)), None)
    if start is not None:
        for line in lines[start + 1 :]:
            if _ANY_HEADING.match(line):
                break
            block.append(line)
    else:
        log.warning("No acceptance-criteria heading found; falling back to all bullets.")
        block = lines

    criteria: list[AcceptanceCriterion] = []
    for line in block:
        match = _BULLET.match(line)
        if not match:
            continue
        raw = _clean(match.group("text"))
        if not raw or raw.lower() in {"none", "n/a", "tbd"}:
            continue

        ac_id: str | None = None
        id_match = _ID_PREFIX.match(raw)
        if id_match:
            ac_id = re.sub(r"[^A-Za-z0-9]", "", id_match.group("id")).upper()
            raw = _clean(id_match.group("text"))

        if not raw:
            continue
        criteria.append(AcceptanceCriterion(id=ac_id or "", text=raw))

    # Fill in generated ids, and de-duplicate any the author repeated.
    used: set[str] = set()
    for index, criterion in enumerate(criteria, start=1):
        ac_id = criterion.id or f"AC{index}"
        if ac_id in used:
            ac_id = f"{ac_id}_{index}"
        criterion.id = ac_id
        used.add(ac_id)

    return criteria


def load_issue_from_fixture(path: str | Path) -> Issue:
    """Read a mock issue JSON file."""
    fixture_path = Path(path)
    if not fixture_path.exists():
        raise FileNotFoundError(
            f"Issue fixture not found: {fixture_path}. "
            f"Expected something like fixtures/issue_rfqa.json."
        )
    data: dict[str, Any] = json.loads(fixture_path.read_text(encoding="utf-8"))

    body = data.get("body", "")
    # An explicit list in the fixture wins over parsing, so a fixture can be
    # unambiguous when the author wants it to be.
    explicit = data.get("acceptance_criteria") or []
    if explicit:
        criteria = [
            AcceptanceCriterion(id=str(item.get("id")), text=str(item.get("text")))
            for item in explicit
        ]
    else:
        criteria = extract_acceptance_criteria(body)

    return Issue(
        title=data.get("title", "Untitled issue"),
        body=body,
        number=data.get("number"),
        repo=data.get("repo", ""),
        url=data.get("url", ""),
        labels=list(data.get("labels", [])),
        column=data.get("column", data.get("project_column", "")),
        source=f"fixture:{fixture_path.name}",
        acceptance_criteria=criteria,
    )


def load_issue_from_github(
    repo: str, number: int, token: str | None = None, timeout: float = 30.0
) -> Issue:
    """Fetch a real issue through the GitHub REST API."""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ValueError(f"repo must look like 'owner/name', got {repo!r}")

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "autonomous-ui-test-agent",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{GITHUB_API}/repos/{repo}/issues/{number}"
    log.info("Fetching issue %s#%s", repo, number)
    response = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
    if response.status_code == 404:
        raise RuntimeError(
            f"Issue {repo}#{number} not found. It may be private — set GITHUB_TOKEN."
        )
    response.raise_for_status()
    data = response.json()

    body = data.get("body") or ""
    return Issue(
        title=data.get("title", ""),
        body=body,
        number=data.get("number", number),
        repo=repo,
        url=data.get("html_url", ""),
        labels=[label.get("name", "") for label in data.get("labels", [])],
        column="",
        source="github",
        acceptance_criteria=extract_acceptance_criteria(body),
    )
