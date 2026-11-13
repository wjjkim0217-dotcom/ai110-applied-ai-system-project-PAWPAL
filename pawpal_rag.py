"""Retrieval over the pet-care knowledge base in knowledge/*.md.

Each markdown file starts with a front-matter block naming the species it covers
(dog, cat, or any). Every "## " section becomes one chunk with a stable id such as
"dogs.md#feeding", which the agent cites in the notes of the tasks it creates.

Scoring is BM25 over the section title, its tags line and its text. It needs no
extra packages and gives the same result for the same query every time.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "for", "from",
    "has", "have", "he", "her", "his", "how", "i", "if", "in", "into", "is", "it", "its",
    "me", "my", "of", "on", "or", "our", "she", "so", "that", "the", "their", "them",
    "they", "this", "to", "up", "was", "we", "what", "when", "which", "who", "will",
    "with", "you", "your", "need", "needs", "want", "please", "help", "day", "daily",
}

# Words an owner might type that the knowledge base spells differently.
SYNONYMS = {
    "pill": "meds", "pills": "meds", "medicine": "meds", "medication": "meds",
    "insulin": "meds", "dose": "meds", "food": "feeding", "feed": "feeding",
    "eat": "feeding", "meal": "feeding", "meals": "feeding", "dinner": "feeding",
    "breakfast": "feeding", "potty": "bathroom", "pee": "bathroom", "brush": "grooming",
    "litterbox": "litter", "kitten": "kitten", "puppy": "puppy", "old": "senior",
    "elderly": "senior", "job": "work", "office": "work",
}

_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercase words, minus stopwords, with naive plural stripping and synonyms."""
    tokens = []
    for word in _TOKEN.findall(text.lower()):
        if word in STOPWORDS:
            continue
        if word not in SYNONYMS and len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        tokens.append(SYNONYMS.get(word, word))
    return tokens


@dataclass(frozen=True)
class Chunk:
    id: str
    title: str
    species: str  # "dog", "cat" or "any"
    tags: tuple[str, ...]
    text: str

    def as_context(self) -> str:
        """The form the agent sees: id and title first, so it can cite the id."""
        return f"[{self.id}] {self.title}\n{self.text}"


@dataclass
class RetrievedChunk:
    chunk: Chunk
    score: float


def _slug(heading: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")


def parse_file(path: Path) -> list[Chunk]:
    """Split one knowledge file into chunks, one per "## " section."""
    raw = path.read_text(encoding="utf-8")
    species = "any"
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.S)
    if match:
        for line in match.group(1).splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "species":
                species = value.strip().lower()
        raw = raw[match.end():]

    chunks = []
    for section in re.split(r"^## ", raw, flags=re.M)[1:]:
        heading, _, body = section.partition("\n")
        lines = body.strip().splitlines()
        tags: tuple[str, ...] = ()
        if lines and lines[0].lower().startswith("tags:"):
            tags = tuple(t.strip() for t in lines[0][5:].split(",") if t.strip())
            lines = lines[1:]
        text = " ".join(line.strip() for line in lines if line.strip())
        chunks.append(
            Chunk(
                id=f"{path.name}#{_slug(heading)}",
                title=heading.strip(),
                species=species,
                tags=tags,
                text=text,
            )
        )
    return chunks


@dataclass
class KnowledgeBase:
    chunks: list[Chunk]
    _docs: list[list[str]] = field(default_factory=list, repr=False)
    _df: Counter = field(default_factory=Counter, repr=False)

    def __post_init__(self) -> None:
        for chunk in self.chunks:
            # Title and tags are repeated so they weigh more than body text.
            tags = " ".join(chunk.tags)
            tokens = tokenize(f"{chunk.title} {chunk.title} {tags} {tags} {chunk.text}")
            self._docs.append(tokens)
            self._df.update(set(tokens))

    @classmethod
    def load(cls, directory: Path = KNOWLEDGE_DIR) -> KnowledgeBase:
        """Read every knowledge/*.md file. Raises FileNotFoundError if there are none."""
        files = sorted(directory.glob("*.md"))
        if not files:
            raise FileNotFoundError(f"No knowledge files found in {directory}")
        return cls([chunk for path in files for chunk in parse_file(path)])

    def get(self, chunk_id: str) -> Chunk | None:
        return next((c for c in self.chunks if c.id == chunk_id), None)

    def search(
        self, query: str, species: list[str] | None = None, k: int = 4
    ) -> list[RetrievedChunk]:
        """Top-k chunks for the query by BM25, limited to the given species plus "any".
        Chunks that share no word with the query are never returned.
        """
        allowed = None if not species else {s.lower() for s in species} | {"any"}
        terms = tokenize(query)
        if not terms:
            return []
        n = len(self.chunks)
        avg_len = sum(len(d) for d in self._docs) / n
        k1, b = 1.5, 0.75
        results = []
        for chunk, doc in zip(self.chunks, self._docs):
            if allowed is not None and chunk.species not in allowed:
                continue
            counts = Counter(doc)
            score = 0.0
            for term in set(terms):
                tf = counts.get(term, 0)
                if not tf:
                    continue
                idf = math.log(1 + (n - self._df[term] + 0.5) / (self._df[term] + 0.5))
                score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(doc) / avg_len))
            if score > 0:
                results.append(RetrievedChunk(chunk, round(score, 3)))
        results.sort(key=lambda r: (-r.score, r.chunk.id))
        return results[:k]
