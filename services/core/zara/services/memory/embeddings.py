"""Embedding generation and storage.

No embedding provider is configured yet, so this module derives a stable
1024-dimension pseudo-embedding from SHA-256. That keeps the pgvector column,
HNSW index, cosine queries, and the whole retrieval pipeline fully exercised in
tests and local development. When a real provider is wired in, replace
:func:`embed_text` with the provider call; the storage path stays identical.
"""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from zara.models import MemoryEmbedding

EMBEDDING_DIM = 1024
EMBEDDING_MODEL = "sha256-pseudo-v1"
EMBEDDING_VERSION = 1


def embed_text(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """Deterministic pseudo-embedding derived from SHA-256.

    Deterministic so duplicate detection and retrieval ranking are reproducible
    across process restarts without an external model.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [(digest[i % len(digest)] - 128) / 128.0 for i in range(dim)]


async def store_current_embedding(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    memory_id: uuid.UUID,
    text: str,
) -> MemoryEmbedding:
    """Persist a new current embedding, retiring any previous current row."""
    await db.execute(
        update(MemoryEmbedding)
        .where(
            MemoryEmbedding.memory_id == memory_id,
            MemoryEmbedding.is_current.is_(True),
        )
        .values(is_current=False)
    )
    embedding = MemoryEmbedding(
        tenant_id=tenant_id,
        user_id=user_id,
        memory_id=memory_id,
        embedding=embed_text(text),
        embedding_model=EMBEDDING_MODEL,
        embedding_version=EMBEDDING_VERSION,
        is_current=True,
    )
    db.add(embedding)
    await db.flush()
    return embedding
