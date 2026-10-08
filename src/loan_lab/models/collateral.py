from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Date, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from loan_lab.models.base import Base, enum_type
from loan_lab.models.column_types import ExactDecimal
from loan_lab.models.enums import CollateralType, LienStatus, PledgeStatus

if TYPE_CHECKING:
    from loan_lab.models.application import LoanApplication
    from loan_lab.models.borrower import Borrower


class Collateral(Base):
    __tablename__ = "collateral"
    __table_args__ = (
        # Source IDs are only unique within the system that issued them.
        UniqueConstraint("source_system", "source_system_id"),
        CheckConstraint(
            "(source_system IS NULL) = (source_system_id IS NULL)", name="source_ref_complete"
        ),
        CheckConstraint("length(trim(description)) > 0", name="description_not_blank"),
        CheckConstraint(
            "appraised_value IS NULL OR appraised_value > 0", name="appraised_value_positive"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_system: Mapped[str | None] = mapped_column(String(50))
    source_system_id: Mapped[str | None] = mapped_column(String(100))
    collateral_type: Mapped[CollateralType] = mapped_column(
        enum_type(CollateralType, "collateral_type")
    )
    description: Mapped[str] = mapped_column(String(500))
    # Nullable so incomplete legacy records load as-is; never default or fabricate a valuation.
    appraised_value: Mapped[Decimal | None] = mapped_column(ExactDecimal(15, 2))
    valuation_date: Mapped[date | None] = mapped_column(Date)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("borrower.id", ondelete="RESTRICT"), index=True
    )

    owner: Mapped[Borrower | None] = relationship(back_populates="owned_collateral")
    pledges: Mapped[list[CollateralPledge]] = relationship(back_populates="collateral")
    applications: Mapped[list[LoanApplication]] = relationship(
        secondary="collateral_pledge", viewonly=True, order_by="LoanApplication.id"
    )
    # Ordered by insertion only; `priority` is recorded data, not a computed legal ranking.
    liens: Mapped[list[Lien]] = relationship(
        back_populates="collateral",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Lien.id",
    )

    def __repr__(self) -> str:
        return f"Collateral(id={self.id!r}, collateral_type={self.collateral_type!r})"


class CollateralPledge(Base):
    __tablename__ = "collateral_pledge"
    __table_args__ = (
        UniqueConstraint("application_id", "collateral_id"),
        CheckConstraint(
            "pledged_amount IS NULL OR pledged_amount > 0", name="pledged_amount_positive"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("loan_application.id", ondelete="CASCADE")
    )
    collateral_id: Mapped[int] = mapped_column(
        ForeignKey("collateral.id", ondelete="RESTRICT"), index=True
    )
    pledged_amount: Mapped[Decimal | None] = mapped_column(ExactDecimal(15, 2))
    status: Mapped[PledgeStatus] = mapped_column(
        enum_type(PledgeStatus, "pledge_status"), default=PledgeStatus.PROPOSED
    )

    application: Mapped[LoanApplication] = relationship(back_populates="collateral_pledges")
    collateral: Mapped[Collateral] = relationship(back_populates="pledges")

    def __repr__(self) -> str:
        return (
            f"CollateralPledge(application_id={self.application_id!r}, "
            f"collateral_id={self.collateral_id!r}, status={self.status!r})"
        )


class Lien(Base):
    __tablename__ = "lien"
    __table_args__ = (
        CheckConstraint("length(trim(creditor_name)) > 0", name="creditor_name_not_blank"),
        CheckConstraint("priority > 0", name="priority_positive"),
        CheckConstraint("outstanding_balance >= 0", name="outstanding_balance_non_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    collateral_id: Mapped[int] = mapped_column(
        ForeignKey("collateral.id", ondelete="CASCADE"), index=True
    )
    creditor_name: Mapped[str] = mapped_column(String(200))
    # Recorded position (1 = first). Not unique per collateral and not used to infer subordination.
    priority: Mapped[int]
    outstanding_balance: Mapped[Decimal] = mapped_column(ExactDecimal(15, 2))
    status: Mapped[LienStatus] = mapped_column(
        enum_type(LienStatus, "lien_status"), default=LienStatus.ACTIVE
    )

    collateral: Mapped[Collateral] = relationship(back_populates="liens")

    def __repr__(self) -> str:
        return (
            f"Lien(id={self.id!r}, collateral_id={self.collateral_id!r}, "
            f"priority={self.priority!r}, status={self.status!r})"
        )
