"""
Quarter Transition Service
Unlocks the current quarter (active) and marks the previous quarter as completed.
Handles year boundaries (e.g. Q1 following Q4 of previous financial year).
"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, and_, or_
from datetime import date
from typing import Tuple, Dict
from sqlalchemy.exc import SQLAlchemyError


from nucleus.models.advance_tax_models.financial_year import FinancialYear
from nucleus.models.advance_tax_models.quarter import Quarter
from app.core.logger import logger


# Status values aligned with app.service.quarter
STATUS_ACTIVE = "active"
STATUS_COMPLETED = "completed"


def get_current_quarter_from_date(today: date) -> Tuple[int, int]:
    """
    Determine the current ongoing quarter and its financial year start year.

    Financial year is April–March. Quarter boundaries:
    - Q1: Apr 1 – Jun 30
    - Q2: Jul 1 – Sep 30
    - Q3: Oct 1 – Dec 31
    - Q4: Jan 1 – Mar 31 (next calendar year)

    Args:
        today: Date to evaluate (defaults to today if not provided by caller).

    Returns:
        Tuple of (fy_start_year, quarter_number).
        e.g. (2025, 1) for Q1 of FY 25-26, (2024, 4) for Q4 of FY 24-25 in Jan–Mar.
    """
    if today.month >= 4:
        fy_start_year = today.year
    else:
        fy_start_year = today.year - 1

    if 4 <= today.month <= 6:
        quarter_number = 1
    elif 7 <= today.month <= 9:
        quarter_number = 2
    elif 10 <= today.month <= 12:
        quarter_number = 3
    else:
        quarter_number = 4  # Jan–Mar

    return fy_start_year, quarter_number


def get_previous_quarter(fy_start_year: int, quarter_number: int) -> Tuple[int, int]:
    """
    Get the previous quarter, handling year boundary (Q1 -> Q4 of previous FY).

    Args:
        fy_start_year: Financial year start year (e.g. 2025).
        quarter_number: Current quarter (1–4).

    Returns:
        Tuple of (fy_start_year_prev, quarter_number_prev).
    """
    if quarter_number == 1:
        return fy_start_year - 1, 4
    return fy_start_year, quarter_number - 1


def _fy_string(fy_start_year: int) -> str:
    """Format financial year as e.g. 25-26 for 2025."""
    return f"{str(fy_start_year)[-2:]}-{str(fy_start_year + 1)[-2:]}"


async def transition_quarters_to_current_state(db: AsyncSession) -> Dict:
    """
    In a single transaction:
    - Unlock the current quarter (is_locked=False, status=active).
    - Mark the previous quarter as completed (status=completed).

    Idempotent: only updates rows that are not already in the target state.
    Assumes quarters already exist in the database.

    Returns:
        Dict with status, message, current_quarters_updated, previous_quarters_updated,
        current_fy_q, previous_fy_q, and error details on failure.
    """
    today = date.today()
    fy_start, q_num = get_current_quarter_from_date(today)
    fy_prev, q_prev = get_previous_quarter(fy_start, q_num)

    current_fy_str = _fy_string(fy_start)
    prev_fy_str = _fy_string(fy_prev)

    logger.info(
        f"Quarter transition: current Q{q_num} FY {current_fy_str}, "
        f"previous Q{q_prev} FY {prev_fy_str} (date={today})"
    )

    try:
        # Resolve financial year IDs for current and previous FY
        stmt_current = select(FinancialYear.id).where(
            FinancialYear.financial_year == current_fy_str
        )
        result_current = await db.execute(stmt_current)
        current_fy_ids = [r[0] for r in result_current.all()]

        stmt_prev = select(FinancialYear.id).where(
            FinancialYear.financial_year == prev_fy_str
        )
        result_prev = await db.execute(stmt_prev)
        prev_fy_ids = [r[0] for r in result_prev.all()]

        # Idempotent: only update current quarters that are locked or not active
        current_updated = 0
        if current_fy_ids:
            stmt_update_current = (
                update(Quarter)
                .where(
                    and_(
                        Quarter.financial_year_id.in_(current_fy_ids),
                        Quarter.quarter_number == q_num,
                        or_(Quarter.is_locked == True, Quarter.status != STATUS_ACTIVE),
                    )
                )
                .values(is_locked=False, status=STATUS_ACTIVE)
            )
            result = await db.execute(stmt_update_current)
            current_updated = result.rowcount

        # Idempotent: only update previous quarters that are not already completed
        previous_updated = 0
        if prev_fy_ids:
            stmt_update_prev = (
                update(Quarter)
                .where(
                    and_(
                        Quarter.financial_year_id.in_(prev_fy_ids),
                        Quarter.quarter_number == q_prev,
                        Quarter.status != STATUS_COMPLETED,
                    )
                )
                .values(status=STATUS_COMPLETED)
            )
            result = await db.execute(stmt_update_prev)
            previous_updated = result.rowcount

        await db.commit()

        logger.info(
            f"Quarter transition committed: current quarters updated={current_updated}, "
            f"previous quarters updated={previous_updated}"
        )

        return {
            "status": "success",
            "message": "Quarter transition completed",
            "current_fy_q": (current_fy_str, q_num),
            "previous_fy_q": (prev_fy_str, q_prev),
            "current_quarters_updated": current_updated,
            "previous_quarters_updated": previous_updated,
        }
    except SQLAlchemyError as e:
        await db.rollback()
        logger.error(f"Database error during quarter transition: {str(e)}", exc_info=True)
        return {
            "status": "error",
            "message": f"Database error: {str(e)}",
            "current_fy_q": (current_fy_str, q_num),
            "previous_fy_q": (prev_fy_str, q_prev),
            "current_quarters_updated": 0,
            "previous_quarters_updated": 0,
        }
    except Exception as e:
        await db.rollback()
        logger.exception(f"Unexpected error during quarter transition: {str(e)}")
        return {
            "status": "error",
            "message": str(e),
            "current_fy_q": (current_fy_str, q_num),
            "previous_fy_q": (prev_fy_str, q_prev),
            "current_quarters_updated": 0,
            "previous_quarters_updated": 0,
        }
