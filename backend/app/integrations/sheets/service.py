"""
GoogleSheetsSyncService — synchronizes qualified CRM buyer requirements
and seller property listings to operational Google Sheets views.
PostgreSQL remains the canonical database of truth; Sheets is a synchronized view.
"""

from datetime import UTC, datetime
from typing import Any

import structlog

from app.core.config import get_settings
from app.integrations.sheets.client import (
    GoogleSheetsClient,
    MockGoogleSheetsClient,
    RealGoogleSheetsClient,
)

logger = structlog.get_logger()
settings = get_settings()

# Default singleton mock instance for dev/testing
_mock_sheets_singleton = MockGoogleSheetsClient()


def get_sheets_client() -> GoogleSheetsClient:
    """Return configured GoogleSheetsClient (real client if credentials present, otherwise mock)."""
    if (
        settings.google_sheets_mode == "real"
        and settings.google_sheets_credentials_json
    ):
        return RealGoogleSheetsClient(settings.google_sheets_credentials_json)
    return _mock_sheets_singleton


class GoogleSheetsSyncService:
    """
    Coordinates synchronized pushes from CRM leads and listings to Google Sheets.
    Enforces stable row updates rather than appending duplicate entries.
    """

    def __init__(self, client: GoogleSheetsClient | None = None) -> None:
        self.client = client or get_sheets_client()

    async def sync_seller_listing(
        self,
        spreadsheet_id: str,
        listing_data: dict[str, Any],
        sheet_name: str = "Seller Listings",
    ) -> dict[str, Any]:
        """
        Synchronize a seller property listing into the operational spreadsheet.
        Matches the HAVN Estates reference sheet format:
          A: Owner name
          B: Owner number
          C: Configuration (property type: flat, plot, commercial land, house, land)
          D: Configuration / Dimensions (e.g. 4 BHK, 45*90, 40*80)
          E: Size (e.g. 480 sq.yd, 240 gaj, 5200 sq ft)
          F: Area / Location (e.g. Balbir road dalanawala, Kuanwala)
          G: Asking price (e.g. 260000, 5.95, 75000)
          H: Date (e.g. August 11, 2026)
          I: Google map link
          J: Remarks
        """
        now_str = datetime.now(UTC).strftime("%B %d, %Y")
        owner_name = listing_data.get("owner_name") or listing_data.get("full_name") or "Owner"
        owner_phone = listing_data.get("owner_phone") or listing_data.get("phone") or ""
        prop_type = (listing_data.get("property_type") or "plot").lower()
        dimensions = listing_data.get("dimensions") or listing_data.get("bhk_or_dimensions") or ""
        size = listing_data.get("size") or listing_data.get("area_size") or ""
        area_location = listing_data.get("location") or listing_data.get("area") or ""
        asking_price = str(listing_data.get("asking_price") or listing_data.get("price") or "")
        map_link = listing_data.get("google_map_link") or listing_data.get("location_link") or ""
        remarks = listing_data.get("remarks") or listing_data.get("notes") or "Verified via WhatsApp"

        row_values = [
            owner_name,
            owner_phone,
            prop_type,
            dimensions,
            size,
            area_location,
            asking_price,
            now_str,
            map_link,
            remarks,
        ]

        # Check existing rows to update rather than append duplicate
        existing_rows = await self.client.get_rows(spreadsheet_id, sheet_name)
        existing_idx = None
        for idx, row in enumerate(existing_rows, start=1):
            if len(row) > 1 and owner_phone and str(row[1]).strip() == str(owner_phone).strip() or len(row) > 0 and str(row[0]).strip().lower() == owner_name.strip().lower() and owner_name != "Owner":
                existing_idx = idx
                break

        if existing_idx:
            success = await self.client.update_row(spreadsheet_id, sheet_name, existing_idx, row_values)
            action = "updated"
            row_num = existing_idx
        else:
            row_num = await self.client.append_row(spreadsheet_id, sheet_name, row_values)
            action = "appended"
            success = True

        logger.info(
            "seller_listing_synced_to_sheets",
            spreadsheet_id=spreadsheet_id,
            action=action,
            row_index=row_num,
            owner_phone=owner_phone,
        )
        return {
            "success": success,
            "action": action,
            "row_index": row_num,
            "spreadsheet_id": spreadsheet_id,
        }

    async def sync_buyer_lead(
        self,
        spreadsheet_id: str,
        lead_data: dict[str, Any],
        sheet_name: str = "Qualified Leads",
    ) -> dict[str, Any]:
        """
        Synchronize a qualified buyer requirement into the operational spreadsheet.
        Columns:
          A: Lead ID
          B: Client Name
          C: Phone
          D: Requirement Type
          E: BHK / Specs
          F: Budget Range
          G: Preferred Location
          H: Purpose / Usage
          I: Timeline
          J: Score
          K: Health Category
          L: Status
          M: Date
          N: Summary
        """
        lead_id = str(lead_data.get("id") or "")
        client_name = lead_data.get("full_name") or "Buyer Lead"
        phone = lead_data.get("phone") or ""
        req_type = lead_data.get("property_type") or "Any"
        bhk = str(lead_data.get("bhk") or lead_data.get("specs") or "")
        budget = lead_data.get("budget_display") or f"{lead_data.get('budget_min', '')} - {lead_data.get('budget_max', '')}"
        location = lead_data.get("preferred_location") or ""
        purpose = lead_data.get("purpose") or ""
        timeline = lead_data.get("move_in_timeline") or ""
        score = str(lead_data.get("intent_score") or "")
        health = lead_data.get("lead_health") or ""
        status = lead_data.get("stage") or "QUALIFIED"
        now_str = datetime.now(UTC).strftime("%B %d, %Y")
        summary = lead_data.get("summary") or lead_data.get("notes") or ""

        row_values = [
            lead_id,
            client_name,
            phone,
            req_type,
            bhk,
            budget,
            location,
            purpose,
            timeline,
            score,
            health,
            status,
            now_str,
            summary,
        ]

        existing_rows = await self.client.get_rows(spreadsheet_id, sheet_name)
        existing_idx = None
        for idx, row in enumerate(existing_rows, start=1):
            if len(row) > 0 and lead_id and str(row[0]).strip() == lead_id.strip() or len(row) > 2 and phone and str(row[2]).strip() == phone.strip():
                existing_idx = idx
                break

        if existing_idx:
            success = await self.client.update_row(spreadsheet_id, sheet_name, existing_idx, row_values)
            action = "updated"
            row_num = existing_idx
        else:
            row_num = await self.client.append_row(spreadsheet_id, sheet_name, row_values)
            action = "appended"
            success = True

        logger.info(
            "buyer_lead_synced_to_sheets",
            lead_id=lead_id,
            action=action,
            row_index=row_num,
            health=health,
        )
        return {
            "success": success,
            "action": action,
            "row_index": row_num,
            "spreadsheet_id": spreadsheet_id,
        }
