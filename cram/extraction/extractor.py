import json
import logging
from typing import List, Optional
from cram.extraction.gemini_client import GeminiClient, ExtractionError
from cram.extraction.prompts import EXTRACTION_SYSTEM_PROMPT, format_dialogue_for_extraction
from cram.models.schemas import CandidateFact, ExtractionResult, MessageRecord

logger = logging.getLogger(__name__)

class FactExtractor:
    def __init__(self, client: Optional[GeminiClient] = None):
        self.client = client or GeminiClient()

    def extract_facts(self, messages: List[MessageRecord]) -> List[CandidateFact]:
        """
        Extracts structured candidate facts from a list of conversational messages.
        """
        if not messages:
            return []

        formatted_dialogue = format_dialogue_for_extraction(messages)

        try:
            raw_json = self.client.generate_structured(
                contents=formatted_dialogue,
                response_schema=ExtractionResult,
                system_instruction=EXTRACTION_SYSTEM_PROMPT,
            )
            result = ExtractionResult.model_validate_json(raw_json)
            return result.facts
        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode JSON from model response: {e}")
            raise ExtractionError(f"Malformed JSON from Gemini: {e}") from e
        except Exception as e:
            logger.error(f"Error during structured fact extraction: {e}")
            raise ExtractionError(f"Extraction failed: {e}") from e
