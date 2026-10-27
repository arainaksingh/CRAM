import sqlite3
from typing import Union
from pathlib import Path
from cram.db.connection import get_connection

SCHEMA_SQL = """
-- Sessions table
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    metadata_json TEXT DEFAULT '{}'
);

-- Real-time message storage
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    turn_index INTEGER NOT NULL DEFAULT 1 CHECK (turn_index >= 1),
    is_processed INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_messages_session_proc ON messages(session_id, is_processed);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_session_turn ON messages(session_id, turn_index);

-- Distinct entities
CREATE TABLE IF NOT EXISTS entities (
    entity_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_canonical ON entities(canonical_name, entity_type);

-- Conceptual fact identities (stable attribute of an entity)
CREATE TABLE IF NOT EXISTS fact_identities (
    fact_id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL,
    attribute TEXT NOT NULL,
    canonical_attribute TEXT NOT NULL,
    current_assertion_id TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (entity_id) REFERENCES entities(entity_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_facts_entity_attr ON fact_identities(entity_id, canonical_attribute);

-- Historical assertions (immutable log of all claims)
CREATE TABLE IF NOT EXISTS assertions (
    assertion_id TEXT PRIMARY KEY,
    fact_id TEXT,
    entity_id TEXT NOT NULL,
    attribute TEXT NOT NULL,
    value_json TEXT NOT NULL,
    raw_value TEXT NOT NULL,
    temporal_expression TEXT,
    assertion_type TEXT NOT NULL,
    supersedes_assertion_id TEXT,
    session_id TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    event_start TEXT,
    event_end TEXT,
    event_status TEXT,
    temporal_type TEXT,
    FOREIGN KEY (fact_id) REFERENCES fact_identities(fact_id) ON DELETE SET NULL,
    FOREIGN KEY (entity_id) REFERENCES entities(entity_id) ON DELETE RESTRICT,
    FOREIGN KEY (supersedes_assertion_id) REFERENCES assertions(assertion_id) ON DELETE SET NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_assertions_fact ON assertions(fact_id);
CREATE INDEX IF NOT EXISTS idx_assertions_entity ON assertions(entity_id);
CREATE INDEX IF NOT EXISTS idx_assertions_session ON assertions(session_id);
CREATE INDEX IF NOT EXISTS idx_assertions_event_start ON assertions(event_start);

-- Evidence and provenance linking assertions to source messages
CREATE TABLE IF NOT EXISTS assertion_evidence (
    evidence_id TEXT PRIMARY KEY,
    assertion_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    evidence_quote TEXT NOT NULL,
    is_exact_match INTEGER NOT NULL DEFAULT 1,
    validation_status TEXT NOT NULL DEFAULT 'valid',
    validation_notes TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (assertion_id) REFERENCES assertions(assertion_id) ON DELETE CASCADE,
    FOREIGN KEY (message_id) REFERENCES messages(message_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_evidence_assertion ON assertion_evidence(assertion_id);
CREATE INDEX IF NOT EXISTS idx_evidence_message ON assertion_evidence(message_id);

-- Decision audit log (NEW, DUPLICATE, UPDATE, CORRECTION, UNRESOLVED)
CREATE TABLE IF NOT EXISTS processing_decisions (
    decision_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    candidate_assertion_id TEXT,
    matched_fact_id TEXT,
    matched_assertion_id TEXT,
    decision_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
    FOREIGN KEY (candidate_assertion_id) REFERENCES assertions(assertion_id) ON DELETE SET NULL,
    FOREIGN KEY (matched_fact_id) REFERENCES fact_identities(fact_id) ON DELETE SET NULL,
    FOREIGN KEY (matched_assertion_id) REFERENCES assertions(assertion_id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_session ON processing_decisions(session_id);

-- Log of claims that failed schema or evidence validation
CREATE TABLE IF NOT EXISTS validation_failures (
    failure_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    message_id TEXT,
    candidate_json TEXT NOT NULL,
    failure_reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
    FOREIGN KEY (message_id) REFERENCES messages(message_id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_failures_session ON validation_failures(session_id);
"""

class MigrationError(Exception):
    """Raised when database schema migration or validation fails."""
    pass

