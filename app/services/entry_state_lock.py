"""Transaction-scoped serialization for one plate's entry/exit state."""

import hashlib

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.services.event_parser import normalize_plate


class EntryStateLockUnavailable(RuntimeError):
    """The database could not serialize a plate-state transaction."""


def plate_lock_resource(plate_number: str) -> str:
    """Return the shared SQL Server application-lock key for a plate."""
    raw_plate = (plate_number or "").strip().upper()
    plate_key = normalize_plate(raw_plate) or raw_plate
    return "entry-v2-plate:" + hashlib.sha256(plate_key.encode()).hexdigest()


def assert_authoritative_lock_backend(dialect_name: str) -> None:
    """Fail closed when authoritative Entry V2 lacks SQL Server app locks."""
    if settings.ENTRY_V2_MODE == "authoritative" and dialect_name != "mssql":
        raise EntryStateLockUnavailable(
            "ENTRY_V2_MODE=authoritative requires the mssql database dialect "
            "for transaction-owned application locks"
        )


def acquire_mssql_application_lock(db: Session, resource: str) -> None:
    """Acquire an exclusive lock owned by the current DB transaction."""
    dialect_name = db.get_bind().dialect.name
    assert_authoritative_lock_backend(dialect_name)
    if dialect_name != "mssql":
        return
    try:
        db.execute(
            text(
                """
                DECLARE @lock_result int;
                EXEC @lock_result = sys.sp_getapplock
                    @Resource = :resource,
                    @LockMode = 'Exclusive',
                    @LockOwner = 'Transaction',
                    @LockTimeout = :lock_timeout;
                IF @lock_result < 0
                    THROW 50001, 'Could not acquire entry state lock', 1;
                """
            ),
            {
                "resource": resource,
                "lock_timeout": settings.ENTRY_V2_APPLOCK_TIMEOUT_MS,
            },
        )
    except Exception as exc:
        raise EntryStateLockUnavailable(
            "could not acquire normalized-plate transaction lock"
        ) from exc


def acquire_plate_transaction_lock(db: Session, plate_number: str) -> None:
    """Serialize all entry/session mutations for one normalized plate."""
    acquire_mssql_application_lock(db, plate_lock_resource(plate_number))
