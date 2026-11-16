"""Configuration.

Every knob is environment-driven so the same checkout runs against OpenRouter
(the default, free tier) or Groq (also free tier) without touching code. The
two providers are wire-compatible with the OpenAI SDK, so only the base URL,
key and model change.

Settings are collected once, validated once, and then passed around
explicitly. No module reaches into ``os.environ`` on its own.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when the environment cannot produce a usable configuration."""


# Free-tier defaults. Both are models that advertise tool calling, which the
# agent cannot work without.
PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "model": "inclusionai/ling-3.2-flash:free",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
        "model": "openai/gpt-oss-120b",
    },
    "sarvam": {
        "base_url": "https://api.sarvam.ai/v2",
        "api_key_env": "SARVAM_API_KEY",
        "model": "sarvam-105b",
    }
}


def _env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None or value.strip() == "" else value.strip()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    """Resolved configuration for a single run."""

    # --- LLM -----------------------------------------------------------
    provider: str = "openrouter"
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.0
    max_tokens: int = 2000
    request_timeout: float = 120.0

    # --- target --------------------------------------------------------
    target_url: str = "https://demo.playwright.dev/todomvc"

    # --- loop guardrails ----------------------------------------------
    max_steps: int = 25
    repeat_nudge: int = 3
    repeat_abort: int = 5
    stagnation_nudge: int = 4
    stagnation_abort: int = 8
    max_verdict_rejections: int = 2
    max_no_tool_turns: int = 2
    snapshot_char_limit: int = 6000

    # --- browser -------------------------------------------------------
    headless: bool = True
    browser: str = "chromium"
    viewport: str = "1280,720"
    mcp_command: str = "npx"
    mcp_package: str = "@playwright/mcp@latest"
    no_sandbox: bool = False
    playwright_browsers_path: str = ""

    # --- evidence ------------------------------------------------------
    output_root: Path = field(default_factory=lambda: Path("evidence"))

    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls, **overrides: Any) -> "Settings":
        """Build settings from the environment, then apply CLI overrides.

        ``overrides`` values that are ``None`` are ignored so argparse defaults
        cannot accidentally clobber the environment.
        """
        load_dotenv(override=False)

        provider = _env_str("LLM_PROVIDER", "openrouter").lower()
        defaults = PROVIDER_DEFAULTS.get(provider)
        api_key_env = defaults["api_key_env"] if defaults else ""

        settings = cls(
            provider=provider,
            model=_env_str("LLM_MODEL", defaults["model"] if defaults else ""),
            api_key=_env_str(api_key_env) if api_key_env else "",
            base_url=defaults["base_url"] if defaults else "",
            temperature=_env_float("LLM_TEMPERATURE", 0.0),
            max_tokens=_env_int("LLM_MAX_TOKENS", 2000),
            request_timeout=_env_float("LLM_TIMEOUT", 120.0),
            target_url=_env_str("TARGET_URL", "https://demo.playwright.dev/todomvc"),
            max_steps=_env_int("MAX_STEPS", 25),
            repeat_nudge=_env_int("REPEAT_NUDGE", 3),
            repeat_abort=_env_int("REPEAT_ABORT", 5),
            stagnation_nudge=_env_int("STAGNATION_NUDGE", 4),
            stagnation_abort=_env_int("STAGNATION_ABORT", 8),
            max_verdict_rejections=_env_int("MAX_VERDICT_REJECTIONS", 2),
            snapshot_char_limit=_env_int("SNAPSHOT_CHAR_LIMIT", 6000),
            headless=_env_bool("HEADLESS", True),
            browser=_env_str("BROWSER", "chromium"),
            viewport=_env_str("VIEWPORT", "1280,720"),
            no_sandbox=_env_bool("MCP_NO_SANDBOX", False),
            playwright_browsers_path=_env_str("PLAYWRIGHT_BROWSERS_PATH"),
            output_root=Path(_env_str("EVIDENCE_DIR", "evidence")),
        )

        for key, value in overrides.items():
            if value is None:
                continue
            if not hasattr(settings, key):
                raise ConfigError(f"Unknown setting {key!r}")
            setattr(settings, key, value)

        if isinstance(settings.output_root, str):
            settings.output_root = Path(settings.output_root)
        return settings

    # ------------------------------------------------------------------
    def validate(self, require_api_key: bool = True) -> None:
        """Fail fast with an actionable message instead of a 401 three steps in."""
        if self.provider not in PROVIDER_DEFAULTS:
            raise ConfigError(
                f"Unknown LLM_PROVIDER {self.provider!r}. "
                f"Choose one of: {', '.join(sorted(PROVIDER_DEFAULTS))}."
            )
        if require_api_key and not self.api_key:
            env_name = PROVIDER_DEFAULTS[self.provider]["api_key_env"]
            raise ConfigError(
                f"No API key found. Set {env_name} in your .env "
                f"(copy .env.example first). "
                f"Get a free key at https://openrouter.ai/keys or "
                f"https://console.groq.com/keys."
            )
        if not self.model:
            raise ConfigError("No model configured. Set LLM_MODEL in your .env.")
        if not self.target_url.startswith(("http://", "https://", "data:", "about:")):
            raise ConfigError(f"TARGET_URL must be an http(s) URL, got {self.target_url!r}")
        if self.max_steps < 3:
            raise ConfigError("MAX_STEPS must be at least 3.")
        if self.stagnation_abort <= self.stagnation_nudge:
            raise ConfigError("STAGNATION_ABORT must be greater than STAGNATION_NUDGE.")
        if self.repeat_abort <= self.repeat_nudge:
            raise ConfigError("REPEAT_ABORT must be greater than REPEAT_NUDGE.")

    # ------------------------------------------------------------------
    def mcp_args(self) -> list[str]:
        """Command-line arguments for the Playwright MCP server."""
        args: list[str] = []
        if Path(self.mcp_command).name in {"npx", "npx.cmd"}:
            args += ["-y", self.mcp_package]
        args += ["--browser", self.browser, "--viewport-size", self.viewport, "--isolated"]
        if self.headless:
            args.append("--headless")
        if self.no_sandbox:
            args.append("--no-sandbox")
        return args

    def child_env(self) -> dict[str, str]:
        """Environment for the MCP child process."""
        env = dict(os.environ)
        if self.playwright_browsers_path:
            env["PLAYWRIGHT_BROWSERS_PATH"] = self.playwright_browsers_path
        return env

    def describe(self) -> str:
        return (
            f"provider={self.provider} model={self.model} "
            f"browser={self.browser} headless={self.headless} "
            f"max_steps={self.max_steps}"
        )
