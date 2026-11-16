"""Autonomous UI test agent — package root.

The modules are deliberately small and single-purpose so each one can be
reasoned about (and unit-tested) on its own:

    config       environment -> Settings
    models       typed data structures passed between modules
    issue_loader GitHub issue / fixture -> Issue with acceptance criteria
    prompts      every prompt the LLM ever sees, in one place
    llm          provider-agnostic chat client (OpenRouter / Groq)
    planner      issue -> test plan (LLM, with a deterministic fallback)
    browser      Playwright MCP server -> OpenAI-style tool definitions
    guardrails   loop detection + evidence verification (the anti-hallucination layer)
    evidence     run recorder: trace log, transcript, screenshots
    agent        the execution loop that ties it all together
    report       pass/fail summary written to markdown + JSON
"""

__version__ = "1.0.0"
