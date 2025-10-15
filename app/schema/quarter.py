from pydantic import BaseModel
from uuid import UUID
from datetime import date


class Quarter(BaseModel):
    financial_year_id: UUID
    quarter_number: int
    start_date: date
    end_date: date
    is_locked: bool
    status: str