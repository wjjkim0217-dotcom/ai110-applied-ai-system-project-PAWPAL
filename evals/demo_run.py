"""End-to-end demo with a full trace, for text-based execution evidence.

Runs requests through the real system (retriever -> agent -> tools -> scheduler ->
self-check) and prints, for each one: the starting state and input, every step the
agent took (guidelines retrieved, model calls, tool calls and tool errors, self-check
rounds), the AI's output, the proposed schedule, and the scenario's evaluation checks.

    python -m evals.demo_run                       # the default demo cases
    python -m evals.demo_run --save docs/evidence/demo_run.txt

The first case is a guardrail check that never reaches the model. The others make real
API calls.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

from evals.run_evals import build_owner, load_scenarios, score
from evals.sample_interactions import describe_setup
from pawpal_agent import PawPalAgent, has_credentials, key_hint, setup_logging

DEMO_CASES = [
    # (scenario name, what this case demonstrates)
    ("empty_request", "Guardrail: an empty request is rejected before any API call"),
    ("dog_and_cat_workday", "RAG + agent: guidelines retrieved, tasks created through tools, plan checked"),
    ("fix_existing_clash", "Agent + self-check: avoids double-booking an existing fixed-time task"),
    ("injection_does_not_delete", "Guardrail: a prompt-injection request cannot delete the owner's tasks"),
]
EMPTY_CASE = {"name": "empty_request", "request": "   ", "day": "2025-01-06",
              "setup": {"available": ["07:00", "21:00"], "pets": []}, "expect": {}}


def trace(agent: PawPalAgent, scenario: dict, purpose: str, number: int, total: int) -> tuple[str, bool]:
    owner = build_owner(scenario.get("setup", {}))
    result = agent.run(owner, scenario["request"], date.fromisoformat(scenario["day"]))
    checks = score(result, owner, scenario.get("expect", {}))
    if scenario["name"] == "empty_request":
        checks = []  # judged below: rejected, no model call, no draft
        guard_ok = result.status == "error" and result.model_calls == 0 and result.draft_owner is None
    passed = all(c.passed for c in checks) if checks else guard_ok

    out = [f"=== Case {number}/{total}: {scenario['name']} ===", f"Purpose:        {purpose}",
           f"Starting state: {describe_setup(scenario.get('setup', {}))}",
           f"Input:          {scenario['request']!r}", "", "Trace:"]
    out += [f"  {i:>2}. {s.kind:<10} {s.detail}" for i, s in enumerate(result.steps, 1)] or ["  (no steps)"]
    out += ["", f"Output (status: {result.status}):"]
    out += [f"  {line}" for line in (result.summary or "(none)").splitlines()]
    if result.changes:
        out += ["", "Changes proposed for owner review:"] + [f"  - {c}" for c in result.changes]
    if result.plan and result.plan.scheduled:
        out += ["", f"Proposed schedule ({result.day:%A}):"]
        out += [f"  {e.start:%H:%M}-{e.end:%H:%M}  {e.pet.name:<6} {e.task.title:<24} [{e.reason}]"
                for e in result.plan.scheduled]
    if result.cited:
        out += ["", f"Guidelines cited: {', '.join(result.cited)}"]
    out += ["", "Evaluation:"]
    if checks:
        out += [f"  {'PASS' if c.passed else 'FAIL'}  {c.name}: {c.detail}" for c in checks]
    else:
        out += [f"  {'PASS' if passed else 'FAIL'}  rejected with status 'error', "
                f"{result.model_calls} model calls, no draft created"]
    out += [f"  Result: {'PASS' if passed else 'FAIL'} | run {result.run_id} | {result.model_calls} model calls, "
            f"{result.tool_calls} tool calls, {result.check_rounds} self-check fix rounds, "
            f"{result.input_tokens:,} in / {result.output_tokens:,} out tokens", ""]
    return "\n".join(out), passed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--save", type=Path, help="also write the trace to this text file")
    args = parser.parse_args(argv)

    load_dotenv()
    if not has_credentials():
        print(f"{key_hint()} is not set. Add it to .env (see .env.example).", file=sys.stderr)
        return 2
    setup_logging()
    agent = PawPalAgent()
    scenarios = {s["name"]: s for s in load_scenarios()} | {"empty_request": EMPTY_CASE}

    header = (f"PawPal+ end-to-end demo | {agent.provider} model {agent.model} | "
              f"{datetime.now():%Y-%m-%d %H:%M}\nCommand: python -m evals.demo_run\n")
    blocks, results = [header], []
    for i, (name, purpose) in enumerate(DEMO_CASES, 1):
        text, passed = trace(agent, scenarios[name], purpose, i, len(DEMO_CASES))
        print(text, flush=True)
        blocks.append(text)
        results.append(passed)
    summary = f"Summary: {sum(results)}/{len(results)} cases passed."
    print(summary)
    blocks.append(summary)
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text("\n".join(blocks) + "\n", encoding="utf-8")
        print(f"Saved to {args.save}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
