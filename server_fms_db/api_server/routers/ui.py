"""Small browser UI for manually exercising the AI endpoints."""
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["AI Test UI"])
_STATIC = Path(__file__).resolve().parent.parent / "static"

@router.get("/ai/test", include_in_schema=False)
async def ai_test_page() -> FileResponse:
    return FileResponse(_STATIC / "ai_test.html")

@router.get("/production/monitor", include_in_schema=False)
async def production_monitor_page() -> FileResponse:
    return FileResponse(_STATIC / "production_monitor.html")
