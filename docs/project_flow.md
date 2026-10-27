# CRAM — Project Flow & Execution Workflows

This document describes the end-to-end operational workflows implemented in CRAM across Feature 1 (Phases 1–4) and Feature 2 (Milestones 1–2).

---

## 1. Message & Transcript Ingestion Workflow

Incoming turns from the user or assistant are ingested and assigned strict sequential turn indexes before any processing occurs.

```mermaid
sequenceDiagram
    autonumber
    actor Client as User / Client
    participant Parser as TranscriptParser
    participant Repo as Repository
    participant DB as SQLite DB

    alt Single Message Ingestion
        Client->>Repo: insert_message(session_id, role, content, timestamp)
        Repo->>DB: BEGIN IMMEDIATE
        Repo->>DB: SELECT COALESCE(MAX(turn_index), 0) + 1
        Repo->>DB: INSERT INTO messages (turn_index=next_idx, is_processed=0)
        Repo->>DB: COMMIT
        DB-->>Client: MessageRecord
    else Batch Transcript Ingestion
        Client->>Parser: parse_transcript(raw_text)
        Parser->>Parser: Normalize CRLF, validate roles, verify non-empty turns
        alt Validation Fails
            Parser-->>Client: TranscriptParseError (Database untouched)
        else Validation Passes
            Parser-->>Client: Validated parsed turns
            Client->>Repo: insert_messages_batch(session_id, turns)
            Repo->>DB: BEGIN IMMEDIATE
            loop For each turn in order
                Repo->>DB: INSERT INTO messages (turn_index=start_idx + i)
            end
            Repo->>DB: COMMIT
            DB-->>Client: List[MessageRecord]
        end
    end
```

---

## 2. Session Fact Extraction & Multi-Stage Validation Workflow

Extraction and resolution are triggered via `process_session(session_id)`. Each candidate fact progresses through a multi-stage validation and resolution pipeline:

```mermaid
sequenceDiagram
    autonumber
    actor Trigger as User / API Trigger
    participant Proc as SessionProcessor
    participant Repo as Repository
    participant LLM as Google Gemini API
    participant Val as EvidenceValidator
    participant Norm as TemporalNormalizer
    participant TempVal as TemporalValidator
    participant Res as FactResolver
    participant DB as SQLite DB

    Trigger->>Proc: process_session(session_id)
    Proc->>Repo: update_session_status('processing')
    Proc->>Repo: get_session_messages(session_id)
    Repo-->>Proc: all_messages (identifying unprocessed_ids)
    
    Proc->>LLM: generate_structured(dialogue_turns, ExtractionResult)
    LLM-->>Proc: ExtractionResult(facts=[CandidateFact, ...])
    
    loop For each CandidateFact
        %% Step 1: Role-Aware Evidence Validation
        Proc->>Val: validate_fact_evidence(candidate, messages_by_id)
        alt Grounding Fails or Unconfirmed Assistant-Only Claim
            Val-->>Proc: is_valid=False, failure_reason
            Proc->>Repo: insert_validation_failure(...)
        else Evidence Validated
            Val-->>Proc: is_valid=True, validated_evidence_items

            %% Step 2: Temporal Normalization
            Proc->>Norm: normalize_temporal(temporal_expression, ref_timestamp)
            Norm-->>Proc: NormalizedTemporal(start, end, status, type)

            %% Step 3: Temporal Bounds Validation
            Proc->>TempVal: validate_temporal_bounds(start, end, type)
            alt Inverted Bounds (start > end) or Invalid Calendar Date
                TempVal-->>Proc: is_valid=False, failure_reason
                Proc->>Repo: insert_validation_failure(...)
            else Bounds Valid
                TempVal-->>Proc: is_valid=True

                %% Step 4: Entity Resolution
                Proc->>Repo: get_or_create_entity(subject_name, entity_type)
                Repo-->>Proc: EntityRecord

                %% Step 5: Temporal-Aware Resolution
                Proc->>Res: resolve_candidate(candidate, entity, existing_facts, active_assertions)
                Res-->>Proc: ResolutionResult(decision_type, matched_ids, reason)
                
                %% Step 6: Atomic Persistence & Audit Logging
                Proc->>Repo: insert_assertion(assertion_type=decision_type, ...)
                Proc->>Repo: insert_evidence(assertion_id, quote, is_exact_match)
                Proc->>Repo: insert_decision(decision_type, reason, ...)
                
                %% Step 7: Active Fact Pointer Mutation Guard
                opt If NEW, UPDATE, or CORRECTION
                    Proc->>Repo: update_fact_identity_active_assertion(fact_id, assertion_id)
                end
                Note over Proc,Repo: DUPLICATE, HISTORICAL, and UNRESOLVED do NOT mutate active pointer!
            end
        end
    end
    
    Proc->>Repo: mark_messages_processed(unprocessed_ids)
    Proc->>Repo: update_session_status('completed')
    Proc-->>Trigger: ProcessingSummary(assertions_created, decisions_count, validation_failures_count)
```

