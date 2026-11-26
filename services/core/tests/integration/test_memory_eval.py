"""Memory-evaluation acceptance suite over the committed datasets.

Runs the real HTTP API against PostgreSQL/pgvector and scores the pipeline on
the labeled datasets in ``evals/datasets/memory/`` (03-MEMORY-ENGINE §11).
No assistant answer is hardcoded: every metric is computed from rows the
pipeline actually wrote and retrieved.

Run with ``ZARA_INTEGRATION=1`` like the rest of the integration suite.
"""

from __future__ import annotations

import json
import math
import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from zara.api.app import create_app
from zara.config import Settings, get_settings
from zara.db.engine import create_engine, dispose_engine

from helpers import unique_email, unique_org

pytestmark = pytest.mark.integration

_DATASETS = Path(__file__).resolve().parents[4] / "evals" / "datasets" / "memory"


@pytest.fixture
def settings() -> Settings:
    """Disable the per-user limiter: the eval intentionally issues hundreds of
    writes/reads for one user in a single window."""
    return get_settings().model_copy(update={"redis_url": None, "rate_limit_enabled": False})


def _load_jsonl(name: str) -> list[dict[str, Any]]:
    with (_DATASETS / name).open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _load_json(name: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((_DATASETS / name).read_text(encoding="utf-8"))
    return body


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _client(settings: Settings) -> AsyncClient:
    app = create_app(settings)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


async def _token(client: AsyncClient) -> str:
    response = await client.post(
        "/api/v1/auth/dev-token",
        json={"email": unique_email("eval"), "org_id": unique_org()},
    )
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


async def _identity(client: AsyncClient, token: str) -> tuple[uuid.UUID, uuid.UUID]:
    response = await client.get("/api/v1/me", headers=_auth(token))
    assert response.status_code == 200, response.text
    body = response.json()
    return uuid.UUID(body["tenant_id"]), uuid.UUID(body["id"])


async def _extract(
    client: AsyncClient,
    token: str,
    text_value: str,
    *,
    source_id: str,
    trust_level: str = "user",
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/memory/extract",
        headers=_auth(token),
        json={"text": text_value, "source_id": source_id, "trust_level": trust_level},
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _fetch_rows(
    tenant_id: uuid.UUID, user_id: uuid.UUID, statement: str, params: dict[str, Any]
) -> list[dict[str, Any]]:
    engine = create_engine()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "SELECT set_config('app.tenant_id', :tenant_id, true),"
                    " set_config('app.user_id', :user_id, true),"
                    " set_config('app.role', 'service', true)"
                ),
                {"tenant_id": str(tenant_id), "user_id": str(user_id)},
            )
            result = await connection.execute(text(statement), params)
            return [dict(row._mapping) for row in result]
    finally:
        await engine.dispose()


# --- extraction -------------------------------------------------------------


async def test_extraction_precision_recall_and_verbatim_evidence(
    settings: Settings,
) -> None:
    rows = _load_jsonl("extraction.jsonl")
    assert len(rows) >= 300
    await dispose_engine()
    async with await _client(settings) as client:
        token = await _token(client)
        tenant_id, user_id = await _identity(client, token)

        emitted = 0
        correct = 0
        for row in rows:
            body = await _extract(
                client,
                token,
                row["text"],
                source_id=row["id"],
                trust_level=row["trust_level"],
            )
            items = [*body["accepted"], *body["suggested"]]
            for item in items:
                emitted += 1
                if (
                    item["content"] == row["expected_quote"]
                    and item["kind"] == row["expected_kind"]
                ):
                    correct += 1

        precision = correct / emitted if emitted else 0.0
        recall = correct / len(rows)
        assert precision >= 0.85, f"precision {precision:.3f}"
        assert recall >= 0.70, f"recall {recall:.3f}"

        # Anti-hallucination: every stored quote must be verbatim source text.
        sources = await _fetch_rows(
            tenant_id,
            user_id,
            "SELECT mi.content, ms.quote FROM memory.memory_items mi"
            " JOIN memory.memory_sources ms ON ms.memory_id = mi.id"
            " WHERE mi.user_id = :user_id",
            {"user_id": user_id},
        )
        assert sources, "extraction should have written provenance rows"
        expected_by_content = {row["text"]: row["text"] for row in rows}
        hallucinated = [
            source
            for source in sources
            if source["quote"] is None
            or source["quote"] not in expected_by_content.get(source["content"], "")
        ]
        assert hallucinated == [], f"hallucinated evidence: {hallucinated[:3]}"
    await dispose_engine()


# --- poisoning --------------------------------------------------------------


async def test_poisoning_never_persists_disallowed_kinds(settings: Settings) -> None:
    rows = _load_jsonl("poisoning.jsonl")
    assert len(rows) >= 80
    await dispose_engine()
    async with await _client(settings) as client:
        token = await _token(client)
        tenant_id, user_id = await _identity(client, token)

        for row in rows:
            body = await _extract(
                client,
                token,
                row["text"],
                source_id=row["id"],
                trust_level=row["trust_level"],
            )
            for item in [*body["accepted"], *body["suggested"]]:
                assert item["kind"] not in row["disallowed_kinds"], (row, item)

        persisted = await _fetch_rows(
            tenant_id,
            user_id,
            "SELECT kind, status FROM memory.memory_items"
            " WHERE user_id = :user_id AND status IN ('active','suggested')"
            " AND kind IN ('preference','procedural','goal')",
            {"user_id": user_id},
        )
        assert persisted == []
    await dispose_engine()


