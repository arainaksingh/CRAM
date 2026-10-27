# CRAM — Implementation Log

## Log Metadata
- **Project:** CRAM (Context Resolution in AI Memory)
- **Status:** Feature 1 (Phases 1–4) & Feature 2 (Milestones 1–2) Complete & Verified
- **Test Suite Status:** 96 Passing Tests (0 Failures)
- **Pyright Status:** 0 Errors, 0 Warnings

---

## 1. Feature 1: Structured Fact Extraction & Memory Foundation

### Initial Baseline (Stage 2: Feature 1)
- Initial Gemini 3.5 Flash Lite structured extraction integration using `google-genai` SDK.
- Relational SQLite schema with 8 tables: `sessions`, `messages`, `entities`, `fact_identities`, `assertions`, `assertion_evidence`, `processing_decisions`, and `validation_failures`.
- Core Pydantic schemas: `CandidateFact`, `ExtractionResult`, `AssertionRecord`, etc.
- Evidence quote grounding validator and baseline fact resolver.

### Phase 1 — Minimal Hardening Fixes
- Added failure-safe transaction and savepoint handling to SQLite migrations in `cram/db/schema.py`.
- Added sequential 1-indexed `turn_index` allocation to `messages` protected by `BEGIN IMMEDIATE`.
- Fixed initialization order so both fresh databases and populated legacy databases migrate without errors.

### Phase 2 — Reliable Transcript Ingestion
- Created `cram/pipeline/transcript_parser.py` parsing multi-turn transcripts with mixed case roles and CRLF normalizations.
- Rejects invalid transcripts without touching SQLite.
- Added batch message ingestion with atomic transaction rollback.

### Phase 3 — Delete All Sessions & Cascade Memory Reset
- Implemented `Repository.delete_all_sessions()` to atomically delete sessions, messages, entities, fact identities, assertions, evidence, decisions, and validation failures.
- Added confirmation guard in Streamlit UI to prevent accidental memory resets.

### Phase 4 — Role-Aware Evidence Validation
- Enforced policy in `cram/pipeline/evidence_validator.py` that assistant-only statements cannot establish durable user facts.
- Added adjacent-turn confirmation verification requiring explicit user affirmation ("yes", "correct") to ground assistant statements.
- Rejected uncertain ("maybe"), qualified ("yes, but..."), and contrastive responses.

---

## 2. Feature 2: Temporal Reasoning & State Progression

### Milestone 1 — Temporal Data Model & Python Normalizer
- Extended `CandidateFact` and `AssertionRecord` with `event_start`, `event_end`, `event_status`, and `temporal_type`.
- Applied backward-compatible SQLite migration adding nullable temporal columns to `assertions`.
- Implemented `cram/temporal/normalizer.py` supporting:
  - Exact ISO dates and month/year intervals.
  - Partial date preservation (`YYYY`, `YYYY-MM`) without inventing days or timestamps.
  - Anchoring relative dates ("yesterday", "tomorrow") against message timestamps.
  - Preserving raw expressions for unsupported or vague dates.

### Milestone 2 — Temporal Validation & Temporal-Aware Resolution
- Implemented `cram/temporal/validator.py`:
  - `parse_iso_date_to_bounds`: maps granularities to calendar bounds.
  - `validate_temporal_bounds`: rejects inverted intervals ($start > end$) and invalid dates to `validation_failures`.
  - `compare_temporal_bounds`: multi-granularity chronological comparisons (`strictly_before`, `strictly_after`, `same`, `during`, `contains`, `ambiguous_overlap`, `unspecified`).
- Integrated temporal resolution into `cram/pipeline/resolver.py`:
  - Separates conversation arrival order from real-world event order.
  - `HISTORICAL` tag: retains older historical claims arriving later without overwriting active facts.
  - Forward status transitions (`planned` $\to$ `ongoing` $\to$ `completed` / `cancelled`).
  - Cancellation guard: cancellations do not infer event occurrence.
  - Contradiction detection: contradictory claims at the same time or invalid status reversals resolve to `UNRESOLVED`.

---

## 3. Automated Test Execution Results

Executed `.venv/bin/python -m pytest -v tests/`:

```
tests/test_delete_all_sessions.py (4 tests) PASSED
tests/test_evidence_validation.py (19 tests) PASSED
tests/test_fact_resolution.py (6 tests) PASSED
tests/test_gemini_client_error_handling.py (4 tests) PASSED
tests/test_persistence_and_recovery.py (3 tests) PASSED
tests/test_schema_and_models.py (3 tests) PASSED
tests/test_temporal_reasoning.py (21 tests) PASSED
tests/test_transcript_ingestion.py (13 tests) PASSED
tests/test_turn_index_and_migration.py (11 tests) PASSED
tests/test_ui_rendering.py (2 tests) PASSED

============================== 96 passed in 1.29s ==============================
```
