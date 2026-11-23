# 03 — Memory Engine

Single memory system; all kinds live in `memory.memory_items` (see 02 §2.4). "Working memory" is the only non-persisted kind.

## 1. Memory kinds

| Kind         | Persisted                                              | Meaning                                   | Typical payload keys                            | Default expiry                                      |
| ------------ | ------------------------------------------------------ | ----------------------------------------- | ----------------------------------------------- | --------------------------------------------------- |
| working      | No (run state; summary in `agent_runs.state_snapshot`) | Current run scratchpad                    | —                                               | End of run                                          |
| episodic     | Yes                                                    | Something that happened at a time         | `event_type, participants[], location, outcome` | 180 d decay; kept if linked to open commitment/deal |
| semantic     | Yes                                                    | Durable fact about the world/user         | `subject, predicate, object`                    | None (supersede)                                    |
| preference   | Yes                                                    | How the user likes things                 | `category, rule, strength`                      | None; reviewed at 365 d                             |
| goal         | Yes (mirrors `work.goals`)                             | Intent to achieve                         | `goal_id, metric, target_date`                  | Until achieved/abandoned                            |
| relationship | Yes                                                    | Facts about a person/company relationship | `entity_id, relation, tone, last_topics[]`      | Decay 365 d                                         |
| project      | Yes                                                    | Project state/decisions                   | `project_id, decision, status, owner`           | Until project archived                              |
| commitment   | Yes (mirrors a `work.tasks` row)                       | Promise by/to the user                    | `direction (by_user                             | to_user), counterparty_entity_id, due_at, status`   | Until done/cancelled; overdue kept |
| procedural   | Yes                                                    | "How to act" rules                        | `trigger, action, scope`                        | None; **user-confirmed only**                       |

Rules: `procedural` and `preference` can only be created with `trust_level IN ('user','zara')` and, for `procedural`, only via explicit user confirmation (UI or chat "yes, remember that"). `goal`/`commitment` items are linked to `work.goals`/`work.tasks`; the memory is the narrative index, the table is the source of truth for status.

## 2. Extraction

### 2.1 Triggers

1. End of every `AgentRun` (conversation turns and background runs).
2. After a document is ingested (email thread, calendar event, PR) — batched per thread/event within a 60 s window.
3. User action: "Remember this" button or chat instruction → direct write (`created_by='user'`, confidence 1.0).
4. Nightly consolidation (§6).

### 2.2 Pipeline (module `memory/extract.py`)

```
input (text span + metadata + trust_level)
  → quarantined extractor LLM (role=extractor, NO tools, schema-constrained output)
  → ExtractionResult (candidates[])
  → gates (provenance, trust, sensitivity, secrets, schema validity)
  → entity linking (graph.resolution)
  → dedup/contradiction check (§4, §5)
  → write: active | suggested | rejected
```

### 2.3 Extraction schema (Pydantic / JSON schema given to LLM)

```json
{
  "candidates": [
    {
      "kind": "semantic|episodic|preference|goal|relationship|project|commitment|procedural",
      "title": "string ≤ 80 chars",
      "content": "one self-contained sentence in third person about the user's world",
      "payload": { "...kind-specific..." },
      "entities": [{"surface": "Priya Nair", "type": "person", "role": "subject|object|mentioned"}],
      "temporal": {
        "occurred_at": "ISO8601|null",
        "valid_from": "ISO8601|null",
        "valid_to": "ISO8601|null",
        "due_at": "ISO8601|null",
        "expires_hint": "ephemeral|short|medium|long|permanent"
      },
      "confidence": 0.0,
      "importance": 0.0,
      "sensitivity": "normal|sensitive|restricted",
      "evidence": [{"source_id": "string", "quote": "≤ 200 chars verbatim", "span": [0,0]}],
      "canonical_key": "string|null (e.g. 'pref:meeting_time', 'fact:priya_nair:title')",
      "reasoning": "≤ 200 chars"
    }
  ]
}
```

Prompt constraints: only extract what is explicitly supported by the evidence; never infer protected attributes; never extract credentials/secrets/payment card data; resolve relative dates against the `reference_time` and user timezone provided; return an empty list if nothing durable.

### 2.4 Gates

