"""Provider selection and the Gemini adapter, tested offline with a fake OpenAI-style client."""

import json
from datetime import date, time
from types import SimpleNamespace

import openai
import pytest

import pawpal_llm
from pawpal_agent import PawPalAgent
from pawpal_llm import AgentError, GeminiClient, from_openai_response, to_openai_messages, to_openai_tools
from pawpal_system import Owner


# ------------------------------------------------------------------ provider selection


def test_defaults_to_claude_when_nothing_is_configured():
    assert pawpal_llm.provider() == "anthropic"
    assert pawpal_llm.default_model() == "claude-opus-5-5"
    assert not pawpal_llm.has_credentials()


def test_gemini_key_alone_selects_gemini(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "real-key")
    assert pawpal_llm.provider() == "gemini"
    assert pawpal_llm.default_model() == "gemini-3.5-flash-lite"
    assert pawpal_llm.has_credentials()


def test_explicit_provider_and_model_win(monkeypatch):
    monkeypatch.setenv("PAWPAL_PROVIDER", "gemini")
    monkeypatch.setenv("PAWPAL_MODEL", "gemini-3-flash-preview")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "real-key")
    assert pawpal_llm.provider() == "gemini"
    assert pawpal_llm.default_model() == "gemini-3-flash-preview"
    assert not pawpal_llm.has_credentials()  # the Gemini key is what counts now
    assert pawpal_llm.key_hint() == "GEMINI_API_KEY"


def test_env_placeholder_is_not_a_key(monkeypatch):
    monkeypatch.setenv("PAWPAL_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "paste-your-gemini-key-here")
    assert not pawpal_llm.has_credentials()


# ------------------------------------------------------------------ format conversion


def test_messages_convert_to_chat_format():
    tool_block = SimpleNamespace(type="tool_use", id="c1", name="add_pet", input={"name": "Rex", "species": "dog"})
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [SimpleNamespace(type="text", text="Adding Rex."), tool_block]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": '{"ok": true}',
                                      "is_error": False}]},
    ]

    out = to_openai_messages("SYS", messages)

    assert out[0] == {"role": "system", "content": "SYS"}
    assert out[1] == {"role": "user", "content": "hello"}
    assert out[2]["content"] == "Adding Rex."
    assert out[2]["tool_calls"][0]["function"] == {"name": "add_pet", "arguments": json.dumps(tool_block.input)}
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'}


def test_raw_tool_call_is_sent_back_unchanged():
    raw = {"id": "c1", "type": "function", "function": {"name": "build_plan", "arguments": "{}"},
           "extra_content": {"google": {"thought_signature": "abc"}}}
    block = SimpleNamespace(type="tool_use", id="c1", name="build_plan", input={}, raw=raw)

    out = to_openai_messages("SYS", [{"role": "user", "content": "x"}, {"role": "assistant", "content": [block]}])

    assert out[2]["tool_calls"] == [raw]
    assert out[2]["content"] is None


def test_tools_without_parameters_omit_the_schema():
    tools = to_openai_tools([
        {"name": "build_plan", "description": "d", "input_schema": {"type": "object", "properties": {}}},
        {"name": "add_pet", "description": "d", "input_schema": {"type": "object", "properties": {"name": {}}}},
    ])
    assert "parameters" not in tools[0]["function"]
    assert tools[1]["function"]["parameters"]["properties"] == {"name": {}}


def completion(content=None, tool_calls=(), finish_reason="stop"):
    calls = [
        SimpleNamespace(
            id=cid, function=SimpleNamespace(name=name, arguments=args),
            model_dump=lambda exclude_none=True, cid=cid, name=name, args=args: {
                "id": cid, "type": "function", "function": {"name": name, "arguments": args}},
        )
        for cid, name, args in tool_calls
    ]
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls or None),
                                 finish_reason=finish_reason)],
        usage=SimpleNamespace(prompt_tokens=50, completion_tokens=7),
    )


def test_tool_calls_become_tool_use_blocks():
    resp = from_openai_response(completion(tool_calls=[("", "add_pet", '{"name": "Rex", "species": "dog"}'),
                                                       ("c2", "build_plan", "not json")]))

    assert resp.stop_reason == "tool_use"
    first, second = resp.content
    assert first.id.startswith("call_") and first.input == {"name": "Rex", "species": "dog"}
    assert first.raw["id"] == first.id
    assert second.input == {"_invalid_arguments": "not json"}
    assert (resp.usage.input_tokens, resp.usage.output_tokens) == (50, 7)


