"""Immutable successful entry-decision receipt, committed with its session."""

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Unicode

from app.database import Base


class EntryConfirmationReceipt(Base):
    __tablename__ = "entry_confirmation_receipts"

    decision_id = Column(Unicode(200).with_variant(Unicode(200, collation="Latin1_General_100_BIN2"), "mssql"), primary_key=True)
    request_fingerprint = Column(String(64), nullable=False)
    plate_number = Column(String(50), nullable=False)
    entry_log_id = Column(Integer, ForeignKey("entry_exit_log.id", ondelete="NO ACTION"), nullable=False)
    session_id = Column(Integer, ForeignKey("parking_sessions.id", ondelete="NO ACTION"), nullable=False)
    created_at = Column(DateTime, nullable=False)