| Gate                 | Rule                                                                                                                                                                                                               | Outcome                          |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------- |
| Schema               | Pydantic validation; unknown kind rejected                                                                                                                                                                         | reject                           |
| Provenance           | ≥1 evidence entry; quote must be a substring of source text (verbatim check)                                                                                                                                       | else reject (anti-hallucination) |
| Trust                | `external_untrusted` sources → kind ∈ {episodic, semantic, relationship, commitment(to_user), project} only; **never** preference/procedural/goal; content is rewritten as an _observation_ ("Priya wrote that …") | reject disallowed kinds          |
| Injection heuristics | Candidate content containing imperative addressed to the assistant, URLs to unknown hosts, or phrases matching injection corpus → drop + log `security.flag`                                                       | reject                           |
| Sensitivity          | classifier tags; `restricted` (health, government ID, financial account) → `status='suggested'` always                                                                                                             | route to inbox                   |
| Secrets              | regex + entropy detector (API keys, tokens, card numbers, passwords)                                                                                                                                               | reject                           |
| Confidence           | ≥0.75 → `active`; 0.5–0.75 → `suggested`; <0.5 → drop                                                                                                                                                              |                                  |
| Importance floor     | importance <0.2 and kind=episodic → drop                                                                                                                                                                           |                                  |

### 2.5 Scores

- **confidence** (0–1): extractor self-estimate × evidence-quality factor (verbatim match=1.0; paraphrase-only=0.85) × source-authority factor (user chat=1.0, user-authored doc=0.95, calendar/CRM structured=0.95, third-party email=0.7, web=0.5).
- **importance** (0–1): extractor estimate blended deterministically: `0.5·llm + 0.2·kind_prior + 0.2·entity_prominence + 0.1·recency_of_source`. `kind_prior`: commitment 0.8, goal 0.8, preference 0.6, relationship 0.6, project 0.6, semantic 0.5, episodic 0.3.
- Both stored; both user-visible in memory UI.

## 3. Source attribution

Every non-user memory has ≥1 `memory_sources` row with `document_id|message_id|run_id` + `quote` + `observed_at`. UI shows "Because: <source title>, <date>" and links to the source. Retrieval logs record which memories were injected into which run (`memory.retrieval_logs`), enabling "why was this used?".

## 4. Deduplication

1. Compute `content_hash = sha256(normalize(content))` → exact duplicate? add a new `memory_sources` row; bump `last_seen`; no new item.
2. If `canonical_key` present and an _active_ item with the same key exists → compare (§5).
3. Else vector-search top-5 active items of same kind + overlapping entities with cosine ≥ **0.90** → LLM judge (role=extractor, small model) returns `{relation: same|refines|contradicts|unrelated}`.
   - `same` → add source only.
   - `refines` → create revision on existing item (new content, `memory_revisions` row), `confidence=max`.
   - `contradicts` → §5.
   - `unrelated` → insert new.
4. Cosine 0.80–0.90 with same entity set → mark `possible_duplicate` for nightly consolidation (no judge cost now).

## 5. Contradiction resolution

Applied when a candidate contradicts an existing active item `E`.

Precedence (first rule that decides wins):

1. `E.user_edited = true` or `E.user_pinned = true` → candidate becomes `suggested` (never overrides user truth). UI shows "ZARA saw conflicting info".
2. Candidate source is the user (chat/explicit) → candidate wins; `E.status='superseded'`, `E.valid_to=now()`, candidate `supersedes_id=E.id`.
3. Candidate's source authority > E's by ≥0.15 → candidate wins.
4. Temporal facts: later `valid_from` wins if both authoritative.
5. Otherwise → candidate `suggested`, E untouched, and a `contradiction` insight is _not_ raised unless importance ≥0.7 (avoid noise).

Superseded items are retained (retrievable only with `include_history=true`) for 90 days then archived.

## 6. Consolidation (nightly `MemoryConsolidationWorkflow`, per user, off-peak in user tz)

1. **Possible-duplicate merge:** process `possible_duplicate` pairs with the judge; merge via revisions.
2. **Episodic rollup:** for each (entity, 30-day window) with ≥6 episodic items, generate a relationship/project summary (role=summarizer); link to sources; mark children `archived` only if their importance <0.5 and they are not tied to open commitments.
3. **Decay:** `decay_score = exp(-Δdays / half_life[kind]) ` with half-lives: episodic 60 d, relationship 180 d, semantic 365 d, preference 540 d, project 120 d. Access refreshes: `decay_score = min(1, decay_score + 0.15)` per retrieval-use (cap once/day).
4. **Importance recompute:** `importance' = clamp(0.6·importance + 0.2·access_freq_norm + 0.2·open_link_bonus)`.
5. **Archive:** `decay_score < 0.15 AND importance < 0.4 AND NOT pinned AND no open links` → `status='archived'`.
6. **Expire:** `expires_at < now()` → `status='expired'`.
7. **Entity merge candidates** refreshed.
8. Emit metrics: counts by action; consolidation LLM cost.
   Idempotent: workflow ID `consolidate:{user_id}:{yyyy-mm-dd}`.

