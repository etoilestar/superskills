from fastapi import APIRouter, Query

from ..config import settings
from ..services.llm_proxy import check_connection

router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("")
async def health_check():
    return {"status": "ok"}


@router.get("/llm")
async def llm_health(deep: bool = Query(False, description="Force a synchronous diagnostic refresh.")):
    result = await check_connection(deep=deep)
    return {"llm_url": settings.llm_base_url, **result}
