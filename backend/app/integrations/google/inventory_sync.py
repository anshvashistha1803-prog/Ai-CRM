"""
Property Inventory Synchronization Service for Google Sheets.
Reads property inventory rows, validates fields, supports configurable column mappings,
and performs idempotent synchronization against canonical CRM Property models and
inventory staging workflow.
"""

import asyncio
import hashlib
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.integrations.google.models import InventoryStagingItem, TenantGoogleIntegration
from app.integrations.google.oauth import OAuthRevokedError
from app.integrations.google.schemas import InventorySyncResponse
from app.integrations.google.service import GoogleIntegrationService
from app.integrations.sheets.client import (
    GoogleSheetsClient,
    GoogleSheetsClientError,
    OAuthGoogleSheetsClient,
    SpreadsheetAccessError,
    SpreadsheetNotFoundError,
)
from app.properties.models import Property

logger = structlog.get_logger()
settings = get_settings()

DEFAULT_COLUMN_MAPPINGS = {
    "external_id": [
        "id",
        "property_id",
        "property id",
        "external_id",
        "listing_id",
        "listing id",
        "code",
        "ref",
        "reference",
        "prop_id",
        "uid",
    ],
    "title": ["title", "property_name", "property name", "name", "project", "listing_title"],
    "property_type": ["property_type", "property type", "type", "category"],
    "bhk": ["configuration", "config", "bhk", "bedrooms", "rooms"],
    "price": ["asking_price", "asking price", "price", "cost", "rate", "budget"],
    "location": ["area", "location", "address", "city", "locality", "submarket"],
    "area_sqft": ["size", "dimensions", "area_sqft", "sqft", "built_up", "carpet_area", "plot_size", "plot size"],
    "description": ["remarks", "description", "notes", "features"],
    "status": ["status", "availability"],
    "owner_name": ["owner_name", "owner name", "owner", "contact_name", "seller_name"],
    "owner_number": ["owner_number", "owner number", "owner_phone", "phone", "contact", "mobile"],
    "google_map_link": ["google_map_link", "google map link", "map_link", "map link", "location_link", "maps"],
    "date": ["date", "listing_date", "added_date"],
}


def _mask_phone(phone: str | None) -> str:
    """Mask phone number to prevent raw PII leakage in logs and responses."""
    if not phone:
        return ""
    p = phone.strip()
    if len(p) <= 4:
        return "****"
    return f"{p[:3]}****{p[-4:]}"


class SyncLockLostError(Exception):
    """Raised when an in-flight sync operation loses its distributed lease lock."""


