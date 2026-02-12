from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nucleus.models.common_models.client import Client
from nucleus.models.advance_tax_models.financial_year import FinancialYear
from app.core.logger import logger
from sqlalchemy.exc import SQLAlchemyError
from fastapi import HTTPException


async def get_client_ids_without_financial_years(db: AsyncSession):
    """
    Get client IDs that don't have any financial year records
    Uses NOT IN - simple and doesn't require relationships
    """
    try:
        # Simple query: Get all client IDs NOT IN financial_years table
        stmt = select(Client.id).where(
            Client.id.not_in(
                select(FinancialYear.client_id)
            )
        )
        
        result = await db.execute(stmt)
        client_ids = result.scalars().all() 
        
        logger.info(f"Found {len(client_ids)} client IDs without financial years")
        return client_ids
    
    except SQLAlchemyError as e:
        logger.error(f"Database error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    
    except Exception as e:
        logger.exception(f"Unexpected error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")
