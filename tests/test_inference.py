"""inference.Embedder over a fake model: its id, empty input, unit-length rows, shared read-only queries, and None
without a model."""

from pathlib import Path

import numpy as np

from orion.inference import Embedder


class FakeModel:
    """model2vec's StaticModel two dimensions wide: every text is (3, 4), an empty one a zero row."""

    dim = 2

    @staticmethod
    def encode(texts: list[str], **_: object) -> np.ndarray:
        """One row per text."""
        return np.array([[3.0, 4.0] if text else [0.0, 0.0] for text in texts])


def test_embedder(tmp_path: Path) -> None:
    """Rows come back unit length in float32, empty texts stay zero, queries are cached read-only, no model is None."""
    emb = Embedder(tmp_path, "minishlab/potion-retrieval-32M", loader=lambda folder, quantize: FakeModel())
    assert emb.id == ""  # neither loaded nor on disk yet: nothing to name
    assert emb.encode([]).shape == (0, 2) and emb.id == "potion-retrieval-32M-int8-v2"
    rows = emb.encode(["red apple", ""])
    assert rows is not None and rows.dtype == np.float32 and np.allclose(rows, [[0.6, 0.8], [0.0, 0.0]])
    query = emb.encode_query("red")
    assert query is not None and not query.flags.writeable and emb.encode_query("red") is query
    assert Embedder(tmp_path, "x/y").encode(["red"]) is None  # no model files: keyword search only
