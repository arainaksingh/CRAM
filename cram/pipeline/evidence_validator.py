import re
from typing import Dict, List, Optional, Tuple
from pydantic import BaseModel
from cram.models.schemas import CandidateFact, MessageRecord

class ValidatedEvidenceItem(BaseModel):
    message_id: str
    quote: str
    is_exact_match: bool
    status: str  # 'valid', 'partial', 'invalid', 'ungrounded'
    notes: Optional[str] = None

class EvidenceValidationResult(BaseModel):
    is_valid: bool
    evidence_items: List[ValidatedEvidenceItem] = []
    failure_reason: Optional[str] = None

def normalize_text(text: str) -> str:
    """Collapses whitespace and lowercases for fuzzy/normalized checking."""
    return re.sub(r"\s+", " ", text.strip().lower())

def split_substantive_sentences(text: str) -> List[str]:
    """
    Splits text into meaningful sentences or clauses, ignoring short salutations
    and boilerplate greetings/closings.
    """
    raw_parts = re.split(r"[\n.!?]+", text)
    sentences = []
    salutations = {
        "hi", "hello", "hey", "sure", "thanks", "thank you",
        "let me know if you need anything else", "hope this helps",
    }
    for p in raw_parts:
        cleaned = p.strip()
        if not cleaned:
            continue
        if cleaned.lower() in salutations:
            continue
        if len(cleaned.split()) >= 3 or len(cleaned) >= 12:
            sentences.append(cleaned)
    return sentences

# Regex patterns for confirmation evaluation
NEGATION_REGEX = re.compile(
    r"\b(no|nope|not|incorrect|false|wrong|untrue|never|isn't|aren't|wasn't|weren't|don't|doesn't|didn't|can't|won't)\b",
    re.IGNORECASE,
)
CONTRAST_REGEX = re.compile(
    r"\b(but|however|except|although|though|except that|other than|apart from)\b",
    re.IGNORECASE,
)
UNCERTAINTY_REGEX = re.compile(
    r"\b(maybe|perhaps|possibly|probably|might|could be|i think|i guess|not sure|unsure|unclear|assuming|suppose)\b",
    re.IGNORECASE,
)
AFFIRMATIVE_PATTERNS = [
    r"\bthat('?s| is) (correct|right|true|accurate)\b",
    r"\b(yes|yeah|yep|yup)\b",
    r"\b(correct|exactly|indeed|absolutely|definitely|precisely)\b",
    r"\b(confirmed|i confirm|agreed|i agree)\b",
]

