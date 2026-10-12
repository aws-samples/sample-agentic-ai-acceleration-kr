"""
Health check routes
"""
from fastapi import APIRouter

router = APIRouter()


@router.get("/")
async def root():
    """Health check endpoint"""
    return {"status": "ok", "message": "API Server"}