---

## 3. Fact Resolution Decision Matrix

| Candidate State vs Active Assertion | Temporal / Evidence Signal | Decision | Active Fact Pointer Action | Historical Assertion Lineage |
| :--- | :--- | :--- | :--- | :--- |
| **No prior fact** exists for `Entity + CanonicalAttribute` | Valid evidence | `NEW` | **Points** to new assertion | `supersedes_assertion_id = None` |
| **Identical value** and no status transition | Semantic duplicate | `DUPLICATE` | **Preserved** (Unchanged) | Appended without superseding |
| **Explicit Correction** | Correction cues ("was wrong", "mistake") | `CORRECTION` | **Updated** to corrected assertion | `supersedes_assertion_id = prior.id` |
| **Historical Statement** arriving later | Candidate concluded before active began (`strictly_before` or historical cue) | `HISTORICAL` | **Preserved** (Unchanged active state) | `supersedes_assertion_id = None` (Retained as historical record) |
| **Temporal Progression** | Candidate started after active concluded (`strictly_after`) | `UPDATE` | **Updated** to newer assertion | `supersedes_assertion_id = prior.id` |
| **Supported Status Transition** | `planned` $\to$ `ongoing` $\to$ `completed` or `planned`/`ongoing` $\to$ `cancelled` | `UPDATE` | **Updated** to new status | `supersedes_assertion_id = prior.id` (Cancellation notes no event occurrence) |
| **Contradictory Status Transition** | `completed` $\to$ `cancelled` or `cancelled` $\to$ `completed` without correction | `UNRESOLVED` | **Preserved** (Unchanged) | Appended as conflict record |
| **Conflicting Claims at Same Time** | Different values for identical date/interval bounds (`same`) | `UNRESOLVED` | **Preserved** (Unchanged) | Appended as conflict record |
| **Ambiguous Overlap** | Partial overlap (`during`, `contains`, `ambiguous_overlap`) without transition cues | `UNRESOLVED` | **Preserved** (Unchanged) | Appended without guessing precedence |
| **Assistant-Only Claim** | Assistant asserts fact without user confirmation | *REJECTED* | **None** | Recorded in `validation_failures` |
| **Inverted Temporal Bounds** | Event start occurs strictly after event end | *REJECTED* | **None** | Recorded in `validation_failures` |

---

## 4. Reset & Deletion Workflow ("Delete All Sessions")

When a user triggers a memory reset, CRAM performs a cascading deletion:

```mermaid
sequenceDiagram
    autonumber
    actor User as User
    participant UI as Streamlit UI / API
    participant Repo as Repository
    participant DB as SQLite DB

    User->>UI: Click "Delete All Sessions"
    UI->>User: Display explicit confirmation guard checkbox
    User->>UI: Confirm deletion & click "Proceed"
    
    UI->>Repo: delete_all_sessions()
    Repo->>DB: BEGIN IMMEDIATE
    Repo->>DB: DELETE FROM messages
    Repo->>DB: DELETE FROM sessions
    Repo->>DB: DELETE FROM assertions (cascades evidence)
    Repo->>DB: DELETE FROM fact_identities
    Repo->>DB: DELETE FROM entities
    Repo->>DB: DELETE FROM processing_decisions
    Repo->>DB: DELETE FROM validation_failures
    Repo->>DB: COMMIT
    
    DB-->>Repo: Success
    Repo-->>UI: Stats (deleted counts across all tables)
    UI-->>User: Memory reset complete; fresh session ready
```
