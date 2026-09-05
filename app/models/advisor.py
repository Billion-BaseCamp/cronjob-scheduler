from uuid import UUID, uuid4

from sqlalchemy import String, UUID as SQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class Advisor(Base):
    __tablename__ = "advisors"

    id: Mapped[UUID] = mapped_column(SQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    first_name: Mapped[str] = mapped_column(String, nullable=False)
