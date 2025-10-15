from sqlalchemy import String, DateTime, Date ,Integer, Boolean, ForeignKey, UUID as SQLUUID
from uuid import UUID, uuid4
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from datetime import datetime,date
from app.db.database import Base
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.financial_year import FinancialYear


class Quarter(Base):
    __tablename__ = "quarters"
    id: Mapped[UUID] = mapped_column(SQLUUID(as_uuid=True), primary_key=True, default=uuid4, index=True)
    
    # Foreign key to financial year
    financial_year_id: Mapped[UUID] = mapped_column(SQLUUID(as_uuid=True), ForeignKey("financial_years.id"), nullable=False)
    
    quarter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    
    # Relationships (commented out - not needed for basic queries)
    # financial_year: Mapped["FinancialYear"] = relationship("FinancialYear", back_populates="quarters")
    
    # Financial data relationships
       
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), onupdate=func.now(), nullable=True)