class PropertyInventorySyncService:
    """
    Coordinates idempotent property inventory sync from Google Sheets to CRM database.
    PostgreSQL is authoritative: direct sheet updates are validated before merging.
    Rows that cannot yet become valid CRM properties (e.g. missing prices, ambiguous types)
    are routed to the inventory staging workflow for review.
    """

    def __init__(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        sheets_client: GoogleSheetsClient | None = None,
        lock_token: str | None = None,
        lock_ttl_seconds: int | None = None,
        heartbeat_interval_seconds: int = 60,
    ) -> None:
        self.db = db
        self.tenant_id = tenant_id
        self._custom_sheets_client = sheets_client
        self.lock_token = lock_token
        self.lock_ttl_seconds = lock_ttl_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self._last_heartbeat = datetime.now(UTC)

    async def renew_lease(self) -> bool:
        """Explicitly renew the active synchronization lease lock."""
        if not self.lock_token:
            return True
        google_svc = GoogleIntegrationService(self.db, self.tenant_id)
        renewed = await google_svc.renew_sync_lock(self.lock_token, lock_ttl_seconds=self.lock_ttl_seconds)
        if not renewed:
            logger.error(
                "sync_lease_lost_during_renewal",
                tenant_id=str(self.tenant_id),
                lock_token=self.lock_token,
            )
            return False
        self._last_heartbeat = datetime.now(UTC)
        await self.db.commit()
        return True

    async def _heartbeat_checkpoint(self) -> None:
        """Verify and extend the lease lock if the heartbeat interval has elapsed."""
        if not self.lock_token:
            return
        now = datetime.now(UTC)
        if (now - self._last_heartbeat).total_seconds() >= self.heartbeat_interval_seconds:
            renewed = await self.renew_lease()
            if not renewed:
                raise SyncLockLostError(
                    f"Sync lease lock {self.lock_token} was lost or expired for tenant {self.tenant_id}"
                )

    async def _resolve_sheets_client(self, integration: TenantGoogleIntegration) -> GoogleSheetsClient:
        """Resolve Google Sheets client based on configuration and tenant OAuth token."""
        if self._custom_sheets_client is not None:
            return self._custom_sheets_client

        if settings.google_sheets_mode == "mock":
            from app.integrations.sheets.service import _mock_sheets_singleton

            return _mock_sheets_singleton

        oauth_svc = GoogleIntegrationService(self.db, self.tenant_id)
        access_token = await oauth_svc.get_valid_access_token()
        return OAuthGoogleSheetsClient(access_token)

    async def sync_inventory(self) -> InventorySyncResponse:
        """
        Execute inventory synchronization for the tenant.
        """
        integration_stmt = select(TenantGoogleIntegration).where(
            TenantGoogleIntegration.tenant_id == self.tenant_id,
            TenantGoogleIntegration.is_deleted.is_(False),
        )
        res = await self.db.execute(integration_stmt)
        integration = res.scalar_one_or_none()

        if not integration or not integration.spreadsheet_id:
            raise GoogleSheetsClientError("Google Spreadsheet ID is not configured for this tenant.")

        spreadsheet_id = integration.spreadsheet_id
        sheet_name = integration.properties_sheet_name or "Properties"
        # Dynamic open-ended range to read all available inventory rows
        range_name = f"{sheet_name}!A1:Z"

        now = datetime.now(UTC)

        # Protect initial client.get_rows() from lease expiry:
        # Bounded timeout for fetching rows from external Google Sheets
        lock_ttl = float(self.lock_ttl_seconds) if self.lock_ttl_seconds is not None else 300.0
        get_rows_timeout = min(lock_ttl, 60.0) if self.lock_token else 60.0

        heartbeat_task = None
        stop_heartbeat = asyncio.Event()

        async def _heartbeat_worker():
            hb_interval = float(self.heartbeat_interval_seconds) if self.heartbeat_interval_seconds else 60.0
            interval = max(1.0, min(hb_interval, lock_ttl / 3.0))
            while not stop_heartbeat.is_set():
                try:
                    await asyncio.wait_for(stop_heartbeat.wait(), timeout=interval)
                    break
                except TimeoutError:
                    pass
                if not stop_heartbeat.is_set():
                    renewed = await self.renew_lease()
                    if not renewed:
                        break

        if self.lock_token:
            heartbeat_task = asyncio.create_task(_heartbeat_worker())

        try:
            client = await self._resolve_sheets_client(integration)
            rows = await asyncio.wait_for(
                client.get_rows(spreadsheet_id, range_name),
                timeout=get_rows_timeout,
            )
        except (OAuthRevokedError, SpreadsheetAccessError, SpreadsheetNotFoundError, GoogleSheetsClientError) as exc:
            if self.lock_token and not await self.renew_lease():
                raise SyncLockLostError(
                    f"Sync lease lock {self.lock_token} was lost during client error handling for tenant {self.tenant_id}"
                )
            integration.last_sync_at = now
            integration.last_sync_status = "FAILED"
            integration.last_sync_error = str(exc)
            await self.db.flush()
            logger.error("inventory_sync_failed_client_error", tenant_id=str(self.tenant_id), error=str(exc))
            raise
        except TimeoutError as exc:
            logger.error("inventory_sync_get_rows_timeout", tenant_id=str(self.tenant_id), timeout=get_rows_timeout)
            raise GoogleSheetsClientError(f"Google Sheets get_rows timed out after {get_rows_timeout} seconds") from exc
        finally:
            stop_heartbeat.set()
            if heartbeat_task:
                await asyncio.gather(heartbeat_task, return_exceptions=True)

        # Stale worker guard: verify lease validity immediately after get_rows completes
        if self.lock_token:
            if not await self.renew_lease():
                raise SyncLockLostError(
                    f"Sync lease lock {self.lock_token} was lost or expired after get_rows for tenant {self.tenant_id}"
                )

        if not rows:
            if self.lock_token and not await self.renew_lease():
                raise SyncLockLostError(
                    f"Sync lease lock {self.lock_token} was lost before writing empty sync results for tenant {self.tenant_id}"
                )
            integration.last_sync_at = now
            integration.last_sync_status = "SUCCESS"
            integration.last_sync_stats = {
                "total_rows": 0,
                "created": 0,
                "updated": 0,
                "staged": 0,
                "duplicate_suspects": 0,
                "skipped": 0,
            }
            integration.last_sync_error = None
            await self.db.flush()
            return InventorySyncResponse(
                status="SUCCESS",
                spreadsheet_id=spreadsheet_id,
                sheet_name=sheet_name,
                total_rows=0,
                created=0,
                updated=0,
                staged=0,
                duplicate_suspects=0,
                skipped=0,
                error_count=0,
                errors=[],
            )

        headers = [str(col).strip().lower() for col in rows[0]]
        raw_headers = [str(col).strip() for col in rows[0]]
        data_rows = rows[1:]

        active_mappings = {**DEFAULT_COLUMN_MAPPINGS, **(integration.column_mappings or {})}
        col_indices = self._build_column_index_map(headers, active_mappings)

        created_count = 0
        updated_count = 0
        staged_count = 0
        duplicate_suspects_count = 0
        skipped_count = 0
        errors: list[dict[str, Any]] = []

        for row_idx, row_values in enumerate(data_rows, start=2):
            await self._heartbeat_checkpoint()

            if not row_values or not any(str(v).strip() for v in row_values):
                skipped_count += 1
                continue

            raw_data = {
                raw_headers[i]: str(row_values[i]).strip()
                for i in range(min(len(raw_headers), len(row_values)))
            }

            try:
                parsed = self._parse_row(row_values, col_indices, raw_headers)
                if parsed is None:
                    skipped_count += 1
                    continue

                action = await self._upsert_property(parsed, raw_data, integration)
                if action == "created":
                    created_count += 1
                elif action == "updated":
                    updated_count += 1
                elif action == "staged":
                    staged_count += 1
                    duplicate_suspects_count += 1
                else:
                    skipped_count += 1

            except SyncLockLostError:
                raise
            except Exception as exc:
                err_msg = str(exc)
                errors.append({"row_number": row_idx, "error": err_msg})
                logger.warning(
                    "inventory_sync_row_parse_error",
                    row=row_idx,
                    error=err_msg,
                )
                staged_count += 1

                # Route invalid row into inventory staging table
                await self._stage_invalid_row(
                    integration_id=integration.id,
                    spreadsheet_id=spreadsheet_id,
                    sheet_name=sheet_name,
                    row_index=row_idx,
                    error=err_msg,
                    raw_data=raw_data,
                    col_indices=col_indices,
                    row_values=row_values,
                )

        if self.lock_token:
            if not await self.renew_lease():
                raise SyncLockLostError(
                    f"Sync lease lock {self.lock_token} was lost or expired for tenant {self.tenant_id}"
                )

        status_str = (
            "SUCCESS"
            if not errors
            else ("PARTIAL_SUCCESS" if (created_count or updated_count or staged_count) else "FAILED")
        )
        integration.last_sync_at = now
        if not self.lock_token:
            integration.last_sync_status = status_str
        stats = dict(integration.last_sync_stats or {})
        stats.update({
            "total_rows": len(data_rows),
            "created": created_count,
            "updated": updated_count,
            "staged": staged_count,
            "duplicate_suspects": duplicate_suspects_count,
            "skipped": skipped_count,
            "errors": len(errors),
        })
        integration.last_sync_stats = stats
        integration.last_sync_error = f"{len(errors)} row synchronization errors" if errors else None
        await self.db.flush()

        logger.info(
            "inventory_sync_completed",
            tenant_id=str(self.tenant_id),
            total=len(data_rows),
            created=created_count,
            updated=updated_count,
            staged=staged_count,
            duplicate_suspects=duplicate_suspects_count,
            errors=len(errors),
        )

        return InventorySyncResponse(
            status=status_str,
            spreadsheet_id=spreadsheet_id,
            sheet_name=sheet_name,
            total_rows=len(data_rows),
            created=created_count,
            updated=updated_count,
            staged=staged_count,
            duplicate_suspects=duplicate_suspects_count,
            skipped=skipped_count,
            error_count=len(errors),
            errors=errors,
        )

    def _build_column_index_map(
        self,
        headers: list[str],
        mappings: dict[str, Any],
    ) -> dict[str, int]:
        """Map standard field keys to column indexes based on header row names."""
        indices: dict[str, int] = {}
        for field, matchers in mappings.items():
            if isinstance(matchers, int):
                if matchers < len(headers):
                    indices[field] = matchers
                continue
            if isinstance(matchers, str):
                matchers = [matchers]

            # 1. Exact match pass (case-insensitive)
            matched = False
            for col_idx, header in enumerate(headers):
                # Ensure "area" matches location ONLY when not carpet/built-up area
                if any(m.lower() == header for m in matchers) and (
                    (field == "location" and header == "area")
                    or (field == "area_sqft" and header in ("size", "plot size"))
                    or (
                        field != "location"
                        and not any(
                            dim in header
                            for dim in ["sqft", "size", "built", "carpet", "dim"]
                        )
                    )
                ):
                    indices[field] = col_idx
                    matched = True
                    break

            # 2. Substring match fallback
            if not matched:
                for col_idx, header in enumerate(headers):
                    if field == "location" and any(
                        dim in header for dim in ["sqft", "size", "built", "carpet", "dim"]
                    ):
                        continue
                    if field == "area_sqft" and header in ["area", "location"]:
                        continue
                    if any(m.lower() in header for m in matchers):
                        indices[field] = col_idx
                        break
        return indices

    def _parse_row(
        self,
        row: list[Any],
        col_indices: dict[str, int],
        raw_headers: list[str],
    ) -> dict[str, Any] | None:
        """
        Extract property attributes from a spreadsheet row.
        Validates price, property type, BHK, and location.
        Raises ValueError if attributes are invalid or unparseable.
        """
        if not row or not any(row):
            return None

        def get_val(field: str) -> str:
            idx = col_indices.get(field)
            if idx is not None and idx < len(row):
                return str(row[idx]).strip()
            return ""

        title = get_val("title")
        loc = get_val("location")
        raw_price = get_val("price")
        raw_type = get_val("property_type")
        raw_bhk = get_val("bhk")
        raw_size = get_val("area_sqft")
        raw_desc = get_val("description")
        raw_ext_id = get_val("external_id")
        owner_name = get_val("owner_name")
        owner_number = get_val("owner_number")
        map_link = get_val("google_map_link")
        raw_date = get_val("date")

        if not raw_price and not title and not loc and not raw_type:
            return None

        # Price parsing: Must be positive integer
        price = self._parse_price(raw_price)
        if price is None or price <= 0:
            raise ValueError(
                f"Invalid or missing property price: {raw_price!r}"
                if raw_price
                else "Missing property price"
            )

        # Property type normalization: Must recognize canonical property type
        prop_type = self._parse_property_type(raw_type, raw_bhk, title)
        if not prop_type:
            raise ValueError(
                f"Ambiguous or unrecognized property type: {raw_type!r}"
                if raw_type
                else "Missing property type"
            )

        # Location validation: Must be non-empty string
        if not loc or not loc.strip():
            raise ValueError(
                f"Invalid or missing property location: {loc!r}"
                if loc
                else "Missing property location"
            )
        loc = loc.strip()

        # BHK extraction
        bhk = self._parse_bhk(raw_bhk, raw_type, prop_type)

        # Area Sqft parsing (Size)
        area_sqft = self._parse_area_sqft(raw_size)

        # Informative title fallback
        if not title:
            title = (
                f"{bhk} BHK {prop_type} in {loc}".strip()
                if bhk
                else f"{prop_type} in {loc}".strip()
            )

        # Public description — strictly excludes owner PII
        desc_parts = []
        if raw_desc:
            desc_parts.append(raw_desc)
        if map_link:
            desc_parts.append(f"Map: {map_link}")
        if raw_date:
            desc_parts.append(f"Listed: {raw_date}")
        final_desc = "\n".join(desc_parts) if desc_parts else None

        # Stable external ID:
        # If not explicitly provided, build deterministic seed from core row specs
        has_explicit_id = bool(raw_ext_id)
        if not raw_ext_id:
            stable_seed = f"{self.tenant_id}:{owner_name.lower()}:{owner_number}:{loc.lower()}:{prop_type}:{raw_size}".encode()
            raw_ext_id = f"HAVN-{hashlib.sha256(stable_seed).hexdigest()[:12].upper()}"

        return {
            "external_id": raw_ext_id,
            "has_explicit_id": has_explicit_id,
            "title": title,
            "property_type": prop_type,
            "bhk": bhk,
            "price": price,
            "location_text": loc,
            "area_sqft": area_sqft,
            "description": final_desc,
            "status": "AVAILABLE",
            "owner_name": owner_name or None,
            "owner_phone": owner_number or None,
            "source_size": raw_size or None,
            "source_area": loc or None,
        }

    def _parse_price(self, text: str) -> int | None:
        """Parse numeric or vernacular Indian pricing (e.g. 5.95 Cr, 75 Lakh, 260000)."""
        if not text:
            return None
        cleaned = re.sub(r"[₹\$\€\£]|(?:rs\.?|inr|/-)", "", text, flags=re.IGNORECASE).strip().lower()
        cleaned = cleaned.replace(",", "")
        try:
            # Check for Crores
            cr_match = re.search(r"([\d.]+)\s*(?:cr|crore|crores|crs)\b", cleaned)
            if cr_match:
                val = float(cr_match.group(1))
                return int(val * 10_000_000) if val > 0 else None

            # Check for Lakhs
            lakh_match = re.search(r"([\d.]+)\s*(?:lakh|lakhs|lac|lacs|l)\b", cleaned)
            if lakh_match:
                val = float(lakh_match.group(1))
                return int(val * 100_000) if val > 0 else None

            # Check for Thousands
            k_match = re.search(r"([\d.]+)\s*(?:k|thousand|thousands)\b", cleaned)
            if k_match:
                val = float(k_match.group(1))
                return int(val * 1_000) if val > 0 else None

            # Raw numeric parsing
            raw_match = re.search(r"[\d.]+", cleaned)
            if raw_match:
                val = float(raw_match.group(0))
                if val <= 0:
                    return None
                # Float < 50 denotes Crores in Indian luxury real estate (e.g., 5.95)
                if val < 50 and "." in cleaned:
                    return int(val * 10_000_000)
                return int(val)
        except Exception:
            return None
        return None

    def _parse_property_type(self, text: str, config: str = "", title: str = "") -> str | None:
        """Map text to canonical property_type enum (APARTMENT, VILLA, PLOT, COMMERCIAL, PENTHOUSE)."""
        for candidate in [text, config, title]:
            if not candidate:
                continue
            c = candidate.lower()
            if "penthouse" in c:
                return "PENTHOUSE"
            if any(term in c for term in ["flat", "apartment", "society flat", "builder floor"]):
                return "APARTMENT"
            if any(term in c for term in ["villa", "kothi", "house", "bungalow", "independent house", "duplex"]):
                return "VILLA"
            if any(term in c for term in ["plot", "land", "residential plot", "residential land"]):
                return "PLOT"
            if any(term in c for term in ["commercial", "office", "shop", "retail", "showroom", "warehouse"]):
                return "COMMERCIAL"
        return None

    def _parse_bhk(self, config_text: str, type_text: str = "", prop_type: str | None = None) -> int | None:
        """Extract BHK integer count without confusing plot dimensions."""
        combined = f"{config_text} {type_text}".lower()

        # Plots and commercial spaces typically don't have BHK counts
        if prop_type in ("PLOT", "COMMERCIAL") and not ("bhk" in combined or "bedroom" in combined):
            return None

        # Look for explicit BHK / bedroom patterns
        m = re.search(r"(\d+)\s*(?:bhk|bedroom|bed|br|rk)", combined)
        if m:
            return int(m.group(1))

        # Direct digit if not a dimension string like 45*90
        s = config_text.strip()
        if s.isdigit() and 1 <= int(s) <= 15:
            return int(s)

        return None

    def _parse_area_sqft(self, text: str) -> int | None:
        """Extract area in square feet (converts gaj/sq.yd, handles dimensions like 45*90)."""
        if not text:
            return None
        cleaned = text.replace(",", "").strip().lower()

        # Check for dimension multiplications: e.g. 45*90 or 40x80
        dim_match = re.search(r"(\d+(?:\.\d+)?)\s*[*xX×]\s*(\d+(?:\.\d+)?)", cleaned)
        if dim_match:
            try:
                l_val = float(dim_match.group(1))
                w_val = float(dim_match.group(2))
                return int(l_val * w_val)
            except Exception:
                pass

        m = re.search(r"([\d.]+)", cleaned)
        if not m:
            return None

        val = float(m.group(1))
        if any(term in cleaned for term in ["gaj", "guz", "sq.yd", "sq yd", "sqyd"]):
            return int(val * 9)  # 1 gaj ≈ 1 sq.yd = 9 sq ft
        if any(term in cleaned for term in ["sqm", "sq.m", "sq m"]):
            return int(val * 10.7639)
        if "acre" in cleaned:
            return int(val * 43560)
        return int(val)

    async def _stage_invalid_row(
        self,
        integration_id: uuid.UUID | None,
        spreadsheet_id: str,
        sheet_name: str,
        row_index: int,
        error: str,
        raw_data: dict[str, Any],
        col_indices: dict[str, int],
        row_values: list[Any],
    ) -> None:
        """Add or update invalid row in inventory staging items session (idempotent)."""
        def get_val(field: str) -> str:
            idx = col_indices.get(field)
            if idx is not None and idx < len(row_values):
                return str(row_values[idx]).strip()
            return ""

        # Check if an active staging record already exists for this row
        existing_staging: InventoryStagingItem | None = None
        try:
            stmt = select(InventoryStagingItem).where(
                InventoryStagingItem.tenant_id == self.tenant_id,
                InventoryStagingItem.spreadsheet_id == spreadsheet_id,
                InventoryStagingItem.sheet_name == sheet_name,
                InventoryStagingItem.row_index == row_index,
                InventoryStagingItem.status == "PENDING_REVIEW",
                InventoryStagingItem.is_deleted.is_(False),
            )
            res = await self.db.execute(stmt)
            val = res.scalar_one_or_none()
            if isinstance(val, InventoryStagingItem):
                existing_staging = val
        except Exception:
            pass

        if existing_staging:
            existing_staging.validation_errors = [error]
            existing_staging.raw_data = raw_data
            existing_staging.updated_at = datetime.now(UTC)
            await self.db.flush()
            return

        staging_item = InventoryStagingItem(
            id=uuid.uuid4(),
            tenant_id=self.tenant_id,
            integration_id=integration_id,
            spreadsheet_id=spreadsheet_id,
            sheet_name=sheet_name,
            row_index=row_index,
            status="PENDING_REVIEW",
            validation_errors=[error],
            raw_data=raw_data,
            parsed_data={},
            source_size=get_val("area_sqft") or None,
            source_area=get_val("location") or None,
            owner_name=get_val("owner_name") or None,
            owner_phone=get_val("owner_number") or None,
        )
        self.db.add(staging_item)
        await self.db.flush()

    async def _upsert_property(
        self,
        data: dict[str, Any],
        raw_data: dict[str, Any],
        integration: TenantGoogleIntegration,
    ) -> str:
        """
        Idempotently insert or update property in database.
        Maintains PostgreSQL as authoritative source of truth.
        Stores private owner contact and raw source data separately.
        """
        ext_id = data["external_id"]
        owner_phone = data.get("owner_phone")
        loc = data.get("location_text")

        # Single authoritative query per row: match by external_id
        # or match by phone and location if row had no explicit external ID
        clauses = [Property.external_id == ext_id]
        if not data.get("has_explicit_id") and owner_phone and loc:
            clauses.append(
                (Property.owner_phone == owner_phone) & (Property.location_text == loc)
            )

        stmt = select(Property).where(
            Property.tenant_id == self.tenant_id,
            Property.is_deleted.is_(False),
            or_(*clauses),
        )
        res = await self.db.execute(stmt)
        prop = res.scalar_one_or_none()

        if prop:
            if prop.external_id == ext_id:
                # Exact match on external_id: update existing property in place
                prop.title = data["title"]
                prop.property_type = data["property_type"]
                if data.get("bhk") is not None:
                    prop.bhk = data["bhk"]
                prop.price = data["price"]
                prop.location_text = data["location_text"]
                if data.get("area_sqft") is not None:
                    prop.area_sqft = data["area_sqft"]
                if data.get("description") is not None:
                    prop.description = data["description"]
                if "owner_name" in data:
                    prop.owner_name = data["owner_name"]
                if "owner_phone" in data:
                    prop.owner_phone = data["owner_phone"]
                prop.source_data = raw_data
                await self.db.flush()
                return "updated"
            else:
                # Different external ID but matching owner phone and location:
                # Potential duplicate detected. Treat as suggestion, not proof.
                existing_staging: InventoryStagingItem | None = None
                try:
                    stmt = select(InventoryStagingItem).where(
                        InventoryStagingItem.tenant_id == self.tenant_id,
                        InventoryStagingItem.spreadsheet_id == integration.spreadsheet_id,
                        InventoryStagingItem.sheet_name == integration.properties_sheet_name,
                        InventoryStagingItem.duplicate_matched_property_id == prop.id,
                        InventoryStagingItem.status == "DUPLICATE_SUSPECT",
                        InventoryStagingItem.is_deleted.is_(False),
                    )
                    res = await self.db.execute(stmt)
                    val = res.scalar_one_or_none()
                    if isinstance(val, InventoryStagingItem):
                        existing_staging = val
                except Exception:
                    pass

                if existing_staging:
                    existing_staging.raw_data = raw_data
                    existing_staging.parsed_data = data
                    existing_staging.updated_at = datetime.now(UTC)
                    await self.db.flush()
                    return "staged"

                staging_item = InventoryStagingItem(
                    id=uuid.uuid4(),
                    tenant_id=self.tenant_id,
                    integration_id=integration.id,
                    spreadsheet_id=integration.spreadsheet_id,
                    sheet_name=integration.properties_sheet_name,
                    status="DUPLICATE_SUSPECT",
                    validation_errors=[
                        f"Potential duplicate of existing property {prop.external_id} "
                        f"({prop.title} in {prop.location_text})"
                    ],
                    raw_data=raw_data,
                    parsed_data=data,
                    source_size=data.get("source_size"),
                    source_area=data.get("source_area"),
                    owner_name=data.get("owner_name"),
                    owner_phone=data.get("owner_phone"),
                    duplicate_matched_property_id=prop.id,
                )
                self.db.add(staging_item)
                await self.db.flush()
                return "staged"

        # Create new property listing
        new_prop = Property(
            id=uuid.uuid4(),
            tenant_id=self.tenant_id,
            external_id=ext_id,
            title=data["title"],
            property_type=data["property_type"],
            bhk=data["bhk"],
            price=data["price"],
            currency_code="INR",
            location_text=data["location_text"],
            area_sqft=data.get("area_sqft"),
            description=data.get("description"),
            status="AVAILABLE",
            owner_name=data.get("owner_name"),
            owner_phone=data.get("owner_phone"),
            source_data=raw_data,
            amenities=[],
            photos=[],
        )
        self.db.add(new_prop)
        await self.db.flush()
        return "created"
