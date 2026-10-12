"""
Google Sheets client interface and implementations.
Follows the Adapter pattern to isolate Google API dependencies and enable
100% deterministic test coverage via MockGoogleSheetsClient.
"""

from typing import Any, Protocol, runtime_checkable

import structlog

logger = structlog.get_logger()


class GoogleSheetsClientError(Exception):
    """Base exception for Google Sheets API interactions."""


class SpreadsheetAccessError(GoogleSheetsClientError):
    """Raised when access to a spreadsheet is denied or forbidden."""


class SpreadsheetNotFoundError(GoogleSheetsClientError):
    """Raised when the specified spreadsheet cannot be found."""


@runtime_checkable
class GoogleSheetsClient(Protocol):
    """Protocol for Google Sheets operations."""

    async def get_rows(self, spreadsheet_id: str, range_name: str) -> list[list[Any]]:
        """Fetch rows from a specified sheet and range."""
        ...

    async def append_row(self, spreadsheet_id: str, range_name: str, values: list[Any]) -> int:
        """Append a single row to the sheet and return the inserted row index (1-based)."""
        ...

    async def update_row(self, spreadsheet_id: str, range_name: str, row_index: int, values: list[Any]) -> bool:
        """Update a specific 1-based row index with new values."""
        ...


class MockGoogleSheetsClient:
    """
    Deterministic in-memory Google Sheets client for local development and test suites.
    Stores sheet data in memory indexed by (spreadsheet_id, sheet_name).
    """

    def __init__(self) -> None:
        self.sheets: dict[str, list[list[Any]]] = {}
        self._simulated_error: Exception | None = None

    def set_simulated_error(self, exc: Exception | None) -> None:
        """Configure client to raise an exception on next API call."""
        self._simulated_error = exc

    def _check_error(self) -> None:
        if self._simulated_error is not None:
            err = self._simulated_error
            self._simulated_error = None
            raise err

    def _get_key(self, spreadsheet_id: str, range_name: str) -> str:
        sheet_name = range_name.split("!")[0] if "!" in range_name else range_name
        return f"{spreadsheet_id}:{sheet_name}"

    def seed_sheet(self, spreadsheet_id: str, range_name: str, rows: list[list[Any]]) -> None:
        """Pre-populate a sheet in mock memory."""
        key = self._get_key(spreadsheet_id, range_name)
        self.sheets[key] = [list(r) for r in rows]

    async def get_rows(self, spreadsheet_id: str, range_name: str) -> list[list[Any]]:
        self._check_error()
        key = self._get_key(spreadsheet_id, range_name)
        return [list(r) for r in self.sheets.get(key, [])]

    async def append_row(self, spreadsheet_id: str, range_name: str, values: list[Any]) -> int:
        self._check_error()
        key = self._get_key(spreadsheet_id, range_name)
        if key not in self.sheets:
            self.sheets[key] = []
        self.sheets[key].append(list(values))
        row_idx = len(self.sheets[key])
        logger.info("mock_sheets_row_appended", key=key, row_index=row_idx, values=values[:4])
        return row_idx

    async def update_row(self, spreadsheet_id: str, range_name: str, row_index: int, values: list[Any]) -> bool:
        self._check_error()
        key = self._get_key(spreadsheet_id, range_name)
        if key not in self.sheets or row_index < 1 or row_index > len(self.sheets[key]):
            logger.warning("mock_sheets_row_not_found_for_update", key=key, row_index=row_index)
            return False
        self.sheets[key][row_index - 1] = list(values)
        logger.info("mock_sheets_row_updated", key=key, row_index=row_index, values=values[:4])
        return True


class OAuthGoogleSheetsClient:
    """
    Production client connecting to Google Sheets API using an OAuth 2.0 access token.
    """

    def __init__(self, access_token: str) -> None:
        self.access_token = access_token
        self._service = None

    def _get_service(self):
        if self._service is None:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            creds = Credentials(token=self.access_token)
            self._service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        return self._service

    async def get_rows(self, spreadsheet_id: str, range_name: str) -> list[list[Any]]:
        try:
            service = self._get_service()
            sheet = service.spreadsheets()
            result = sheet.values().get(spreadsheetId=spreadsheet_id, range=range_name).execute()
            return result.get("values", [])
        except Exception as exc:
            err_str = str(exc)
            if "404" in err_str:
                raise SpreadsheetNotFoundError(f"Spreadsheet not found: {spreadsheet_id}") from exc
            if "403" in err_str or "Permission" in err_str:
                raise SpreadsheetAccessError("Access denied to Google Spreadsheet. Check permissions.") from exc
            raise GoogleSheetsClientError(f"Google Sheets API error: {err_str}") from exc

    async def append_row(self, spreadsheet_id: str, range_name: str, values: list[Any]) -> int:
        try:
            service = self._get_service()
            sheet = service.spreadsheets()
            body = {"values": [values]}
            result = sheet.values().append(
                spreadsheetId=spreadsheet_id,
                range=range_name,
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body=body,
            ).execute()
            updates = result.get("updates", {})
            return updates.get("updatedRows", 1)
        except Exception as exc:
            raise GoogleSheetsClientError(f"Failed to append row: {exc}") from exc

    async def update_row(self, spreadsheet_id: str, range_name: str, row_index: int, values: list[Any]) -> bool:
        try:
            service = self._get_service()
            sheet = service.spreadsheets()
            target_range = f"{range_name.split('!')[0]}!A{row_index}:{row_index}"
            body = {"values": [values]}
            sheet.values().update(
                spreadsheetId=spreadsheet_id,
                range=target_range,
                valueInputOption="USER_ENTERED",
                body=body,
            ).execute()
            return True
        except Exception as exc:
            raise GoogleSheetsClientError(f"Failed to update row: {exc}") from exc


class RealGoogleSheetsClient:
    """
    Production client connecting to Google Sheets API via Service Account JSON.
    """

    def __init__(self, credentials_json: str) -> None:
        self.credentials_json = credentials_json
        self._service = None

    def _get_service(self):
        if self._service is None:
            import json

            from google.oauth2.service_account import Credentials
            from googleapiclient.discovery import build

            creds_data = json.loads(self.credentials_json)
            scopes = ["https://www.googleapis.com/auth/spreadsheets"]
            creds = Credentials.from_service_account_info(creds_data, scopes=scopes)
            self._service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        return self._service

    async def get_rows(self, spreadsheet_id: str, range_name: str) -> list[list[Any]]:
        service = self._get_service()
        sheet = service.spreadsheets()
        result = sheet.values().get(spreadsheetId=spreadsheet_id, range=range_name).execute()
        return result.get("values", [])

    async def append_row(self, spreadsheet_id: str, range_name: str, values: list[Any]) -> int:
        service = self._get_service()
        sheet = service.spreadsheets()
        body = {"values": [values]}
        result = sheet.values().append(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body,
        ).execute()
        updates = result.get("updates", {})
        return updates.get("updatedRows", 1)

    async def update_row(self, spreadsheet_id: str, range_name: str, row_index: int, values: list[Any]) -> bool:
        service = self._get_service()
        sheet = service.spreadsheets()
        target_range = f"{range_name.split('!')[0]}!A{row_index}:{row_index}"
        body = {"values": [values]}
        sheet.values().update(
            spreadsheetId=spreadsheet_id,
            range=target_range,
            valueInputOption="USER_ENTERED",
            body=body,
        ).execute()
        return True
