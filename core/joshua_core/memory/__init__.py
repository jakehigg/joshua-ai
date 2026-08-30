"""In-core RAG over the data volume: chunker, embedder, indexer, and search.

The indexer drives pluggable source adapters (``files`` is the kernel) into the
person-scoped ``kb_chunk`` pgvector table; ``search`` answers "top-k chunks for
this text, visible to this person". The ``recall`` builtin tool exposes it to the
agent.
"""
