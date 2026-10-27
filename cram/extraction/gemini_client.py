import time
import logging
from typing import Optional, Type
from pydantic import BaseModel
from google import genai
from google.genai import types
from google.genai.errors import APIError, ClientError, ServerError
from cram.config import config

logger = logging.getLogger(__name__)

class CRAMError(Exception):
    """Base exception for CRAM errors."""
    pass

class AuthenticationError(CRAMError):
    """Raised when authentication with Gemini API fails."""
    pass

class RateLimitError(CRAMError):
    """Raised when rate limits or capacity constraints are exceeded."""
    pass

class ModelUnavailableError(CRAMError):
    """Raised when the specified model is not available."""
    pass

class ExtractionError(CRAMError):
    """Raised when extraction fails unexpectedly."""
    pass

class GeminiClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        model_name: Optional[str] = None,
        max_retries: Optional[int] = None,
        timeout: Optional[float] = None,
        client: Optional[genai.Client] = None,
    ):
        self.api_key = api_key if api_key is not None else config.gemini_api_key
        self.model_name = model_name or config.model_name
        self.max_retries = max_retries if max_retries is not None else config.max_retries
        self.timeout = timeout if timeout is not None else config.request_timeout

        if not self.api_key and client is None:
            raise AuthenticationError(
                "Gemini API key is not configured. Set GEMINI_API_KEY in .env or your environment."
            )

        self._client = client or genai.Client(api_key=self.api_key)

    def generate_structured(
        self,
        contents: str,
        response_schema: Type[BaseModel],
        system_instruction: Optional[str] = None,
    ) -> str:
        """
        Calls Gemini with structured JSON schema output and bounded exponential retries.
        """
        attempt = 0
        backoff = 2.0

        gen_config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=response_schema,
            system_instruction=system_instruction,
        )

        while attempt < self.max_retries:
            attempt += 1
            try:
                response = self._client.models.generate_content(
                    model=self.model_name,
                    contents=contents,
                    config=gen_config,
                )
                if not response.text:
                    raise ExtractionError("Gemini returned an empty response body.")
                return response.text.strip()

            except (ServerError, APIError) as e:
                status_code = getattr(e, "code", None) or getattr(e, "status_code", None)
                msg = str(e)

                # Check for Authentication failures
                if status_code in (401, 403) or "API_KEY_INVALID" in msg or "PERMISSION_DENIED" in msg:
                    raise AuthenticationError(f"Authentication failed: {msg}") from e

                # Check for Model unavailable
                if status_code == 404 or "NOT_FOUND" in msg or "is no longer available" in msg:
                    raise ModelUnavailableError(
                        f"Configured model '{self.model_name}' is not available: {msg}"
                    ) from e

                # Rate limits or temporary overload (503 / 429)
                if status_code in (429, 503) or "UNAVAILABLE" in msg or "RESOURCE_EXHAUSTED" in msg:
                    logger.warning(
                        f"Transient error from Gemini (attempt {attempt}/{self.max_retries}): {msg}. "
                        f"Retrying in {backoff:.1f}s..."
                    )
                    if attempt >= self.max_retries:
                        raise RateLimitError(
                            f"Exceeded max retries ({self.max_retries}) due to rate limits or overload: {msg}"
                        ) from e
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue

                # Non-retryable client error (e.g. 400 Bad Request)
                if status_code and 400 <= status_code < 500:
                    raise ExtractionError(f"Gemini client error (code {status_code}): {msg}") from e

                # Other errors
                if attempt >= self.max_retries:
                    raise ExtractionError(f"Gemini API error after {attempt} attempts: {msg}") from e
                time.sleep(backoff)
                backoff *= 2.0

            except Exception as e:
                logger.error(f"Unexpected error during Gemini call: {e}")
                if attempt >= self.max_retries:
                    raise ExtractionError(f"Unexpected error calling Gemini: {e}") from e
                time.sleep(backoff)
                backoff *= 2.0

        raise ExtractionError(f"Failed to extract facts after {self.max_retries} attempts.")