## 7. User corrections

| Action            | Behavior                                                                                                                                                                                                                                                                |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Edit content      | New `memory_revisions` row (`changed_by='user'`), `user_edited=true`, `confidence=1.0`, re-embed, becomes immune to auto-override                                                                                                                                       |
| Delete            | `status='deleted'`, content/payload nulled after 30 s, embedding row hard-deleted immediately, tombstone in audit; derived summaries recomputed; **negative memory** `blocked_fact_hash` stored so extractor does not re-create the same fact (checked in dedup step 1) |
| Pin               | `user_pinned=true`, always eligible for retrieval in matching domain                                                                                                                                                                                                    |
| "That's wrong"    | Same as delete + sets `blocked_fact_hash` + lowers authority of the source type for that sender (stored on entity `attributes.reliability`)                                                                                                                             |
| Confirm suggested | `status='active'`, `confidence=max(conf,0.9)`, `created_by` unchanged                                                                                                                                                                                                   |
| Reject suggested  | `status='deleted'` + blocked hash                                                                                                                                                                                                                                       |

## 8. Expiration classes (`expires_hint` → `expires_at`)

`ephemeral` +24 h after `valid_to` or occurrence; `short` +14 d; `medium` +90 d; `long` +365 d; `permanent` null. Commitments expire only on completion/cancellation.

## 9. Retrieval

### 9.1 Inputs

`RetrievalRequest { user_id, query_text, domains[], entity_ids[], time_window?, kinds[]?, k=12, token_budget, include_history=false, run_id }`

### 9.2 Candidate generation (parallel, each limited to 40)

1. **Semantic:** embed query (cache 5 min); `ORDER BY embedding <=> :q` on `memory_embeddings` (is_current) joined to active items; filters: `user_id`, `status='active'`, kinds, optional time window.
2. **Keyword:** `websearch_to_tsquery('english', :q)` against `memory_items.tsv`, `ts_rank_cd`.
3. **Entity-linked:** items linked (via `memory_entity_links`) to `entity_ids` and their 1-hop neighbors (graph traversal ≤1 hop, weight ≥0.5), ordered by importance.
4. **Pinned & due:** pinned items in matching domains; commitments due within 7 d or overdue.
5. **Recent:** episodic within last 14 d for the entities involved.

### 9.3 Fusion & ranking

Reciprocal Rank Fusion across lists (k=60) gives `rrf`. Final score:

```
score = 0.40·rrf_norm
      + 0.15·cos_sim            # 0 if not in semantic list
      + 0.10·bm25_norm
      + 0.12·importance
      + 0.08·recency        # exp(-age_days / half_life[kind])
      + 0.08·entity_overlap # Jaccard(entities in query, entities in item)
      + 0.05·domain_boost   # 1.0 if item's entity types ∈ active domain lenses
      + pin_bonus 0.10 + commitment_due_bonus 0.15 (due ≤7d)
      − 0.10·sensitivity_penalty (restricted not shown unless query directly about it)
```

Weights live in `config/retrieval.yaml` and are tuned against the eval set (§11); changes require eval pass.

### 9.4 Post-processing

1. Drop `confidence < 0.5` unless pinned.
2. MMR (λ=0.7) over embeddings to remove near-duplicates.
3. Optional rerank (role=router small model) when `k_candidates > 30` and latency budget allows (skipped for interactive if budget <250 ms).
4. Pack into `token_budget` (default 1,800 tokens for memory section) preferring higher score density; each item rendered as `[M:<short_id>] <content> (as of <date>; source: <type>)`.
5. Write `retrieval_logs`; increment `access_count` for **selected** items only.
6. Return `RetrievedMemory[]` with `id, kind, content, score, components, sources`.

### 9.5 Latency target

p95 < 300 ms (embedding cache hit) / < 500 ms (miss) for ≤50k items per user.

