"""Cross-process model-admission quota pools (see ADR-034).

Holds no secrets: ``pool_id`` is a non-secret identifier and capacity
lives in the database so an operator can tune it without a release.
The default is a single conservative pool (with an OCR sub-limit) so a
shared upstream never sees multiplied concurrency.
"""
from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.sql import func, text
from ..core.db import Base
from .base import BaseModel


class ModelAdmissionPool(BaseModel):
    __tablename__ = "model_admission_pools"

    pool_id = Column(String(64), unique=True, index=True, nullable=False)
    capacity = Column(Integer, nullable=False)
    background_capacity = Column(Integer, nullable=False)
    ocr_sublimit = Column(Integer, nullable=False, default=1, server_default=text("1"))
    cooldown_until = Column(DateTime(timezone=True), nullable=True)
    cooldown_retry_after = Column(Integer, nullable=True)


class ModelAdmissionLease(Base):
    __tablename__ = "model_admission_leases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pool_id = Column(String(64), nullable=False, index=True)
    owner_token = Column(String(64), nullable=False, unique=True)
    kind = Column(String(16), nullable=False)
    purpose = Column(String(16), nullable=False)
    # Opaque digest of (purpose, endpoint, model, credential). Never a raw URL,
    # key or model name; NULL for callers that do not opt into scoped isolation.
    failure_scope = Column(String(64), nullable=True)
    lease_until = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())