# --- contradictions ---------------------------------------------------------


async def test_contradiction_resolution_supersedes_old_claim(settings: Settings) -> None:
    rows = _load_jsonl("contradiction.jsonl")
    assert len(rows) >= 60
    await dispose_engine()
    async with await _client(settings) as client:
        token = await _token(client)

        for row in rows:
            old = await _extract(client, token, row["old_text"], source_id=f"{row['id']}-old")
            assert old["accepted"], (row, old)
            old_id = old["accepted"][0]["id"]

            new = await _extract(client, token, row["new_text"], source_id=f"{row['id']}-new")
            assert new["accepted"], (row, new)
            new_id = new["accepted"][0]["id"]

            old_detail = await client.get(f"/api/v1/memory/{old_id}", headers=_auth(token))
            new_detail = await client.get(f"/api/v1/memory/{new_id}", headers=_auth(token))
            assert old_detail.json()["status"] == "superseded", row
            assert new_detail.json()["status"] == "active", row
    await dispose_engine()


# --- deletion / negative memory --------------------------------------------


async def test_deletion_blocks_recreation_and_retrieval(settings: Settings) -> None:
    rows = _load_jsonl("deletion.jsonl")
    assert len(rows) >= 20
    await dispose_engine()
    async with await _client(settings) as client:
        token = await _token(client)

        for row in rows:
            created = await _extract(client, token, row["text"], source_id=f"{row['id']}-a")
            assert created["accepted"], (row, created)
            memory_id = created["accepted"][0]["id"]

            deleted = await client.delete(
                f"/api/v1/memory/{memory_id}", headers=_auth(token), params={"block": "true"}
            )
            assert deleted.status_code == 204, deleted.text

            retrieved = await client.post(
                "/api/v1/memory/retrieve",
                headers=_auth(token),
                json={"query": row["query"]},
            )
            assert retrieved.status_code == 200
            assert all(item["id"] != memory_id for item in retrieved.json()["items"])

            again = await _extract(client, token, row["text"], source_id=f"{row['id']}-b")
            assert again["accepted"] == []
            assert again["suggested"] == []
            assert any(entry["reason"] == "blocked_fact" for entry in again["rejected"]), row
    await dispose_engine()


# --- retrieval --------------------------------------------------------------


async def test_retrieval_recall_and_ndcg(settings: Settings) -> None:
    corpus = _load_jsonl("retrieval_corpus.jsonl")
    queries = _load_jsonl("retrieval.jsonl")
    assert len(corpus) >= 200
    assert len(queries) >= 200
    await dispose_engine()
    async with await _client(settings) as client:
        token = await _token(client)

        for row in corpus:
            body = await _extract(client, token, row["text"], source_id=row["id"])
            assert body["accepted"], (row, body)

        hits = 0
        dcg = 0.0
        for row in queries:
            response = await client.post(
                "/api/v1/memory/retrieve",
                headers=_auth(token),
                json={"query": row["query"], "k": 12},
            )
            assert response.status_code == 200, response.text
            items = response.json()["items"]
            rank = next(
                (
                    index
                    for index, item in enumerate(items, start=1)
                    if row["relevant_marker"] in item["content"]
                ),
                None,
            )
            if rank is not None:
                hits += 1
                dcg += 1.0 / math.log2(rank + 1)

        recall = hits / len(queries)
        ndcg = dcg / len(queries)
        assert recall >= 0.85, f"recall@12 {recall:.3f}"
        assert ndcg >= 0.75, f"nDCG@12 {ndcg:.3f}"
    await dispose_engine()


# --- acceptance conversation ------------------------------------------------


async def test_acceptance_conversation_across_restart(settings: Settings) -> None:
    scenario = _load_json("acceptance_conversation.json")
    await dispose_engine()
    async with await _client(settings) as client:
        token = await _token(client)
        for message in scenario["messages"]:
            body = await _extract(client, token, message["text"], source_id=message["id"])
            assert body["accepted"], (message, body)

    # Simulate a process restart before asking the combining question.
    await dispose_engine()
    async with await _client(settings) as client:
        response = await client.post(
            "/api/v1/memory/retrieve",
            headers=_auth(token),
            json={"query": scenario["query"]},
        )
        assert response.status_code == 200, response.text
        items = response.json()["items"]
    await dispose_engine()

    contents = [item["content"] for item in items]
    joined = " ".join(contents).lower()
    for phrase in scenario["required_phrases"]:
        assert phrase in joined, (phrase, joined)
    assert sum(1 for content in contents if "Lisbon" in content) >= scenario["min_lisbon_matches"]
    for phrase in scenario["forbidden_phrases"]:
        assert phrase not in joined, (phrase, joined)

    corpus = " ".join(message["text"] for message in scenario["messages"])
    for item in items:
        assert item["sources"], item
        for source in item["sources"]:
            assert source["quote"] in corpus, (source, corpus)
