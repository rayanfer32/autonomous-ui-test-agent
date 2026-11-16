# Test Plan & Strategy

**System under test:** <https://demo.playwright.dev/todomvc> — the TodoMVC React
reference app, chosen because it is public, fast, has real state (browser
storage) and a counter whose correctness can be asserted without ambiguity.
**Ticket:** `fixtures/issue_rfqa.json` — a mock GitHub issue in the
"Ready for QA" column with five acceptance criteria (see
[SCENARIOS.md](SCENARIOS.md)).
**Agent:** LLM (OpenRouter or Groq, free tier) + Playwright MCP + Chromium.

---

## 1. Architecture

### 1.1 Components

```
                      ┌──────────────────────────────────────────┐
   GitHub issue  ───► │ issue_loader    body → acceptance criteria│
   (fixture or API)   └───────────────────┬──────────────────────┘
                                          │ Issue(AC1..AC5)
                                          ▼
                      ┌──────────────────────────────────────────┐
                      │ planner   LLM proposes, Python validates  │
                      │           → Scenario[]  (plan.json)       │
                      └───────────────────┬──────────────────────┘
                                          │ one scenario at a time
                                          ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │ executor loop (src/agent.py)                                        │
   │                                                                     │
   │   messages ──► LLM ──► tool_calls ──► BrowserSession ──► Chromium    │
   │      ▲                                    │                          │
   │      │                                    ▼                          │
   │      │                          accessibility snapshot              │
   │      │                                    │                          │
   │      └──── tool result ◄──────────────────┘                         │
   │                 │                                                   │
   │                 ├──► RunRecorder   (trace log, transcript, snapshot) │
   │                 └──► LoopGuard     (repeat / stagnation)            │
   │                                                                     │
   │   submit_verdict ──► evidence verifier ──► accept | reject + nudge  │
   └─────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
                      ┌──────────────────────────────────────────┐
                      │ report   report.md + report.json          │
                      └──────────────────────────────────────────┘
```

| Module | Responsibility | Never does |
|---|---|---|
| `issue_loader` | Parse ACs out of markdown; fetch a live issue | Talk to an LLM |
| `planner` | Issue → scenarios; validate/repair the model's plan | Touch a browser |
| `prompts` | Every string the model sees | Contain logic |
| `llm` | Chat completions, retries, JSON extraction | Know about browsers |
| `browser` | MCP stdio session; MCP tools → OpenAI tool schemas | Decide what to test |
| `guardrails` | Loop detection, evidence verification | Perform I/O |
| `evidence` | The single writer of the run folder | Interpret results |
| `agent` | The loop; the only place that owns a verdict | Format reports |
| `report` | Markdown/JSON rendering | Change a verdict |

The split is deliberate: the parts that can be wrong in interesting ways
(guardrails, parsing) are pure functions with no I/O, and they are the parts
with unit tests.

### 1.2 Why Playwright MCP, and why the accessibility snapshot

The agent does **not** import Playwright. It speaks MCP over stdio to
`@playwright/mcp`, which owns the browser. Three consequences:

* **The browser is a tool boundary, not a library.** Swapping in Playwright CLI,
  a different MCP server or Computer Use is a rewrite of `src/browser.py` alone.
* **The model sees a structured page, not pixels.** Playwright MCP returns an
  accessibility tree in which every interactive element carries a reference
  (`- button "Clear completed" [ref=e31]`). The model passes `target: "e31"`.
  This is cheaper than vision, works with non-vision free models, and is
  *assertable*: an accessibility line is text that can be quoted and matched
  exactly.
* **Refs are stable within a snapshot and invalid across one.** That constraint
  is what the prompt is built around (section 4).

Vision (`--caps=vision`, coordinate clicking) is available in the server and
would be the right fallback for a canvas-based app. It is not the foundation
here because coordinates cannot be quoted as evidence.

### 1.3 The turn lifecycle

1. The loop sends the conversation plus 12 tool definitions (11 browser tools +
   `submit_verdict`).
2. The model replies with one tool call.
3. `BrowserSession.call()` runs it, times it out at `LLM_TIMEOUT`, and returns
   text. Screenshots are named by the wrapper (`screenshots/step_07.png`), not
   by the model, so the evidence folder is always complete and ordered.
4. The result is appended to the transcript and, if it is a page observation, to
   the scenario's snapshot buffer.
