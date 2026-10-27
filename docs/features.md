# CRAM — Features Guide

Welcome to the CRAM Features Guide! This document explains what each feature in CRAM does in plain, everyday language, how the system makes sense of what you tell it, what each memory tag means, and which files in the codebase make the magic happen.

---

## 1. Feature 1: Structured Fact Extraction & Memory Foundation

### What is it in plain English?
Most AI chatbots either have no long-term memory (they forget everything once you start a new conversation) or they try to remember everything as fuzzy, unorganized blobs of text. This leads to hallucinations, repeating stale information, and forgetting key details.

**Feature 1** gives the AI a clean, organized, verifiable memory notebook:
1. **It identifies distinct people and things (Entities)**: e.g., the User, their brother, their company, or their city.
2. **It tracks specific attributes (Fact Identities)**: e.g., "Where does the User live?" or "Who is the User's employer?".
3. **It stores claims with exact proof (Evidence & Grounding)**: It saves the exact quote from the conversation so you can always see *why* the AI remembers something.
4. **It classifies how new statements relate to old knowledge (Resolution Decisions)**.

---

### The Fact Resolution Tags (In Plain English)

Whenever the AI extracts a fact from conversation, it compares it against what it already knows and assigns it a clear tag:

#### 1. `NEW` — The First Time
* **What it means**: The AI just learned something about someone for the first time.
* **Everyday Example**: *"Hi, my name is Alex and I live in Seattle."*
* **What CRAM does**: It creates a brand-new fact profile for "Alex's residence" and marks Seattle as the active answer.

#### 2. `DUPLICATE` — Repeating What We Already Know
* **What it means**: You mentioned something you've already shared before.
* **Everyday Example**: In Session 1 you said *"I live in Seattle."* In Session 2 you say *"I reside in Seattle."*
* **What CRAM does**: It recognizes that nothing has changed. It links the new quote to your profile without creating a clutter of duplicate facts or overwriting anything.

#### 3. `UPDATE` — Life Changes Over Time
* **What it means**: Something in the real world changed.
* **Everyday Example**: *"I got a new job at Microsoft!"* or *"I moved from Seattle to New York."*
* **What CRAM does**: It marks New York as your new active city, but it doesn't throw away Seattle. It links New York to Seattle through an append-only history line, so the AI knows you lived in Seattle before New York.

#### 4. `CORRECTION` — Fixing a Slip of the Tongue
* **What it means**: You or the AI made a mistake, and you are explicitly setting the record straight.
* **Everyday Example**: *"Actually, I made a mistake earlier—my dog is 5 years old, not 7."*
* **What CRAM does**: It immediately corrects the active number to 5, tags it as a `CORRECTION`, and records why the old number was wrong.

#### 5. `UNRESOLVED` — Don't Guess When It's Ambiguous
* **What it means**: You said something that seems to clash or is temporary, but you didn't say you moved or changed.
* **Everyday Example**: Your active home is Seattle, and you say *"I'm spending a couple of weeks in Chicago."*
* **What CRAM does**: Did you move to Chicago? Are you just visiting? The AI does not guess! It flags Chicago as `UNRESOLVED`, keeps Seattle as your home, and logs the ambiguous note safely.

---

### Feature 1 Expansion Phases

