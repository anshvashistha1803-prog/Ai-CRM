"""
Development-only bootstrap command for HAVN Realty CRM.
Provisions an initial tenant and active MANAGER user safely and idempotently.

Refuses to run in production environments. Never logs or prints plaintext passwords.
"""

import argparse
import asyncio
import getpass
import os
import sys
import uuid
from datetime import UTC, datetime

import structlog
from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import AsyncSessionLocal
from app.core.dependencies import UserRole
from app.core.security import hash_password
from app.tenants.models import Tenant
from app.users.models import User

logger = structlog.get_logger()


def validate_bootstrap_email(email: str) -> str:
    """Validate and normalize email address."""
    clean = email.strip()
    if not clean:
        raise ValueError("Email cannot be empty.")
    try:
        valid = validate_email(clean, check_deliverability=False)
        return valid.normalized.lower()
    except EmailNotValidError as exc:
        raise ValueError(f"Invalid email address: {exc}") from exc


def validate_bootstrap_password(password: str) -> str:
    """
    Validate password strength using the application's standard requirements:
    10-128 characters, at least one uppercase letter, at least one digit,
    and at least one special character.
    """
    if len(password) < 10 or len(password) > 128:
        raise ValueError("Password must be between 10 and 128 characters long.")
    errors = []
    if not any(c.isupper() for c in password):
        errors.append("at least one uppercase letter")
    if not any(c.isdigit() for c in password):
        errors.append("at least one digit")
    if not any(c in "!@#$%^&*()_+-=[]{}|;:,.<>?" for c in password):
        errors.append("at least one special character (!@#$%^&*()_+-=[]{}|;:,.<>?)")
    if errors:
        raise ValueError(f"Password must contain: {', '.join(errors)}")
    return password


async def bootstrap_dev_environment(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    tenant_name: str = "HAVN Realty",
    tenant_slug: str = "havn",
    full_name: str = "HAVN Manager",
    role: str = UserRole.MANAGER.value,
    timezone: str = "Asia/Kolkata",
    currency_code: str = "INR",
    settings_override: Settings | None = None,
) -> tuple[Tenant, User, bool, bool]:
    """
    Bootstrap an initial tenant and active user in development or staging.

    Guarantees:
    1. Refuses to run if environment is production.
    2. Validates email and password using CRM security rules.
    3. Hashes password using bcrypt. Never logs or returns plaintext secrets.
    4. Idempotent: reuses existing tenant and user if they already exist,
       preventing duplicate records.
    5. Returns (tenant, user, tenant_created, user_created).
    """
    settings = settings_override or get_settings()
    if settings.is_production or settings.environment.lower() in ("production", "prod"):
        logger.error("bootstrap_refused_production", environment=settings.environment)
        raise RuntimeError("Bootstrap procedure is strictly disabled in production environments.")

    # Validate inputs
    norm_email = validate_bootstrap_email(email)
    validate_bootstrap_password(password)

    valid_roles = [r.value for r in UserRole]
    if role not in valid_roles:
        raise ValueError(f"Invalid role '{role}'. Must be one of: {', '.join(valid_roles)}")

    clean_slug = tenant_slug.strip().lower()
    now = datetime.now(UTC)

    # 1. Lookup or create tenant
    tenant_stmt = select(Tenant).where(Tenant.slug == clean_slug)
    res_tenant = await db.execute(tenant_stmt)
    tenant = res_tenant.scalar_one_or_none()
    tenant_created = False

    if not tenant:
        tenant = Tenant(
            id=uuid.uuid4(),
            name=tenant_name.strip(),
            slug=clean_slug,
            timezone=timezone,
            currency_code=currency_code,
            is_active=True,
            settings={},
            created_at=now,
            updated_at=now,
            is_deleted=False,
        )
        db.add(tenant)
        await db.flush()
        tenant_created = True
        logger.info("bootstrap_tenant_created", tenant_id=str(tenant.id), slug=clean_slug)
    else:
        # Reactivate or update existing tenant in place
        if tenant.is_deleted:
            tenant.is_deleted = False
            tenant.deleted_at = None
        if not tenant.is_active:
            tenant.is_active = True
        tenant.updated_at = now
        await db.flush()
        logger.info("bootstrap_tenant_reused", tenant_id=str(tenant.id), slug=clean_slug)

    # 2. Lookup or create user
    user_stmt = select(User).where(
        User.tenant_id == tenant.id,
        User.email_normalized == norm_email,
    )
    res_user = await db.execute(user_stmt)
    user = res_user.scalar_one_or_none()
    user_created = False

    hashed = hash_password(password)

    if not user:
        user = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            email=norm_email,
            email_normalized=norm_email,
            password_hash=hashed,
            full_name=full_name.strip(),
            role=role,
            is_active=True,
            created_at=now,
            updated_at=now,
            is_deleted=False,
        )
        db.add(user)
        await db.flush()
        user_created = True
        logger.info("bootstrap_user_created", user_id=str(user.id), email=norm_email, role=role)
    else:
        # Reactivate and update existing user in place
        if user.is_deleted:
            user.is_deleted = False
            user.deleted_at = None
        user.is_active = True
        user.password_hash = hashed
        user.role = role
        user.full_name = full_name.strip()
        user.updated_at = now
        await db.flush()
        logger.info("bootstrap_user_updated", user_id=str(user.id), email=norm_email, role=role)

    return tenant, user, tenant_created, user_created


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap initial tenant and manager user for local development."
    )
    parser.add_argument(
        "--email",
        default="manager@havnrealty.com",
        help="Email address for the initial manager user (default: manager@havnrealty.com).",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="Password for the initial user (optional, can also be prompted securely).",
    )
    parser.add_argument(
        "--password-env",
        default=None,
        help="Name of environment variable containing the password.",
    )
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read password from standard input.",
    )
    parser.add_argument(
        "--tenant-name",
        default="HAVN Realty",
        help="Name of the initial agency tenant (default: HAVN Realty).",
    )
    parser.add_argument(
        "--tenant-slug",
        default="havn",
        help="Slug for the tenant (default: havn).",
    )
    parser.add_argument(
        "--full-name",
        default="HAVN Manager",
        help="Display name for the initial manager (default: HAVN Manager).",
    )
    parser.add_argument(
        "--role",
        default=UserRole.MANAGER.value,
        choices=[r.value for r in UserRole],
        help=f"Role for the initial user (default: {UserRole.MANAGER.value}).",
    )
    parser.add_argument(
        "--timezone",
        default="Asia/Kolkata",
        help="Tenant timezone (default: Asia/Kolkata).",
    )
    parser.add_argument(
        "--currency",
        default="INR",
        help="Tenant currency code (default: INR).",
    )
    return parser.parse_args(args)


