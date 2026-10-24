"""Environment-driven settings and safety limits. Credentials are read here and nowhere else."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_MODEL = "claude-sonnet-5-5"
# Models that accept the server-side refusal fallback (`fallbacks: "default"`).
FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None
    if value < 1:
        raise ValueError(f"{name} must be >= 1")
    return value


@dataclass
class Limits:
    max_agent_steps: int = 6
    max_tool_calls: int = 10
    max_llm_calls: int = 7
    max_total_tokens: int = 60000
    max_runtime_seconds: int = 120
    max_files: int = 15
    max_file_chars: int = 16000
    max_diff_chars: int = 30000
    max_identical_tool_requests: int = 1
    max_bad_tool_calls: int = 3
    max_findings: int = 8

    @classmethod
    def from_env(cls) -> "Limits":
        d = cls()
        return cls(**{name: _int(name.upper(), getattr(d, name)) for name in d.__dataclass_fields__})


@dataclass
class Settings:
    model: str = DEFAULT_MODEL
    effort: str = "medium"
    use_fallbacks: bool = True
    bot_login: str | None = None  # expected author of our PR comment; resolved at runtime if unset
    limits: Limits = field(default_factory=Limits)

    @classmethod
    def from_env(cls) -> "Settings":
        model = os.environ.get("ANTHROPIC_MODEL", "").strip() or DEFAULT_MODEL
        effort = os.environ.get("ANTHROPIC_EFFORT", "").strip() or "medium"
        if effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError("ANTHROPIC_EFFORT must be one of low, medium, high, xhigh, max")
        fallbacks_env = os.environ.get("ANTHROPIC_FALLBACKS", "on").strip().lower()
        return cls(
            model=model,
            effort=effort,
            use_fallbacks=fallbacks_env not in {"off", "0", "false"} and model in FALLBACK_MODELS,
            bot_login=os.environ.get("PR_SENTINEL_BOT_LOGIN", "").strip() or None,
            limits=Limits.from_env(),
        )


def github_token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or None


def has_anthropic_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
