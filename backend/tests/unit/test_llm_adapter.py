"""
Unit tests for qualification/llm_adapter.py:
- Protocol adherence
- MockLLMAdapter heuristic extraction
- Pydantic validation (rejecting hallucinated fields)
- Hash generation
"""

import pytest
from pydantic import ValidationError

from app.qualification.llm_adapter import (
    LLMAdapter,
    MockLLMAdapter,
    QualificationExtraction,
    get_llm_adapter,
)


class TestQualificationSchema:
    def test_valid_extraction_payload(self):
        data = {
            "budget_min": 5000000,
            "budget_max": 8000000,
            "property_type": "APARTMENT",
            "bhk": 3,
            "preferred_location": "Whitefield",
            "purpose": "BUY",
            "move_in_timeline": "Immediate",
            "confidence_scores": {"bhk": 0.95},
            "summary": "Looking for 3 BHK in Whitefield",
        }
        item = QualificationExtraction.model_validate(data)
        assert item.bhk == 3
        assert item.property_type == "APARTMENT"
        assert item.purpose == "BUY"

    def test_case_insensitive_property_type(self):
        item = QualificationExtraction(property_type="apartment", purpose="buy")
        assert item.property_type == "APARTMENT"
        assert item.purpose == "BUY"

    def test_invalid_property_type_raises_validation_error(self):
        with pytest.raises(ValidationError):
            QualificationExtraction(property_type="PALACE_HALLUCINATED")

    def test_invalid_purpose_raises_validation_error(self):
        with pytest.raises(ValidationError):
            QualificationExtraction(purpose="INVALID_ACTION")

    def test_invalid_bhk_out_of_range(self):
        with pytest.raises(ValidationError):
            QualificationExtraction(bhk=25)


class TestMockLLMAdapter:
    @pytest.mark.asyncio
    async def test_protocol_implementation(self):
        adapter = get_llm_adapter()
        assert isinstance(adapter, LLMAdapter)

    @pytest.mark.asyncio
    async def test_extract_qualification_heuristics(self):
        adapter = MockLLMAdapter()
        messages = [
            "Hi, I'm looking to buy a 3 BHK apartment in Whitefield.",
            "My budget is around 1.5 cr and I need immediate possession.",
        ]
        extraction, model, prompt_hash = await adapter.extract_qualification(messages)

        assert extraction.bhk == 3
        assert extraction.property_type == "APARTMENT"
        assert extraction.purpose == "BUY"
        assert extraction.preferred_location == "Whitefield"
        assert extraction.budget_max == 15000000
        assert extraction.move_in_timeline == "IMMEDIATE"
        assert model == "mock-v1"
        assert len(prompt_hash) == 64

    @pytest.mark.asyncio
    async def test_override_result_support(self):
        custom = QualificationExtraction(
            bhk=4,
            property_type="VILLA",
            preferred_location="Indiranagar",
            summary="Custom override",
        )
        adapter = MockLLMAdapter(override_result=custom)
        extraction, _, _ = await adapter.extract_qualification(["Hello"])
        assert extraction.bhk == 4
        assert extraction.property_type == "VILLA"

    def test_penthouse_property_type_supported(self):
        extraction = QualificationExtraction(
            bhk=3,
            property_type="PENTHOUSE",
            purpose="BUY",
            summary="Looking for a luxury penthouse",
        )
        assert extraction.property_type == "PENTHOUSE"


class TestLiteLLMAdapter:
    @pytest.mark.asyncio
    async def test_litellm_adapter_success(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.qualification.llm_adapter import LiteLLMAdapter

        adapter = LiteLLMAdapter()
        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(
                message=MagicMock(
                    content='{"budget_min": 10000000, "budget_max": 20000000, "property_type": "PENTHOUSE", "bhk": 4, "preferred_location": "Dalanwala", "purpose": "BUY", "move_in_timeline": "Immediate", "confidence_scores": {"budget": 0.9}, "summary": "Penthouse buyer"}'
                )
            )
        ]

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response):
            extraction, model, prompt_hash = await adapter.extract_qualification(["Looking for 4 BHK penthouse in Dalanwala"])
            assert extraction.property_type == "PENTHOUSE"
            assert extraction.bhk == 4
            assert extraction.budget_max == 20000000
            assert model == adapter.settings.openai_model
            assert len(prompt_hash) == 64

    @pytest.mark.asyncio
    async def test_litellm_adapter_empty_response_raises_error(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.qualification.llm_adapter import LiteLLMAdapter, LLMAdapterError

        adapter = LiteLLMAdapter()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content=""))]

        with (
            patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response),
            pytest.raises(LLMAdapterError, match="empty completion"),
        ):
            await adapter.extract_qualification(["Hello"])

    @pytest.mark.asyncio
    async def test_litellm_adapter_network_failure_raises_clean_error(self):
        from unittest.mock import AsyncMock, patch

        from app.qualification.llm_adapter import LiteLLMAdapter, LLMAdapterError

        adapter = LiteLLMAdapter()
        with (
            patch("litellm.acompletion", new_callable=AsyncMock, side_effect=RuntimeError("Connection timed out")),
            pytest.raises(LLMAdapterError, match="Connection timed out"),
        ):
            await adapter.extract_qualification(["Hello"])

