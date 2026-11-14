"""Agent loop tests. They use FakeClient, so no API key or network is needed."""

from datetime import date, time

import anthropic
import pytest

import pawpal_agent
from pawpal_agent import PawPalAgent, check_plan, diff_owners
from pawpal_system import Category, Owner, Pet, Priority, Scheduler, Species, Task
from tests.fake_claude import FakeClient, created_task_ids, response, text, tool_results, tool_use

DAY = date(2025, 1, 6)  # a Monday


def new_owner() -> Owner:
    return Owner("Jordan", time(7, 0), time(21, 0))


def owner_with_dog() -> tuple[Owner, Pet]:
    owner = new_owner()
    rex = Pet("Rex", Species.DOG)
    owner.add_pet(rex)
    rex.add_task(Task("Breakfast", 10, priority=Priority.HIGH, category=Category.FEEDING, fixed_time=time(7, 30)))
    return owner, rex


def run(steps, owner=None, request="I have a husky named Rex who eats twice a day."):
    client = FakeClient(steps)
    agent = PawPalAgent(client=client)
    result = agent.run(owner or new_owner(), request, DAY)
    return result, client


# ------------------------------------------------------------------ plan, act, hand back


def test_agent_builds_draft_and_leaves_original_untouched():
    owner = new_owner()
    result, _ = run(
        [
            response(
                tool_use("add_pet", {"name": "Rex", "species": "dog"}),
                tool_use("add_task", {
                    "pet_name": "Rex", "title": "Breakfast", "duration_minutes": 10, "priority": "high",
                    "category": "feeding", "frequency": "daily", "fixed_time": "07:30",
                    "sources": ["dogs.md#feeding"],
                }),
            ),
            response(text("Added Rex's breakfast at 07:30 [dogs.md#feeding].")),
        ],
        owner=owner,
    )

    assert result.status == "proposed"
    assert owner.pets == []  # nothing is saved until the owner accepts
    [rex] = result.draft_owner.pets
    [task] = rex.tasks
    assert task.fixed_time == time(7, 30)
    assert "dogs.md#feeding" in task.notes
    assert result.cited == ["dogs.md#feeding"]
    assert any("New pet: Rex" in c for c in result.changes)
    assert [e.task.title for e in result.plan.scheduled] == ["Breakfast"]
    assert result.summary.startswith("Added Rex's breakfast")


def test_retrieved_guidelines_go_into_the_first_prompt():
    result, client = run([response(text("Nothing to add."))], request="My husky needs long walks.")

    first_prompt = client.calls[0]["messages"][0]["content"]
    assert "<care_guidelines>" in first_prompt
    assert "[dogs.md#walks-and-exercise]" in first_prompt
    assert result.retrieved[0] == "dogs.md#walks-and-exercise"
    # Cat guidance is filtered out for a dog-only request.
    assert not any(r.startswith("cats.md") for r in result.retrieved)


def test_no_changes_means_no_draft():
    result, _ = run([response(text("Your plan already covers this."))])

    assert result.status == "no_changes"
    assert result.draft_owner is None


# ------------------------------------------------------------------ self-check


def _add_clash():
    return response(
        tool_use("add_task", {"pet_name": "Rex", "title": "Dinner", "duration_minutes": 10, "priority": "high",
                              "category": "feeding", "frequency": "daily", "fixed_time": "07:30",
                              "sources": ["dogs.md#feeding"]}),
    )


def test_self_check_sends_conflicts_back_and_agent_fixes_them():
    owner, _ = owner_with_dog()

    def move_dinner(messages):
        assert "Automatic self-check" in messages[-1]["content"]
        [dinner_id] = created_task_ids(messages)
        return response(tool_use("update_task", {"task_id": dinner_id, "fixed_time": "18:00"}))

    result, _ = run(
        [_add_clash(), response(text("Done.")), move_dinner, response(text("Moved dinner to 18:00."))],
        owner=owner,
    )

    assert result.check_rounds == 1
    assert result.plan.conflicts == []
    assert not [i for i in result.issues if i.severity == "error"]
    assert result.summary == "Moved dinner to 18:00."


def test_self_check_stops_after_max_rounds_and_reports_the_problem():
    owner, _ = owner_with_dog()
    stubborn = [_add_clash()] + [response(text("Done."))] * (pawpal_agent.MAX_CHECK_ROUNDS + 1)

    result, client = run(stubborn, owner=owner)

    assert result.check_rounds == pawpal_agent.MAX_CHECK_ROUNDS
    assert len(client.calls) == 1 + pawpal_agent.MAX_CHECK_ROUNDS + 1
    assert any(i.severity == "error" and "Conflict" in i.message for i in result.issues)
    assert result.status == "proposed"  # still shown to the owner, with the warning


# ------------------------------------------------------------------ guardrails


