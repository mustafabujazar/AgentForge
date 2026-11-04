"""Deterministic, offline embeddings for the AgentForge knowledge store.

There is **no network call and no new dependency**: the vectorizer is a hashed
n-gram TF-IDF model implemented with the standard library (``re`` + ``hashlib`` +
``math``).  It is intentionally simple but genuinely useful for small internal
corpora:

* word unigrams + bigrams (context), plus character 4/5-grams for longer words
  (robustness to morphology and typos);
* feature hashing into a fixed-size (default 384) float vector with signed
  hashes, so cosine similarity approximates sparse TF-IDF cosine similarity;
* IDF weights fitted on the ingested corpus, sub-linear TF, then L2
  normalization.

Properties that matter for AgentForge:

* **Deterministic** — ``hashlib.blake2b`` is stable across processes/machines
  (unlike Python's salted ``hash()``), so persisted vectors and query vectors are
  reproducible across restarts.
* **Local** — runs inside the app, no embedding API, works offline.
* **Small** — a 384-dim list of floats per chunk; stored as JSON in the
  ``knowledge_chunks`` table, portable to Postgres (no pgvector requirement).
* **Upgradable** — swap ``LocalHashEmbedder`` for an API embedder later; the
  store only depends on the ``Embedder`` protocol (``fit`` / ``embed`` / ``dim``).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Sequence
from typing import Protocol, runtime_checkable

# Words that carry little retrieval signal. Kept deliberately short so domain
# terms ("margin", "returns", "discount") are never dropped.
STOPWORDS: frozenset[str] = frozenset(
    """
    a an and are as at be been but by can could did do does for from had has have he her him
    his how i if in into is it its just may me might more most must my no not of on or our out
    over she should so some such than that the their them then there these they this those to
    too up us very was we were what when where which who why will with would you your
    """.split()
)

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'_-]*")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_CHAR_NGRAM_SIZES: tuple[int, ...] = (4, 5)
_CHAR_NGRAM_MIN_TOKEN = 6


@runtime_checkable
class Embedder(Protocol):
    """Minimal interface the knowledge store needs from an embedding model."""

    dim: int

    def fit(self, texts: Sequence[str]) -> "Embedder":
        """Fit corpus statistics (IDF). Must be deterministic."""

    def embed(self, text: str) -> list[float]:
        """Return an L2-normalized vector of length ``dim``."""

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        """Vectorize a batch."""


def _stem(word: str) -> str:
    """Very light suffix stripping so inflections share a bucket.

    Deliberately conservative (no Porter stemmer dependency): ``returns`` /
    ``returned`` -> ``return``, ``tickets`` / ``ticketing`` -> ``ticket``,
    ``underperforming`` -> ``underperform``.
    """
    if len(word) < 6:
        return word
    for suffix in ("ing", "ed"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")) and len(word) - 1 >= 4:
        return word[:-1]
    return word


def tokenize(text: str) -> list[str]:
    """Lower-case word unigrams/bigrams plus char n-grams for long words."""
    words = _TOKEN_RE.findall(text.lower())
    features: list[str] = []
    previous: str | None = None
    previous_stem: str | None = None
    for word in words:
        if len(word) < 2:
            previous = None
            previous_stem = None
            continue
        if word in STOPWORDS:
            previous = None
            previous_stem = None
            continue
        features.append(word)
        stem = _stem(word)
        if stem != word:
            features.append(stem)
        if previous is not None:
            features.append(f"{previous} {word}")
            if previous_stem is not None and stem != word:
                features.append(f"{previous_stem} {stem}")
        if len(word) >= _CHAR_NGRAM_MIN_TOKEN:
            padded = f"#{word}#"
            for size in _CHAR_NGRAM_SIZES:
                for start in range(len(padded) - size + 1):
                    features.append(f"c:{padded[start:start + size]}")
        previous = word
        previous_stem = stem
    return features


def _bucket(feature: str, dim: int) -> tuple[int, float]:
    """Stable signed hash of a feature into a bucket index."""
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    index = value % dim
    sign = 1.0 if (value >> 63) & 1 else -1.0
    return index, sign


class LocalHashEmbedder:
    """Hashed n-gram TF-IDF embedder with a 384-dim default output."""

    def __init__(self, dim: int = 384) -> None:
        if dim < 8:
            raise ValueError("embedding dimension must be >= 8")
        self.dim = int(dim)
        self._idf: list[float] = [1.0] * self.dim
        self._doc_count = 0
        self._fitted = False

    # -- fitting -----------------------------------------------------
    @property
    def fitted(self) -> bool:
        return self._fitted

    @property
    def doc_count(self) -> int:
        return self._doc_count

    def fit(self, texts: Sequence[str]) -> "LocalHashEmbedder":
        """Compute document-frequency IDF weights over the supplied corpus."""
        df = [0] * self.dim
        count = 0
        for text in texts:
            count += 1
            seen: set[int] = set()
            for feature in tokenize(text):
                index, _sign = _bucket(feature, self.dim)
                seen.add(index)
            for index in seen:
                df[index] += 1
        self._doc_count = count
        self._idf = [math.log((1.0 + count) / (1.0 + freq)) + 1.0 for freq in df]
        self._fitted = True
        return self

    # -- encoding ----------------------------------------------------
    def _counts(self, text: str) -> tuple[dict[int, int], dict[int, float]]:
        counts: dict[int, int] = {}
        signs: dict[int, float] = {}
        for feature in tokenize(text):
            index, sign = _bucket(feature, self.dim)
            counts[index] = counts.get(index, 0) + 1
            signs[index] = signs.get(index, 0.0) + sign
        return counts, signs

    def embed(self, text: str) -> list[float]:
        """Return an L2-normalized hashed TF-IDF vector as a plain list."""
        if not text or not text.strip():
            return [0.0] * self.dim
        counts, signs = self._counts(text)
        if not counts:
            return [0.0] * self.dim
        idf = self._idf
        vector = [0.0] * self.dim
        for index, count in counts.items():
            sign = 1.0 if signs[index] >= 0.0 else -1.0
            if count > 1:
                tf = 1.0 + math.log(count)
            else:
                tf = 1.0
            vector[index] = sign * tf * idf[index]
        norm = math.sqrt(sum(value * value for value in vector))
        if norm <= 0.0:
            return [0.0] * self.dim
        inverse = 1.0 / norm
        # Rounding keeps JSON storage compact and cross-run byte-stable.
        return [round(value * inverse, 6) for value in vector]

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed(text) for text in texts]

    # -- lexical (hybrid retrieval) ----------------------------------
    def _term_weights(self, text: str) -> dict[str, float]:
        """IDF-weighted word/stem feature weights (no char n-grams)."""
        weights: dict[str, float] = {}
        for word in _TOKEN_RE.findall(text.lower()):
            if len(word) < 2 or word in STOPWORDS:
                continue
            for term in {word, _stem(word)}:
                index, _sign = _bucket(term, self.dim)
                weights[term] = self._idf[index]
        return weights

    def binary_similarity(self, left: str, right: str) -> float:
        """IDF-weighted cosine over exact word/stem overlap.

        Fused with the hashed cosine score it gives a light lexical rerank that
        rewards exact rare terms (product names, SKU numbers) without bringing in
        a tokenizer dependency. This is the "hybrid" half of retrieval.
        """
        left_weights = self._term_weights(left)
        if not left_weights:
            return 0.0
        right_weights = self._term_weights(right)
        if not right_weights:
            return 0.0
        shared = left_weights.keys() & right_weights.keys()
        if not shared:
            return 0.0
        numerator = sum(min(left_weights[term], right_weights[term]) for term in shared)
        left_norm = math.sqrt(sum(weight * weight for weight in left_weights.values()))
        right_norm = math.sqrt(sum(weight * weight for weight in right_weights.values()))
        if left_norm <= 0.0 or right_norm <= 0.0:
            return 0.0
        return numerator / (left_norm * right_norm)


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity for two vectors (L2-normalized fast path included)."""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for lvalue, rvalue in zip(left, right):
        dot += lvalue * rvalue
        left_norm += lvalue * lvalue
        right_norm += rvalue * rvalue
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return dot / math.sqrt(left_norm * right_norm)


def split_sentences(text: str) -> list[str]:
    """Sentence-ish splitter shared by the chunker (no NLP dependency)."""
    parts = [part.strip() for part in _SENTENCE_RE.split(text) if part.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def iter_words(text: str) -> Iterable[str]:
    return (match.group(0) for match in _TOKEN_RE.finditer(text.lower()))


__all__ = [
    "Embedder",
    "LocalHashEmbedder",
    "STOPWORDS",
    "cosine_similarity",
    "iter_words",
    "split_sentences",
    "tokenize",
]
