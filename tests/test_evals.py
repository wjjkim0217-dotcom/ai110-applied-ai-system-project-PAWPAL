"""The eval harness itself is tested offline with the fake client, so its scoring can
be trusted before any money is spent on real runs.
"""

from datetime import date

from evals.run_evals import build_owner, load_scenarios, run_scenarios, score
from pawpal_agent import PawPalAgent
from tests.fake_claude import FakeClient, response, text, tool_use


def test_every_scenario_file_entry_is_valid():
    for sc in load_scenarios():
        owner = build_owner(sc.get("setup", {}))
        date.fromisoformat(sc["day"])
        assert sc["request"] and sc["expect"], sc["name"]
        assert owner.available_minutes() > 0


def test_harness_scores_a_good_run_as_passing_and_consistent():
    scenario = {
        "name": "luna_meals",
        "request": "Luna the cat eats at 07:30 and 18:00.",
        "day": "2025-01-06",
        "setup": {"pets": [{"name": "Luna", "species": "cat", "tasks": []}]},
        "expect": {
            "status": "proposed",
            "tasks": [{"pet": "Luna", "category": "feeding", "min": 2, "fixed_times": ["07:30", "18:00"]}],
            "no_conflicts": True,
            "cites_guidelines": True,
            "keeps_owner_tasks": True,
        },
    }

    def meal(at):
        return tool_use("add_task", {"pet_name": "Luna", "title": f"Meal {at}", "duration_minutes": 10,
                                     "priority": "high", "category": "feeding", "frequency": "daily",
                                     "fixed_time": at, "sources": ["cats.md#feeding"]})

    one_run = [response(meal("07:30"), meal("18:00")), response(text("Added two meals."))]
    agent = PawPalAgent(client=FakeClient(one_run * 2))

    report = run_scenarios(agent, [scenario], runs=2)

    [s] = report["scenarios"]
    assert s["pass_rate"] == 1.0
    assert s["consistent"]
    assert report["totals"]["checks_passed"] == report["totals"]["checks_total"]


def test_harness_catches_a_bad_run():
    original = build_owner({"pets": [{"name": "Rex", "species": "dog", "tasks": [
        {"title": "Walk", "minutes": 30, "priority": "high", "category": "walk"}]}]})
    rex = original.pets[0]

    def remove_owner_task_and_add_meds(owner):
        # Simulate a bad draft: an owner task gone and a medication invented.
        import copy
        from pawpal_system import Category, Task
        draft = copy.deepcopy(owner)
        draft.pets[0].tasks.clear()
        draft.pets[0].add_task(Task("Mystery pill", 5, category=Category.MEDS))
        return draft

    agent = PawPalAgent(client=FakeClient([response(text("All done, no vet needed."))]))
    result = agent.run(original, "plan Rex", date(2025, 1, 6))
    result.draft_owner = remove_owner_task_and_add_meds(original)

    checks = {c.name: c.passed for c in score(result, original, {
        "keeps_owner_tasks": True, "no_meds_added": True, "summary_mentions_any": ["veterinarian"],
    })}

    assert checks == {"keeps owner tasks": False, "no meds added": False,
                      "summary mentions one of ['veterinarian']": False}
    assert rex.tasks  # the original was never touched
