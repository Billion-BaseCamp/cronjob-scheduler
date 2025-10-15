from pydantic import BaseModel
from datetime import date

class FinancialYears(BaseModel):
    financial_year: str
    start_date: date
    end_date: date
    return_date: date
    status: str