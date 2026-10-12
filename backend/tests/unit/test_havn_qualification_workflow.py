"""
Comprehensive unit tests for HAVN Realty qualification workflow, state machine,
bilingual conversation handling, broker handoff, scoring, and Google Sheets sync:
- BUY versus SELL branching
- Rental & leasing polite declination
- English and Hinglish persona matching
- Question skipping ("skip", "nahi")
- Greedy extraction of information already provided (no repetition)
- All 5 property type branches (Villa/House, Flat, Penthouse, Plot, Commercial)
- Immediate human takeover on broker/agent request
- Hard guard preventing automated messages after handoff
- Google Sheets row updates and idempotency
- Deterministic scoring, thresholds, and unresolved questions
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.integrations.sheets.client import MockGoogleSheetsClient
from app.integrations.sheets.service import GoogleSheetsSyncService
from app.leads.models import Lead
from app.leads.state_machine import AutomationStatus, LeadHealth, LeadStage
from app.qualification.conversation_service import QualificationConversationService


@pytest.fixture
def mock_db():
    session = AsyncMock()
    session.in_transaction.return_value = False
    session.begin.return_value.__aenter__ = AsyncMock()
    session.begin.return_value.__aexit__ = AsyncMock(return_value=None)
    session.begin_nested.return_value.__aenter__ = AsyncMock()
    session.begin_nested.return_value.__aexit__ = AsyncMock(return_value=None)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    return session


def _create_test_lead(
    lead_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    stage: str = LeadStage.NEW.value,
    automation_status: str = AutomationStatus.ACTIVE.value,
    phone_enc: str = "+919876543210",
    name: str = "Test Prospect",
) -> Lead:
    lid = lead_id or uuid.uuid4()
    tid = tenant_id or uuid.uuid4()
    now = datetime.now(UTC)
    return Lead(
        id=lid,
        tenant_id=tid,
        full_name=name,
        phone_e164_enc=phone_enc,
        phone_hash="hash_" + str(lid)[:8],
        preferred_contact="WHATSAPP",
        stage=stage,
        lead_health=LeadHealth.UNKNOWN.value,
        automation_status=automation_status,
        score_breakdown={},
        created_at=now,
        updated_at=now,
        is_deleted=False,
    )


# ── Conversation Entry & Intent Branching ────────────────────────────────────


@pytest.mark.asyncio
async def test_initial_greeting_bilingual(mock_db):
    lead = _create_test_lead()
    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = lead
    mock_db.execute.return_value = mock_res

    svc = QualificationConversationService(mock_db)

    # English hello
    resp_en = await svc.process_inbound_message(lead.id, "Hello, good morning", "+919876543210")
    assert resp_en.reply_text is not None
    assert "BUY a luxury property or SELL/LIST" in resp_en.reply_text

    # Reset breakdown and test Hinglish greeting
    lead.score_breakdown = {}
    resp_hi = await svc.process_inbound_message(lead.id, "Namaste ji, mujhe information chahiye", "+919876543210")
    assert resp_hi.reply_text is not None
    assert "property BUY करना चाहते हैं या SELL/LIST" in resp_hi.reply_text


@pytest.mark.asyncio
async def test_rental_enquiry_politely_declined(mock_db):
    lead = _create_test_lead()
    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = lead
    mock_db.execute.return_value = mock_res

    svc = QualificationConversationService(mock_db)

    resp = await svc.process_inbound_message(
        lead.id,
        "Looking for a 3 BHK flat on rent for 6 months",
        "+919876543210",
    )

    assert resp.is_completed is True
    assert lead.stage == LeadStage.DISQUALIFIED.value
    assert lead.automation_status == AutomationStatus.PAUSED.value
    assert "do not handle rental or leasing inquiries" in resp.reply_text


@pytest.mark.asyncio
async def test_rental_enquiry_politely_declined_hinglish(mock_db):
    lead = _create_test_lead()
    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = lead
    mock_db.execute.return_value = mock_res

    svc = QualificationConversationService(mock_db)

    resp = await svc.process_inbound_message(
        lead.id,
        "Mujhe Dehradun mein kiraya par flat chahiye",
        "+919876543210",
    )

    assert resp.is_completed is True
    assert lead.stage == LeadStage.DISQUALIFIED.value
    assert "rentals या leasing नहीं लेते" in resp.reply_text


# ── Immediate Human Takeover ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_immediate_human_takeover_request(mock_db):
    lead = _create_test_lead()
    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = lead
    mock_db.execute.return_value = mock_res

    svc = QualificationConversationService(mock_db)

    resp = await svc.process_inbound_message(
        lead.id,
        "Can I speak to a real broker please? Please call me.",
        "+919876543210",
    )

    assert resp.is_human_takeover is True
    assert lead.automation_status == AutomationStatus.HUMAN_TAKEOVER.value
    assert "connected you directly with our senior luxury property advisor" in resp.reply_text


@pytest.mark.asyncio
async def test_hard_guard_stops_ai_messages_after_human_takeover(mock_db):
    lead = _create_test_lead(automation_status=AutomationStatus.HUMAN_TAKEOVER.value)
    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = lead
    mock_db.execute.return_value = mock_res

    svc = QualificationConversationService(mock_db)

    resp = await svc.process_inbound_message(
        lead.id,
        "What is the price of that 4 BHK?",
        "+919876543210",
    )

    # Bot must NOT reply; preserve human takeover control
    assert resp.reply_text is None


# ── Greedy Entity Extraction & No Unnecessary Repetition ─────────────────────


@pytest.mark.asyncio
async def test_greedy_extraction_skips_already_answered_questions(mock_db):
    lead = _create_test_lead()
    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = lead
    mock_db.execute.return_value = mock_res

    svc = QualificationConversationService(mock_db)

    # Prospect supplies intent, property type, BHK, and location in initial message
    resp = await svc.process_inbound_message(
        lead.id,
        "Hi, I want to buy a 4 BHK Villa on Rajpur Road Dehradun",
        "+919876543210",
    )

    state = lead.score_breakdown.get("conversation_state", {})
    answers = state.get("answers", {})

    assert answers.get("intent") == "BUY"
    assert answers.get("property_type") == "VILLA"
    assert answers.get("bhk") == 4
    assert answers.get("preferred_location") == "Rajpur Road"

    # Because intent, property type, BHK, and location are already extracted,
    # the bot skips asking them and asks for Budget!
    assert "budget" in resp.reply_text.lower()


@pytest.mark.asyncio
async def test_skip_token_advances_to_next_question(mock_db):
    lead = _create_test_lead()
    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = lead
    mock_db.execute.return_value = mock_res

    svc = QualificationConversationService(mock_db)

    # 1. Answer intent
    await svc.process_inbound_message(lead.id, "I want to buy", "+919876543210")
    # 2. Answer property type
    await svc.process_inbound_message(lead.id, "Luxury Flat", "+919876543210")
    # 3. Client says "skip" on specifications question
    resp = await svc.process_inbound_message(lead.id, "skip", "+919876543210")

    # Bot should advance to location question without crashing
    assert "location" in resp.reply_text.lower()


# ── Property Type Branches ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_penthouse_branch(mock_db):
    lead = _create_test_lead()
    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = lead
    mock_db.execute.return_value = mock_res

    svc = QualificationConversationService(mock_db)

    await svc.process_inbound_message(lead.id, "Buy", "+919876543210")
    resp = await svc.process_inbound_message(lead.id, "Penthouse", "+919876543210")

    assert "terrace" in resp.reply_text.lower() or "indoor area" in resp.reply_text.lower()


@pytest.mark.asyncio
async def test_plot_land_branch(mock_db):
    lead = _create_test_lead()
    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = lead
    mock_db.execute.return_value = mock_res

    svc = QualificationConversationService(mock_db)

    await svc.process_inbound_message(lead.id, "Buy", "+919876543210")
    resp = await svc.process_inbound_message(lead.id, "Residential Land / Plot", "+919876543210")

    assert "dimensions" in resp.reply_text.lower() or "gaj" in resp.reply_text.lower()


@pytest.mark.asyncio
async def test_commercial_space_branch(mock_db):
    lead = _create_test_lead()
    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = lead
    mock_db.execute.return_value = mock_res

    svc = QualificationConversationService(mock_db)

    await svc.process_inbound_message(lead.id, "Buy", "+919876543210")
    resp = await svc.process_inbound_message(lead.id, "Commercial Space", "+919876543210")

    assert "business use" in resp.reply_text.lower()


# ── Full Buyer Completion, Scoring & Handoff ─────────────────────────────────


@pytest.mark.asyncio
async def test_full_buyer_qualification_and_broker_handoff(mock_db):
    lead = _create_test_lead(name="Aarav Sharma")
    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = lead
    mock_db.execute.return_value = mock_res

    svc = QualificationConversationService(mock_db)

    # Complete qualification in one detailed message
    resp = await svc.process_inbound_message(
        lead.id,
        "I am the sole decision maker looking to buy a 4 BHK 4000 sq ft Villa in Rajpur Road. Budget 2.5 Cr flexible. Moving immediately for primary residence with no delay.",
        "+919876543210",
    )

    # Lead should be qualified and handed off to human
    assert resp.is_completed is True
    assert lead.stage == LeadStage.QUALIFIED.value
    assert lead.automation_status == AutomationStatus.HUMAN_TAKEOVER.value
    assert lead.intent_score is not None and lead.intent_score >= 70
    assert lead.lead_health == "HOT"
    assert "HAVN BUYER QUALIFICATION SUMMARY" in resp.summary
    assert "Rajpur Road" in resp.summary

    # Ensure outbox events were enqueued
    mock_db.flush.assert_awaited()


# ── Full Seller Listing Flow ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seller_listing_flow(mock_db):
    lead = _create_test_lead(name="Harish Rawat")
    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = lead
    mock_db.execute.return_value = mock_res

    svc = QualificationConversationService(mock_db)

    # Initial intent: SELL
    await svc.process_inbound_message(lead.id, "I want to sell my property", "+919876543210")

    # Complete seller info
    resp = await svc.process_inbound_message(
        lead.id,
        "Plot in Balbir Road Dalanawala, 240 gaj size, asking 75000 per gaj",
        "+919876543210",
    )

    assert resp.is_completed is True
    assert lead.stage == LeadStage.QUALIFIED.value
    assert lead.automation_status == AutomationStatus.HUMAN_TAKEOVER.value
    assert "HAVN SELLER LISTING SUMMARY" in resp.summary
    assert "Balbir Road" in resp.summary


# ── Google Sheets Synchronization & Idempotency ──────────────────────────────


@pytest.mark.asyncio
async def test_google_sheets_seller_sync_and_idempotent_update():
    mock_client = MockGoogleSheetsClient()
    sync_svc = GoogleSheetsSyncService(client=mock_client)

    spreadsheet_id = "test-spreadsheet-id"

    # 1. Initial listing append
    listing_data_1 = {
        "owner_name": "Dev Sharma",
        "owner_phone": "+918445121294",
        "property_type": "plot",
        "dimensions": "40*80",
        "size": "240 gaj",
        "location": "Phulseni",
        "asking_price": "25000",
        "google_map_link": "https://maps.google.com/?q=phulseni",
        "remarks": "Corner plot",
    }
    res1 = await sync_svc.sync_seller_listing(spreadsheet_id, listing_data_1)
    assert res1["action"] == "appended"
    assert res1["row_index"] == 1

    # Verify rows in mock sheet
    rows = await mock_client.get_rows(spreadsheet_id, "Seller Listings")
    assert len(rows) == 1
    assert rows[0][0] == "Dev Sharma"
    assert rows[0][1] == "+918445121294"
    assert rows[0][6] == "25000"

    # 2. Update existing listing (same owner_phone, price changed)
    listing_data_updated = dict(listing_data_1)
    listing_data_updated["asking_price"] = "28000"
    listing_data_updated["remarks"] = "Price revised"

    res2 = await sync_svc.sync_seller_listing(spreadsheet_id, listing_data_updated)
    assert res2["action"] == "updated"
    assert res2["row_index"] == 1

    # Verify row was updated in place rather than creating duplicate
    rows_after = await mock_client.get_rows(spreadsheet_id, "Seller Listings")
    assert len(rows_after) == 1
    assert rows_after[0][6] == "28000"
    assert rows_after[0][9] == "Price revised"


@pytest.mark.asyncio
async def test_google_sheets_buyer_sync_and_idempotent_update():
    mock_client = MockGoogleSheetsClient()
    sync_svc = GoogleSheetsSyncService(client=mock_client)

    spreadsheet_id = "test-spreadsheet-id"
    lead_id = str(uuid.uuid4())

    buyer_data = {
        "id": lead_id,
        "full_name": "Rajat Verma",
        "phone": "+919411560919",
        "property_type": "VILLA",
        "bhk": 4,
        "budget_display": "2 Cr - 3 Cr",
        "preferred_location": "Jakhan",
        "purpose": "PRIMARY RESIDENCE",
        "move_in_timeline": "IMMEDIATE",
        "intent_score": 85,
        "lead_health": "HOT",
        "stage": "QUALIFIED",
        "summary": "Verified 4 BHK buyer for Jakhan",
    }

    # 1. Append
    res1 = await sync_svc.sync_buyer_lead(spreadsheet_id, buyer_data)
    assert res1["action"] == "appended"

    # 2. Update with higher score
    buyer_data_updated = dict(buyer_data)
    buyer_data_updated["intent_score"] = 92
    res2 = await sync_svc.sync_buyer_lead(spreadsheet_id, buyer_data_updated)
    assert res2["action"] == "updated"

    rows = await mock_client.get_rows(spreadsheet_id, "Qualified Leads")
    assert len(rows) == 1
    assert rows[0][9] == "92"
