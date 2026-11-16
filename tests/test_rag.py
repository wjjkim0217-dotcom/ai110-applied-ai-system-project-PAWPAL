from pathlib import Path

import pytest

from pawpal_rag import KnowledgeBase, parse_file, tokenize


@pytest.fixture(scope="module")
def kb():
    return KnowledgeBase.load()


def test_every_section_becomes_a_chunk_with_a_stable_id(kb):
    ids = [c.id for c in kb.chunks]
    assert len(ids) == len(set(ids))
    assert "dogs.md#feeding" in ids
    assert "medication.md#medication-timing" in ids
    assert all(c.text and c.species in {"dog", "cat", "any"} for c in kb.chunks)


@pytest.mark.parametrize(
    "query, species, expected",
    [
        ("my husky needs long walks", ["dog"], "dogs.md#walks-and-exercise"),
        ("how often should I feed my kitten", ["cat"], "cats.md#feeding"),
        ("she gets a pill twice a day", None, "medication.md#medication-timing"),
        ("cleaning the litter box", ["cat"], "cats.md#litter-box"),
        ("I work 9 to 5 and I'm busy", None, "scheduling.md#fitting-the-owner-s-day"),
    ],
)
def test_search_ranks_the_relevant_section_first(kb, query, species, expected):
    assert kb.search(query, species)[0].chunk.id == expected


def test_species_filter_keeps_shared_guidance(kb):
    results = kb.search("feeding meals medication", ["cat"], k=10)
    assert {r.chunk.species for r in results} <= {"cat", "any"}
    assert any(r.chunk.species == "any" for r in results)


def test_query_with_no_known_words_returns_nothing(kb):
    assert kb.search("the and of", None) == []
    assert kb.search("xylophone quasar", None) == []


def test_search_is_deterministic(kb):
    first = [(r.chunk.id, r.score) for r in kb.search("dog walks feeding", ["dog"])]
    assert first == [(r.chunk.id, r.score) for r in kb.search("dog walks feeding", ["dog"])]


def test_tokenize_maps_owner_words_to_knowledge_words():
    assert tokenize("Pills with dinner") == ["meds", "feeding"]


def test_parse_file_reads_species_and_tags(tmp_path: Path):
    f = tmp_path / "birds.md"
    f.write_text("---\nspecies: other\n---\n# Birds\n\n## Cage cleaning\ntags: cage, clean\nClean daily.\n",
                 encoding="utf-8")
    [chunk] = parse_file(f)
    assert (chunk.id, chunk.species, chunk.tags, chunk.text) == (
        "birds.md#cage-cleaning", "other", ("cage", "clean"), "Clean daily."
    )


def test_missing_knowledge_folder_fails_loudly(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        KnowledgeBase.load(tmp_path)


def test_current_retrieval_beats_plain_keyword_matching():
    from evals.retrieval_comparison import compare

    rows = compare()
    totals = {s: [0, 0] for s in ("before", "after")}
    for row in rows:
        totals[row["setup"]][0] += len(row["needed_found"])
        totals[row["setup"]][1] += len(row["wrong_species"])
    assert totals["after"][0] == sum(len(r["needed"]) for r in rows if r["setup"] == "after")
    assert totals["after"][1] == 0
    assert totals["after"][0] > totals["before"][0]
    assert totals["after"][1] < totals["before"][1]
