"""A stand-in for the Anthropic client so agent tests run offline, for free, and the
same way every time. Each scripted step is either a response or a function of the
messages sent so far that returns one (for steps that need ids from earlier results).
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any, Callable


def text(t: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=t)


def tool_use(name: str, tool_input: dict[str, Any], block_id: str | None = None) -> SimpleNamespace:
    tool_use.counter += 1
    return SimpleNamespace(type="tool_use", id=block_id or f"toolu_{tool_use.counter}", name=name, input=tool_input)


tool_use.counter = 0


def response(*blocks: SimpleNamespace, stop_reason: str | None = None) -> SimpleNamespace:
    if stop_reason is None:
        stop_reason = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
    )


Step = SimpleNamespace | Callable[[list[dict[str, Any]]], SimpleNamespace] | Exception


class FakeClient:
    def __init__(self, steps: list[Step]) -> None:
        self.steps = list(steps)
        self.calls: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append({**kwargs, "messages": copy.copy(kwargs["messages"])})
        if not self.steps:
            raise AssertionError("FakeClient ran out of scripted responses")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(kwargs["messages"]) if callable(step) else step


def tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every tool_result block sent so far, in order."""
    out = []
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            out.extend(b for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result")
    return out


def created_task_ids(messages: list[dict[str, Any]]) -> list[str]:
    ids = []
    for r in tool_results(messages):
        if not r["is_error"]:
            data = json.loads(r["content"])
            if "task_id" in data:
                ids.append(data["task_id"])
    return ids
