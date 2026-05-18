"""Semantic search over the corpus using a static (model2vec) embedding model.

No transformer forward pass at query time — the model is a static lookup +
mean pooling, so a query embeds in milliseconds on CPU. The corpus matrix is
loaded once at startup from search.db (already L2-normalized at build time),
so cosine similarity is a single dense matrix-vector product.

Degrades gracefully: if the model dir or embeddings are missing, `ready` is
False and the caller falls back to BM25-only.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class EmbeddingIndex:
    """Static-embedding semantic index loaded from search.db."""

    def __init__(self, db_path: str, model_path: str):
        self._model = None
        self._ids = None        # np.ndarray[int64] (n,)
        self._matrix = None     # np.ndarray[float32] (n, dim)
        self.ready = False
        self.status = "not initialized"
        self._load(db_path, model_path)

    def _load(self, db_path: str, model_path: str):
        if not Path(model_path).is_dir():
            self.status = f"model dir missing: {model_path}"
            logger.warning("EmbeddingIndex disabled: %s", self.status)
            return
        try:
            import numpy as np  # noqa: F401
            from model2vec import StaticModel

            from app.search.fts import SearchIndex

            self._model = StaticModel.from_pretrained(model_path, normalize=True)

            idx = SearchIndex(db_path, read_only=True)
            try:
                ids, matrix = idx.load_embedding_matrix()
            finally:
                idx.close()

            if ids is None or matrix is None or len(ids) == 0:
                self.status = "no embeddings in DB (run build_index --embeddings-only)"
                logger.warning("EmbeddingIndex disabled: %s", self.status)
                return

            self._ids = ids
            self._matrix = matrix
            self.ready = True
            self.status = f"ready ({len(ids)} vectors, dim={matrix.shape[1]})"
            logger.info("EmbeddingIndex %s", self.status)
        except Exception as e:  # pragma: no cover - defensive
            self.status = f"load failed: {e}"
            logger.warning("EmbeddingIndex disabled: %s", self.status)
            self._model = None
            self._ids = None
            self._matrix = None
            self.ready = False

    def _encode_query(self, query: str):
        import numpy as np

        vec = self._model.encode(
            [query],
            use_multiprocessing=False,
        ).astype(np.float32)[0]
        n = np.linalg.norm(vec)
        if n > 0:
            vec = vec / n
        return vec

    def search(self, query: str, limit: int = 50) -> list[tuple[int, float]]:
        """Return [(doc_id, cosine_score)] sorted by score desc.

        Synchronous and CPU-bound but fast (static model + one matmul over
        ~few-thousand rows). Call via asyncio.to_thread from async code.
        """
        if not self.ready or not query.strip():
            return []
        import numpy as np

        qvec = self._encode_query(query)
        scores = self._matrix @ qvec  # (n,) cosine, vectors pre-normalized
        k = min(limit, scores.shape[0])
        # argpartition for top-k, then sort just those
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [(int(self._ids[i]), float(scores[i])) for i in top]
