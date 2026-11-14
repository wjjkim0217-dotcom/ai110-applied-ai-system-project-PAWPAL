"""Model providers for the PawPal+ agent.

The agent loop is written against the Anthropic Messages format (content blocks,
tool_use, tool_result). GeminiClient lets the same loop run on Google Gemini's free
tier: it takes Messages-style requests, sends them to Gemini's OpenAI-compatible
endpoint, and returns Messages-style responses. Nothing else in the app needs to know
which provider is in use.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

logger = logging.getLogger("pawpal.agent")  # same log file as the agent

GEMINI_BASE_URL ="https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_MODELS = {"anthropic": "claude-opus-5-5", "gemini": "gemini-3.5-flash-lite"}
KEY_VARS = {"anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"), "gemini": ("GEMINI_API_KEY",)}

# finish_reason (OpenAI format) -> stop_reason (Messages format)
STOP_REASONS = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens", "content_filter": "refusal"}


class AgentError(Exception):
    """The model could not be reached or answered with an API error. The message is
    safe to show the owner."""


def _is_key(value: str | None) -> bool:
    return bool(value) and not value.startswith(("your-", "paste-"))


def provider() -> str:
    """The provider to use: PAWPAL_PROVIDER if set, otherwise gemini when only a Gemini
    key is configured, otherwise anthropic."""
    chosen = os.environ.get("PAWPAL_PROVIDER", "").strip().lower()
    if chosen in KEY_VARS:
        return chosen
    if _is_key(os.environ.get("GEMINI_API_KEY")) and not _is_key(os.environ.get("ANTHROPIC_API_KEY")):
        return "gemini"
    return "anthropic"


def default_model(name: str | None = None) -> str:
    return os.environ.get("PAWPAL_MODEL") or DEFAULT_MODELS[name or provider()]


def has_credentials(name: str | None = None) -> bool:
    """True if the chosen provider has a real (non-placeholder) key in the environment."""
    return any(_is_key(os.environ.get(var)) for var in KEY_VARS[name or provider()])


def key_hint(name: str | None = None) -> str:
    return KEY_VARS[name or provider()][0]


# --------------------------------------------------------------------------- Gemini


def _text_of(block: Any) -> str | None:
    return block.text if getattr(block, "type", None) == "text" else None


def _get(block: Any, key: str) -> Any:
    return block.get(key) if isinstance(block, dict) else getattr(block, key, None)


def to_openai_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert a Messages-format conversation to chat-completions messages."""
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        content = m["content"]
        if m["role"] == "user":
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
                continue
            texts = []
            for block in content:
                if _get(block, "type") == "tool_result":
                    out.append({"role": "tool", "tool_call_id": _get(block, "tool_use_id"),
                                "content": str(_get(block, "content"))})
                elif _get(block, "type") == "text":
                    texts.append(_get(block, "text"))
            if texts:
                out.append({"role": "user", "content": "\n".join(texts)})
        elif m["role"] == "assistant":
            blocks = content if isinstance(content, list) else [SimpleNamespace(type="text", text=content)]
            text = "\n".join(t for t in (_text_of(b) for b in blocks) if t)
            calls = []
            for b in blocks:
                if getattr(b, "type", None) != "tool_use":
                    continue
                # Send back exactly what Gemini returned (it may carry a thought signature
                # that newer models require); fall back to rebuilding the call.
                calls.append(getattr(b, "raw", None) or {
                    "id": b.id, "type": "function",
                    "function": {"name": b.name, "arguments": json.dumps(b.input)},
                })
            msg: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
    return out


def to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for t in tools:
        function = {"name": t["name"], "description": t["description"]}
        if t["input_schema"].get("properties"):  # Gemini rejects objects with no properties
            function["parameters"] = t["input_schema"]
        out.append({"type": "function", "function": function})
    return out


def from_openai_response(completion: Any) -> SimpleNamespace:
    """Convert a chat completion to a Messages-style response the agent loop understands."""
    choice = completion.choices[0]
    message = choice.message
    blocks: list[SimpleNamespace] = []
    if message.content:
        blocks.append(SimpleNamespace(type="text", text=message.content))
    for call in message.tool_calls or []:
        raw = call.model_dump(exclude_none=True) if hasattr(call, "model_dump") else dict(call)
        call_id = call.id or f"call_{uuid4().hex[:12]}"
        raw["id"] = call_id
        try:
            args = json.loads(call.function.arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError
        except ValueError:
            # Passed through so the tool reports an argument error back to the model.
            args = {"_invalid_arguments": call.function.arguments}
        blocks.append(SimpleNamespace(type="tool_use", id=call_id, name=call.function.name, input=args, raw=raw))
    if any(b.type == "tool_use" for b in blocks):
        stop_reason = "tool_use"
    else:
        stop_reason = STOP_REASONS.get(choice.finish_reason or "stop", "end_turn")
    usage = getattr(completion, "usage", None)
    return SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        ),
    )


