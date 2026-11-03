"""Public non-secret capabilities, so disabled UI does not present dead account forms."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.config import get_settings

router = APIRouter(prefix="/api/v1", tags=["features"])


@router.get("/features")
async def features():
    return JSONResponse({"tactics_enabled": get_settings().tactics_enabled},
                        headers={"Cache-Control": "no-store"})
