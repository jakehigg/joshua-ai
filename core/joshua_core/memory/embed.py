"""Text embedding — the ONLY module that touches the embedding model.

Local, in-pod embeddings via fastembed (ONNX runtime, no torch). Text
never leaves the cluster and there is no per-call cost. Kept behind one module
so the provider can change without touching callers.

The import is lazy and guarded: compile checks and offline tests run without
fastembed installed, in which case ``embed()`` returns None and retrieval
degrades to an "unavailable" message rather than crashing.

``EMBED_DIM`` must match the ``vector(384)`` column in the ``kb_chunk`` schema —
do not change the dimension without re-embedding every row.

The model cache directory is tested before the model is built. A directory that
the process cannot write degrades to a temporary one, so a wrong mount costs a
download on each start and not the whole memory.
"""

from __future__ import annotations

import os
import tempfile
import threading

from joshua_shared.log import get_logger

logger = get_logger("memory.embed")

# Must match the vector(N) dimension baked into the kb_chunk schema.
# BAAI/bge-small-en-v1.5 → 384 dims.
EMBED_DIM = 384

_lock = threading.Lock()
_model = None  # fastembed.TextEmbedding | None
_unavailable = False  # set once if fastembed can't be loaded, to stop retrying


def is_available() -> bool:
    """True while the model can still be used. False once a load has failed.

    The answer needs no load, so a probe route can call it on each request. A
    model that is not tried yet reads as available, because nothing is wrong
    yet. No name, no path, and no secret leave this function.
    """
    return not _unavailable


def _is_writable(path: str) -> bool:
    """True when ``path`` exists or can be made, and accepts a new file.

    A probe file is used, not ``os.access``. ``os.access`` reads the mode bits
    and gives a wrong answer on an NFS export with root squash, and with an
    ACL that the mode bits do not show.
    """
    probe = os.path.join(path, ".joshua-write-probe")
    try:
        os.makedirs(path, exist_ok=True)
        with open(probe, "w"):
            pass
        return True
    except OSError:
        return False
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass


def _usable_cache_dir(configured: str | None) -> str | None:
    """Return a writable model cache directory, or None to let fastembed choose.

    A deployment can point MEMORY_EMBED_CACHE_DIR at a directory that the
    container user cannot write: a k8s subPath that arrives root-owned, a bind
    mount, an NFS export, or a rootless host where the uid does not map. The
    model download then fails, so every embedding fails, and the agent keeps
    answering with no memory. A temporary directory keeps the memory alive.
    The model is downloaded again on each start, which is the cost of the wrong
    mount, so the warning names both paths.
    """
    if not configured:
        return None
    if _is_writable(configured):
        return configured
    fallback = os.path.join(tempfile.gettempdir(), "joshua-fastembed")
    if not _is_writable(fallback):
        logger.warning(
            {
                "message": "embed cache dir not writable, and no fallback",
                "configured": configured,
                "fallback": fallback,
            }
        )
        return None
    logger.warning(
        {
            "message": "embed cache dir not writable, using a temporary directory",
            "configured": configured,
            "fallback": fallback,
        }
    )
    return fallback


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
            # so it survives restarts instead of re-downloading each time. An
            # unwritable directory degrades to a temporary one, not to no memory.
            cache_dir = _usable_cache_dir(os.environ.get("MEMORY_EMBED_CACHE_DIR") or None)
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
