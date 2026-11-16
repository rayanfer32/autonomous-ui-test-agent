"""Guardrails: the parts that stop the agent lying or spinning.

Two independent problems, two independent mechanisms.

1. **Infinite loops.** A ``LoopGuard`` watches the sequence of actions and the
   page state. It nudges first (cheap, keeps the model in control) and aborts
   second (bounded, guarantees termination). Prompt-level instructions alone are
   not a guarantee — this is the belt to the prompt's braces.

2. **Hallucinated results.** ``verify_quote`` checks, mechanically, that the
   evidence a verdict cites actually appears in a snapshot the agent captured
   during that scenario. The model cannot pass a test by asserting it passed.

Both are pure functions/classes with no I/O, which is what makes them unit
testable without a browser or an API key.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable

# Snapshot attribute noise: [ref=e12], [level=1], [cursor=pointer], [active]
_SNAPSHOT_MARKER = re.compile(r"\[[a-zA-Z-]+=[^\]]*\]|\[(?:active|checked|disabled|expanded)\]")
_WHITESPACE = re.compile(r"\s+")

# Words too common to prove anything on their own.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "is", "it", "on", "for",
    "with", "as", "at", "by", "be", "that", "this", "are", "was", "were",
}


def normalise(text: str) -> str:
    """Flatten a snapshot or a quote into comparable text.

    Lowercases, strips accessibility markers, and collapses whitespace, so that
    a quote copied by hand still matches the snapshot it came from.
    """
    text = _SNAPSHOT_MARKER.sub(" ", text or "")
    text = _WHITESPACE.sub(" ", text)
    return text.strip().lower()


def _tokens(text: str) -> list[str]:
    """Meaningful words in ``text``.

    Digits count (a counter assertion often hinges on "2" vs "1"), common
    stopwords do not.
    """
    tokens: list[str] = []
    for token in re.split(r"[^a-z0-9]+", text):
        if not token:
            continue
        if token.isdigit():
            tokens.append(token)
        elif len(token) > 2 and token not in _STOPWORDS:
            tokens.append(token)
    return tokens


@dataclass
class QuoteCheck:
    """Result of verifying an evidence quote."""

    ok: bool
    snapshot_index: int | None = None
    similarity: float = 0.0
    detail: str = ""


def verify_quote(quote: str, snapshots: Iterable[str], min_similarity: float = 0.9) -> QuoteCheck:
    """Is ``quote`` genuinely present in one of the captured snapshots?

    Two passes, cheapest first:

    * exact substring match on normalised text — this is what a verbatim copy
      produces, and it is the only path that yields similarity 1.0;
    * token containment — guards against the model re-joining lines or dropping
      a word while copying. Requires ``min_similarity`` of the quote's
      meaningful tokens to appear in one single snapshot.

    A quote must be at least a few characters; one-word quotes ("todos") prove
    nothing and are rejected.
    """
    raw = (quote or "").strip()
    if len(normalise(raw)) < 8:
        return QuoteCheck(False, detail="evidence quote is too short to prove anything")

    needle = normalise(raw)
    snapshots = list(snapshots)
    if not snapshots:
        return QuoteCheck(False, detail="no snapshots were captured in this scenario")

    for index, snapshot in enumerate(snapshots):
        if needle in normalise(snapshot):
            return QuoteCheck(True, index, 1.0, "verbatim match")

    needle_tokens = _tokens(needle)
    if len(needle_tokens) < 2:
        return QuoteCheck(
            False, detail="evidence quote has too few meaningful words to prove anything"
        )

    best_index, best_score = None, 0.0
    for index, snapshot in enumerate(snapshots):
        haystack = set(_tokens(normalise(snapshot)))
        if not haystack:
            continue
        score = sum(1 for token in needle_tokens if token in haystack) / len(needle_tokens)
        if score > best_score:
            best_index, best_score = index, score

    if best_index is not None and best_score >= min_similarity:
        return QuoteCheck(
            True, best_index, best_score,
            f"partial match ({best_score:.0%} of quote tokens found)",
        )
    return QuoteCheck(
        False,
        best_index,
        best_score,
        f"quote not found in any of {len(snapshots)} snapshots "
        f"(best token match {best_score:.0%})",
    )


def snapshot_fingerprint(text: str) -> str:
    """Stable hash of the *meaningful* part of a snapshot."""
    return hashlib.sha1(normalise(text).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Loop detection
# ---------------------------------------------------------------------------

@dataclass
class GuardDecision:
    """What the loop should do after a step."""

    action: str = "continue"  # "continue" | "nudge" | "abort"
    message: str = ""
    reason: str = ""

    @property
    def should_abort(self) -> bool:
        return self.action == "abort"

    @property
    def should_nudge(self) -> bool:
        return self.action == "nudge"


@dataclass
class LoopGuard:
    """Detects repetition and stagnation, nudging before aborting.

    Two counters, both fed after every step:

    * **repetition** — the same ``(tool, arguments)`` back to back. Three in a
      row earns a nudge, five aborts.
    * **stagnation** — the page snapshot not changing across steps. Four steps
      earns a nudge, eight aborts.

    Nudges fire once per breach (not every step) so the model is corrected
    without being flooded with identical messages.
    """

    repeat_nudge: int = 3
    repeat_abort: int = 5
    stagnation_nudge: int = 4
    stagnation_abort: int = 8

    _last_signature: str = ""
    _repeat_count: int = 0
    _last_fingerprint: str = ""
    _stagnant_count: int = 0
    _repeat_nudged: bool = False
    _stagnation_nudged: bool = False
    events: list[str] = field(default_factory=list)

    def record(self, tool: str, arguments: dict, snapshot_text: str | None) -> GuardDecision:
        """Feed one step in; get back what to do next."""
        signature = f"{tool}:{sorted((arguments or {}).items())}"
        if signature == self._last_signature:
            self._repeat_count += 1
        else:
            self._last_signature = signature
            self._repeat_count = 1
            self._repeat_nudged = False

        if snapshot_text:
            fingerprint = snapshot_fingerprint(snapshot_text)
            if fingerprint == self._last_fingerprint:
                self._stagnant_count += 1
            else:
                self._last_fingerprint = fingerprint
                self._stagnant_count = 1
                self._stagnation_nudged = False

        if self._repeat_count >= self.repeat_abort:
            reason = f"same action repeated {self._repeat_count}x ({tool}) — aborting"
            self.events.append(f"abort: {reason}")
            return GuardDecision("abort", reason=reason)

        if self._stagnant_count >= self.stagnation_abort:
            reason = f"page unchanged for {self._stagnant_count} steps — aborting"
            self.events.append(f"abort: {reason}")
            return GuardDecision("abort", reason=reason)

        if self._repeat_count >= self.repeat_nudge and not self._repeat_nudged:
            self._repeat_nudged = True
            self.events.append(f"nudge: repeat {self._repeat_count}x on {tool}")
            return GuardDecision(
                "nudge",
                message=(
                    f"You have called `{tool}` with the same arguments "
                    f"{self._repeat_count} times in a row and the page is not changing. "
                    f"Stop. Call `browser_snapshot` to re-read the page, then choose a "
                    f"genuinely different approach."
                ),
                reason=f"repeat:{self._repeat_count}",
            )

        if self._stagnant_count >= self.stagnation_nudge and not self._stagnation_nudged:
            self._stagnation_nudged = True
            self.events.append(f"nudge: stagnation {self._stagnant_count}")
            return GuardDecision(
                "nudge",
                message=(
                    f"The page has not changed for {self._stagnant_count} steps. Your "
                    f"last actions are not affecting the application. Re-read the "
                    f"snapshot and reconsider whether you are targeting the right "
                    f"element — or whether the feature you are looking for is missing."
                ),
                reason=f"stagnation:{self._stagnant_count}",
            )

        return GuardDecision()

    def reset(self) -> None:
        """Called between scenarios so one scenario cannot poison the next."""
        self._last_signature = ""
        self._repeat_count = 0
        self._last_fingerprint = ""
        self._stagnant_count = 0
        self._repeat_nudged = False
        self._stagnation_nudged = False
