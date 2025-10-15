from pydantic import BaseModel
from uuid import UUID

class Client(BaseModel):
    id: UUID
    first_name: str
    last_name: str


    