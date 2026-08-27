from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from datetime import date
from uuid import UUID
from typing import List, Tuple, Dict
from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from app.models.client import Client
from app.models.financial_year import FinancialYear
from app.models.quarter import Quarter
from app.core.logger import logger
from app.service.quarter import (
    create_quarters_for_financial_year,
    backfill_missing_quarters_for_financial_year,
)


def calculate_current_financial_year() -> Tuple[str, int]:
    """
    Calculate current financial year based on today's date
    
    Returns:
        Tuple of (financial_year_string, start_year)
        Example: ("25-26", 2025)
    """
    today = date.today()
    
    # If month >= April (4), FY starts this year
    # If month < April (Jan-Mar), FY started last year
    if today.month >= 4:
        fy_start_year = today.year
    else:
        fy_start_year = today.year - 1
    
    fy_end_year = fy_start_year + 1
    # Format: 25-26 (last 2 digits only)
    fy_string = f"{str(fy_start_year)[-2:]}-{str(fy_end_year)[-2:]}"
    
    logger.info(f"Calculated current financial year: {fy_string} (starts {fy_start_year})")
    return fy_string, fy_start_year




async def get_clients_without_current_fy(db: AsyncSession) -> List[UUID]:
    """
    Get client IDs that don't have financial year for current year
    
    Returns:
        List of client UUIDs without current FY
    """
    try:
        current_fy, _ = calculate_current_financial_year()
        
        # Find clients without the current financial year
        stmt = select(Client.id).where(
            Client.id.not_in(
                select(FinancialYear.client_id).where(
                    FinancialYear.financial_year == current_fy
                )
            )
        )
        
        result = await db.execute(stmt)
        client_ids = result.scalars().all()
        
        logger.info(f"Found {len(client_ids)} clients without FY {current_fy}")
        return client_ids
        
    except SQLAlchemyError as e:
        logger.error(f"Database error getting clients without FY: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    
    except Exception as e:
        logger.exception(f"Unexpected error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")


async def create_financial_year_with_quarters(
    client_id: UUID,
    db: AsyncSession
) -> FinancialYear:
    """
    Create financial year with 4 quarters for a client
    
    Args:
        client_id: Client UUID
        db: Database session
        
    Returns:
        Created FinancialYear object
    """
    try:
        # Calculate current FY
        fy_string, fy_start_year = calculate_current_financial_year()
        
        # Create Financial Year
        fy_start_date = date(fy_start_year, 4, 1)
        fy_end_date = date(fy_start_year + 1, 3, 31)
        fy_return_date = date(fy_start_year + 1, 7, 31)  # Default return date
        
        financial_year = FinancialYear(
            client_id=client_id,
            financial_year=fy_string,
            start_date=fy_start_date,
            end_date=fy_end_date,
            return_date=fy_return_date,
            status="active"
        )
        
        db.add(financial_year)
        await db.flush()  # Get the ID without committing
        
        logger.info(f"Created financial year {fy_string} for client {client_id}")
        
        # Create 4 Quarters using quarter service
        quarters = await create_quarters_for_financial_year(
            financial_year.id,
            fy_start_year,
            db
        )
        
        # Commit all changes
        await db.commit()
        await db.refresh(financial_year)
        
        logger.success(
            f"Successfully created FY {fy_string} with {len(quarters)} quarters "
            f"for client {client_id}"
        )
        
        return financial_year
        
    except SQLAlchemyError as e:
        await db.rollback()
        logger.error(f"Database error creating FY: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    
    except Exception as e:
        await db.rollback()
        logger.exception(f"Unexpected error creating FY: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")


async def get_financial_years_needing_quarter_backfill(
    db: AsyncSession,
) -> List[Tuple[UUID, date]]:
    """
    Find current-FY records that have fewer than 4 quarters.

    Returns:
        List of (financial_year_id, start_date) tuples.
    """
    current_fy, _ = calculate_current_financial_year()

    stmt = (
        select(FinancialYear.id, FinancialYear.start_date)
        .outerjoin(Quarter, Quarter.financial_year_id == FinancialYear.id)
        .where(FinancialYear.financial_year == current_fy)
        .group_by(FinancialYear.id, FinancialYear.start_date)
        .having(func.count(Quarter.id) < 4)
    )
    result = await db.execute(stmt)
    return result.all()


async def backfill_missing_quarters(db: AsyncSession) -> Dict:
    """
    Backfill missing quarters for current financial year records.

    Handles FY rows created without quarters (e.g. by another service).
    Safe to re-run: only creates quarter numbers that do not yet exist.
    """
    fy_records = await get_financial_years_needing_quarter_backfill(db)

    if not fy_records:
        logger.info("No financial years need quarter backfill")
        return {
            "financial_years_backfilled": 0,
            "quarters_backfilled": 0,
            "backfill_failed": 0,
        }

    logger.info(f"Found {len(fy_records)} financial year(s) needing quarter backfill")

    financial_years_backfilled = 0
    quarters_backfilled = 0
    backfill_failed = 0

    for fy_id, start_date in fy_records:
        try:
            created = await backfill_missing_quarters_for_financial_year(
                fy_id,
                start_date.year,
                db,
            )
            await db.commit()
            if created > 0:
                financial_years_backfilled += 1
                quarters_backfilled += created
        except Exception as e:
            await db.rollback()
            backfill_failed += 1
            logger.error(f"Failed to backfill quarters for FY {fy_id}: {str(e)}")

    logger.info(
        f"Quarter backfill completed: {financial_years_backfilled} FY(s), "
        f"{quarters_backfilled} quarter(s) created, {backfill_failed} failed"
    )

    return {
        "financial_years_backfilled": financial_years_backfilled,
        "quarters_backfilled": quarters_backfilled,
        "backfill_failed": backfill_failed,
    }


async def create_financial_years_for_all_clients(db: AsyncSession) -> Dict:
    """
    Cron job function: Create financial years for all clients without current FY
    
    Returns:
        Dictionary with summary of created records
    """
    try:
        logger.info("Starting financial year creation job...")
        
        # Step 1: Create FY + quarters for clients missing current FY
        client_ids = await get_clients_without_current_fy(db)

        success_count = 0
        failed_count = 0
        failed_clients = []

        if client_ids:
            for client_id in client_ids:
                try:
                    await create_financial_year_with_quarters(client_id, db)
                    success_count += 1
                except Exception as e:
                    failed_count += 1
                    failed_clients.append(str(client_id))
                    logger.error(f"Failed to create FY for client {client_id}: {str(e)}")
        else:
            logger.info("No clients need financial year creation")

        # Step 2: Backfill quarters for FY records that exist without full quarter sets
        backfill = await backfill_missing_quarters(db)

        total_quarters_created = success_count * 4 + backfill["quarters_backfilled"]
        has_failures = failed_count > 0 or backfill["backfill_failed"] > 0

        result = {
            "status": "partial" if has_failures else "success",
            "message": (
                f"Created financial years for {success_count} client(s); "
                f"backfilled {backfill['quarters_backfilled']} quarter(s) "
                f"for {backfill['financial_years_backfilled']} existing FY record(s)"
            ),
            "clients_processed": len(client_ids),
            "financial_years_created": success_count,
            "quarters_created": total_quarters_created,
            "financial_years_backfilled": backfill["financial_years_backfilled"],
            "quarters_backfilled": backfill["quarters_backfilled"],
            "failed_count": failed_count,
            "backfill_failed": backfill["backfill_failed"],
            "failed_clients": failed_clients if failed_clients else None,
        }

        logger.success(
            f"Financial year creation job completed: "
            f"{success_count} FY(s) created, "
            f"{backfill['financial_years_backfilled']} FY(s) backfilled, "
            f"{failed_count + backfill['backfill_failed']} failed"
        )

        return result
        
    except Exception as e:
        logger.exception(f"Financial year creation job failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Job failed: {str(e)}")