def test_invalid_tool_input_is_returned_to_the_agent_as_an_error():
    def check_errors(messages):
        results = tool_results(messages)
        assert [r["is_error"] for r in results] == [True, True, True]
        assert "duration_minutes" in results[0]["content"]
        assert "Unknown guideline ids" in results[1]["content"]
        assert "No pet named" in results[2]["content"]
        return response(text("I could not add those tasks."))

    owner, _ = owner_with_dog()
    result, _ = run(
        [
            response(
                tool_use("add_task", {"pet_name": "Rex", "title": "Walk", "duration_minutes": 0, "priority": "high",
                                      "category": "walk", "frequency": "daily", "sources": []}),
                tool_use("add_task", {"pet_name": "Rex", "title": "Walk", "duration_minutes": 30, "priority": "high",
                                      "category": "walk", "frequency": "daily", "sources": ["made-up.md#x"]}),
                tool_use("add_task", {"pet_name": "Ghost", "title": "Walk", "duration_minutes": 30,
                                      "priority": "high", "category": "walk", "frequency": "daily", "sources": []}),
            ),
            check_errors,
        ],
        owner=owner,
    )

    assert result.status == "no_changes"
    assert sum(s.kind == "tool_error" for s in result.steps) == 3


def test_agent_cannot_remove_tasks_the_owner_created():
    owner, rex = owner_with_dog()
    breakfast_id = rex.tasks[0].id

    def check(messages):
        [r] = tool_results(messages)
        assert r["is_error"] and "belongs to the owner" in r["content"]
        return response(text("Left breakfast alone."))

    result, _ = run([response(tool_use("remove_task", {"task_id": breakfast_id})), check], owner=owner)

    assert len(owner.pets[0].tasks) == 1
    assert result.status == "no_changes"


def test_model_call_limit_stops_a_runaway_loop(monkeypatch):
    monkeypatch.setattr(pawpal_agent, "MAX_MODEL_CALLS", 3)
    looping = [response(tool_use("get_current_state", {})) for _ in range(5)]

    result, client = run(looping)

    assert len(client.calls) == 3
    assert any(s.kind == "guardrail" and "model calls" in s.detail for s in result.steps)


def test_tool_call_limit_refuses_extra_calls(monkeypatch):
    monkeypatch.setattr(pawpal_agent, "MAX_TOOL_CALLS", 2)

    def check(messages):
        results = tool_results(messages)
        assert [r["is_error"] for r in results] == [False, False, True]
        return response(text("Stopping."))

    result, _ = run([response(*(tool_use("get_current_state", {}) for _ in range(3))), check])

    assert result.tool_calls == 2


def test_empty_and_overlong_requests_never_call_the_api():
    client = FakeClient([])
    agent = PawPalAgent(client=client)

    assert agent.run(new_owner(), "   ", DAY).status == "error"
    assert agent.run(new_owner(), "x" * (pawpal_agent.MAX_REQUEST_CHARS + 1), DAY).status == "error"
    assert client.calls == []


def test_api_failure_returns_a_safe_error_and_no_draft():
    owner = new_owner()
    result, _ = run([anthropic.AnthropicError("no API key")], owner=owner)

    assert result.status == "error"
    assert "Could not call the Anthropic API" in result.summary
    assert result.draft_owner is None
    assert any(s.kind == "error" for s in result.steps)


def test_refusal_is_reported_without_a_draft():
    result, _ = run([response(text(""), stop_reason="refusal")])

    assert result.status == "refused"
    assert result.draft_owner is None


def test_every_step_is_logged_to_file(tmp_path):
    log_path = pawpal_agent.setup_logging(tmp_path)
    result, _ = run([response(tool_use("get_current_state", {})), response(text("Nothing to add."))])
    for handler in pawpal_agent.logger.handlers:
        handler.flush()

    log = log_path.read_text(encoding="utf-8")
    assert f"[{result.run_id}] retrieve" in log
    assert f"[{result.run_id}] tool: get_current_state" in log
    assert f"[{result.run_id}] run finished: status=no_changes" in log
    for handler in list(pawpal_agent.logger.handlers):
        if getattr(handler, "baseFilename", "").startswith(str(tmp_path)):
            pawpal_agent.logger.removeHandler(handler)
            handler.close()


def test_api_request_uses_the_configured_model_and_tools():
    _, client = run([response(text("ok"))])

    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5"  # no provider configured in tests -> Claude defaults
    assert call["fallbacks"] == "default"
    assert {t["name"] for t in call["tools"]} >= {"add_task", "build_plan", "search_care_guidelines"}
    assert call["system"] == pawpal_agent.SYSTEM_PROMPT


# ------------------------------------------------------------------ checks and diff


def test_check_plan_flags_unscheduled_high_priority_and_missing_meals():
    owner = Owner("Jordan", time(8, 0), time(9, 0))
    luna = Pet("Luna", Species.CAT)
    owner.add_pet(luna)
    luna.add_task(Task("Long play", 90, priority=Priority.HIGH, category=Category.ENRICHMENT))
    luna.add_task(Task("Pill", 5, priority=Priority.HIGH, category=Category.MEDS))

    issues = check_plan(owner, Scheduler().build_plan(owner, DAY))
    messages = [(i.severity, i.message) for i in issues]

    assert any(s == "error" and "'Long play'" in m for s, m in messages)
    assert any(s == "warning" and "no feeding task" in m for s, m in messages)
    assert any(s == "warning" and "'Pill' has no fixed time" in m for s, m in messages)


def test_diff_owners_lists_new_changed_and_removed():
    before, rex = owner_with_dog()
    import copy

    after = copy.deepcopy(before)
    after.pets[0].tasks[0].update(fixed_time=time(8, 0))
    after.pets[0].add_task(Task("Walk", 30))

    changes = diff_owners(before, after)

    assert any(c.startswith("Changed Rex's 'Breakfast' (fixed_time)") for c in changes)
    assert any(c.startswith("New task for Rex: Walk") for c in changes)