5. `LoopGuard` inspects the pattern of actions and page changes.
6. Repeat.

The loop ends in exactly four ways, and it always ends: a verified verdict
(PASS/FAIL), a guardrail abort (BLOCKED), step-budget exhaustion (BLOCKED), or
an LLM/browser exception (ERROR).

### 1.4 Tool allowlist

The server exposes 25 tools. The model gets 11:

`browser_navigate`, `browser_snapshot`, `browser_find`, `browser_click`,
`browser_type`, `browser_press_key`, `browser_wait_for`,
`browser_take_screenshot`, `browser_console_messages`, `browser_evaluate`,
`browser_select_option`.

Excluded on purpose: `browser_run_code_unsafe` (arbitrary JS in the page),
`browser_file_upload`, `browser_handle_dialog`, `browser_tabs`, and the network
mocking tools. None are needed for a functional test, and each is a way to
produce a result that did not really come from the UI. A shorter list also keeps
the request small, which matters when the free model has a modest context
budget.

---

## 2. Prompt engineering strategy

Prompt text is in `src/prompts.py`. The strategy rests on one principle:
**prompts shape behaviour, they do not guarantee it.** Every rule in the prompt
has a mechanical counterpart in Python, because a model that ignores the rule
must be stopped, not asked again.

### 2.1 Structure of the executor prompt

| Block | Purpose |
|---|---|
| Role and mission | "You control a real browser and you decide, honestly, whether a scenario passes." Sets the standard as judgement, not transcription. |
| How you see the page | Explains the snapshot and refs. A model that does not understand the medium cannot use it well. |
| Seven hard rules | One action per turn; never invent a ref; no CSS/XPath; do not repeat a failing action; never guess a result; quote verbatim; if a control is missing, prove it by quoting the region you inspected. |
| How to finish | `submit_verdict` exactly once, only when every AC is settled. |
| Budget | "You have at most N steps." Injected from config so the number is never stale. |

The rules are written as prohibitions with a *reason* attached, not as
preferences. "Never invent a ref. If you did not see `e12` in the latest
snapshot, it does not exist" is checkable by the model on every turn.

### 2.2 Four layers of loop control

Infinite loops in agent systems come from a model that believes it is one step
away from success. Each layer catches a different shape of that.

| Layer | Mechanism | Fires when | Effect |
|---|---|---|---|
| 1. Prompt | "Do not repeat a failing action... re-observe with `browser_snapshot`" | Always | Prevents most loops before they start |
| 2. Corrective nudge | `LoopGuard` → injected user message naming the exact problem | Same action 3× in a row, or page unchanged for 4 steps | Model is told *what* it is doing wrong, once |
| 3. Hard abort | `LoopGuard` | Same action 5×, or page unchanged for 8 steps | Scenario ends BLOCKED with a specific reason |
| 4. Budget | `MAX_STEPS` (25) + a warning three steps before the end | Always | Guarantees termination regardless of model behaviour |

Details that matter:

* **Nudges fire once per breach, not every step.** A model that receives the
  same correction five times in a row learns nothing and burns context. The
  guard tracks `_repeat_nudged` / `_stagnation_nudged` and re-arms only when the
  situation changes.
* **Nudges are specific and actionable.** Not "try something else" but "you have
  called `browser_click` with the same arguments 3 times and the page is not
  changing; call `browser_snapshot` and choose a different approach."
* **Stagnation is measured on normalised content.** `snapshot_fingerprint()`
  strips refs and whitespace before hashing, so a page that only renumbers its
  refs is still recognised as unchanged.
* **A text-only reply is a stall, not progress.** Two consecutive turns without a
  tool call ends the scenario. Models on a free tier drift into commentary;
  counting that as a step without consequence would let them talk until the
  budget runs out.
* **The budget warning is deliberately early.** At `MAX_STEPS - 3` the model is
  told how many steps remain, which converts an exploration failure into a
  partial, honestly-reported result.

### 2.3 Guardrails against hallucinating results

This is the failure mode that matters most in QA: a green tick that means
nothing. The defence is structural, not textual.

`submit_verdict` is **not a browser tool**. It is a local function. Its
`evidence_quote` for every acceptance criterion is checked against the snapshots
captured *during that scenario* before the verdict is allowed to exist.

