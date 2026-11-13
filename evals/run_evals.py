"""Reliability harness for the PawPal+ agent.

Runs each scenario in evals/scenarios.json through the real agent (real API calls),
scores the final draft with deterministic checks, and repeats each scenario --runs
times to measure consistency. Prints a table and writes a JSON report to
evals/results/.

    python -m evals.run_evals                 # every scenario once
    python -m evals.run_evals --runs 3        # consistency: 3 runs each
    python -m evals.run_evals --only kitten_feeding

Each run costs API tokens; the report records how many were used.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from pawpal_agent import AgentResult, PawPalAgent, has_credentials, parse_hhmm, setup_logging
from pawpal_system import Category, Owner, Pet, Priority, Species, Task

EVAL_DIR = Path(__file__).parent
SCENARIOS = EVAL_DIR / "scenarios.json"
RESULTS_DIR = EVAL_DIR / "results"


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


def load_scenarios(path: Path = SCENARIOS) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_owner(setup: dict[str, Any]) -> Owner:
    start, end = setup.get("available", ["07:00", "21:00"])
    owner = Owner("Eval", parse_hhmm(start, "start"), parse_hhmm(end, "end"))
    for p in setup.get("pets", []):
        pet = Pet(p["name"], Species(p["species"]))
        owner.add_pet(pet)
        for t in p.get("tasks", []):
            pet.add_task(Task(
                t["title"], t["minutes"],
                priority=Priority(t.get("priority", "medium")),
                category=Category(t.get("category", "other")),
                fixed_time=parse_hhmm(t["fixed_time"], "fixed_time") if t.get("fixed_time") else None,
            ))
    return owner


def score(result: AgentResult, original: Owner, expect: dict[str, Any]) -> list[Check]:
    """Deterministic checks on one agent run. Only the expectations a scenario lists are checked."""
    checks: list[Check] = []
    final = result.draft_owner or original
    plan = result.plan

    if "status" in expect:
        checks.append(Check("status", result.status == expect["status"], result.status))

    for name, species in expect.get("pets", {}).items():
        pet = next((p for p in final.pets if p.name.lower() == name.lower()), None)
        checks.append(Check(f"pet {name}", pet is not None and pet.species.value == species,
                            pet.species.value if pet else "missing"))

    for rule in expect.get("tasks", []):
        pet = next((p for p in final.pets if p.name.lower() == rule["pet"].lower()), None)
        tasks = [t for t in (pet.tasks if pet else []) if t.category.value == rule["category"]]
        ok = len(tasks) >= rule.get("min", 1)
        detail = f"{len(tasks)} {rule['category']} task(s)"
        if ok and rule.get("fixed_times"):
            times = {f"{t.fixed_time:%H:%M}" for t in tasks if t.fixed_time}
            ok = set(rule["fixed_times"]) <= times
            detail += f" at {sorted(times)}"
        checks.append(Check(f"{rule['pet']} {rule['category']} >= {rule.get('min', 1)}", ok, detail))

    if expect.get("no_conflicts"):
        n = len(plan.conflicts) if plan else 0
        checks.append(Check("no conflicts", n == 0, f"{n} conflict(s)"))

    if expect.get("all_high_priority_scheduled"):
        missing = [u.task.title for u in (plan.unscheduled if plan else []) if u.task.priority is Priority.HIGH]
        checks.append(Check("high priority scheduled", not missing, ", ".join(missing) or "all fit"))

    if expect.get("cites_guidelines"):
        checks.append(Check("cites guidelines", bool(result.cited), ", ".join(result.cited) or "none"))

    if expect.get("keeps_owner_tasks"):
        before = {t.id for _, t in original.get_all_tasks()}
        after = {t.id for _, t in final.get_all_tasks()}
        lost = before - after
        checks.append(Check("keeps owner tasks", not lost, f"{len(lost)} removed"))

    if expect.get("no_meds_added"):
        before = {t.id for _, t in original.get_all_tasks()}
        added = [t for _, t in final.get_all_tasks() if t.id not in before and t.category is Category.MEDS]
        checks.append(Check("no meds added", not added, ", ".join(t.title for t in added) or "none"))

    if "summary_mentions_any" in expect:
        words = expect["summary_mentions_any"]
        hit = [w for w in words if w.lower() in result.summary.lower()]
        checks.append(Check(f"summary mentions one of {words}", bool(hit), ", ".join(hit) or "none"))

    return checks


def fingerprint(result: AgentResult, original: Owner) -> tuple:
    """What a run produced, ignoring ids and wording: (pet, category) counts of the final tasks."""
    final = result.draft_owner or original
    return tuple(sorted(Counter((p.name.lower(), t.category.value) for p, t in final.get_all_tasks()).items()))


def run_scenarios(agent: PawPalAgent, scenarios: list[dict[str, Any]], runs: int = 1) -> dict[str, Any]:
    report: dict[str, Any] = {"model": agent.model, "effort": agent.effort, "runs_per_scenario": runs,
                              "started": datetime.now().isoformat(timespec="seconds"), "scenarios": []}
    for sc in scenarios:
        day = date.fromisoformat(sc["day"])
        run_rows, prints = [], []
        for i in range(runs):
            original = build_owner(sc.get("setup", {}))
            result = agent.run(original, sc["request"], day)
            checks = score(result, original, sc.get("expect", {}))
            run_rows.append({
                "run": i + 1, "run_id": result.run_id, "status": result.status,
                "passed": all(c.passed for c in checks),
                "checks": [c.__dict__ for c in checks],
                "check_rounds": result.check_rounds, "model_calls": result.model_calls,
                "tool_calls": result.tool_calls, "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens, "summary": result.summary,
            })
            prints.append(fingerprint(result, original))
        report["scenarios"].append({
            "name": sc["name"],
            "pass_rate": sum(r["passed"] for r in run_rows) / runs,
            "consistent": len(set(prints)) == 1,
            "runs": run_rows,
        })
    rows = [r for s in report["scenarios"] for r in s["runs"]]
    report["totals"] = {
        "runs": len(rows),
        "passed": sum(r["passed"] for r in rows),
        "checks_passed": sum(c["passed"] for r in rows for c in r["checks"]),
        "checks_total": sum(len(r["checks"]) for r in rows),
        "input_tokens": sum(r["input_tokens"] for r in rows),
        "output_tokens": sum(r["output_tokens"] for r in rows),
    }
    return report


def print_report(report: dict[str, Any]) -> None:
    print(f"\nPawPal+ agent eval · model {report['model']} · effort {report['effort']} · "
          f"{report['runs_per_scenario']} run(s) per scenario\n")
    print(f"{'scenario':<28} {'pass rate':>9}  {'consistent':>10}  failed checks")
    print("-" * 90)
    for s in report["scenarios"]:
        failed = sorted({f"{c['name']} ({c['detail']})" for r in s["runs"] for c in r["checks"] if not c["passed"]})
        print(f"{s['name']:<28} {s['pass_rate']:>8.0%}  {'yes' if s['consistent'] else 'no':>10}  "
              f"{'; '.join(failed) or '-'}")
    t = report["totals"]
    print("-" * 90)
    print(f"Runs passed: {t['passed']}/{t['runs']} · checks passed: {t['checks_passed']}/{t['checks_total']} · "
          f"tokens: {t['input_tokens']:,} in / {t['output_tokens']:,} out\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=1, help="runs per scenario (consistency check)")
    parser.add_argument("--only", nargs="*", help="scenario names to run")
    args = parser.parse_args(argv)

    load_dotenv()
    if not has_credentials():
        print("ANTHROPIC_API_KEY is not set. Add it to .env (see .env.example).", file=sys.stderr)
        return 2
    scenarios = load_scenarios()
    if args.only:
        scenarios = [s for s in scenarios if s["name"] in args.only]
        if not scenarios:
            print(f"No scenarios named {args.only}", file=sys.stderr)
            return 2

    setup_logging()
    report = run_scenarios(PawPalAgent(), scenarios, max(1, args.runs))
    print_report(report)
    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"eval_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Full report: {out}")
    return 0 if report["totals"]["passed"] == report["totals"]["runs"] else 1


if __name__ == "__main__":
    sys.exit(main())
