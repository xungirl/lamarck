"""Research extension: retrieve the most relevant SIM entries instead of showing all of them.

The paper gives the proposer the complete SIM "without retrieval or truncation".
That stops scaling once memory accumulates over many runs. This module ranks
problem entries against the current failures and keeps the top-k that fit a
token budget:

  * lexical: BM25 (k1 = 1.2, b = 0.75) over each entry's root cause + attempt descriptions
  * dense:   cosine similarity of embeddings. By default a dependency-free hashed
             character-trigram embedding; `set_embedder()` swaps in a neural model
             (the experiments use BAAI/bge-small-en-v1.5 when it is available locally)
  * fusion:  reciprocal-rank fusion, RRF(d) = sum over rankers of 1 / (60 + rank)

The retrieved memory keeps the original ids, aliases and provenance, so citations
made from it resolve against the full record when the run updates SIM.
"""

from __future__ import annotations

import copy
import hashlib
import math
import re
from collections import Counter
from collections.abc import Callable, Sequence

import numpy as np

from .memory import SIM, Problem
from .prompts import approx_tokens, render

Embedder = Callable[[Sequence[str]], np.ndarray]
_TOKEN = re.compile(r"[a-z0-9_]+")
_STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "to",
        "in",
        "on",
        "for",
        "and",
        "or",
        "is",
        "are",
        "was",
        "be",
        "with",
        "by",
        "as",
        "at",
        "it",
        "this",
        "that",
        "from",
        "not",
        "no",
    ]
)


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP and len(t) > 1]


def entry_text(p: Problem) -> str:
    parts = [p.name, p.name]  # the root cause counts twice
    for a in p.attempts:
        parts.append(f"{a.what} {a.files} {' '.join(a.missed)}")
    return " ".join(parts)


def bm25_scores(docs: list[list[str]], query: list[str], k1: float = 1.2, b: float = 0.75) -> list[float]:
    n = len(docs)
    if n == 0:
        return []
    avgdl = sum(len(d) for d in docs) / n or 1.0
    df: Counter[str] = Counter()
    for d in docs:
        df.update(set(d))
    out = []
    for d in docs:
        tf = Counter(d)
        s = 0.0
        for q in set(query):
            if q not in tf:
                continue
            idf = math.log(1 + (n - df[q] + 0.5) / (df[q] + 0.5))
            s += idf * tf[q] * (k1 + 1) / (tf[q] + k1 * (1 - b + b * len(d) / avgdl))
        out.append(s)
    return out


def hashed_trigram_embed(texts: Sequence[str], dim: int = 512) -> np.ndarray:
    """A deterministic bag of hashed character trigrams, L2-normalized."""
    m = np.zeros((len(texts), dim), dtype=np.float32)
    for i, t in enumerate(texts):
        s = f"  {t.lower()}  "
        for j in range(len(s) - 2):
            h = int(hashlib.md5(s[j : j + 3].encode()).hexdigest()[:8], 16)
            m[i, h % dim] += 1.0
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.where(norms == 0, 1.0, norms)


_embedder: Embedder = hashed_trigram_embed
_embedder_name = "hashed-char-trigram-512"


def set_embedder(fn: Embedder, name: str) -> None:
    global _embedder, _embedder_name
    _embedder, _embedder_name = fn, name


def embedder_name() -> str:
    return _embedder_name


def rank(sim: SIM, query: str) -> list[tuple[str, float]]:
    """(problem id, fused score), best first."""
    if not sim.problems:
        return []
    texts = [entry_text(p) for p in sim.problems]
    lex = bm25_scores([tokenize(t) for t in texts], tokenize(query))
    vecs = _embedder([query, *texts])
    dense = (vecs[1:] @ vecs[0]).tolist()
    fused: dict[str, float] = {p.id: 0.0 for p in sim.problems}
    for scores, need_positive in ((lex, True), (dense, False)):
        # a lexical ranker only votes for documents that share at least one query term
        order = sorted(
            (i for i in range(len(scores)) if scores[i] > 0 or not need_positive),
            key=lambda i: (-scores[i], i),
        )
        for r, i in enumerate(order):
            fused[sim.problems[i].id] += 1.0 / (60 + r + 1)
    return sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))


def retrieve(sim: SIM, query: str, top_k: int, token_budget: int) -> SIM:
    """The top-k entries for `query` whose rendering fits `token_budget` (greedy, best first)."""
    out = copy.deepcopy(sim)
    keep: list[Problem] = []
    by_id = {p.id: p for p in sim.problems}
    for pid, _score in rank(sim, query):
        if len(keep) >= top_k:
            break
        trial = copy.deepcopy(out)
        trial.problems = [*keep, by_id[pid]]
        if keep and approx_tokens(render(trial, "full")) > token_budget:
            continue
        keep.append(by_id[pid])
    out.problems = [copy.deepcopy(p) for p in keep]
    return out