#### Phase 1: Rock-Solid Database Foundations & Turn Ordering
* **What it does**: Ensures that messages never get mixed up, even if server clocks are slightly off or multiple messages arrive quickly. Every message in a chat receives a strict turn number (Turn 1, Turn 2, Turn 3). Database updates are failure-safe: if something fails halfway through, it cleanly rolls back so memory is never corrupted.
* **Files implementing Phase 1**:
  - [`cram/db/schema.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/db/schema.py): Database tables, migration runner, and savepoint rollbacks.
  - [`cram/db/repository.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/db/repository.py): Sequential turn index allocation and atomic message storage.
  - [`cram/db/connection.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/db/connection.py): SQLite WAL mode and foreign-key enforcement.
  - [`tests/test_turn_index_and_migration.py`](file:///Users/harshjain/Desktop/cram_new%20copy/tests/test_turn_index_and_migration.py): Tests verifying turn order and failure-safe rollback.

#### Phase 2: Reliable Transcript Ingestion
* **What it does**: Lets you copy and paste entire chat logs into CRAM. It automatically handles different line endings (`Windows` vs `Mac`), figures out who is speaking regardless of capitalization (`User:`, `ASSISTANT:`), and checks that the transcript is valid before saving anything.
* **Files implementing Phase 2**:
  - [`cram/pipeline/transcript_parser.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/pipeline/transcript_parser.py): The transcript text parser and validator.
  - [`cram/db/repository.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/db/repository.py): `insert_messages_batch()` for atomic multi-message insertion.
  - [`tests/test_transcript_ingestion.py`](file:///Users/harshjain/Desktop/cram_new%20copy/tests/test_transcript_ingestion.py): Ingestion and validation tests.

#### Phase 3: "Delete All Sessions" & Clean Slate
* **What it does**: Provides a clean-slate button to wipe all conversation history and derived facts during testing, protected by an explicit confirmation guard so you never delete your data by accident.
* **Files implementing Phase 3**:
  - [`cram/db/repository.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/db/repository.py): `delete_all_sessions()` cascading deletion.
  - [`cram/ui/app.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/ui/app.py): Streamlit web interface confirmation dialog.
  - [`tests/test_delete_all_sessions.py`](file:///Users/harshjain/Desktop/cram_new%20copy/tests/test_delete_all_sessions.py): Safe deletion tests.

#### Phase 4: Truth Filter for the AI Assistant
* **What it does**: Prevents the AI assistant from inventing memories about you. If an AI assistant says *"You like skydiving!"*, CRAM will **not** store that as a fact about you unless you immediately say *"Yes, that's right!"*. If you say *"No"*, *"Maybe"*, or *"I like indoor skydiving, but not from planes"*, the AI's claim is rejected.
* **Files implementing Phase 4**:
  - [`cram/pipeline/evidence_validator.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/pipeline/evidence_validator.py): Role-aware confirmation matcher and quote verifier.
  - [`cram/extraction/prompts.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/extraction/prompts.py): Extraction prompt guidance for user vs assistant turns.
  - [`tests/test_evidence_validation.py`](file:///Users/harshjain/Desktop/cram_new%20copy/tests/test_evidence_validation.py): Role-aware confirmation tests.

---

## 2. Feature 2: Temporal Reasoning & State Progression

### What is it in plain English?
Time is one of the hardest things for conversational AI. Why? Because **when you talk about something** is often very different from **when it actually happened**:
- If on Tuesday you say: *"I moved to London yesterday"*, the event happened on Monday, not Tuesday!
- If today you say: *"In 2018, I lived in Delhi"*, you are talking about the past. A naive AI might overwrite your current home in Berlin and think you moved back to Delhi!
- If you say: *"I cancelled my flight"*, you didn't take the flight. The AI shouldn't think you completed the trip.

**Feature 2** makes CRAM fully time-aware.

---

### The Temporal Concepts (In Plain English)

#### 1. Real Calendar Granularity (No Inventing Days)
* **What it means**: If you say *"I lived in France in 2024"*, CRAM remembers `"2024"`. It does not pretend you lived there on `"2024-01-01 at 00:00:00"`. If you say *"in April 2024"*, it remembers `"2024-04"`.

#### 2. Relative Dates Anchored to Messages
* **What it means**: When you say *"yesterday"* or *"tomorrow"*, CRAM looks at the exact timestamp of that chat message and calculates the real date. If you talk about *"sometime in the future"*, it doesn't make up a fake calendar date.

#### 3. Temporal Validation: You Can't Finish Before You Start!
* **What it means**: If someone enters an impossible timeline (e.g., *"I worked there from May 2024 to April 2024"*), or a non-existent calendar date like February 30th, CRAM immediately catches the contradiction and rejects it into a validation failure log instead of polluting your memory.

#### 4. The `HISTORICAL` Tag: Remembering the Past Without Erasing the Present
* **What it means**: If you currently live in Berlin (asserted in 2024), and later in the chat you say: *"Back in 2020, I lived in Delhi"*, CRAM tags Delhi as `HISTORICAL`.
* **What CRAM does**: It stores the Delhi memory in your permanent records with proof, but it **keeps Berlin as your current active home**.

#### 5. Lifecycle Status Transitions (Planned $\to$ Ongoing $\to$ Completed)
* **What it means**: Events go through natural stages:
  - **Planned**: *"I am planning a trip to Japan in December."*
  - **Ongoing**: *"I am currently on my trip in Japan."*
  - **Completed**: *"I completed my trip to Japan."*
* Each stage updates the previous stage gracefully.

#### 6. Cancellation Guard: A Cancelled Event Did Not Happen!
* **What it means**: If you plan a conference presentation and later say *"I cancelled my presentation"*, CRAM marks the status as `cancelled`. It explicitly records: *"Cancellation recorded without inferring event occurrence."* It will never mark the event as completed.

#### 7. Contradiction Detection: No Flipping Coins
* **What it means**: If you say you were in Berlin on April 20, 2024, and in another sentence say you were in Tokyo on April 20, 2024 (at the exact same time without saying you corrected yourself), CRAM flags it as `UNRESOLVED`. It refuses to guess which one is true without clarification.

---

### Feature 2 Milestones and Implementing Files

#### Milestone 1: Temporal Data Model & Python Normalizer
* **What it does**: Adds structured time fields (`event_start`, `event_end`, `event_status`, `temporal_type`) to assertions and models. Includes a deterministic Python normalizer that handles ISO dates, month/year ranges, and relative date offsets without hallucinating dates.
* **Files implementing Milestone 1**:
  - [`cram/models/schemas.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/models/schemas.py): Temporal fields added to `CandidateFact` and `AssertionRecord`.
  - [`cram/db/schema.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/db/schema.py): Safe SQLite database migration adding temporal columns to `assertions`.
  - [`cram/temporal/normalizer.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/temporal/normalizer.py): Text-to-date normalizer and message timestamp anchor.
  - [`cram/pipeline/session_processor.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/pipeline/session_processor.py): Populating normalized temporal attributes during extraction.
  - [`tests/test_temporal_reasoning.py`](file:///Users/harshjain/Desktop/cram_new%20copy/tests/test_temporal_reasoning.py): Parsing, precision, and anchoring tests.

#### Milestone 2: Temporal Validation & Temporal-Aware Resolution
* **What it does**: Validates interval boundaries, performs multi-granularity chronological comparisons (`strictly_before`, `strictly_after`, `same`, `during`, `contains`, `ambiguous_overlap`), and implements temporal-aware resolution in the fact resolver.
* **Files implementing Milestone 2**:
  - [`cram/temporal/validator.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/temporal/validator.py): Boundary validation and interval comparison engine.
  - [`cram/temporal/__init__.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/temporal/__init__.py): Module exports for validator and comparison types.
  - [`cram/pipeline/resolver.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/pipeline/resolver.py): Temporal-aware resolver with `HISTORICAL` tag, status transitions, and contradiction checks.
  - [`cram/pipeline/session_processor.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/pipeline/session_processor.py): Pre-resolution temporal validation and active assertion preservation.
  - [`cram/db/repository.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/db/repository.py): Fact identity accessors supporting resolution checks.
  - [`tests/test_temporal_reasoning.py`](file:///Users/harshjain/Desktop/cram_new%20copy/tests/test_temporal_reasoning.py): Comprehensive tests for interval validation, historical retention, status transitions, cancellations, and conflicts.

---

## 3. Quick Reference: Feature-to-File Matrix

| Feature / Capability | Main Implementation Files | Test Suite Files |
| :--- | :--- | :--- |
| **Fact Extraction & Prompts** | [`cram/extraction/extractor.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/extraction/extractor.py)<br>[`cram/extraction/prompts.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/extraction/prompts.py)<br>[`cram/extraction/gemini_client.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/extraction/gemini_client.py) | [`tests/test_gemini_client_error_handling.py`](file:///Users/harshjain/Desktop/cram_new%20copy/tests/test_gemini_client_error_handling.py)<br>[`tests/test_schema_and_models.py`](file:///Users/harshjain/Desktop/cram_new%20copy/tests/test_schema_and_models.py) |
| **Evidence Validation & Grounding** | [`cram/pipeline/evidence_validator.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/pipeline/evidence_validator.py) | [`tests/test_evidence_validation.py`](file:///Users/harshjain/Desktop/cram_new%20copy/tests/test_evidence_validation.py) |
| **Turn Order & Transcript Ingestion** | [`cram/pipeline/transcript_parser.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/pipeline/transcript_parser.py)<br>[`cram/db/repository.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/db/repository.py) | [`tests/test_transcript_ingestion.py`](file:///Users/harshjain/Desktop/cram_new%20copy/tests/test_transcript_ingestion.py)<br>[`tests/test_turn_index_and_migration.py`](file:///Users/harshjain/Desktop/cram_new%20copy/tests/test_turn_index_and_migration.py) |
| **Memory Reset / Delete All** | [`cram/db/repository.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/db/repository.py)<br>[`cram/ui/app.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/ui/app.py) | [`tests/test_delete_all_sessions.py`](file:///Users/harshjain/Desktop/cram_new%20copy/tests/test_delete_all_sessions.py) |
| **Fact Resolution (`NEW`, `UPDATE`, etc.)** | [`cram/pipeline/resolver.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/pipeline/resolver.py) | [`tests/test_fact_resolution.py`](file:///Users/harshjain/Desktop/cram_new%20copy/tests/test_fact_resolution.py) |
| **Temporal Model & Normalizer** | [`cram/models/schemas.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/models/schemas.py)<br>[`cram/temporal/normalizer.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/temporal/normalizer.py) | [`tests/test_temporal_reasoning.py`](file:///Users/harshjain/Desktop/cram_new%20copy/tests/test_temporal_reasoning.py) |
| **Temporal Validation & Resolution** | [`cram/temporal/validator.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/temporal/validator.py)<br>[`cram/pipeline/resolver.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/pipeline/resolver.py)<br>[`cram/pipeline/session_processor.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/pipeline/session_processor.py) | [`tests/test_temporal_reasoning.py`](file:///Users/harshjain/Desktop/cram_new%20copy/tests/test_temporal_reasoning.py) |
| **Persistence, Storage & Migrations** | [`cram/db/schema.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/db/schema.py)<br>[`cram/db/repository.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/db/repository.py)<br>[`cram/db/connection.py`](file:///Users/harshjain/Desktop/cram_new%20copy/cram/db/connection.py) | [`tests/test_persistence_and_recovery.py`](file:///Users/harshjain/Desktop/cram_new%20copy/tests/test_persistence_and_recovery.py)<br>[`tests/test_turn_index_and_migration.py`](file:///Users/harshjain/Desktop/cram_new%20copy/tests/test_turn_index_and_migration.py) |