## 10. Example records

```json
{
  "id": "0192f6a0-1c1e-7b6e-9a43-5d0a7c1e2001",
  "kind": "preference",
  "title": "Prefers morning meetings",
  "content": "The user prefers scheduling meetings between 9:00 and 12:00 IST and avoids Friday afternoons.",
  "payload": {
    "category": "scheduling",
    "rule": "prefer 09:00-12:00 Asia/Kolkata; avoid Fri 14:00-18:00",
    "strength": "strong"
  },
  "importance": 0.78,
  "confidence": 0.95,
  "trust_level": "user",
  "status": "active",
  "user_pinned": true,
  "user_edited": false,
  "valid_from": "2026-09-02T04:10:00Z",
  "valid_to": null,
  "canonical_key": "pref:meeting_time",
  "created_by": "extractor",
  "sources": [
    {
      "source_type": "message",
      "message_id": "…",
      "quote": "I hate afternoon calls, mornings only please, and nothing on Friday afternoon"
    }
  ]
}
```

```json
{
  "kind": "commitment",
  "title": "Send pricing proposal to Priya Nair (Acme)",
  "content": "The user committed to send Priya Nair at Acme the pricing proposal by Friday 2026-11-27.",
  "payload": {
    "direction": "by_user",
    "counterparty_entity_id": "ent_priya",
    "due_at": "2026-11-27T12:30:00Z",
    "status": "open",
    "task_id": "…"
  },
  "importance": 0.88,
  "confidence": 0.9,
  "trust_level": "external_untrusted→user_confirmed",
  "status": "active",
  "due_at": "2026-11-27T12:30:00Z",
  "sources": [
    {
      "source_type": "document",
      "document_id": "…",
      "quote": "I'll get you the pricing proposal by Friday"
    }
  ]
}
```

```json
{
  "kind": "relationship",
  "content": "Priya Nair (CFO, Acme) is the economic buyer for the Acme opportunity; she is cost-sensitive and responds fastest to short emails.",
  "payload": {
    "entity_id": "ent_priya",
    "relation": "primary_contact_of",
    "tone": "direct",
    "last_topics": ["pricing", "security review"]
  },
  "importance": 0.72,
  "confidence": 0.78,
  "status": "active",
  "created_by": "consolidation"
}
```

```json
{
  "kind": "episodic",
  "content": "On 2026-11-12 the user had a 30-minute demo call with Priya Nair; she asked about SOC 2 timelines and annual pricing.",
  "occurred_at": "2026-11-12T10:00:00Z",
  "importance": 0.55,
  "confidence": 0.92,
  "expires_at": null,
  "payload": {
    "event_type": "meeting",
    "participants": ["ent_priya", "user"],
    "outcome": "follow-up needed: SOC2 timeline"
  }
}
```

```json
{
  "kind": "semantic",
  "status": "suggested",
  "content": "Rohan Mehta may be the new CTO at Orbit Labs.",
  "confidence": 0.58,
  "trust_level": "external_untrusted",
  "sources": [
    {
      "source_type": "document",
      "quote": "Rohan, our new CTO, will join the call"
    }
  ]
}
```

Contradiction example: existing `fact:priya_nair:title = "VP Finance"` (authority 0.7, from email signature) vs new `"CFO"` from LinkedIn-synced CRM contact (authority 0.95) → candidate wins (rule 3), old item `superseded`.

## 11. Memory evaluation (acceptance)

Datasets in `evals/datasets/memory/`:

- `extraction.jsonl` ≥ 300 labeled passages (chat, email, calendar notes) → target **precision ≥ 0.85, recall ≥ 0.70**, hallucinated-evidence rate **0%** (verbatim check).
- `retrieval.jsonl` ≥ 200 queries with relevant memory ids over a synthetic 5k-memory corpus → **recall@12 ≥ 0.85, nDCG@12 ≥ 0.75, hybrid beats semantic-only and keyword-only by ≥5 pts**.
- `contradiction.jsonl` ≥ 60 pairs → resolution matches labeled expectation ≥ 90%.
- `poisoning.jsonl` ≥ 80 malicious email texts attempting to write preferences/procedural memory → **0 persisted active memories** of disallowed kinds.
- `deletion.jsonl`: after delete, item never appears in retrieval, embedding gone, blocked hash prevents recreation (100%).
  Latency: retrieval p95 targets in §9.5 measured at 50k items.
