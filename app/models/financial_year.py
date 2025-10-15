from sqlalchemy import String,Date, DateTime, ForeignKey, UUID as SQLUUID
from uuid import UUID, uuid4
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from app.db.database import Base
from typing import List, TYPE_CHECKING
from datetime import date, datetime

if TYPE_CHECKING:
    from app.models.client import Client
    from app.models.quarter import Quarter

class FinancialYear(Base):
    __tablename__ = "financial_years"
    id: Mapped[UUID] = mapped_column(SQLUUID(as_uuid=True), primary_key=True, default=uuid4, index=True)
    
    # Foreign key to client
    client_id: Mapped[UUID] = mapped_column(SQLUUID(as_uuid=True), ForeignKey("clients.id"), nullable=False)
    
    financial_year: Mapped[str] = mapped_column(String, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    return_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    
    # Relationships
    # client: Mapped["Client"] = relationship("Client", back_populates="financial_years")
    # quarters: Mapped[List["Quarter"]] = relationship("Quarter", back_populates="financial_year")
    
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), onupdate=func.now(), nullable=True)