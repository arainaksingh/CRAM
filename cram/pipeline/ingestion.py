"""
Ingestion Service for CRAM (Context Resolution in AI Memory).

This module provides reliable, validated message and transcript ingestion:
- Normalizes role headers ('user' and 'assistant', case-insensitively).
- Preserves multiline message bodies, paragraph boundaries, and whitespace formatting.
- Rejects empty transcripts, empty message bodies, unrecognized role prefixes, and preamble text.
- Atomically batches insertions into SQLite using the Repository.
- Strict isolation: Saving or importing messages does NOT invoke Gemini extraction or fact processing.

Inherent Ambiguities in Transcript Formatting:
---------------------------------------------
In line-oriented dialogue transcripts, turns begin with '<Role>:' prefixes.
A syntactic ambiguity arises when continuation lines in a message contain colons,
such as 'Note: ...', 'Port: 8080', or URLs ('http://...').
This parser resolves this ambiguity by:
1. Recognizing 'User:' and 'Assistant:' (case-insensitive) as turn demarcators.
2. Treating known continuation labels ('Note:', 'Warning:', 'Example:', 'http:', etc.)
   occurring within an active turn as message content.
3. Explicitly rejecting other '<Word>:' labels (such as 'System:', 'Admin:', 'Bot:')
   as unsupported role prefixes rather than misinterpreting them as assistant turns or folding them in.
4. Requiring that transcripts start with a valid role prefix; any leading text without
   a role prefix is rejected as invalid preamble.
"""

import re
from typing import List, Union
from pydantic import BaseModel, Field
from cram.db.repository import Repository
from cram.models.schemas import MessageRecord

ROLE_PREFIX_RE = re.compile(r"^(?P<role>[A-Za-z0-9_-]+)\s*:\s*(?P<content>.*)$")
ALLOWED_CONTINUATION_PREFIXES = {
    "note", "warning", "example", "tip", "notice",
    "ps", "p.s", "http", "https", "file", "url", "step", "todo"
}


class TranscriptParseError(Exception):
    """Raised when transcript parsing or structural validation fails."""
    pass


class ParsedTurn(BaseModel):
    """A validated conversation turn extracted from a transcript."""
    role: str = Field(..., description="Normalized role: 'user' or 'assistant'")
    content: str = Field(..., description="Full message content, preserving multiline structure")


def parse_transcript(text: str) -> List[ParsedTurn]:
    """
    Parses a raw multi-turn transcript string into a sequence of validated ParsedTurn objects.

    Rules:
    - Recognizes 'User:' and 'Assistant:' (case-insensitive) as start of turns.
    - Preserves internal newlines and paragraph breaks.
    - Supports CRLF and LF line endings.
    - Normalizes role names to lowercase ('user', 'assistant').
    - Rejects empty transcripts and messages with empty content.
    - Rejects unknown role prefixes (e.g., 'System:', 'Admin:') and invalid preambles.
    """
    if not text or not text.strip():
        raise TranscriptParseError("Transcript cannot be empty.")

    # Normalize CRLF and CR to LF
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")

    parsed_turns: List[ParsedTurn] = []
    current_role: Union[str, None] = None
    current_lines: List[str] = []
    turn_count = 0

    for line_idx, line in enumerate(lines, start=1):
        match = ROLE_PREFIX_RE.match(line)
        if match:
            role_str = match.group("role").strip()
            role_lower = role_str.lower()
            if role_lower in ("user", "assistant"):
                # Save preceding turn
                if current_role is not None:
                    content_str = "\n".join(current_lines).strip()
                    if not content_str:
                        raise TranscriptParseError(f"Turn {turn_count} (role '{current_role}') has empty content.")
                    parsed_turns.append(ParsedTurn(role=current_role, content=content_str))

                turn_count += 1
                current_role = role_lower
                first_line = match.group("content")
                current_lines = [first_line] if first_line else []
                continue
            else:
                # Line matched a colon pattern with non-user/assistant prefix
                if current_role is None:
                    raise TranscriptParseError(
                        f"Unsupported role prefix '{role_str}:' on line {line_idx}. Expected 'User:' or 'Assistant:'."
                    )
                elif role_lower in ALLOWED_CONTINUATION_PREFIXES:
                    current_lines.append(line)
                    continue
                else:
                    raise TranscriptParseError(
                        f"Unsupported role prefix '{role_str}:' on line {line_idx}. Expected 'User:' or 'Assistant:'."
                    )

        # Line did not match ROLE_PREFIX_RE
        if current_role is None:
            stripped = line.strip()
            if stripped:
                raise TranscriptParseError(
                    f"Preamble text found before first role declaration on line {line_idx}: '{stripped}'"
                )
            # Ignore empty lines before first turn
            continue

        current_lines.append(line)

    # Process final turn
    if current_role is not None:
        content_str = "\n".join(current_lines).strip()
        if not content_str:
            raise TranscriptParseError(f"Turn {turn_count} (role '{current_role}') has empty content.")
        parsed_turns.append(ParsedTurn(role=current_role, content=content_str))

    if not parsed_turns:
        raise TranscriptParseError("No valid turns found in transcript.")

    return parsed_turns


class IngestionService:
    """
    Service responsible for message and transcript ingestion into CRAM storage.
    Strictly handles parsing, validation, and persistence without invoking fact processing.
    """

    def __init__(self, repository: Repository):
        self.repository = repository

    def ingest_message(self, session_id: str, role: str, content: str) -> MessageRecord:
        """
        Validates and persists a single message into the repository.
        Does not trigger fact extraction or background processing.
        """
        role_clean = role.strip().lower()
        if role_clean not in ("user", "assistant"):
            raise ValueError(f"Invalid role '{role}'. Allowed roles are 'user' and 'assistant'.")

        content_clean = content.strip()
        if not content_clean:
            raise ValueError("Message content cannot be empty.")

        return self.repository.insert_message(
            session_id=session_id,
            role=role_clean,
            content=content_clean,
        )

    def ingest_transcript(self, session_id: str, transcript_text: str) -> List[MessageRecord]:
        """
        Parses, validates, and atomically persists a multi-turn transcript into the session.
        Validates the entire transcript before any database changes are made.
        All messages are persisted in a single transaction with contiguous turn_indexes.
        Does not trigger fact extraction or background processing.
        """
        turns = parse_transcript(transcript_text)
        return self.repository.insert_messages_batch(
            session_id=session_id,
            messages=[{"role": t.role, "content": t.content} for t in turns],
        )