@pytest.mark.parametrize("finish, expected", [("stop", "end_turn"), ("length", "max_tokens"),
                                              ("content_filter", "refusal")])
def test_finish_reasons_map_to_stop_reasons(finish, expected):
    assert from_openai_response(completion("hi", finish_reason=finish)).stop_reason == expected


# ------------------------------------------------------------------ the agent on Gemini


class FakeChat:
    """Stands in for openai.OpenAI(): chat.completions.create returns scripted completions."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def test_full_agent_run_on_gemini():
    fake = FakeChat([
        completion(tool_calls=[("c1", "add_pet", '{"name": "Rex", "species": "dog"}'),
                               ("c2", "add_task", json.dumps({
                                   "pet_name": "Rex", "title": "Breakfast", "duration_minutes": 10,
                                   "priority": "high", "category": "feeding", "frequency": "daily",
                                   "fixed_time": "07:30", "sources": ["dogs.md#feeding"]}))],
                   finish_reason="tool_calls"),
        completion("Added Rex's breakfast at 07:30 [dogs.md#feeding]."),
    ])
    agent = PawPalAgent(client=GeminiClient(openai_client=fake), provider_name="gemini")

    result = agent.run(Owner("Jo", time(7, 0), time(21, 0)), "My dog Rex eats breakfast at 7:30", date(2025, 1, 6))

    assert result.status == "proposed"
    assert [t.title for t in result.draft_owner.pets[0].tasks] == ["Breakfast"]
    assert fake.requests[0]["model"] == "gemini-3.5-flash-lite"
    assert fake.requests[0]["tool_choice"] == "auto"
    assert "fallbacks" not in fake.requests[0]  # Claude-only options are not sent to Gemini
    second = fake.requests[1]["messages"]
    assert [m["role"] for m in second[-3:]] == ["assistant", "tool", "tool"]


def test_gemini_errors_become_readable_agent_errors():
    request = openai._base_client.httpx.Request("POST", "https://example.test") if hasattr(
        openai._base_client, "httpx") else None
    fake = FakeChat([openai.APIConnectionError(request=request)])
    agent = PawPalAgent(client=GeminiClient(openai_client=fake), provider_name="gemini")

    result = agent.run(Owner("Jo", time(7, 0), time(21, 0)), "Add my dog Rex", date(2025, 1, 6))

    assert result.status == "error"
    assert "Could not reach the Gemini API" in result.summary


def test_create_raises_agent_error_on_empty_response():
    fake = FakeChat([SimpleNamespace(choices=[], usage=None)])
    with pytest.raises(AgentError, match="empty response"):
        GeminiClient(openai_client=fake).create(model="m", system="s", tools=[], messages=[
            {"role": "user", "content": "x"}])


def _rate_limit_error(message="Resource exhausted: requests per minute"):
    import httpx2

    response = httpx2.Response(429, request=httpx2.Request("POST", "https://example.test"))
    return openai.RateLimitError(message, response=response, body=None)


def test_rate_limit_is_waited_out_then_retried():
    waits = []
    fake = FakeChat([_rate_limit_error(), _rate_limit_error(), completion("ok")])
    client = GeminiClient(openai_client=fake, sleep=waits.append)

    resp = client.create(model="m", system="s", tools=[], messages=[{"role": "user", "content": "x"}])

    assert resp.content[0].text == "ok"
    assert waits == [15, 30]
    assert len(fake.requests) == 3


def test_rate_limit_that_never_clears_becomes_a_readable_error():
    waits = []
    fake = FakeChat([_rate_limit_error("Resource exhausted, per minute")] * 4)
    client = GeminiClient(openai_client=fake, sleep=waits.append)

    with pytest.raises(AgentError, match="still reached after waiting"):
        client.create(model="m", system="s", tools=[], messages=[{"role": "user", "content": "x"}])
    assert waits == list(GeminiClient.RATE_LIMIT_WAITS)


def test_daily_quota_fails_fast_with_the_reset_time():
    waits = []
    body = [{"error": {"code": 429, "message": "Generate content requests per day limit exceeded on Free Tier. "
                                               "Please retry in 10h56m50.08s."}}]
    error = _rate_limit_error("Error code: 429")
    error.body = body
    fake = FakeChat([error])

    with pytest.raises(AgentError) as info:
        GeminiClient(openai_client=fake, sleep=waits.append).create(
            model="gemini-x", system="s", tools=[], messages=[{"role": "user", "content": "x"}])

    assert waits == []  # no point waiting out a daily quota
    assert "daily limit for 'gemini-x' is used up" in str(info.value)
    assert "resets in about 10h56m" in str(info.value)