def evaluate_user_confirmation(
    user_text: str,
    assistant_text: str,
    candidate: CandidateFact,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Evaluates whether a user's response is an unambiguous, explicit confirmation
    of an assistant statement.

    Returns:
        (is_confirmed, confirmation_quote, failure_reason)
    """
    u_text = user_text.strip()
    if not u_text:
        return False, None, "User confirmation message is empty."

    # 1. Reject questions
    if "?" in u_text:
        return False, None, "User response is a question, not an explicit confirmation."

    # 2. Reject negation or contradictions
    if NEGATION_REGEX.search(u_text):
        return False, None, "User response contains negation or contradiction."

    # 3. Reject contrastive or qualifying statements (e.g. "Yes, but that date is wrong")
    if CONTRAST_REGEX.search(u_text):
        return False, None, "User response contains qualification or contrastive statement."

    # 4. Reject uncertainty or hedging (e.g. "Maybe", "I think so")
    if UNCERTAINTY_REGEX.search(u_text):
        return False, None, "User response expresses uncertainty rather than explicit confirmation."

    # 5. Must contain an explicit affirmative confirmation signal
    has_affirmation = any(re.search(pat, u_text, re.IGNORECASE) for pat in AFFIRMATIVE_PATTERNS)
    if not has_affirmation:
        return False, None, "User response does not contain an explicit affirmative confirmation."

    # 6. Check for compound assistant message (Ambiguity Check)
    # If the assistant message contains multiple substantive statements,
    # a bare confirmation like "Yes" is ambiguous unless the user specifies the claim.
    substantive_asst_sentences = split_substantive_sentences(assistant_text)
    if len(substantive_asst_sentences) >= 2:
        attr_words = set(re.findall(r"[a-zA-Z0-9]+", (candidate.attribute or "").lower()))
        val_words = set(re.findall(r"[a-zA-Z0-9]+", (candidate.raw_value or "").lower()))
        user_words = set(re.findall(r"[a-zA-Z0-9]+", u_text.lower()))
        common_stops = {"the", "a", "an", "is", "of", "in", "to", "at", "for"}
        attr_words -= common_stops
        val_words -= common_stops

        has_specific_cue = bool((attr_words & user_words) or (val_words & user_words))
        if not has_specific_cue:
            return (
                False,
                None,
                "Ambiguous confirmation: assistant message contains multiple distinct claims and user response does not specify which claim is confirmed.",
            )

    # Determine confirmation quote
    if candidate.confirmation_quote and candidate.confirmation_quote.strip() in u_text:
        confirm_quote = candidate.confirmation_quote.strip()
    else:
        confirm_quote = u_text

    return True, confirm_quote, None

class EvidenceValidator:
    def evaluate_user_confirmation(
        self,
        user_text: str,
        assistant_text: str,
        candidate: CandidateFact,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        return evaluate_user_confirmation(user_text, assistant_text, candidate)

    def validate_fact_evidence(
        self,
        candidate: CandidateFact,
        messages_by_id: Dict[str, MessageRecord],
    ) -> EvidenceValidationResult:
        """
        Validates that candidate fact's evidence quote and source message IDs
        are grounded in actual conversation messages and adhere to the
        evidence-trust policy:
        - Rule A: Direct user evidence is accepted.
        - Rule B: Assistant-only evidence is rejected as insufficient for real-world facts.
        - Rule C: Explicit user confirmation of an assistant statement is accepted
                  only if conservative, unambiguous, and adjacent.
        """
        quote = (candidate.evidence_quote or "").strip()
        if not quote:
            return EvidenceValidationResult(
                is_valid=False,
                failure_reason="Evidence quote is empty or missing.",
            )

        source_ids = list(candidate.source_message_ids or [])

        # If LLM omitted source_message_ids, attempt to recover by searching messages
        if not source_ids:
            found_ids = []
            for m_id, m in messages_by_id.items():
                if quote in m.content:
                    found_ids.append(m_id)
            if not found_ids:
                norm_quote = normalize_text(quote)
                for m_id, m in messages_by_id.items():
                    if norm_quote in normalize_text(m.content):
                        found_ids.append(m_id)
            if found_ids:
                source_ids = found_ids
            else:
                return EvidenceValidationResult(
                    is_valid=False,
                    failure_reason="No source message referenced and evidence quote not found in any message.",
                )

        # Verify all referenced source message IDs exist in session context
        for m_id in source_ids:
            if m_id not in messages_by_id:
                return EvidenceValidationResult(
                    is_valid=False,
                    evidence_items=[
                        ValidatedEvidenceItem(
                            message_id=m_id,
                            quote=quote,
                            is_exact_match=False,
                            status="invalid",
                            notes=f"Message ID '{m_id}' not found in session context.",
                        )
                    ],
                    failure_reason=f"Referenced message ID '{m_id}' does not exist in session.",
                )

        # Identify which message contains the claim's evidence_quote
        claim_msg: Optional[MessageRecord] = None
        claim_is_exact = False
        for m_id in source_ids:
            m = messages_by_id[m_id]
            if quote in m.content:
                claim_msg = m
                claim_is_exact = True
                break
            elif normalize_text(quote) in normalize_text(m.content):
                claim_msg = m
                claim_is_exact = False
                break

        if claim_msg is None:
            # Check if quote exists in any message in session context
            for m in messages_by_id.values():
                if quote in m.content:
                    claim_msg = m
                    claim_is_exact = True
                    break
                elif normalize_text(quote) in normalize_text(m.content):
                    claim_msg = m
                    claim_is_exact = False
                    break

        if claim_msg is None:
            first_id = source_ids[0]
            return EvidenceValidationResult(
                is_valid=False,
                evidence_items=[
                    ValidatedEvidenceItem(
                        message_id=first_id,
                        quote=quote,
                        is_exact_match=False,
                        status="ungrounded",
                        notes="Quote text not present in specified message.",
                    )
                ],
                failure_reason=f"Evidence quote '{quote}' not found in message '{first_id}' text.",
            )

        # -------------------------------------------------------------
        # Rule A: Direct User Evidence
        # -------------------------------------------------------------
        if claim_msg.role == "user":
            evidence_items = []
            failure_reasons = []
            for m_id in source_ids:
                m = messages_by_id[m_id]
                if quote in m.content:
                    evidence_items.append(
                        ValidatedEvidenceItem(
                            message_id=m_id,
                            quote=quote,
                            is_exact_match=True,
                            status="valid",
                            notes="Direct user-authored evidence.",
                        )
                    )
                elif normalize_text(quote) in normalize_text(m.content):
                    evidence_items.append(
                        ValidatedEvidenceItem(
                            message_id=m_id,
                            quote=quote,
                            is_exact_match=False,
                            status="partial",
                            notes="Quote matches normalized user message text.",
                        )
                    )
                else:
                    failure_reasons.append(
                        f"Evidence quote '{quote}' not found in message '{m_id}' text."
                    )
                    evidence_items.append(
                        ValidatedEvidenceItem(
                            message_id=m_id,
                            quote=quote,
                            is_exact_match=False,
                            status="ungrounded",
                            notes="Quote text not present in specified message.",
                        )
                    )
            if failure_reasons:
                return EvidenceValidationResult(
                    is_valid=False,
                    evidence_items=evidence_items,
                    failure_reason="; ".join(failure_reasons),
                )
            if not evidence_items:
                evidence_items.append(
                    ValidatedEvidenceItem(
                        message_id=claim_msg.message_id,
                        quote=quote,
                        is_exact_match=claim_is_exact,
                        status="valid" if claim_is_exact else "partial",
                        notes="Direct user-authored evidence." if claim_is_exact else "Quote matches normalized user message text.",
                    )
                )
            return EvidenceValidationResult(
                is_valid=True,
                evidence_items=evidence_items,
                failure_reason=None,
            )

        # -------------------------------------------------------------
        # Claim originated from an ASSISTANT message
        # -------------------------------------------------------------
        # Assistant-authored claim requires explicit user confirmation.
        # Find candidate confirmation message:
        confirm_msg: Optional[MessageRecord] = None

        # 1. Explicit confirmation_message_id on candidate
        if candidate.confirmation_message_id and candidate.confirmation_message_id in messages_by_id:
            candidate_confirm = messages_by_id[candidate.confirmation_message_id]
            if candidate_confirm.role == "user":
                confirm_msg = candidate_confirm

        # 2. Check other messages in source_ids
        if confirm_msg is None:
            for m_id in source_ids:
                if m_id != claim_msg.message_id:
                    m = messages_by_id[m_id]
                    if m.role == "user":
                        confirm_msg = m
                        break

        # 3. Check immediately succeeding turn in session context
        if confirm_msg is None:
            if getattr(claim_msg, "turn_index", None) is not None:
                target_turn = claim_msg.turn_index + 1
                for m in messages_by_id.values():
                    if m.session_id == claim_msg.session_id and getattr(m, "turn_index", None) == target_turn:
                        if m.role == "user":
                            confirm_msg = m
                        break
            else:
                # If turn_index is not available, find next user message in session
                session_msgs = [m for m in messages_by_id.values() if m.session_id == claim_msg.session_id]
                session_msgs_sorted = sorted(session_msgs, key=lambda x: (x.turn_index or 0, x.timestamp or ""))
                for idx, m in enumerate(session_msgs_sorted):
                    if m.message_id == claim_msg.message_id and idx + 1 < len(session_msgs_sorted):
                        next_msg = session_msgs_sorted[idx + 1]
                        if next_msg.role == "user":
                            confirm_msg = next_msg
                        break

        # If no user confirmation message exists at all -> Rule B: Assistant-only evidence rejected
        if confirm_msg is None:
            return EvidenceValidationResult(
                is_valid=False,
                evidence_items=[
                    ValidatedEvidenceItem(
                        message_id=claim_msg.message_id,
                        quote=quote,
                        is_exact_match=claim_is_exact,
                        status="invalid",
                        notes="Rejected: assistant-only evidence cannot establish durable real-world facts.",
                    )
                ],
                failure_reason="Assistant-only evidence is insufficient to establish real-world facts.",
            )

        # Check turn adjacency constraint:
        # A confirmation must be in the immediately succeeding turn after the assistant claim.
        if (
            getattr(claim_msg, "turn_index", None) is not None
            and getattr(confirm_msg, "turn_index", None) is not None
        ):
            if confirm_msg.turn_index != claim_msg.turn_index + 1:
                return EvidenceValidationResult(
                    is_valid=False,
                    evidence_items=[
                        ValidatedEvidenceItem(
                            message_id=confirm_msg.message_id,
                            quote=confirm_msg.content,
                            is_exact_match=False,
                            status="invalid",
                            notes="Rejected: user confirmation does not immediately succeed the assistant claim.",
                        )
                    ],
                    failure_reason="Confirmation must immediately succeed the assistant claim (non-adjacent turn).",
                )

        # Evaluate confirmation phrasing and ambiguity
        is_confirmed, confirm_quote, failure_reason = self.evaluate_user_confirmation(
            user_text=confirm_msg.content,
            assistant_text=claim_msg.content,
            candidate=candidate,
        )

        if not is_confirmed or confirm_quote is None:
            fallback_quote = confirm_quote if confirm_quote is not None else confirm_msg.content
            return EvidenceValidationResult(
                is_valid=False,
                evidence_items=[
                    ValidatedEvidenceItem(
                        message_id=confirm_msg.message_id,
                        quote=fallback_quote,
                        is_exact_match=False,
                        status="invalid",
                        notes=f"Confirmation rejected: {failure_reason or 'Confirmation quote is missing.'}",
                    )
                ],
                failure_reason=failure_reason or "User response did not confirm assistant claim.",
            )

        # Successful Rule C confirmation!
        # Return two evidence items:
        # 1. User confirmation item (decisive user-authored evidence)
        user_is_exact = confirm_quote in confirm_msg.content
        user_item = ValidatedEvidenceItem(
            message_id=confirm_msg.message_id,
            quote=confirm_quote,
            is_exact_match=user_is_exact,
            status="valid" if user_is_exact else "partial",
            notes="Explicit user confirmation of assistant claim.",
        )
        # 2. Corroborating assistant claim item
        asst_item = ValidatedEvidenceItem(
            message_id=claim_msg.message_id,
            quote=quote,
            is_exact_match=claim_is_exact,
            status="valid" if claim_is_exact else "partial",
            notes="Assistant-authored claim confirmed by user.",
        )

        return EvidenceValidationResult(
            is_valid=True,
            evidence_items=[user_item, asst_item],
            failure_reason=None,
        )
