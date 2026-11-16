"""Browser control through the Playwright MCP server.

The agent never imports Playwright. It talks MCP over stdio to
``@playwright/mcp``, which owns the browser and exposes the accessibility
snapshot. That indirection is the point: swapping in a different MCP browser
server, or Playwright CLI, is a change to this one file.

Two things this module adds on top of the raw server:

* **An allowlist.** The server exposes ~25 tools; the model gets 11. Fewer
  tools means fewer ways to wander off, and a smaller schema in every request
  (which matters on a free tier with a small context budget).
* **Deterministic screenshot paths.** The model does not get to choose where
  screenshots land; the wrapper names them ``step_NN.png`` inside the run's
  evidence folder, so the evidence folder is always complete and ordered.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src.config import Settings

log = logging.getLogger("agent.browser")

# The tools the model is allowed to use. Deliberately excludes
# browser_run_code_unsafe (arbitrary code execution), file upload, dialogs,
# tabs and network mocking: none are needed for a functional UI test, and each
# is a way to produce a result that is not really a UI result.
ALLOWED_TOOLS: tuple[str, ...] = (
    "browser_navigate",
    "browser_snapshot",
    "browser_find",
    "browser_click",
    "browser_type",
    "browser_press_key",
    "browser_wait_for",
    "browser_take_screenshot",
    "browser_console_messages",
    "browser_evaluate",
    "browser_select_option",
)

# Tools whose output is a page observation worth keeping as evidence.
OBSERVATION_TOOLS = frozenset(
    {
        "browser_navigate",
        "browser_snapshot",
        "browser_find",
        "browser_click",
        "browser_type",
        "browser_press_key",
        "browser_wait_for",
        "browser_select_option",
        "browser_evaluate",
    }
)


class BrowserError(RuntimeError):
    """The MCP server or the browser itself failed."""


@dataclass
class ToolOutcome:
    """What a tool call produced, in the shape the agent loop needs."""

    text: str
    is_error: bool = False
    screenshot: str = ""
    observation: str = ""  # text to store as page evidence (empty if none)


def _clean_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    """Strip JSON-Schema keywords that trip up smaller models."""
    schema = dict(schema or {})
    schema.pop("$schema", None)
    schema.pop("additionalProperties", None)
    if "properties" not in schema:
        schema["properties"] = {}
    schema.setdefault("type", "object")
    return schema


class BrowserSession:
    """Async context manager wrapping one Playwright MCP server process."""

    def __init__(self, settings: Settings, run_dir: Path) -> None:
        self.settings = settings
        self.run_dir = Path(run_dir)
        self.screenshots_dir = self.run_dir / "screenshots"
        self._session: ClientSession | None = None
        self._stack: Any = None
        self._errlog: Any = None
        self._tools: list[dict[str, Any]] = []
        self._step = 0
        self._screenshot_count = 0

    # ------------------------------------------------------------------
    async def __aenter__(self) -> "BrowserSession":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        await self.close()
        return False

    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Launch the MCP server and cache its tool definitions."""
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

        params = StdioServerParameters(
            command=self.settings.mcp_command,
            args=self.settings.mcp_args(),
            env=self.settings.child_env(),
            cwd=str(self.run_dir),  # keeps screenshot writes inside evidence
        )
        log.info(
            "Starting Playwright MCP: %s %s",
            self.settings.mcp_command,
            " ".join(self.settings.mcp_args()),
        )

        self._errlog = open(self.run_dir / "mcp_server.log", "w")
        self._stack = stdio_client(params, errlog=self._errlog)
        read_stream, write_stream = await self._stack.__aenter__()

        self._session = ClientSession(read_stream, write_stream)
        await self._session.__aenter__()
        await self._session.initialize()

        available = (await self._session.list_tools()).tools
        self._tools = []
        for tool in available:
            if tool.name not in ALLOWED_TOOLS:
                continue
            self._tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": (tool.description or "").strip()[:600],
                        "parameters": _clean_schema(
                            getattr(tool, "input_schema", None)
                            or getattr(tool, "inputSchema", None)
                        ),
                    },
                }
            )

        missing = set(ALLOWED_TOOLS) - {t["function"]["name"] for t in self._tools}
        if missing:
            log.warning("MCP server does not expose: %s", ", ".join(sorted(missing)))
        log.info("Playwright MCP ready with %d tools", len(self._tools))

    # ------------------------------------------------------------------
    def openai_tools(self) -> list[dict[str, Any]]:
        """Tool definitions in OpenAI function-calling format."""
        return self._tools

    # ------------------------------------------------------------------
    async def call(self, name: str, arguments: dict[str, Any], step: int = 0) -> ToolOutcome:
        """Invoke one MCP tool and normalise the result."""
        if self._session is None:
            raise BrowserError("BrowserSession is not started")
        if name not in ALLOWED_TOOLS:
            return ToolOutcome(
                f"Tool {name!r} is not available to the agent. "
                f"Use one of: {', '.join(ALLOWED_TOOLS)}.",
                is_error=True,
            )

        self._step = step
        arguments = dict(arguments or {})

        # Evidence paths are ours, not the model's.
        if name == "browser_take_screenshot":
            self._screenshot_count += 1
            arguments["filename"] = f"screenshots/step_{step:02d}.png"
            arguments.setdefault("scale", "css")

        try:
            result = await asyncio.wait_for(
                self._session.call_tool(name, arguments),
                timeout=self.settings.request_timeout,
            )
        except asyncio.TimeoutError:
            return ToolOutcome(
                f"Tool {name!r} timed out after {self.settings.request_timeout:.0f}s.",
                is_error=True,
            )
        except Exception as exc:  # server crash, browser death, protocol error
            log.error("MCP call %s failed: %s", name, exc)
            return ToolOutcome(f"Tool {name!r} failed: {exc}", is_error=True)

        text = self._render(result)
        is_error = bool(
            getattr(result, "is_error", False) or getattr(result, "isError", False)
        )
        if text.lstrip().startswith("### Error"):
            is_error = True

        screenshot = ""
        if name == "browser_take_screenshot" and not is_error:
            screenshot = arguments["filename"]
            text = f"Screenshot captured and saved as {screenshot}."

        observation = "" if is_error else text
        return ToolOutcome(
            text=text,
            is_error=is_error,
            screenshot=screenshot,
            observation=observation if name in OBSERVATION_TOOLS else "",
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _render(result: Any) -> str:
        """Flatten an MCP CallToolResult into text."""
        parts: list[str] = []
        for block in getattr(result, "content", []) or []:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
                continue
            if getattr(block, "type", "") == "image":
                parts.append("[image content omitted — use browser_snapshot for text]")
        if not parts:
            structured = getattr(result, "structured_content", None) or getattr(
                result, "structuredContent", None
            )
            if structured:
                parts.append(str(structured))
        return "\n".join(parts).strip()

    # ------------------------------------------------------------------
    async def close(self) -> None:
        """Shut the browser and the server down, swallowing teardown noise."""
        try:
            if self._session is not None:
                await self._session.__aexit__(None, None, None)
        except Exception as exc:  # pragma: no cover - teardown races are noise
            log.debug("Ignoring MCP session teardown error: %s", exc)
        finally:
            self._session = None
        try:
            if self._stack is not None:
                await self._stack.__aexit__(None, None, None)
        except Exception as exc:  # pragma: no cover
            log.debug("Ignoring MCP transport teardown error: %s", exc)
        finally:
            self._stack = None
        if self._errlog is not None:
            self._errlog.close()
            self._errlog = None
