# CRAM — Implementation Reference Document

## Overview

This document provides a comprehensive technical reference for the implementations in CRAM (**Context Resolution in AI Memory**), detailing the four expansion phases of **Feature 1** and the two milestones of **Feature 2**.

---

## 1. Feature 1: Structured Fact Extraction & Memory Lifecycle

### Phase 1 — Minimal Hardening Fixes
* **Transactional Schema Migrations**:
  - Implemented safe database initialization and migration order in [`cram/db/schema.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/db/schema.py).
  - Uses explicit SQLite savepoints (`SAVEPOINT migration_sp`) with full rollback handling on failure (`ROLLBACK TO SAVEPOINT migration_sp`), preserving existing schemas and tables.
  - Accommodates SQLite autocommit behaviors and prevents errors with `BEGIN IMMEDIATE` if an active transaction is already open.
* **Strict Per-Session Turn Indexing**:
  - Added `turn_index` (INTEGER NOT NULL) to the `messages` table with a composite unique constraint `UNIQUE(session_id, turn_index)` and index `idx_messages_session_turn`.
  - [`Repository._insert_message_with_turn_index`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/db/repository.py#L226) and [`Repository.insert_messages_batch`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/db/repository.py#L169) assign sequential 1-indexed turn numbers using `SELECT COALESCE(MAX(turn_index), 0) + 1` inside an immediate transaction, preventing timestamp skew or race conditions.

### Phase 2 — Reliable Transcript Ingestion
* **Transcript Parsing Engine**:
  - Implemented [`cram/pipeline/transcript_parser.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/pipeline/transcript_parser.py) with [`TranscriptParser.parse()`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/pipeline/transcript_parser.py#L42-L135).
  - Normalizes line endings (`\r\n` $\to$ `\n`), handles case-insensitive role headers (`User:`, `ASSISTANT:`, `user:`), supports multi-paragraph dialogue turns, and enforces non-empty turn contents.
  - Validates full transcripts before touching the database; rejects invalid preambles or malformed structures atomically without leaving orphaned rows.
* **Batch Ingestion & Rollback**:
  - Integrated with [`Repository.insert_messages_batch`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/db/repository.py#L169) to atomically ingest parsed dialogue turns within an explicit transaction.

### Phase 3 — "Delete All Sessions" & Memory Cascade Reset
* **Safe Cascading Deletion**:
  - Implemented [`Repository.delete_all_sessions()`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/db/repository.py#L71-L127) executing an atomic transaction that purges: `messages`, `sessions`, `assertions` (cascading `assertion_evidence`), `fact_identities`, `entities`, `processing_decisions`, and `validation_failures`.
  - Preserves table schemas, SQLite indexes, and database integrity while resetting memory state.
* **UI Safety Guards**:
  - Integrated confirmation guard in [`cram/ui/app.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/ui/app.py) requiring explicit user confirmation before executing deletion.

### Phase 4 — Role-Aware Evidence Validation & User Confirmation
* **Python-Level Role Policy**:
  - Implemented in [`cram/pipeline/evidence_validator.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/pipeline/evidence_validator.py).
  - Enforces that statements made solely by the assistant (`role='assistant'`) cannot independently establish durable real-world facts about the user or third parties.
* **Turn-Adjacent Confirmation Matching**:
  - If a candidate fact originates from an assistant turn, the validator inspects the immediately adjacent subsequent user turn (`turn_index + 1`).
  - Accepts the claim only if the user explicitly affirms it with affirmative markers ("yes", "exactly", "correct", "that's right").
  - Rejects qualified ("yes, but..."), uncertain ("maybe", "perhaps"), contrastive, or questioning responses in Python.
  - Rejections are recorded in `validation_failures`, leaving fact identities and assertions unpolluted.

---

## 2. Feature 2: Temporal Reasoning & State Progression

### Milestone 1 — Temporal Data Model & Python Normalizer
* **Model Extensions**:
  - Extended [`CandidateFact`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/models/schemas.py#L11-L40) and [`AssertionRecord`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/models/schemas.py#L65-L89) with optional temporal fields:
    - `event_start`: Optional ISO date string (`YYYY`, `YYYY-MM`, or `YYYY-MM-DD`).
    - `event_end`: Optional ISO date string (`YYYY`, `YYYY-MM`, or `YYYY-MM-DD`).
    - `event_status`: Literal `["planned", "ongoing", "completed", "cancelled"]`.
    - `temporal_type`: Literal `["point", "interval", "relative", "recurring"]`.
* **Backward-Compatible SQLite Migration**:
  - Updated [`cram/db/schema.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/db/schema.py) with safe `ALTER TABLE assertions ADD COLUMN` statements wrapped in savepoints.
  - Pre-existing rows safely populate with `NULL` without data loss.
* **Deterministic Normalizer**:
  - Implemented [`cram/temporal/normalizer.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/temporal/normalizer.py) with [`normalize_temporal()`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/temporal/normalizer.py#L130-L380).
  - Preserves partial-date precision (`"2024"` remains `"2024"`, `"April 2024"` remains `"2024-04"`) without inventing day or time components.
  - Anchors relative terms ("yesterday", "tomorrow") against source message timestamps via [`parse_reference_date()`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/temporal/normalizer.py#L77).
  - Preserves vague expressions ("sometime next summer") as raw strings without fabricating bounds.

### Milestone 2 — Temporal Validation & Temporal-Aware Resolution
* **Temporal Validator**:
  - Implemented [`cram/temporal/validator.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/temporal/validator.py).
  - [`validate_temporal_bounds()`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/temporal/validator.py#L97): Rejects inverted intervals where $earliest(start) > latest(end)$ and validates calendar bounds (e.g., rejecting February 30).
  - [`compare_temporal_bounds()`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/temporal/validator.py#L150): Multi-granularity comparison classifying relations into: `strictly_before`, `strictly_after`, `same`, `during`, `contains`, `ambiguous_overlap`, and `unspecified`.
* **Temporal-Aware Resolution**:
  - Integrated in [`cram/pipeline/resolver.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/pipeline/resolver.py) and [`cram/pipeline/session_processor.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/pipeline/session_processor.py).
  - **Arrival vs Event Order**: Statements describing older events arriving in later sessions are classified as `HISTORICAL`. They are appended to the assertion history with full evidence, but do **not** mutate the active fact pointer (`fact_identity.current_assertion_id`).
  - **Temporal Progression**: Claims strictly newer than the active state (`strictly_after`) produce `UPDATE`, setting `supersedes_assertion_id` to the prior active assertion.
  - **Status Transitions**: Models forward transitions (`planned` $\to$ `ongoing` $\to$ `completed` / `cancelled`).
  - **Cancellation Guard**: Explicitly notes that an event cancellation does not imply event occurrence (`event_status='cancelled'`, not `completed`).
  - **Contradiction Detection**: Reverses in status (`completed` $\to$ `cancelled` without correction) or contradictory claims for the exact same point in time (`same`) yield `UNRESOLVED` audit records rather than guessed updates.

---

## 3. Verification & Test Suite Summary

- **Total Tests Passing**: 96 tests across all modules.
- **Pyright Type Checking**: 0 errors, 0 warnings across all production and test files for Feature 1 & 2.
- **Active DB Preservation**: `cram_memory.db` was isolated during all testing.
