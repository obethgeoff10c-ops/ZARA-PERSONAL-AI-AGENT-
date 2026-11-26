"""Memory endpoints (07-API.md §4) without graph/entity extras."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status

from zara.api.deps import (
    DbDep,
    PrincipalDep,
    SettingsDep,
    UserRateLimit,
    client_ip,
    parse_if_match,
)
from zara.api.pagination import MAX_PAGE_SIZE
from zara.schemas import (
    ConsolidationOut,
    MemoryCreate,
    MemoryDetail,
    MemoryExtractIn,
    MemoryExtractionRejection,
    MemoryExtractOut,
    MemoryOut,
    MemoryRetrievedOut,
    MemoryRetrieveIn,
    MemoryRetrieveOut,
    MemorySearchIn,
    MemorySourceOut,
    MemoryUpdate,
    Page,
    PersonalizeOut,
)
from zara.services import memory as service
from zara.services.memory.personalization import PersonalizationService
from zara.services.memory.retrieval import RetrievalRequest, RetrievalService

router = APIRouter(prefix="/memory", tags=["memory"])

_Writes = UserRateLimit("memory-write", limit=60)
_Retrievals = UserRateLimit("memory-retrieve", limit=120)


def _out(memory: object, sources_count: int = 0) -> MemoryOut:
    payload = MemoryOut.model_validate(memory)
    return payload.model_copy(update={"sources_count": sources_count})


@router.get("", response_model=Page[MemoryOut], summary="List memory items")
async def list_memory(
    principal: PrincipalDep,
    db: DbDep,
    q: str | None = None,
    kind: str | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = "active",
    pinned: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    cursor: str | None = None,
) -> Page[MemoryOut]:
    rows, next_cursor = await service.list_memory(
        db,
        principal,
        q=q,
        kind=kind,
        status=status_filter,
        pinned=pinned,
        limit=limit,
        cursor=cursor,
    )
    return Page(items=[_out(row) for row in rows], next_cursor=next_cursor)


@router.get(
    "/suggestions",
    response_model=Page[MemoryOut],
    summary="Suggested memories awaiting confirmation",
)
async def suggestions(principal: PrincipalDep, db: DbDep) -> Page[MemoryOut]:
    rows = await service.list_suggestions(db, principal)
    return Page(items=[_out(row) for row in rows])


@router.post(
    "/search",
    response_model=Page[MemoryOut],
    summary="Lexical/full-text memory search",
)
async def search_memory(
    payload: MemorySearchIn, principal: PrincipalDep, db: DbDep
) -> Page[MemoryOut]:
    rows = await service.search_memory(
        db, principal, query=payload.query, kinds=payload.kinds, limit=payload.limit
    )
    return Page(items=[_out(row) for row in rows])


@router.post(
    "/extract",
    response_model=MemoryExtractOut,
    summary="Extract and store memories from a text span",
    dependencies=[Depends(_Writes)],
)
async def extract_memory(
    payload: MemoryExtractIn, principal: PrincipalDep, db: DbDep
) -> MemoryExtractOut:
    pipeline = service.ExtractionPipeline()
    result = await pipeline.extract_and_store(
        db,
        principal,
        text=payload.text,
        source_id=payload.source_id,
        source_type=payload.source_type,
        trust_level=payload.trust_level,
        reference_time=payload.reference_time,
        message_id=payload.message_id,
        run_id=payload.run_id,
    )
    return MemoryExtractOut(
        accepted=[_out(item) for item in result.accepted],
        suggested=[_out(item) for item in result.suggested],
        rejected=[
            MemoryExtractionRejection(reason=entry["reason"], candidate=entry["candidate"])
            for entry in result.rejected
        ],
    )


@router.post(
    "/retrieve",
    response_model=MemoryRetrieveOut,
    summary="Hybrid memory retrieval (semantic + keyword + entity + recency)",
    dependencies=[Depends(_Retrievals)],
)
async def retrieve_memory(
    payload: MemoryRetrieveIn, principal: PrincipalDep, db: DbDep
) -> MemoryRetrieveOut:
    retrieval = RetrievalService()
    items, log_id = await retrieval.retrieve(
        db,
        principal,
        RetrievalRequest(
            query_text=payload.query,
            kinds=payload.kinds,
            domains=payload.domains,
            entity_ids=payload.entity_ids,
            k=payload.k,
            token_budget=payload.token_budget,
            include_history=payload.include_history,
        ),
    )
    return MemoryRetrieveOut(
        items=[MemoryRetrievedOut.model_validate(item) for item in items],
        retrieval_log_id=log_id,
    )


@router.get(
    "/personalize",
    response_model=PersonalizeOut,
    summary="Contextual user profile for personalization",
    dependencies=[Depends(_Retrievals)],
)
async def personalize(
    principal: PrincipalDep,
    db: DbDep,
    context: str | None = Query(default=None, max_length=2000),
) -> PersonalizeOut:
    result = await PersonalizationService().personalize(db, principal, context=context)
    return PersonalizeOut.model_validate(result)


@router.post(
    "/consolidate",
    response_model=ConsolidationOut,
    summary="Run memory decay/archive/expiration for the current user",
    dependencies=[Depends(_Writes)],
)
async def consolidate(principal: PrincipalDep, db: DbDep) -> ConsolidationOut:
    stats = await service.consolidate_user(db, principal)
    return ConsolidationOut(**stats)


@router.post(
    "",
    response_model=MemoryOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a memory item",
    dependencies=[Depends(_Writes)],
)
async def create_memory(
    payload: MemoryCreate,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
    request: Request,
) -> MemoryOut:
    memory = await service.create_memory(
        db,
        principal,
        payload.model_dump(exclude_unset=True),
        ip=client_ip(request, settings),
    )
    return _out(memory)


@router.get("/{memory_id}", response_model=MemoryDetail, summary="Memory detail with sources")
async def get_memory(memory_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> MemoryDetail:
    memory = await service.get_memory(db, principal, memory_id)
    sources = await service.list_sources(db, principal, memory_id)
    base = _out(memory, sources_count=len(sources))
    return MemoryDetail(
        **base.model_dump(),
        sources=[MemorySourceOut.model_validate(source) for source in sources],
    )


@router.patch("/{memory_id}", response_model=MemoryOut, summary="Edit a memory item")
async def update_memory(
    memory_id: uuid.UUID,
    payload: MemoryUpdate,
    request: Request,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
) -> MemoryOut:
    memory = await service.update_memory(
        db,
        principal,
        memory_id,
        payload.model_dump(exclude_unset=True),
        if_match_version=parse_if_match(request),
        ip=client_ip(request, settings),
    )
    return _out(memory)


@router.delete(
    "/{memory_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete or block a memory item",
)
async def delete_memory(
    memory_id: uuid.UUID,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
    request: Request,
    block: bool = False,
) -> Response:
    await service.delete_memory(
        db, principal, memory_id, block=block, ip=client_ip(request, settings)
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{memory_id}/confirm", response_model=MemoryOut, summary="Confirm a suggestion")
async def confirm_memory(
    memory_id: uuid.UUID,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
    request: Request,
) -> MemoryOut:
    memory = await service.confirm_memory(db, principal, memory_id, ip=client_ip(request, settings))
    return _out(memory)


@router.post(
    "/{memory_id}/reject",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Reject a suggested memory",
)
async def reject_memory(
    memory_id: uuid.UUID,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
    request: Request,
) -> Response:
    await service.reject_memory(db, principal, memory_id, ip=client_ip(request, settings))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
