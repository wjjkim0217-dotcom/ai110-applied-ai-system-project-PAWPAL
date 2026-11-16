"""Before/after comparison of retrieval quality, offline and deterministic.

Compares the guideline sections the agent would receive for typical owner requests
under two retrieval setups:

- before: plain keyword matching. No synonym map, and species are only detected from
  the words "dog(s)" and "cat(s)" (so "husky" or "kitten" does not filter by species).
- after:  the retrieval the agent uses today (pawpal_rag.SYNONYMS plus the breed and
  age words in pawpal_agent.SPECIES_WORDS).

For each request it lists the sections retrieved (top 5, the same k the agent uses),
whether the sections the plan depends on were among them, and how many sections for
the wrong species slipped in.

    python -m evals.retrieval_comparison
    python -m evals.retrieval_comparison --save docs/evidence/retrieval_comparison.txt

No API key or network is needed.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from unittest import mock

import pawpal_rag
from pawpal_agent import SPECIES_WORDS
from pawpal_rag import KnowledgeBase

K = 5  # the agent puts the top 5 sections into its first message

# (request, species of the pets already saved, sections the plan needs, wrong species)
CASES = [
    ("I have a 2-year-old husky named Mochi. I work 9 to 5 and he needs his walks.",
     [], ["dogs.md#walks-and-exercise", "scheduling.md#fitting-the-owner-s-day"], "cat"),
    ("Rex needs his medicine with breakfast.",
     ["dog"], ["medication.md#medication-with-food"], "cat"),
    ("My lab needs potty breaks while I'm at the office.",
     [], ["dogs.md#bathroom-breaks", "scheduling.md#fitting-the-owner-s-day"], "cat"),
    ("We just adopted a 10-week-old kitten called Pip. What food does she need?",
     [], ["cats.md#feeding"], "dog"),
    ("Luna is getting old. Plan her meals and a check-up reminder.",
     ["cat"], ["cats.md#senior-cats", "cats.md#feeding"], "dog"),
]

BASIC_SPECIES = {"dog": "dog", "dogs": "dog", "cat": "cat", "cats": "cat"}


def detect_species(request: str, saved: list[str], words: dict[str, str]) -> list[str] | None:
    species = set(saved) | {words[w] for w in re.findall(r"[a-z]+", request.lower()) if w in words}
    return sorted(species) or None


def retrieve(kb: KnowledgeBase, request: str, saved: list[str], words: dict[str, str]) -> list[str]:
    return [hit.chunk.id for hit in kb.search(request, detect_species(request, saved, words), k=K)]


def compare() -> list[dict]:
    """Run every case under both setups and return one row per case and setup."""
    with mock.patch.dict(pawpal_rag.SYNONYMS, clear=True):
        before_kb = KnowledgeBase.load()  # documents tokenized without synonyms
        before = [retrieve(before_kb, req, saved, BASIC_SPECIES) for req, saved, _, _ in CASES]
    after_kb = KnowledgeBase.load()
    after = [retrieve(after_kb, req, saved, SPECIES_WORDS) for req, saved, _, _ in CASES]

    rows = []
    for (request, saved, needed, wrong), b, a in zip(CASES, before, after):
        for setup, ids in (("before", b), ("after", a)):
            rows.append({
                "request": request, "setup": setup, "retrieved": ids,
                "needed_found": [n for n in needed if n in ids], "needed": needed,
                "wrong_species": [i for i in ids if i.startswith(f"{wrong}s.md#")],
            })
    return rows


def render(rows: list[dict]) -> str:
    lines = ["Retrieval before/after comparison (offline, top %d sections per request)" % K, ""]
    totals = {"before": [0, 0, 0], "after": [0, 0, 0]}  # needed found, needed total, wrong species
    for i in range(0, len(rows), 2):
        lines.append(f"Request: {rows[i]['request']!r}")
        lines.append(f"  Needs: {', '.join(rows[i]['needed'])}")
        for row in rows[i:i + 2]:
            t = totals[row["setup"]]
            t[0] += len(row["needed_found"])
            t[1] += len(row["needed"])
            t[2] += len(row["wrong_species"])
            lines.append(
                f"  {row['setup']:<6}  needed {len(row['needed_found'])}/{len(row['needed'])}, "
                f"wrong species {len(row['wrong_species'])}: {', '.join(row['retrieved']) or 'none'}"
            )
        lines.append("")
    for setup, (found, needed, wrong) in totals.items():
        lines.append(f"{setup:<6}  needed sections retrieved: {found}/{needed} | "
                     f"wrong-species sections retrieved: {wrong}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--save", type=Path, help="also write the report to this file")
    args = parser.parse_args()
    report = render(compare())
    print(report)
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(report + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
