"""Drive the real Streamlit app headlessly (streamlit.testing.AppTest) with a fake
Claude client, to check the AI assistant's review step end to end.
"""

from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import pawpal_agent
from tests.fake_claude import FakeClient, response, text, tool_use

REAL_AGENT = pawpal_agent.PawPalAgent
APP = Path(__file__).resolve().parent.parent / "app.py"


def scripted_agent(steps):
    return lambda *args, **kwargs: REAL_AGENT(client=FakeClient(steps))


def button(at: AppTest, label: str):
    return next(b for b in at.button if b.label == label)


@pytest.fixture(autouse=True)
def fresh_cache():
    st.cache_resource.clear()
    yield
    st.cache_resource.clear()


def test_without_a_key_the_assistant_is_disabled(monkeypatch):
    monkeypatch.setenv("PAWPAL_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "paste-your-gemini-key-here")  # the .env placeholder

    at = AppTest.from_file(str(APP), default_timeout=30).run()

    assert button(at, "Ask PawPal AI").disabled
    assert any("GEMINI_API_KEY" in i.value for i in at.info)


def test_draft_is_reviewed_then_accepted(monkeypatch):
    monkeypatch.setenv("PAWPAL_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(pawpal_agent, "PawPalAgent", scripted_agent([
        response(
            tool_use("add_pet", {"name": "Rex", "species": "dog"}),
            tool_use("add_task", {"pet_name": "Rex", "title": "Morning walk", "duration_minutes": 30,
                                  "priority": "high", "category": "walk", "frequency": "daily",
                                  "fixed_time": "08:00", "sources": ["dogs.md#walks-and-exercise"]}),
        ),
        response(text("Added Rex and a 30 minute morning walk [dogs.md#walks-and-exercise].")),
    ]))

    at = AppTest.from_file(str(APP), default_timeout=30).run()
    at.text_area[0].input("I have a dog named Rex who needs walks.")
    button(at, "Ask PawPal AI").click()
    at.run()

    # Draft shown, real data untouched.
    assert any("Proposed plan" in m.value for m in at.markdown)
    assert any("New pet: Rex (dog)" in m.value for m in at.markdown)
    assert at.session_state.owner.pets == []

    button(at, "✅ Accept changes").click()
    at.run()

    [rex] = at.session_state.owner.pets
    assert [t.title for t in rex.tasks] == ["Morning walk"]
    assert at.session_state.agent_result is None


def test_discard_keeps_the_original(monkeypatch):
    monkeypatch.setenv("PAWPAL_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(pawpal_agent, "PawPalAgent", scripted_agent([
        response(tool_use("add_pet", {"name": "Luna", "species": "cat"})),
        response(text("Added Luna.")),
    ]))

    at = AppTest.from_file(str(APP), default_timeout=30).run()
    at.text_area[0].input("Add my cat Luna")
    button(at, "Ask PawPal AI").click()
    at.run()
    button(at, "✖️ Discard").click()
    at.run()

    assert at.session_state.owner.pets == []
    assert at.session_state.agent_result is None


def test_api_error_is_shown_not_raised(monkeypatch):
    import anthropic

    monkeypatch.setenv("PAWPAL_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(pawpal_agent, "PawPalAgent", scripted_agent([anthropic.AnthropicError("boom")]))

    at = AppTest.from_file(str(APP), default_timeout=30).run()
    at.text_area[0].input("Add my cat Luna")
    button(at, "Ask PawPal AI").click()
    at.run()

    assert not at.exception
    assert any("Could not call the Anthropic API" in e.value for e in at.error)
