from sqlalchemy import Integer, String, DateTime, Boolean, ForeignKey, UUID as SQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from datetime import datetime
from app.db.database import Base
from typing import List, TYPE_CHECKING
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from app.models.financial_year import FinancialYear



class Client(Base):
    __tablename__ = "clients"
    id: Mapped[UUID] = mapped_column(SQLUUID(as_uuid=True), primary_key=True, default=uuid4, index=True)
    first_name: Mapped[str] = mapped_column(String, nullable=False)
    last_name: Mapped[str] = mapped_column(String, nullable=False)
    adhar_number: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    pan_number: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    gender: Mapped[str] = mapped_column(String, nullable=False)
    date_of_birth: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_family_member: Mapped[bool] = mapped_column(Boolean, default=False)
    parent_id: Mapped[UUID] = mapped_column(SQLUUID(as_uuid=True), nullable=True)
    family_relationship: Mapped[str] = mapped_column(String, nullable=False)
    is_advance_tax_payer: Mapped[bool] = mapped_column(Boolean, default=False)
   
    
    
    # Relationships (commented out - not needed for basic queries)
    # financial_years: Mapped[List["FinancialYear"]] = relationship("FinancialYear", back_populates="client")
    
    