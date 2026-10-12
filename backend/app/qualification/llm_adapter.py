"""
LLM adapter interface and implementations for lead qualification extraction.
Follows the Adapter pattern to decouple business logic from specific AI providers.
Supports MockLLMAdapter for testing and LiteLLMAdapter for OpenAI/Gemini/Ollama.
"""

import hashlib
import json
from typing import Any, Protocol, runtime_checkable

import structlog
from pydantic import BaseModel, Field, field_validator

from app.core.config import get_settings

logger = structlog.get_logger()

ALLOWED_PROPERTY_TYPES = {"APARTMENT", "VILLA", "PLOT", "COMMERCIAL", "PENTHOUSE"}
ALLOWED_PURPOSES = {"BUY", "RENT", "INVEST"}


class LLMAdapterError(Exception):
    """Raised when an external LLM request fails or returns unparseable content."""
    pass


class QualificationExtraction(BaseModel):
    """Structured extraction of lead intent from conversation messages."""

    budget_min: int | None = Field(default=None, ge=0)
    budget_max: int | None = Field(default=None, ge=0)
    property_type: str | None = None
    bhk: int | None = Field(default=None, ge=1, le=10)
    preferred_location: str | None = None
    purpose: str | None = None
    move_in_timeline: str | None = None
    confidence_scores: dict[str, float] = Field(default_factory=dict)
    summary: str = Field(default="", max_length=500)
    raw_response: str = ""

    @field_validator("property_type", mode="before")
    @classmethod
    def validate_property_type(cls, v: Any) -> str | None:
        if v is None:
            return None
        s = str(v).strip().upper()
        if s not in ALLOWED_PROPERTY_TYPES:
            raise ValueError(f"Invalid property_type: {v}. Must be one of {ALLOWED_PROPERTY_TYPES}")
        return s

    @field_validator("purpose", mode="before")
    @classmethod
    def validate_purpose(cls, v: Any) -> str | None:
        if v is None:
            return None
        s = str(v).strip().upper()
        if s not in ALLOWED_PURPOSES:
            raise ValueError(f"Invalid purpose: {v}. Must be one of {ALLOWED_PURPOSES}")
        return s


@runtime_checkable
class LLMAdapter(Protocol):
    """Protocol for pluggable LLM qualification engines."""

    async def extract_qualification(
        self,
        messages: list[str],
    ) -> tuple[QualificationExtraction, str, str]:
        """
        Extract structured qualification from a list of user messages.
        Returns (QualificationExtraction, model_name, prompt_sha256).
        """
        ...


class MockLLMAdapter:
    """
    Deterministic mock adapter for unit tests and local development.
    Does not make external API calls.
    """

    def __init__(self, override_result: QualificationExtraction | None = None) -> None:
        self.override_result = override_result

    async def extract_qualification(
        self,
        messages: list[str],
    ) -> tuple[QualificationExtraction, str, str]:
        if self.override_result:
            return self.override_result, "mock-v1", "mock-prompt-hash"

        combined_text = " ".join(messages).lower()
        model_name = "mock-v1"
        prompt_hash = hashlib.sha256(combined_text.encode("utf-8")).hexdigest()

        # Heuristic extraction for testing
        budget_min, budget_max = None, None
        if "1 crore" in combined_text or "1 cr" in combined_text or "1.5 cr" in combined_text:
            budget_min = 10000000
            budget_max = 15000000
        elif "80 lakhs" in combined_text or "80l" in combined_text:
            budget_min = 6000000
            budget_max = 8500000

        bhk = None
        for count in [1, 2, 3, 4, 5]:
            if f"{count} bhk" in combined_text or f"{count}bhk" in combined_text:
                bhk = count
                break

        prop_type = None
        if "villa" in combined_text:
            prop_type = "VILLA"
        elif "apartment" in combined_text or "flat" in combined_text:
            prop_type = "APARTMENT"
        elif "plot" in combined_text or "land" in combined_text:
            prop_type = "PLOT"

        purpose = "BUY" if "buy" in combined_text else ("RENT" if "rent" in combined_text else None)

        location = None
        for loc in ["whitefield", "indiranagar", "koramangala", "sarjapur", "electronic city"]:
            if loc in combined_text:
                location = loc.title()
                break

        timeline = "IMMEDIATE" if "immediate" in combined_text or "urgent" in combined_text else "1-3 MONTHS"

        confidence = {
            "budget": 0.90 if budget_max else 0.0,
            "bhk": 0.95 if bhk else 0.0,
            "property_type": 0.85 if prop_type else 0.0,
            "location": 0.88 if location else 0.0,
        }

        extraction = QualificationExtraction(
            budget_min=budget_min,
            budget_max=budget_max,
            property_type=prop_type,
            bhk=bhk,
            preferred_location=location,
            purpose=purpose,
            move_in_timeline=timeline,
            confidence_scores=confidence,
            summary=f"Mock extraction from {len(messages)} messages.",
            raw_response=json.dumps({"extracted": True}),
        )
        return extraction, model_name, prompt_hash


class LiteLLMAdapter:
    """Production LLM adapter leveraging LiteLLM for multi-provider routing."""

    SYSTEM_PROMPT = """You are an AI Real Estate Lead Qualification assistant.
Analyze the buyer's messages and extract structured real estate requirements.
Output MUST be strict valid JSON matching this schema:
{
  "budget_min": integer or null (in INR currency),
  "budget_max": integer or null (in INR currency),
  "property_type": "APARTMENT" | "VILLA" | "PLOT" | "COMMERCIAL" | null,
  "bhk": integer (1-10) or null,
  "preferred_location": string or null,
  "purpose": "BUY" | "RENT" | "INVEST" | null,
  "move_in_timeline": string or null,
  "confidence_scores": {
    "budget": float between 0.0 and 1.0,
    "property_type": float between 0.0 and 1.0,
    "bhk": float between 0.0 and 1.0,
    "location": float between 0.0 and 1.0
  },
  "summary": string (brief explanation of the client's needs)
}
Only output the JSON object without markdown or commentary."""

    def __init__(self) -> None:
        self.settings = get_settings()

    async def extract_qualification(
        self,
        messages: list[str],
    ) -> tuple[QualificationExtraction, str, str]:
        import litellm

        user_content = "\n".join([f"- {msg}" for msg in messages])
        prompt_full = f"{self.SYSTEM_PROMPT}\n\nClient messages:\n{user_content}"
        prompt_hash = hashlib.sha256(prompt_full.encode("utf-8")).hexdigest()

        try:
            response = await litellm.acompletion(
                model=self.settings.openai_model,
                messages=[
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                temperature=self.settings.llm_temperature,
                max_tokens=self.settings.llm_max_tokens,
                response_format={"type": "json_object"},
                api_key=self.settings.openai_api_key or None,
                timeout=30.0,
            )

            raw_content = response.choices[0].message.content
            if not raw_content:
                raise LLMAdapterError("OpenAI returned an empty completion response.")

            data = json.loads(raw_content)
            data["raw_response"] = raw_content

            extraction = QualificationExtraction.model_validate(data)
            return extraction, self.settings.openai_model, prompt_hash
        except LLMAdapterError:
            raise
        except Exception as exc:
            logger.error("llm_extraction_failed", error=str(exc), model=self.settings.openai_model)
            raise LLMAdapterError(f"OpenAI extraction failed: {type(exc).__name__} - {exc}") from exc


def get_llm_adapter() -> LLMAdapter:
    """Factory to retrieve configured LLM adapter based on settings."""
    settings = get_settings()
    if settings.llm_provider == "mock":
        return MockLLMAdapter()
    return LiteLLMAdapter()
