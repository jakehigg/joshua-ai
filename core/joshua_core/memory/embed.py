"""Text embedding — the ONLY module that touches the embedding model.

Local, in-pod embeddings via fastembed (ONNX runtime, no torch). Text
never leaves the cluster and there is no per-call cost. Kept behind one module
so the provider can change without touching callers.

The import is lazy and guarded: compile checks and offline tests run without
fastembed installed, in which case ``embed()`` returns None and retrieval
degrades to an "unavailable" message rather than crashing.

``EMBED_DIM`` must match the ``vector(384)`` column in the ``kb_chunk`` schema —
do not change the dimension without re-embedding every row.
"""

from __future__ import annotations

import os
import threading

from joshua_shared.log import get_logger

logger = get_logger("memory.embed")

# Must match the vector(N) dimension baked into the kb_chunk schema.
# BAAI/bge-small-en-v1.5 → 384 dims.
EMBED_DIM = 384

_lock = threading.Lock()
_model = None  # fastembed.TextEmbedding | None
_unavailable = False  # set once if fastembed can't be loaded, to stop retrying


def _get_model(model_name: str):
    global _model, _unavailable
    if _model is not None or _unavailable:
        return _model
    with _lock:
        if _model is not None or _unavailable:
            return _model
        try:
            from fastembed import TextEmbedding  # type: ignore

            # Persist the ONNX model on the embed cache dir (MEMORY_EMBED_CACHE_DIR)
            # so it survives restarts instead of re-downloading each time.
            cache_dir = os.environ.get("MEMORY_EMBED_CACHE_DIR") or None
            _model = TextEmbedding(model_name=model_name, cache_dir=cache_dir)
            logger.info(
                {"message": "embedding model loaded", "model": model_name, "cache_dir": cache_dir}
            )
        except Exception as e:  # noqa: BLE001 — any failure → disable, don't crash
            _unavailable = True
            logger.warning({"message": "embedding unavailable", "error": str(e)})
    return _model


def embed(text: str, model_name: str = "BAAI/bge-small-en-v1.5") -> list[float] | None:
    """Embed one string. Returns None if the model can't be loaded.

    NOTE: synchronous and CPU-bound — call from a worker thread
    (``asyncio.to_thread``) on the async hot path.
    """
    text = (text or "").strip()
    if not text:
        return None
    model = _get_model(model_name)
    if model is None:
        return None
    try:
        vec = next(iter(model.embed([text])))
        return [float(x) for x in vec]
    except Exception as e:  # noqa: BLE001
        logger.warning({"message": "embed failed", "error": str(e)})
        return None


def warmup(model_name: str = "BAAI/bge-small-en-v1.5") -> bool:
    """Eagerly load the model and run one throwaway embedding, so the model is
    pulled on pod start rather than lazily on the first reconcile (which
    otherwise pays a multi-second cold-start cost on a fresh pod).

    Best-effort: a failure flips embedding to unavailable (``embed()`` returns
    None) but must NOT raise — the pod still serves and returns graceful
    "unavailable" responses rather than crash-looping.
    """
    ok = embed("warmup", model_name) is not None
    logger.info({"message": "embedding warmup complete", "ok": ok, "model": model_name})
    return ok


def to_pgvector(vec: list[float]) -> str:
    """Render an embedding as a pgvector text literal for ``%s::vector`` params."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"
