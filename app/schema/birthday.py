from datetime import date
from uuid import UUID

from pydantic import BaseModel


class BirthdayPerson(BaseModel):
    id: UUID
    first_name: str
    last_name: str
    date_of_birth: date
    is_family_member: bool
    family_relationship: str | None = None
    household_client_id: UUID
    household_client_name: str
