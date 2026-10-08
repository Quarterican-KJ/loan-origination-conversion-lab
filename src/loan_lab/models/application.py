from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from loan_lab.models.base import Base, enum_type
from loan_lab.models.column_types import ExactDecimal
from loan_lab.models.enums import ApplicationStatus, LoanProduct, PartyRole

if TYPE_CHECKING:
    from loan_lab.models.borrower import Borrower
    from loan_lab.models.collateral import Collateral, CollateralPledge


class LoanApplication(Base):
    __tablename__ = "loan_application"
    __table_args__ = (
        # Source IDs are only unique within the system that issued them.
        UniqueConstraint("source_system", "source_system_id"),
        CheckConstraint(
            "(source_system IS NULL) = (source_system_id IS NULL)", name="source_ref_complete"
        ),
        CheckConstraint("requested_amount > 0", name="requested_amount_positive"),
        CheckConstraint("interest_rate >= 0", name="interest_rate_non_negative"),
        CheckConstraint("term_months > 0", name="term_months_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_system: Mapped[str | None] = mapped_column(String(50))
    source_system_id: Mapped[str | None] = mapped_column(String(100))
    loan_product: Mapped[LoanProduct] = mapped_column(enum_type(LoanProduct, "loan_product"))
    requested_amount: Mapped[Decimal] = mapped_column(ExactDecimal(15, 2))
    # Annual rate as a percentage: Decimal("6.1250") means 6.125%.
    interest_rate: Mapped[Decimal] = mapped_column(ExactDecimal(7, 4))
    term_months: Mapped[int]
    status: Mapped[ApplicationStatus] = mapped_column(
        enum_type(ApplicationStatus, "application_status"), default=ApplicationStatus.DRAFT
    )

    parties: Mapped[list[ApplicationParty]] = relationship(
        back_populates="application", cascade="all, delete-orphan", passive_deletes=True
    )
    borrowers: Mapped[list[Borrower]] = relationship(
        secondary="application_party", viewonly=True, order_by="Borrower.id"
    )
    collateral_pledges: Mapped[list[CollateralPledge]] = relationship(
        back_populates="application", cascade="all, delete-orphan", passive_deletes=True
    )
    collateral: Mapped[list[Collateral]] = relationship(
        secondary="collateral_pledge", viewonly=True, order_by="Collateral.id"
    )

    def __repr__(self) -> str:
        return f"LoanApplication(id={self.id!r}, loan_product={self.loan_product!r})"


class ApplicationParty(Base):
    __tablename__ = "application_party"
    __table_args__ = (
        UniqueConstraint("application_id", "borrower_id"),
        Index(
            "uq_application_party_one_primary_borrower",
            "application_id",
            unique=True,
            sqlite_where=text("role = 'primary_borrower'"),
            postgresql_where=text("role = 'primary_borrower'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("loan_application.id", ondelete="CASCADE")
    )
    borrower_id: Mapped[int] = mapped_column(
        ForeignKey("borrower.id", ondelete="RESTRICT"), index=True
    )
    role: Mapped[PartyRole] = mapped_column(enum_type(PartyRole, "party_role"))

    application: Mapped[LoanApplication] = relationship(back_populates="parties")
    borrower: Mapped[Borrower] = relationship(back_populates="parties")

    def __repr__(self) -> str:
        return (
            f"ApplicationParty(application_id={self.application_id!r}, "
            f"borrower_id={self.borrower_id!r}, role={self.role!r})"
        )
