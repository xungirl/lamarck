"""A small, dependency-free BM25 index for knowledge-base search.

BM25 score of document d for query q:
    sum over terms t in q:  idf(t) * tf(t,d) * (k1 + 1) / (tf(t,d) + k1 * (1 - b + b * |d| / avgdl))
with idf(t) = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5)).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are as at be by can do for from how i if in is it my of on or the to was what when where "  # noqa: SIM905
    "which who will with you your".split()
)


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS]


@dataclass(frozen=True)
class Hit:
    doc_id: str
    score: float


class BM25Index:
    def __init__(self, docs: dict[str, str], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self._tf: dict[str, Counter[str]] = {doc_id: Counter(tokenize(text)) for doc_id, text in docs.items()}
        self._len = {doc_id: sum(tf.values()) for doc_id, tf in self._tf.items()}
        self._avgdl = (sum(self._len.values()) / len(self._len)) if self._len else 0.0
        df: Counter[str] = Counter()
        for tf in self._tf.values():
            df.update(tf.keys())
        n = len(self._tf)
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, limit: int = 5) -> list[Hit]:
        terms = tokenize(query)
        hits: list[Hit] = []
        for doc_id, tf in self._tf.items():
            norm = self.k1 * (1 - self.b + self.b * self._len[doc_id] / (self._avgdl or 1.0))
            score = sum(self._idf[t] * tf[t] * (self.k1 + 1) / (tf[t] + norm) for t in terms if t in tf)
            if score > 0:
                hits.append(Hit(doc_id, score))
        hits.sort(key=lambda h: (-h.score, h.doc_id))
        return hits[:limit]
