"""Memory services package.

Re-exports the CRUD layer (from :mod:`zara.services.memory.crud`) and the
ingestion/retrieval/personalization/consolidation services implemented across
this package.
"""

from __future__ import annotations

from zara.services.memory.consolidation import consolidate_user
from zara.services.memory.crud import (
    confirm_memory,
    create_memory,
    delete_memory,
    get_memory,
    list_memory,
    list_sources,
    list_suggestions,
    reject_memory,
    search_memory,
    snapshot_revision,
    update_memory,
)
from zara.services.memory.dedup import (
    DedupJudge,
    DedupResult,
    DeterministicJudge,
    check_duplicate_and_contradiction,
)
from zara.services.memory.extract import (
    EntityCandidate,
    Evidence,
    ExtractionCandidate,
    ExtractionPipeline,
    ExtractionResult,
    Extractor,
    HeuristicExtractor,
    Temporal,
    trust_level_for,
)
from zara.services.memory.personalization import PersonalizationService
from zara.services.memory.retrieval import (
    RetrievalRequest,
    RetrievalService,
    RetrievedMemory,
)

__all__ = [
    "DedupJudge",
    "DedupResult",
    "DeterministicJudge",
    "EntityCandidate",
    "Evidence",
    "ExtractionCandidate",
    "ExtractionPipeline",
    "ExtractionResult",
    "Extractor",
    "HeuristicExtractor",
    "PersonalizationService",
    "RetrievalRequest",
    "RetrievalService",
    "RetrievedMemory",
    "Temporal",
    "check_duplicate_and_contradiction",
    "confirm_memory",
    "consolidate_user",
    "create_memory",
    "delete_memory",
    "get_memory",
    "list_memory",
    "list_sources",
    "list_suggestions",
    "reject_memory",
    "search_memory",
    "snapshot_revision",
    "trust_level_for",
    "update_memory",
]