def migrate_database(conn: sqlite3.Connection) -> None:
    """
    Safely and idempotently migrates an existing SQLite database schema:
    1. Messages table migration:
       - Adds 'turn_index INTEGER NOT NULL DEFAULT 1 CHECK (turn_index >= 1)' if missing.
       - Backfills existing messages with sequential 1-based turn_index per session
         using (timestamp ASC, rowid ASC) as best-effort ordering.
       - Validates turn_index integrity (> 0 and unique within session) if already present.
       - Ensures UNIQUE INDEX idx_messages_session_turn ON messages(session_id, turn_index).
    2. Assertions table migration (Feature 2: Temporal Reasoning):
       - Adds nullable columns 'event_start', 'event_end', 'event_status', 'temporal_type'
         via ALTER TABLE if missing.
       - Preserves all existing assertions, evidence links, foreign keys, and assertion history.
       - Ensures INDEX idx_assertions_event_start ON assertions(event_start).
    """
    # -------------------------------------------------------------------------
    # 1. Check and migrate messages table (turn_index)
    # -------------------------------------------------------------------------
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone()
    if row:
        table_info = conn.execute("PRAGMA table_info(messages)").fetchall()
        cols = [r[1] for r in table_info]

        if "turn_index" not in cols:
            in_tx = conn.in_transaction
            if not in_tx:
                conn.execute("BEGIN IMMEDIATE")
            else:
                conn.execute("SAVEPOINT cram_migration")

            try:
                # Step A: Add column with SQLite-compatible NOT NULL constraint, default, and CHECK constraint
                conn.execute(
                    "ALTER TABLE messages ADD COLUMN turn_index INTEGER NOT NULL DEFAULT 1 CHECK (turn_index >= 1);"
                )

                # Step B: Backfill existing rows sequentially starting at 1 per session
                # Use (timestamp ASC, rowid ASC) as best-effort ordering.
                sessions = [
                    r[0] for r in conn.execute("SELECT DISTINCT session_id FROM messages").fetchall()
                ]
                for s_id in sessions:
                    rows = conn.execute(
                        "SELECT message_id FROM messages WHERE session_id = ? ORDER BY timestamp ASC, rowid ASC",
                        (s_id,),
                    ).fetchall()
                    for idx, r in enumerate(rows, start=1):
                        conn.execute(
                            "UPDATE messages SET turn_index = ? WHERE message_id = ?",
                            (idx, r[0]),
                        )

                # Step C: Create unique composite index after backfill
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_session_turn ON messages(session_id, turn_index);"
                )

                if not in_tx:
                    conn.commit()
                else:
                    conn.execute("RELEASE cram_migration")
            except Exception as e:
                if not in_tx:
                    conn.rollback()
                else:
                    conn.execute("ROLLBACK TO cram_migration")
                    conn.execute("RELEASE cram_migration")
                if isinstance(e, MigrationError):
                    raise
                raise MigrationError(f"Schema migration failed and was rolled back: {e}") from e
        else:
            # Column already exists. Validate integrity without modifying valid data.
            invalid_cnt = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE turn_index IS NULL OR turn_index <= 0"
            ).fetchone()[0]
            if invalid_cnt > 0:
                raise MigrationError(
                    f"Database contains {invalid_cnt} message(s) with invalid turn_index (<= 0 or NULL). "
                    "Manual inspection required."
                )

            dup_rows = conn.execute(
                "SELECT session_id, turn_index, COUNT(*) as cnt "
                "FROM messages GROUP BY session_id, turn_index HAVING cnt > 1"
            ).fetchall()
            if dup_rows:
                dup_details = ", ".join(f"session={d[0]}, index={d[1]} (count={d[2]})" for d in dup_rows)
                raise MigrationError(
                    f"Database contains duplicate turn_index records within a session: [{dup_details}]. "
                    "Manual inspection required."
                )

            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_session_turn ON messages(session_id, turn_index);"
            )

    # -------------------------------------------------------------------------
    # 2. Check and migrate assertions table (Temporal Reasoning fields)
    # -------------------------------------------------------------------------
    ass_row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='assertions'"
    ).fetchone()
    if ass_row:
        ass_table_info = conn.execute("PRAGMA table_info(assertions)").fetchall()
        ass_cols = [r[1] for r in ass_table_info]
        expected_temporal_cols = [
            ("event_start", "TEXT"),
            ("event_end", "TEXT"),
            ("event_status", "TEXT"),
            ("temporal_type", "TEXT"),
        ]
        missing_ass_cols = [c for c in expected_temporal_cols if c[0] not in ass_cols]
        if missing_ass_cols:
            in_tx = conn.in_transaction
            if not in_tx:
                conn.execute("BEGIN IMMEDIATE")
            else:
                conn.execute("SAVEPOINT cram_assertion_migration")

            try:
                for col_name, col_type in missing_ass_cols:
                    conn.execute(f"ALTER TABLE assertions ADD COLUMN {col_name} {col_type};")

                if not in_tx:
                    conn.commit()
                else:
                    conn.execute("RELEASE cram_assertion_migration")
            except Exception as e:
                if not in_tx:
                    conn.rollback()
                else:
                    conn.execute("ROLLBACK TO cram_assertion_migration")
                    conn.execute("RELEASE cram_assertion_migration")
                if isinstance(e, MigrationError):
                    raise
                raise MigrationError(f"Assertions schema migration failed and was rolled back: {e}") from e

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_assertions_event_start ON assertions(event_start);"
        )

def initialize_database(db_path: Union[str, Path]) -> None:
    """Initializes the database schema if tables do not already exist, and migrates legacy schemas."""
    conn = get_connection(db_path)
    try:
        # 1. Run safe transactional migration for legacy databases
        migrate_database(conn)
        # 2. Apply fresh schema / ensure all tables and indexes exist
        conn.executescript(SCHEMA_SQL)
    finally:
        conn.close()