```
verdict submitted
  ├─ every AC of the scenario covered?        no → reject
  ├─ each status PASS or FAIL?                no → reject
  ├─ quote ≥ 2 meaningful words?              no → reject
  ├─ quote found in a scenario snapshot?      no → reject
  └─ all clear → recorded, status derived from the per-AC verdicts
```

A rejection is not a crash: the model receives a message naming the exact
problem and may try again (`MAX_VERDICT_REJECTIONS = 2`). If it still cannot
support its claim, the scenario ends BLOCKED — *"the agent could not support its
verdict with evidence"* — which is the honest outcome and a far better signal to
a reviewer than a fabricated PASS.

Two more anti-hallucination rules are enforced in code:

* **The scenario-level `status` the model sends is advisory.** The recorded
  status is derived from the per-AC verdicts. A model that says `PASS` while
  reporting a failed criterion gets a FAIL and a warning in the trace.
* **Evidence cannot cross scenarios.** The snapshot buffer is cleared at the
  start of each scenario, so a quote from scenario 1 cannot validate a claim in
  scenario 3.

### 2.4 The planner prompt

Separate prompt, separate failure mode. The planner is told: cover every AC, keep
it to 2–4 scenarios, write steps as user intentions and never as selectors, do
not invent requirements. Its JSON is then validated by
`_coerce_scenarios()`, which drops unknown AC ids, rejects empty scenarios, and
**auto-adds a scenario for any AC the model forgot**. If the whole reply is
unusable, `deterministic_plan()` produces one scenario per pair of criteria — a
worse plan, but a runnable one, and the report states which path was taken.

---

## 3. Element identification

### 3.1 The rules

| Rule | Why |
|---|---|
| Target elements by `ref` from the latest snapshot | The snapshot is the model's actual view of the page; refs are derived from the accessibility tree, so they follow role and name rather than markup |
| Never reuse a ref after the page changed | Refs are per-snapshot; a stale ref silently clicks the wrong thing or fails |
| Never use CSS ids, classes or XPath | They encode implementation, not intent. A class rename is not a regression but would fail a selector-based test |
| If you must use a locator, use role/name: `getByRole('button', { name: 'Clear completed' })` | Semantically anchored, resilient to markup changes |
| To find something specific, `browser_find` before acting | Returns only matching nodes; cheaper and more reliable than scanning a large snapshot |

These are prompt rules *and* an architectural preference: the tool schemas make
refs the natural path (every interaction tool takes `target`), so the model has
to go out of its way to use a brittle selector.

### 3.2 Handling dynamic UI

* **Re-observation is free.** Every action returns a fresh snapshot, so the loop
  never needs a separate "read the page" step unless the model wants one.
* **Waiting is explicit.** `browser_wait_for` (by text, text-gone, or time) is in
  the allowlist so the model can handle async rendering instead of clicking into
  a half-rendered page.
* **Assertions are made against text, not DOM structure.** "1 item left" is
  visible in the accessibility tree whether it is a `<span>`, a `<div>` or a
  component that re-renders.
* **Failure to find is evidence, not an error.** If a control genuinely does not
  exist, the prompt requires the model to quote the region it inspected (the
  list, the footer, the form) so the reviewer can see what was actually there.
  That quote is verified like any other.

---

## 4. Assertions and verdicts

### 4.1 Why not "ask the model if it passed"

Because that is a coin flip with extra steps. The model's judgement is used for
navigation — what to click, where to look — and its *claims* are checked
mechanically. The split:

* **Model decides:** which actions to take, what the page appears to be, which
  observation is relevant to which criterion.
* **Python decides:** whether the cited observation actually exists, whether all
  criteria were addressed, whether the run passes.

### 4.2 The verdict contract

```json
{
  "scenario_id": "S2",
  "status": "PASS",
  "summary": "Adding two todos updates the counter and completing one decrements it.",
  "ac_results": [
    {
      "ac_id": "AC3",
      "status": "PASS",
      "evidence_quote": "2 items left",
      "reasoning": "The footer counter shows two items after the second todo was added."
    }
  ]
}
```

Every AC in the scenario needs an entry with a quote. Verdicts are **per
criterion, not per scenario**, because a single scenario can pass three criteria
and fail a fourth, and a reviewer needs to know which.

### 4.3 Quote matching (`verify_quote`)

Two passes, cheapest first:

