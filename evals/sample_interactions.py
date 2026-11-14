"""Capture real sample interactions for the README.

Runs a few eval scenarios through the real agent and renders each one as markdown:
the owner's input, the AI's summary, the changes it proposed, the resulting schedule,
the self-check result and the guidelines it cited.

    python -m evals.sample_interactions                 # print the markdown
    python -m evals.sample_interactions --write-readme  # replace the README section

The README section sits between the SAMPLES_START and SAMPLES_END markers. Each run
uses API tokens.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from evals.run_evals import build_owner, load_scenarios
from pawpal_agent import key_hint, AgentResult, PawPalAgent, has_credentials, setup_logging

README = Path(__file__).resolve().parent.parent / "README.md"
SAMPLES_START = "<!-- sample-interactions:start -->"
SAMPLES_END = "<!-- sample-interactions:end -->"
SAMPLE_SCENARIOS = ["dog_and_cat_workday", "fix_existing_clash", "illness_goes_to_vet"]


def describe_setup(setup: dict[str, Any]) -> str:
    start, end = setup.get("available", ["07:00", "21:00"])
    parts = [f"available {start}-{end}"]
    for pet in setup.get("pets", []):
        tasks = ", ".join(
            f"{t['title']}" + (f" at {t['fixed_time']}" if t.get("fixed_time") else "") for t in pet.get("tasks", [])
        )
        parts.append(f"{pet['name']} ({pet['species']})" + (f" with {tasks}" if tasks else ", no tasks"))
    if not setup.get("pets"):
        parts.append("no pets yet")
    return "; ".join(parts)


def render(title: str, scenario: dict[str, Any], result: AgentResult) -> str:
    lines = [f"#### {title}", ""]
    lines.append(f"**Starting state:** {describe_setup(scenario.get('setup', {}))}.")
    lines += ["", "**Input**", "", f"> {scenario['request']}", ""]
    lines += ["**AI output** (status: `" + result.status + "`)", ""]
    lines += [f"> {line}" if line.strip() else ">" for line in (result.summary or "(no summary)").splitlines()]
    if result.changes:
        lines += ["", "**Changes proposed for review**", ""] + [f"- {c}" for c in result.changes]
    if result.plan and result.plan.scheduled:
        lines += ["", f"**Proposed schedule for {result.day:%A}**", "", "```"]
        lines += [f"{e.start:%H:%M}-{e.end:%H:%M}  {e.pet.name:<6} {e.task.title:<22} [{e.reason}]"
                  for e in result.plan.scheduled]
        lines += ["```"]
    issues = "; ".join(f"{i.severity}: {i.message.rstrip('.')}" for i in result.issues) or "no issues"
    lines += ["", f"**Self-check:** {result.check_rounds} fix round(s); final check: {issues}."]
    if result.cited:
        lines += [f"**Guidelines cited:** {', '.join(f'`{c}`' for c in result.cited)}."]
    lines += [f"**Run:** `{result.run_id}`, {result.model_calls} model calls, {result.tool_calls} tool calls, "
              f"{result.input_tokens:,} input / {result.output_tokens:,} output tokens.", ""]
    return "\n".join(lines)


def capture(agent: PawPalAgent, names: list[str] = SAMPLE_SCENARIOS) -> str:
    by_name = {s["name"]: s for s in load_scenarios()}
    effort = f" (effort `{agent.effort}`)" if agent.provider == "anthropic" else ""
    blocks = [f"_Captured with {agent.provider} model `{agent.model}`{effort} on {date.today():%Y-%m-%d} "
              f"by `python -m evals.sample_interactions`._", ""]
    for i, name in enumerate(names, 1):
        sc = by_name[name]
        result = agent.run(build_owner(sc.get("setup", {})), sc["request"], date.fromisoformat(sc["day"]))
        blocks.append(render(f"Example {i}: {name.replace('_', ' ')}", sc, result))
    return "\n".join(blocks)


def write_readme(markdown: str, path: Path = README) -> None:
    text = path.read_text(encoding="utf-8")
    start, end = text.index(SAMPLES_START) + len(SAMPLES_START), text.index(SAMPLES_END)
    path.write_text(text[:start] + "\n" + markdown.strip() + "\n" + text[end:], encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write-readme", action="store_true", help="replace the README sample section")
    args = parser.parse_args(argv)

    load_dotenv()
    if not has_credentials():
        print(f"{key_hint()} is not set. Add it to .env (see .env.example).", file=sys.stderr)
        return 2
    setup_logging()
    markdown = capture(PawPalAgent())
    if args.write_readme:
        write_readme(markdown)
        print(f"Updated the sample interactions in {README}")
    else:
        print(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