def resolve_password(args: argparse.Namespace) -> str:
    """Resolve password from CLI arg, environment, stdin, or interactive prompt."""
    if args.password:
        return args.password

    if args.password_env:
        env_val = os.environ.get(args.password_env)
        if not env_val:
            raise ValueError(f"Environment variable '{args.password_env}' is not set or empty.")
        return env_val

    if args.password_stdin:
        stdin_val = sys.stdin.read().strip()
        if not stdin_val:
            raise ValueError("No password provided on stdin.")
        return stdin_val

    # Check fallback environment variable
    fallback_env = os.environ.get("BOOTSTRAP_PASSWORD")
    if fallback_env:
        return fallback_env

    # If running interactively, prompt securely via getpass
    if sys.stdin.isatty():
        pw1 = getpass.getpass("Enter initial manager password: ")
        pw2 = getpass.getpass("Confirm initial manager password: ")
        if pw1 != pw2:
            raise ValueError("Passwords do not match.")
        return pw1

    raise ValueError(
        "Password required. Provide via --password, --password-env, --password-stdin, "
        "BOOTSTRAP_PASSWORD env var, or run interactively."
    )


async def async_main(args: argparse.Namespace) -> int:
    try:
        password = resolve_password(args)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    try:
        async with AsyncSessionLocal() as session, session.begin():
            tenant, user, t_created, u_created = await bootstrap_dev_environment(
                    session,
                    email=args.email,
                    password=password,
                    tenant_name=args.tenant_name,
                    tenant_slug=args.tenant_slug,
                    full_name=args.full_name,
                    role=args.role,
                    timezone=args.timezone,
                    currency_code=args.currency,
                )

        t_status = "Created" if t_created else "Reused existing"
        u_status = "Created" if u_created else "Updated existing"
        print(f"[OK] Tenant: {tenant.name} (slug: '{tenant.slug}', id: {tenant.id}) [{t_status}]")
        print(f"[OK] User:   {user.email} (role: {user.role}, id: {user.id}) [{u_status}]")
        print("[OK] Bootstrap completed successfully. You can now log in via /api/v1/auth/login.")
        return 0
    except Exception as exc:
        print(f"[ERROR] Bootstrap failed: {exc}", file=sys.stderr)
        return 1


def main() -> None:
    args = parse_args()
    code = asyncio.run(async_main(args))
    sys.exit(code)


if __name__ == "__main__":
    main()