1. **Verbatim substring** on normalised text (lowercased, accessibility markers
   such as `[ref=e12]` stripped, whitespace collapsed). A model that copies a
   snapshot line including the ref still matches.
2. **Token containment**, requiring 90% of the quote's meaningful tokens to be
   present in one snapshot. This forgives re-joining lines or dropping a word;
   digits count as meaningful tokens, because "1 item left" vs "2 items left" is
   exactly the distinction being tested.

Rejected outright: quotes under 8 normalised characters, and quotes with fewer
than two meaningful tokens — "todos" proves nothing.

**Known limit.** A quote that contains all the right words in the wrong order
passes pass 2 at 100%. Tightening this further (requiring adjacency) would reject
honest copies that span a line break, so the trade-off is deliberate: the
verifier is strong against invention, not against deliberate misordering.

### 4.4 Failure semantics

| Status | Meaning | Reviewer action |
|---|---|---|
| `PASS` | Every AC observed to hold, each with verified evidence | Ship |
| `FAIL` | At least one AC observed *not* to hold, with verified evidence of what was seen instead | Fix the bug |
| `BLOCKED` | The agent could not settle the criteria — guardrail abort, budget exhausted, or an unverifiable verdict | Read `agent_trace.log`; usually a vague criterion, a slow page, or a model too weak for the flow |
| `ERROR` | Infrastructure: LLM unreachable, browser crash | Re-run; check keys and rate limits |

`BLOCKED` exists precisely so that "we do not know" never gets rounded up to
PASS or down to a bug report. It is the most useful of the four statuses when
something is wrong with the test rather than the product.

### 4.5 Test isolation

Each scenario starts with a navigation to `TARGET_URL` and a
`localStorage`/`sessionStorage` clear, performed by the harness — not by the
model. This is isolation, not assistance: the agent still has to create every
piece of state it asserts on, it just does not inherit state from the previous
scenario. Without it, AC1 ("empty list on first load") would pass or fail
depending on test order, which is not a property of the product.

---

## 5. Error handling and reporting

* **LLM errors** are retried with exponential backoff, honouring `Retry-After`
  (free tiers rate-limit aggressively). After four attempts the scenario is
  ERROR with the provider's message, not a stack trace.
* **Malformed tool arguments** (invalid JSON from a small model) are logged and
  reported back to the model as a tool result so it can correct itself.
* **Tool errors** are returned as text with `is_error`, logged as ERROR in the
  trace, and never counted as observations — an error page is not evidence.
* **Browser/MCP failures** are caught per call and surfaced as a tool result;
  a dead browser ends the scenario rather than the process.
* **Every run writes `report.md`**, even when it fails, because the failure
  report is the deliverable: overall status, a per-AC table with the evidence
  cited, per-scenario reasoning, the step log, and notes on anything unsettled.

---

## 6. Constraints that shaped the design

| Constraint | Consequence |
|---|---|
| Free tier: ~20 req/min, 50 req/day (OpenRouter) | Loop kept tight (25 steps), tools pruned to 11, retries with backoff, `--dry-run` for plan-only checks |
| Free models vary in tool-calling reliability | Deterministic plan fallback; strict verdict validation; per-turn nudges |
| Small context budgets | Snapshots truncated head+tail (not head-only) to keep the footer/counter visible; tool schemas stripped of `$schema`/`additionalProperties` |
| No vision model assumed | Accessibility snapshots as the primary sense; vision is an opt-in capability, not a dependency |

---

## 7. Trade-offs and what I would do next

| Decision | Trade-off |
|---|---|
| Accessibility snapshot over vision | Blind to canvas/pixel-only UI; in exchange, evidence is exact and models stay cheap |
| Verdict verification by quote | A model can satisfy it with an irrelevant-but-real quote; mitigated by per-AC reasoning being shown in the report for human review |
| One browser, storage reset per scenario | Fast; but a scenario cannot test cross-session behaviour of a second profile |
| No self-healing retry | Deliberate: automatic retries are how real regressions get retested into a pass |
| No GitHub webhook | Deliberate scope choice; the trigger is a CLI command |

Next steps, in order of value: (1) a second verifier that re-checks a PASS by
re-querying the page after the verdict, independent of the model's quote; (2)
screenshot-diff assertions for visual criteria; (3) parallel scenarios with one
browser context each; (4) a GitHub Action that runs the agent on `RFQA` label
events and comments the report on the issue.
