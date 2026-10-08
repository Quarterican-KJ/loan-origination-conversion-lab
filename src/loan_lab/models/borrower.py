from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from loan_lab.models.base import Base, enum_type
from loan_lab.models.enums import BorrowerType

if TYPE_CHECKING:
    from loan_lab.models.application import ApplicationParty, LoanApplication
    from loan_lab.models.collateral import Collateral


class Borrower(Base):
    __tablename__ = "borrower"
    __table_args__ = (
        # Source IDs are only unique within the system that issued them.
        UniqueConstraint("source_system", "source_system_id"),
        CheckConstraint(
            "(source_system IS NULL) = (source_system_id IS NULL)", name="source_ref_complete"
        ),
        CheckConstraint("length(trim(legal_name)) > 0", name="legal_name_not_blank"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_system: Mapped[str | None] = mapped_column(String(50))
    source_system_id: Mapped[str | None] = mapped_column(String(100))
    legal_name: Mapped[str] = mapped_column(String(200))
    borrower_type: Mapped[BorrowerType] = mapped_column(enum_type(BorrowerType, "borrower_type"))

    parties: Mapped[list[ApplicationParty]] = relationship(back_populates="borrower")
    applications: Mapped[list[LoanApplication]] = relationship(
        secondary="application_party", viewonly=True, order_by="LoanApplication.id"
    )
    owned_collateral: Mapped[list[Collateral]] = relationship(
        back_populates="owner", order_by="Collateral.id"
    )

    def __repr__(self) -> str:
        return f"Borrower(id={self.id!r}, legal_name={self.legal_name!r})"