def _error_text(error: Exception) -> str:
    """The error message plus its JSON body; the useful details (which quota, when it
    resets) are in the body."""
    return f"{error} {getattr(error, 'body', '') or ''}"


def is_daily_limit(error: Exception) -> bool:
    return "per day" in _error_text(error).lower() or "perday" in _error_text(error).lower()


class GeminiClient:
    """Exposes beta.messages.create(...) like the Anthropic client, backed by Gemini."""

    # The free tier allows only a few requests per minute and one agent run makes several,
    # so a 429 is waited out (these many seconds, then retried) before giving up.
    RATE_LIMIT_WAITS = (15, 30, 60)

    def __init__(self, api_key: str | None = None, openai_client: Any = None, sleep: Any = None) -> None:
        if openai_client is None:
            from openai import OpenAI

            openai_client = OpenAI(
                api_key=api_key or os.environ.get("GEMINI_API_KEY"),
                base_url=GEMINI_BASE_URL,
                max_retries=2,  # quick retries for transient errors; long 429 waits are below
                timeout=120,
            )
        self._openai = openai_client
        self._sleep = sleep or time.sleep
        self.rate_limit_waits: list[float] = []  # seconds waited, for logging and tests
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def _complete(self, request: dict[str, Any]) -> Any:
        import openai

        for wait in (*self.RATE_LIMIT_WAITS, None):
            try:
                return self._openai.chat.completions.create(**request)
            except openai.RateLimitError as e:
                if wait is None or is_daily_limit(e):
                    raise  # waiting a minute cannot fix a daily quota
                header = getattr(getattr(e, "response", None), "headers", {}) or {}
                try:
                    wait = max(wait, float(header.get("retry-after", 0)))
                except (TypeError, ValueError):
                    pass
                logger.warning("Gemini rate limit (429); waiting %ss before retrying", wait)
                self.rate_limit_waits.append(wait)
                self._sleep(wait)

    def create(
        self, *, model: str, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]],
        max_tokens: int = 8192, **_ignored: Any,
    ) -> SimpleNamespace:
        import openai

        try:
            completion = self._complete({
                "model": model,
                "messages": to_openai_messages(system, messages),
                "tools": to_openai_tools(tools),
                "tool_choice": "auto",
                "max_tokens": max_tokens,
            })
        except openai.RateLimitError as e:
            if is_daily_limit(e):
                retry = re.search(r"retry in ((?:\d+h)?(?:\d+m)?)", _error_text(e))
                when = f" It resets in about {retry.group(1)}." if retry and retry.group(1) else ""
                raise AgentError(
                    f"The Gemini free-tier daily limit for {model!r} is used up.{when} "
                    "Try again later, or set PAWPAL_MODEL to another Gemini model in .env."
                ) from None
            raise AgentError(
                f"The Gemini free-tier rate limit was still reached after waiting "
                f"{sum(self.RATE_LIMIT_WAITS)} seconds. Try again in a few minutes."
            ) from None
        except openai.AuthenticationError:
            raise AgentError("The Gemini API key was rejected. Check GEMINI_API_KEY in your .env file.") from None
        except openai.PermissionDeniedError:
            raise AgentError("This Gemini API key is not allowed to use the model.") from None
        except openai.NotFoundError:
            raise AgentError(f"Gemini model {model!r} was not found. Check PAWPAL_MODEL.") from None
        except openai.BadRequestError as e:
            raise AgentError(f"Gemini rejected the request: {e.message}") from None
        except openai.APIStatusError as e:
            raise AgentError(f"The Gemini API returned an error ({e.status_code}). Try again later.") from None
        except openai.APIConnectionError:
            raise AgentError("Could not reach the Gemini API. Check your internet connection.") from None
        except openai.OpenAIError as e:
            raise AgentError(f"Could not call the Gemini API: {e}") from None
        if not completion.choices:
            raise AgentError("Gemini returned an empty response. Try again.")
        return from_openai_response(completion)
