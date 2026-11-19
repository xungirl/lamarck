"""Optional neural embedder for SIM retrieval: BAAI/bge-small-en-v1.5, run locally (no API calls).

Install with `uv sync --extra retrieval`. The model files are expected under runs/models/ (a local
copy of the Hugging Face snapshot); without them retrieval falls back to the hashed-trigram embedder.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from . import retrieval

MODEL_DIR = Path(__file__).resolve().parents[2] / "runs" / "models" / "bge-small-en-v1.5"


def enable_bge(model_dir: Path = MODEL_DIR) -> bool:
    """Swap the retrieval embedder for bge-small if it can be loaded; return whether it was."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return False
    if not model_dir.exists():
        return False
    model = SentenceTransformer(str(model_dir), device="cpu")

    def embed(texts: object) -> np.ndarray:
        vecs = model.encode(list(texts), normalize_embeddings=True, convert_to_numpy=True)  # type: ignore[call-overload]
        return np.asarray(vecs, dtype=np.float32)

    retrieval.set_embedder(embed, "BAAI/bge-small-en-v1.5")
    return True
