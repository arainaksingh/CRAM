import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from cram.db.connection import get_connection
from cram.models.schemas import (
    AssertionRecord,
    DecisionRecord,
    EntityRecord,
    EventStatus,
    EvidenceRecord,
    FactIdentityRecord,
    MessageRecord,
    TemporalType,
    ValidationFailureRecord,
    get_utc_now_iso,
)

class Repository:
    def __init__(self, db_path: Union[str, Path]):
        self.db_path = db_path

    def _get_conn(self) -> sqlite3.Connection:
        return get_connection(self.db_path)

    # ---------------- SESSIONS ----------------

    def create_or_get_session(self, session_id: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        now = get_utc_now_iso()
        meta_str = json.dumps(metadata or {})
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id, created_at, updated_at, status, metadata_json) "
                "VALUES (?, ?, ?, 'active', ?)",
                (session_id, now, now, meta_str),
            )
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            return dict(row)

    def update_session_status(self, session_id: str, status: str) -> None:
        now = get_utc_now_iso()
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE sessions SET status = ?, updated_at = ? WHERE session_id = ?",
                (status, now, session_id),
            )

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_sessions(self) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_storage_stats(self) -> Dict[str, int]:
        """Returns live counts of all records across tables."""
        with self._get_conn() as conn:
            return {
                "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "entities": conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
                "facts": conn.execute("SELECT COUNT(*) FROM fact_identities").fetchone()[0],
                "assertions": conn.execute("SELECT COUNT(*) FROM assertions").fetchone()[0],
                "evidence": conn.execute("SELECT COUNT(*) FROM assertion_evidence").fetchone()[0],
                "decisions": conn.execute("SELECT COUNT(*) FROM processing_decisions").fetchone()[0],
                "validation_failures": conn.execute("SELECT COUNT(*) FROM validation_failures").fetchone()[0],
            }

    def delete_all_sessions(self) -> Dict[str, int]:
        """
        Permanently and atomically deletes all sessions and their derived memory:
        messages, assertions, assertion evidence, processing decisions, validation failures,
        fact identities, and entities.

        Executed in an immediate write transaction with foreign keys enforced.
        Deletions respect strict foreign-key dependency order (child tables before parent tables).
        Rolls back completely if any deletion fails.
        Idempotent on empty databases.
        """
        with self._get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")

            # Capture counts before deletion
            counts = {
                "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "entities": conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
                "facts": conn.execute("SELECT COUNT(*) FROM fact_identities").fetchone()[0],
                "assertions": conn.execute("SELECT COUNT(*) FROM assertions").fetchone()[0],
                "evidence": conn.execute("SELECT COUNT(*) FROM assertion_evidence").fetchone()[0],
                "decisions": conn.execute("SELECT COUNT(*) FROM processing_decisions").fetchone()[0],
                "validation_failures": conn.execute("SELECT COUNT(*) FROM validation_failures").fetchone()[0],
            }

            # 1. Evidence and provenance (references assertions and messages)
            conn.execute("DELETE FROM assertion_evidence")
            # 2. Decision audit logs (references sessions, assertions, facts)
            conn.execute("DELETE FROM processing_decisions")
            # 3. Validation failure logs (references sessions, messages)
            conn.execute("DELETE FROM validation_failures")
            # 4. Assertions (references sessions, facts, entities, supersedes_assertion_id)
            conn.execute("DELETE FROM assertions")
            # 5. Conceptual fact identities (references entities)
            conn.execute("DELETE FROM fact_identities")
            # 6. Messages (references sessions)
            conn.execute("DELETE FROM messages")
            # 7. Sessions (parent of messages, assertions, decisions, failures)
            conn.execute("DELETE FROM sessions")
            # 8. Entities (parent of facts and assertions; now unreferenced)
            conn.execute("DELETE FROM entities")

        return counts

    # ---------------- MESSAGES ----------------

    def insert_message(
        self,
        session_id: str,
        role: str,
        content: str,
        message_id: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> MessageRecord:
        msg_id = message_id or str(uuid.uuid4())
        ts = timestamp or get_utc_now_iso()
        self.create_or_get_session(session_id)

        with self._get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT COALESCE(MAX(turn_index), 0) + 1 AS next_idx FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            assigned_turn_index = row["next_idx"]

            conn.execute(
                "INSERT INTO messages (message_id, session_id, role, content, timestamp, turn_index, is_processed, retry_count) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, 0)",
                (msg_id, session_id, role, content, ts, assigned_turn_index),
            )
        return MessageRecord(
            message_id=msg_id,
            session_id=session_id,
            role=role,
            content=content,
            timestamp=ts,
            turn_index=assigned_turn_index,
            is_processed=False,
            retry_count=0,
            error_message=None,
        )

    def insert_messages_batch(
        self,
        session_id: str,
        messages: List[Union[Dict[str, Any], Tuple[str, str]]],
    ) -> List[MessageRecord]:
        """
        Atomically persists a batch of messages into a session within a single SQLite transaction.
        Assigns sequential turn_index values internally in transcript order, continuing from
        MAX(turn_index) (or starting at 1 if session has no prior messages).
        Protected by BEGIN IMMEDIATE against concurrent writers.
        """
        if not messages:
            return []

        self.create_or_get_session(session_id)
        now_iso = get_utc_now_iso()

        with self._get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT COALESCE(MAX(turn_index), 0) AS max_idx FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            start_idx = row["max_idx"] + 1

            records: List[MessageRecord] = []
            for i, item in enumerate(messages):
                current_turn = start_idx + i
                if isinstance(item, tuple):
                    role, content = item[0], item[1]
                    msg_id = str(uuid.uuid4())
                    ts = now_iso
                elif isinstance(item, dict):
                    role = item["role"]
                    content = item["content"]
                    msg_id = item.get("message_id") or str(uuid.uuid4())
                    ts = item.get("timestamp") or now_iso
                else:
                    role = getattr(item, "role")
                    content = getattr(item, "content")
                    msg_id = getattr(item, "message_id", None) or str(uuid.uuid4())
                    ts = getattr(item, "timestamp", None) or now_iso

                conn.execute(
                    "INSERT INTO messages (message_id, session_id, role, content, timestamp, turn_index, is_processed, retry_count) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0, 0)",
                    (msg_id, session_id, role, content, ts, current_turn),
                )
                records.append(
                    MessageRecord(
                        message_id=msg_id,
                        session_id=session_id,
                        role=role,
                        content=content,
                        timestamp=ts,
                        turn_index=current_turn,
                        is_processed=False,
                        retry_count=0,
                        error_message=None,
                    )
                )
        return records

    def _insert_message_with_turn_index(
        self,
        session_id: str,
        role: str,
        content: str,
        turn_index: int,
        message_id: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> MessageRecord:
        """Internal helper for test setups requiring explicit turn index assignment."""
        if turn_index < 1:
            raise ValueError(f"turn_index must be >= 1, got {turn_index}")
        msg_id = message_id or str(uuid.uuid4())
        ts = timestamp or get_utc_now_iso()
        self.create_or_get_session(session_id)

        with self._get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO messages (message_id, session_id, role, content, timestamp, turn_index, is_processed, retry_count) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, 0)",
                (msg_id, session_id, role, content, ts, turn_index),
            )
        return MessageRecord(
            message_id=msg_id,
            session_id=session_id,
            role=role,
            content=content,
            timestamp=ts,
            turn_index=turn_index,
            is_processed=False,
            retry_count=0,
            error_message=None,
        )

    def get_message(self, message_id: str) -> Optional[MessageRecord]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone()
            if not row:
                return None
            return MessageRecord(
                message_id=row["message_id"],
                session_id=row["session_id"],
                role=row["role"],
                content=row["content"],
                timestamp=row["timestamp"],
                turn_index=row["turn_index"],
                is_processed=bool(row["is_processed"]),
                retry_count=row["retry_count"],
                error_message=row["error_message"],
            )

    def get_unprocessed_messages(self, session_id: str) -> List[MessageRecord]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? AND is_processed = 0 ORDER BY turn_index ASC",
                (session_id,),
            ).fetchall()
            return [
                MessageRecord(
                    message_id=r["message_id"],
                    session_id=r["session_id"],
                    role=r["role"],
                    content=r["content"],
                    timestamp=r["timestamp"],
                    turn_index=r["turn_index"],
                    is_processed=bool(r["is_processed"]),
                    retry_count=r["retry_count"],
                    error_message=r["error_message"],
                )
                for r in rows
            ]

    def get_session_messages(self, session_id: str) -> List[MessageRecord]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY turn_index ASC",
                (session_id,),
            ).fetchall()
            return [
                MessageRecord(
                    message_id=r["message_id"],
                    session_id=r["session_id"],
                    role=r["role"],
                    content=r["content"],
                    timestamp=r["timestamp"],
                    turn_index=r["turn_index"],
                    is_processed=bool(r["is_processed"]),
                    retry_count=r["retry_count"],
                    error_message=r["error_message"],
                )
                for r in rows
            ]

    def mark_messages_processed(self, message_ids: List[str]) -> None:
        if not message_ids:
            return
        placeholders = ",".join("?" for _ in message_ids)
        with self._get_conn() as conn:
            conn.execute(
                f"UPDATE messages SET is_processed = 1, error_message = NULL WHERE message_id IN ({placeholders})",
                message_ids,
            )

    def increment_message_retry(self, message_id: str, error_msg: str) -> None:
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE messages SET retry_count = retry_count + 1, error_message = ? WHERE message_id = ?",
                (error_msg, message_id),
            )

    # ---------------- ENTITIES ----------------

    def get_or_create_entity(self, name: str, entity_type: str) -> EntityRecord:
        canonical_name = name.strip().lower()
        now = get_utc_now_iso()
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM entities WHERE canonical_name = ? AND entity_type = ?",
                (canonical_name, entity_type),
            ).fetchone()
            if row:
                return EntityRecord(
                    entity_id=row["entity_id"],
                    name=row["name"],
                    entity_type=row["entity_type"],
                    canonical_name=row["canonical_name"],
                    created_at=row["created_at"],
                )
            entity_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO entities (entity_id, name, entity_type, canonical_name, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (entity_id, name.strip(), entity_type, canonical_name, now),
            )
            return EntityRecord(
                entity_id=entity_id,
                name=name.strip(),
                entity_type=entity_type,
                canonical_name=canonical_name,
                created_at=now,
            )

    def get_entity(self, entity_id: str) -> Optional[EntityRecord]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM entities WHERE entity_id = ?", (entity_id,)
            ).fetchone()
            if not row:
                return None
            return EntityRecord(
                entity_id=row["entity_id"],
                name=row["name"],
                entity_type=row["entity_type"],
                canonical_name=row["canonical_name"],
                created_at=row["created_at"],
            )

    def list_entities(self) -> List[EntityRecord]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM entities ORDER BY name ASC").fetchall()
            return [
                EntityRecord(
                    entity_id=r["entity_id"],
                    name=r["name"],
                    entity_type=r["entity_type"],
                    canonical_name=r["canonical_name"],
                    created_at=r["created_at"],
                )
                for r in rows
            ]

    # ---------------- FACT IDENTITIES ----------------

    def find_fact_identity(self, entity_id: str, canonical_attribute: str) -> Optional[FactIdentityRecord]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM fact_identities WHERE entity_id = ? AND canonical_attribute = ?",
                (entity_id, canonical_attribute),
            ).fetchone()
            if not row:
                return None
            return FactIdentityRecord(
                fact_id=row["fact_id"],
                entity_id=row["entity_id"],
                attribute=row["attribute"],
                canonical_attribute=row["canonical_attribute"],
                current_assertion_id=row["current_assertion_id"],
                status=row["status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    def create_fact_identity(
        self,
        entity_id: str,
        attribute: str,
        canonical_attribute: str,
        current_assertion_id: Optional[str] = None,
    ) -> FactIdentityRecord:
        fact_id = str(uuid.uuid4())
        now = get_utc_now_iso()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO fact_identities (fact_id, entity_id, attribute, canonical_attribute, current_assertion_id, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
                (fact_id, entity_id, attribute, canonical_attribute, current_assertion_id, now, now),
            )
        return FactIdentityRecord(
            fact_id=fact_id,
            entity_id=entity_id,
            attribute=attribute,
            canonical_attribute=canonical_attribute,
            current_assertion_id=current_assertion_id,
            status="active",
            created_at=now,
            updated_at=now,
        )

    def update_fact_identity_active_assertion(
        self,
        fact_id: str,
        current_assertion_id: str,
        status: str = "active",
    ) -> None:
        now = get_utc_now_iso()
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE fact_identities SET current_assertion_id = ?, status = ?, updated_at = ? WHERE fact_id = ?",
                (current_assertion_id, status, now, fact_id),
            )

    def get_fact_identity(self, fact_id: str) -> Optional[FactIdentityRecord]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM fact_identities WHERE fact_id = ?",
                (fact_id,),
            ).fetchone()
            if not row:
                return None
            return FactIdentityRecord(
                fact_id=row["fact_id"],
                entity_id=row["entity_id"],
                attribute=row["attribute"],
                canonical_attribute=row["canonical_attribute"],
                current_assertion_id=row["current_assertion_id"],
                status=row["status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    def list_fact_identities(self, entity_id: Optional[str] = None) -> List[FactIdentityRecord]:
        with self._get_conn() as conn:
            if entity_id:
                rows = conn.execute(
                    "SELECT * FROM fact_identities WHERE entity_id = ? ORDER BY attribute ASC",
                    (entity_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM fact_identities ORDER BY created_at DESC"
                ).fetchall()
            return [
                FactIdentityRecord(
                    fact_id=r["fact_id"],
                    entity_id=r["entity_id"],
                    attribute=r["attribute"],
                    canonical_attribute=r["canonical_attribute"],
                    current_assertion_id=r["current_assertion_id"],
                    status=r["status"],
                    created_at=r["created_at"],
                    updated_at=r["updated_at"],
                )
                for r in rows
            ]

    # ---------------- ASSERTIONS ----------------

    def insert_assertion(
        self,
        entity_id: str,
        attribute: str,
        value_json: str,
        raw_value: str,
        session_id: str,
        assertion_type: str,
        fact_id: Optional[str] = None,
        temporal_expression: Optional[str] = None,
        event_start: Optional[str] = None,
        event_end: Optional[str] = None,
        event_status: Optional[EventStatus] = None,
        temporal_type: Optional[TemporalType] = None,
        supersedes_assertion_id: Optional[str] = None,
        confidence: float = 1.0,
        assertion_id: Optional[str] = None,
    ) -> AssertionRecord:
        ass_id = assertion_id or str(uuid.uuid4())
        now = get_utc_now_iso()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO assertions (assertion_id, fact_id, entity_id, attribute, value_json, raw_value, "
                "temporal_expression, assertion_type, supersedes_assertion_id, session_id, confidence, created_at, "
                "event_start, event_end, event_status, temporal_type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ass_id,
                    fact_id,
                    entity_id,
                    attribute,
                    value_json,
                    raw_value,
                    temporal_expression,
                    assertion_type,
                    supersedes_assertion_id,
                    session_id,
                    confidence,
                    now,
                    event_start,
                    event_end,
                    event_status,
                    temporal_type,
                ),
            )
        return AssertionRecord(
            assertion_id=ass_id,
            fact_id=fact_id,
            entity_id=entity_id,
            attribute=attribute,
            value_json=value_json,
            raw_value=raw_value,
            temporal_expression=temporal_expression,
            event_start=event_start,
            event_end=event_end,
            event_status=event_status,
            temporal_type=temporal_type,
            assertion_type=assertion_type,
            supersedes_assertion_id=supersedes_assertion_id,
            session_id=session_id,
            confidence=confidence,
            created_at=now,
        )

    def get_assertion(self, assertion_id: str) -> Optional[AssertionRecord]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM assertions WHERE assertion_id = ?", (assertion_id,)
            ).fetchone()
            if not row:
                return None
            return AssertionRecord(
                assertion_id=row["assertion_id"],
                fact_id=row["fact_id"],
                entity_id=row["entity_id"],
                attribute=row["attribute"],
                value_json=row["value_json"],
                raw_value=row["raw_value"],
                temporal_expression=row["temporal_expression"],
                event_start=row["event_start"] if "event_start" in row.keys() else None,
                event_end=row["event_end"] if "event_end" in row.keys() else None,
                event_status=row["event_status"] if "event_status" in row.keys() else None,
                temporal_type=row["temporal_type"] if "temporal_type" in row.keys() else None,
                assertion_type=row["assertion_type"],
                supersedes_assertion_id=row["supersedes_assertion_id"],
                session_id=row["session_id"],
                confidence=row["confidence"],
                created_at=row["created_at"],
            )

    def list_assertions(
        self,
        fact_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> List[AssertionRecord]:
        with self._get_conn() as conn:
            query = "SELECT * FROM assertions WHERE 1=1"
            params: List[Any] = []
            if fact_id:
                query += " AND fact_id = ?"
                params.append(fact_id)
            if session_id:
                query += " AND session_id = ?"
                params.append(session_id)
            query += " ORDER BY created_at DESC"
            rows = conn.execute(query, params).fetchall()
            return [
                AssertionRecord(
                    assertion_id=r["assertion_id"],
                    fact_id=r["fact_id"],
                    entity_id=r["entity_id"],
                    attribute=r["attribute"],
                    value_json=r["value_json"],
                    raw_value=r["raw_value"],
                    temporal_expression=r["temporal_expression"],
                    event_start=r["event_start"] if "event_start" in r.keys() else None,
                    event_end=r["event_end"] if "event_end" in r.keys() else None,
                    event_status=r["event_status"] if "event_status" in r.keys() else None,
                    temporal_type=r["temporal_type"] if "temporal_type" in r.keys() else None,
                    assertion_type=r["assertion_type"],
                    supersedes_assertion_id=r["supersedes_assertion_id"],
                    session_id=r["session_id"],
                    confidence=r["confidence"],
                    created_at=r["created_at"],
                )
                for r in rows
            ]

    # ---------------- EVIDENCE ----------------

    def insert_evidence(
        self,
        assertion_id: str,
        message_id: str,
        evidence_quote: str,
        is_exact_match: bool = True,
        validation_status: str = "valid",
        validation_notes: Optional[str] = None,
        evidence_id: Optional[str] = None,
    ) -> EvidenceRecord:
        ev_id = evidence_id or str(uuid.uuid4())
        now = get_utc_now_iso()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO assertion_evidence (evidence_id, assertion_id, message_id, evidence_quote, "
                "is_exact_match, validation_status, validation_notes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ev_id,
                    assertion_id,
                    message_id,
                    evidence_quote,
                    1 if is_exact_match else 0,
                    validation_status,
                    validation_notes,
                    now,
                ),
            )
        return EvidenceRecord(
            evidence_id=ev_id,
            assertion_id=assertion_id,
            message_id=message_id,
            evidence_quote=evidence_quote,
            is_exact_match=is_exact_match,
            validation_status=validation_status,
            validation_notes=validation_notes,
            created_at=now,
        )

    def get_evidence_for_assertion(self, assertion_id: str) -> List[EvidenceRecord]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM assertion_evidence WHERE assertion_id = ?",
                (assertion_id,),
            ).fetchall()
            return [
                EvidenceRecord(
                    evidence_id=r["evidence_id"],
                    assertion_id=r["assertion_id"],
                    message_id=r["message_id"],
                    evidence_quote=r["evidence_quote"],
                    is_exact_match=bool(r["is_exact_match"]),
                    validation_status=r["validation_status"],
                    validation_notes=r["validation_notes"],
                    created_at=r["created_at"],
                )
                for r in rows
            ]

    def list_all_evidence(self) -> List[EvidenceRecord]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM assertion_evidence ORDER BY created_at DESC"
            ).fetchall()
            return [
                EvidenceRecord(
                    evidence_id=r["evidence_id"],
                    assertion_id=r["assertion_id"],
                    message_id=r["message_id"],
                    evidence_quote=r["evidence_quote"],
                    is_exact_match=bool(r["is_exact_match"]),
                    validation_status=r["validation_status"],
                    validation_notes=r["validation_notes"],
                    created_at=r["created_at"],
                )
                for r in rows
            ]

    # ---------------- DECISIONS ----------------

    def insert_decision(
        self,
        session_id: str,
        decision_type: str,
        reason: str,
        candidate_assertion_id: Optional[str] = None,
        matched_fact_id: Optional[str] = None,
        matched_assertion_id: Optional[str] = None,
        confidence: float = 1.0,
        decision_id: Optional[str] = None,
    ) -> DecisionRecord:
        dec_id = decision_id or str(uuid.uuid4())
        now = get_utc_now_iso()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO processing_decisions (decision_id, session_id, candidate_assertion_id, "
                "matched_fact_id, matched_assertion_id, decision_type, reason, confidence, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    dec_id,
                    session_id,
                    candidate_assertion_id,
                    matched_fact_id,
                    matched_assertion_id,
                    decision_type,
                    reason,
                    confidence,
                    now,
                ),
            )
        return DecisionRecord(
            decision_id=dec_id,
            session_id=session_id,
            candidate_assertion_id=candidate_assertion_id,
            matched_fact_id=matched_fact_id,
            matched_assertion_id=matched_assertion_id,
            decision_type=decision_type,
            reason=reason,
            confidence=confidence,
            created_at=now,
        )

    def list_decisions(self, session_id: Optional[str] = None) -> List[DecisionRecord]:
        with self._get_conn() as conn:
            if session_id:
                rows = conn.execute(
                    "SELECT * FROM processing_decisions WHERE session_id = ? ORDER BY created_at DESC",
                    (session_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM processing_decisions ORDER BY created_at DESC"
                ).fetchall()
            return [
                DecisionRecord(
                    decision_id=r["decision_id"],
                    session_id=r["session_id"],
                    candidate_assertion_id=r["candidate_assertion_id"],
                    matched_fact_id=r["matched_fact_id"],
                    matched_assertion_id=r["matched_assertion_id"],
                    decision_type=r["decision_type"],
                    reason=r["reason"],
                    confidence=r["confidence"],
                    created_at=r["created_at"],
                )
                for r in rows
            ]

    # ---------------- VALIDATION FAILURES ----------------

    def insert_validation_failure(
        self,
        session_id: str,
        candidate_json: str,
        failure_reason: str,
        message_id: Optional[str] = None,
        failure_id: Optional[str] = None,
    ) -> ValidationFailureRecord:
        fail_id = failure_id or str(uuid.uuid4())
        now = get_utc_now_iso()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO validation_failures (failure_id, session_id, message_id, candidate_json, "
                "failure_reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (fail_id, session_id, message_id, candidate_json, failure_reason, now),
            )
        return ValidationFailureRecord(
            failure_id=fail_id,
            session_id=session_id,
            message_id=message_id,
            candidate_json=candidate_json,
            failure_reason=failure_reason,
            created_at=now,
        )

    def list_validation_failures(self, session_id: Optional[str] = None) -> List[ValidationFailureRecord]:
        with self._get_conn() as conn:
            if session_id:
                rows = conn.execute(
                    "SELECT * FROM validation_failures WHERE session_id = ? ORDER BY created_at DESC",
                    (session_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM validation_failures ORDER BY created_at DESC"
                ).fetchall()
            return [
                ValidationFailureRecord(
                    failure_id=r["failure_id"],
                    session_id=r["session_id"],
                    message_id=r["message_id"],
                    candidate_json=r["candidate_json"],
                    failure_reason=r["failure_reason"],
                    created_at=r["created_at"],
                )
                for r in rows
            ]
