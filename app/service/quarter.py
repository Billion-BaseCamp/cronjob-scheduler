"""
Quarter Service
Handles quarter creation and management
"""
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import date
from uuid import UUID
from typing import List, Dict
from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from app.models.quarter import Quarter
from app.core.logger import logger


def get_quarter_dates(fy_start_year: int) -> List[Dict]:
    """
    Get quarter date ranges for a financial year
    
    Args:
        fy_start_year: Year when FY starts (e.g., 2025)
        
    Returns:
        List of quarter dictionaries with dates
        
    Example:
        Q1: April 1 - June 30
        Q2: July 1 - September 30
        Q3: October 1 - December 31
        Q4: January 1 - March 31
    """
    quarters = [
        {
            "quarter_number": 1,
            "start_date": date(fy_start_year, 4, 1),
            "end_date": date(fy_start_year, 6, 30)
        },
        {
            "quarter_number": 2,
            "start_date": date(fy_start_year, 7, 1),
            "end_date": date(fy_start_year, 9, 30)
        },
        {
            "quarter_number": 3,
            "start_date": date(fy_start_year, 10, 1),
            "end_date": date(fy_start_year, 12, 31)
        },
        {
            "quarter_number": 4,
            "start_date": date(fy_start_year + 1, 1, 1),
            "end_date": date(fy_start_year + 1, 3, 31)
        }
    ]
    
    return quarters


def determine_quarter_status(
    quarter_start_date: date, 
    quarter_end_date: date,
    today: date = None
) -> tuple[bool, str]:
    """
    Determine quarter status based on current date
    
    Args:
        quarter_start_date: Start date of the quarter
        quarter_end_date: End date of the quarter
        today: Current date (defaults to today)
        
    Returns:
        Tuple of (is_locked, status)
        
    Status Logic:
        - completed: Quarter has ended (today > end_date)
        - active: Currently in this quarter (start_date <= today <= end_date)
        - inactive: Quarter hasn't started yet (today < start_date)
    """
    if today is None:
        today = date.today()
    
    # Quarter has ended
    if today > quarter_end_date:
        return False, "completed"  # Not locked, completed
    
    # Currently in this quarter
    elif today >= quarter_start_date:
        return False, "active"  # Not locked, active
    
    # Quarter hasn't started yet
    else:
        return True, "inactive"  # Locked, inactive


async def create_quarters_for_financial_year(
    financial_year_id: UUID,
    fy_start_year: int,
    db: AsyncSession
) -> List[Quarter]:
    """
    Create 4 quarters for a financial year
    
    Args:
        financial_year_id: Financial year UUID
        fy_start_year: Year when FY starts (e.g., 2025)
        db: Database session
        
    Returns:
        List of created Quarter objects
    """
    try:
        today = date.today()
        quarter_dates = get_quarter_dates(fy_start_year)
        created_quarters = []
        
        for q_data in quarter_dates:
            # Determine quarter status (completed/active/inactive)
            is_locked, status = determine_quarter_status(
                q_data["start_date"], 
                q_data["end_date"],
                today
            )
            
            quarter = Quarter(
                financial_year_id=financial_year_id,
                quarter_number=q_data["quarter_number"],
                start_date=q_data["start_date"],
                end_date=q_data["end_date"],
                is_locked=is_locked,
                status=status
            )
            
            db.add(quarter)
            created_quarters.append(quarter)
            
            logger.debug(
                f"Created Q{q_data['quarter_number']}: "
                f"{q_data['start_date']} to {q_data['end_date']} - "
                f"Status: {status}"
            )
        
        logger.info(f"Created {len(created_quarters)} quarters for FY {financial_year_id}")
        return created_quarters
        
    except SQLAlchemyError as e:
        logger.error(f"Database error creating quarters: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    
    except Exception as e:
        logger.exception(f"Unexpected error creating quarters: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")

