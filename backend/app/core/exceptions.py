"""
Custom exception classes and FastAPI exception handlers.
All HTTP errors are raised as these typed exceptions — never raise HTTPException directly
in service code (keep services framework-agnostic).
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


# ── Base ──────────────────────────────────────────────────────────────────────
class CRMException(Exception):
    """Base exception for all application errors."""

    status_code: int = 500
    error_code: str = "INTERNAL_ERROR"
    message: str = "An unexpected error occurred."

    def __init__(self, message: str | None = None, detail: str | None = None):
        self.message = message or self.__class__.message
        self.detail = detail
        super().__init__(self.message)


# ── 400 Bad Request ───────────────────────────────────────────────────────────
class ValidationError(CRMException):
    status_code = 400
    error_code = "VALIDATION_ERROR"
    message = "Invalid input."


class InvalidTransitionError(CRMException):
    status_code = 422
    error_code = "INVALID_TRANSITION"
    message = "This state transition is not allowed."


# ── 401 Unauthorized ──────────────────────────────────────────────────────────
class AuthenticationError(CRMException):
    status_code = 401
    error_code = "AUTHENTICATION_ERROR"
    message = "Authentication required."


class TokenExpiredError(CRMException):
    status_code = 401
    error_code = "TOKEN_EXPIRED"
    message = "Token has expired."


class InvalidTokenError(CRMException):
    status_code = 401
    error_code = "INVALID_TOKEN"
    message = "Invalid token."


# ── 403 Forbidden ─────────────────────────────────────────────────────────────
class PermissionDeniedError(CRMException):
    status_code = 403
    error_code = "PERMISSION_DENIED"
    message = "You do not have permission to perform this action."


class TenantMismatchError(CRMException):
    status_code = 403
    error_code = "TENANT_MISMATCH"
    message = "Access to this resource is not allowed."


# ── 404 Not Found ─────────────────────────────────────────────────────────────
class NotFoundError(CRMException):
    status_code = 404
    error_code = "NOT_FOUND"
    message = "Resource not found."


# ── 409 Conflict ─────────────────────────────────────────────────────────────
class DuplicateLeadError(CRMException):
    status_code = 409
    error_code = "DUPLICATE_LEAD"
    message = "A lead with this contact already exists."


class ConflictError(CRMException):
    status_code = 409
    error_code = "CONFLICT"
    message = "Resource conflict."


# ── 422 Business Logic ────────────────────────────────────────────────────────
class BusinessRuleError(CRMException):
    status_code = 422
    error_code = "BUSINESS_RULE_VIOLATION"
    message = "This action violates a business rule."


# ── 429 Rate Limit ────────────────────────────────────────────────────────────
class RateLimitError(CRMException):
    status_code = 429
    error_code = "RATE_LIMITED"
    message = "Too many requests. Please slow down."


# ── Exception handlers ────────────────────────────────────────────────────────
def register_exception_handlers(app: FastAPI) -> None:
    """Register all custom exception handlers on the FastAPI app."""

    @app.exception_handler(CRMException)
    async def crm_exception_handler(request: Request, exc: CRMException) -> JSONResponse:
        body: dict = {
            "error": exc.error_code,
            "message": exc.message,
        }
        if exc.detail and not request.app.state.settings.is_production:
            body["detail"] = exc.detail
        return JSONResponse(status_code=exc.status_code, content=body)
