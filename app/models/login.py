from uuid import UUID

from sqlalchemy import Integer, String, UUID as SQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class Login(Base):
    __tablename__ = "logins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    advisor_id: Mapped[UUID | None] = mapped_column(SQLUUID(as_uuid=True), nullable=True)
