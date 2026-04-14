"""
API Router: Chaturbate Authentication
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/api/chaturbate", tags=["chaturbate-auth"])

# These will be set by main.py at startup
_auth_service = None
_flaresolverr = None


def init(auth_service, flaresolverr=None):
    global _auth_service, _flaresolverr
    _auth_service = auth_service
    _flaresolverr = flaresolverr


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def chaturbate_login(req: LoginRequest):
    """Login to Chaturbate"""
    if not _auth_service:
        raise HTTPException(status_code=503, detail="Auth service not initialized")

    result = await _auth_service.login(req.username, req.password)
    if not result.get("success"):
        raise HTTPException(status_code=401, detail=result.get("error", "Login failed"))

    return result


@router.get("/status")
async def chaturbate_status():
    """Get Chaturbate auth status"""
    status = {}
    if _auth_service:
        status = _auth_service.get_status()

    flaresolverr_available = False
    if _flaresolverr:
        flaresolverr_available = await _flaresolverr.is_available()

    status["flaresolverrAvailable"] = flaresolverr_available
    status["flaresolverrUrl"] = _flaresolverr.base_url if _flaresolverr else None
    return status


@router.post("/logout")
async def chaturbate_logout():
    """Clear Chaturbate session"""
    if not _auth_service:
        raise HTTPException(status_code=503, detail="Auth service not initialized")

    await _auth_service.logout()
    return {"success": True}


@router.post("/refresh")
async def chaturbate_refresh():
    """Force re-login using saved credentials"""
    if not _auth_service:
        raise HTTPException(status_code=503, detail="Auth service not initialized")

    # Get saved credentials from DB
    auth_state = await _auth_service.db.get_auth_state()
    if not auth_state or not auth_state.get("username"):
        raise HTTPException(status_code=400, detail="No saved credentials")

    # We can't re-login without the password (only hash is stored)
    raise HTTPException(
        status_code=400,
        detail="Please login again with your credentials"
    )
