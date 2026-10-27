# CRAM: Context Resolution in AI Memory

> **B.Tech Final-Year Research Project**  
> **Stage 2: Feature 1 — Structured Fact Extraction Pipeline**

CRAM is a conversational memory system designed to extract discrete, evidence-grounded facts from conversations across multiple sessions, identify entities, recognize duplicates, preserve assertion history, and track updates without destructive overwrites.

---

## 1. Feature 1 Implementation Scope

Feature 1 establishes the production-grade ingestion and extraction backbone:
* **Immediate Message Ingestion**: Real-time persistence of turns to SQLite (`messages` & `sessions`).
* **Session-Level Fact Extraction**: Triggered via `process_session`, extracting discrete facts from conversation turns using Google Gemini.
* **Entity Discrimination**: Automatically identifies and tracks the User and third-party entities (people, organizations, projects, locations).
* **Flexible Dynamic Attributes**: Stable schema with flexible values (scalars, lists, structured values) without rigid attribute taxonomies.
* **Evidence Validation & Grounding**: Verifies exact substring or normalized quote matches against source messages; rejects ungrounded or fabricated claims.
* **Cross-Session Fact Resolution**: Compares new assertions against existing facts across all sessions:
  * `NEW`: First time a fact is asserted.
  * `DUPLICATE`: Reassertion of an active fact (links new evidence without duplicating active state).
  * `UPDATE`: State evolution (e.g., relocation, job change, numerical progression); preserves earlier assertions and links lineage.
  * `CORRECTION`: Explicit retraction of a prior error; retains provenance for both original and corrected claims.
  * `UNRESOLVED`: Ambiguous or non-exclusive claims (e.g., "I live in Delhi. I also spend time in Mumbai"); preserves ambiguity without overwriting established state.
* **SQLite Persistence**: ACID relational storage with WAL mode, foreign keys, and transaction guarantees.
* **Inspection Interfaces**: Both a modern Streamlit web inspector and a full-featured CLI.

---

## 2. Installation & Setup

### Prerequisites
* Python 3.10+ (Tested on Python 3.11.9)
* Google Gemini API Key

### Setup Instructions
1. Clone or navigate to the repository directory:
   ```bash
   cd /Users/arainasingh/Desktop/cram_new
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Configure environment variables:
   Copy `.env.example` to `.env` and provide your Google Gemini API key:
   ```bash
   cp .env.example .env
   ```
   Edit `.env`:
   ```ini
   GEMINI_API_KEY=your_gemini_api_key_here
   CRAM_MODEL=gemini-3.5-flash-lite
   CRAM_DB_PATH=cram_memory.db
   ```

> **Note on Model Selection**: During live API verification, `gemini-3.5-flash-lite` was confirmed as supported and fast for structured JSON output in Google GenAI Developer API mode. `gemini-2.5-flash` and `gemini-2.0-flash` have been decommissioned by Google's API, and `gemini-3.8-flash` experiences transient 503 high-demand spikes.

---

## 3. Verification & Testing

### Running Automated Test Suite
Run the full pytest suite (21 automated unit tests covering all 15 acceptance criteria):
```bash
pytest -v tests/
```
All 21 unit tests run using controlled fixtures and mocks with zero network dependencies.

### Running Real Gemini API Smoke Test
To verify live extraction against Google's servers with the real API key:
```bash
python -m tests.smoke_test_gemini
```
This smoke test executes two real sessions:
1. Session 1: Extracts 5 assertions across two distinct entities (`User` and `Brother`), verifying exact evidence quote grounding.
2. Session 2: Verifies cross-session `DUPLICATE` detection ("reside in Delhi") and `UPDATE` detection ("moved to Mumbai").

---

## 4. Running the Inspection Interfaces

### A. Streamlit Web Inspector
Launch the interactive web UI:
```bash
python -m streamlit run run_ui.py
```
Open [http://localhost:8501](http://localhost:8501) in your browser. The UI allows you to:
* Create or select dialogue sessions.
* Submit user/assistant messages or import multi-turn transcripts.
* Trigger session fact extraction with real-time status.
* Inspect Active Facts grouped by entity.
* Inspect the Immutable Historical Assertion log with lineage links.
* Inspect the Decision Audit Log (`NEW`, `DUPLICATE`, `UPDATE`, `CORRECTION`, `UNRESOLVED`).
* Inspect Evidence Quotes and verify exact message grounding.
* View Logged Validation Failures for ungrounded claims.

### B. Command-Line Interface (CLI)
Initialize database:
```bash
python cli.py init
```

Add a message to a session:
```bash
python cli.py add-message --session session-1 --role user --content "I live in Delhi and work at Google."
```

Trigger session extraction:
```bash
python cli.py process --session session-1
```

Inspect active facts and decisions:
```bash
python cli.py list-facts
python cli.py list-assertions --session session-1
python cli.py list-decisions --session session-1
```

---

## 5. Required Acceptance Scenarios Walkthrough

| Scenario | Example Input | Expected System Behavior |
| :--- | :--- | :--- |
| **Semantic Duplicate** | S1: *"I live in Delhi."*<br>S2: *"I reside in Delhi."* | Recognized as `DUPLICATE`. Links both evidence sources to `residence`; does not duplicate active fact state. |
| **Distinct Relationships** | S1: *"I live in Delhi."*<br>S1: *"I work in Delhi."* | Extracted as two distinct conceptual attributes: `residence` vs `workplace`. Never conflated. |
| **Residence Update** | S1: *"I live in Delhi."*<br>S2: *"I have moved to Mumbai. I live here now."* | Retains both assertions in SQLite; classifies as `UPDATE`. Lineage points new assertion to old assertion. |
| **Explicit Correction** | S1: *"I live in Delhi."*<br>S2: *"My earlier statement was wrong. I lived in Mumbai, not Delhi."* | Retains provenance for both; classifies as `CORRECTION`. Supersedes earlier claim. |
| **Ambiguous Information** | S1: *"I live in Delhi."*<br>S2: *"I also spend time in Mumbai."* | Classifies as `UNRESOLVED`. Does not overwrite Delhi as primary residence. |
| **Changing Numerical Value**| S1: *"My age is 22."*<br>S2: *"My age is 23."* | Classifies as `UPDATE` without inventing a date of birth or unsupported event dates. |

---

## 6. Project Documentation Index
* [Architecture Document](file:///Users/arainasingh/Desktop/cram_new/docs/architecture.md)
* [Project Flow & Sequence Diagrams](file:///Users/arainasingh/Desktop/cram_new/docs/project_flow.md)
* [Implementation Log](file:///Users/arainasingh/Desktop/cram_new/docs/implementation_log.md)
