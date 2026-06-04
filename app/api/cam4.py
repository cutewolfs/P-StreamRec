"""
API Router: CAM4 authentication and favorites.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..logger import logger

router = APIRouter(prefix="/api/cam4", tags=["cam4"])

_auth_service = None
_db = None


def init(auth_service, db):
    global _auth_service, _db
    _auth_service = auth_service
    _db = db


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def cam4_login(req: LoginRequest):
    if _auth_service is None:
        raise HTTPException(status_code=503, detail="CAM4 auth service non initialisé")
    result = await _auth_service.login(req.username, req.password)
    if not result.get("success"):
        raise HTTPException(status_code=401, detail=result.get("error", "Login failed"))
    return result


@router.get("/status")
async def cam4_status():
    if _auth_service is None:
        return {"isLoggedIn": False, "username": None, "lastError": "Not initialized"}
    return _auth_service.get_status()


@router.post("/logout")
async def cam4_logout():
    if _auth_service is None:
        raise HTTPException(status_code=503, detail="CAM4 auth service non initialisé")
    await _auth_service.logout()
    return {"success": True}


def _require_auth():
    if _auth_service is None:
        raise HTTPException(status_code=503, detail="CAM4 auth service non initialisé")
    status = _auth_service.get_status()
    if not status.get("isLoggedIn"):
        raise HTTPException(status_code=401, detail="CAM4 session absente — connectez-vous dans Settings")
    username = status.get("username")
    if not username:
        raise HTTPException(status_code=401, detail="Username CAM4 introuvable")
    return username, _auth_service.get_cookies()


@router.get("/is-following/{username}")
async def cam4_is_following(username: str):
    auth_user, cookies = _require_auth()
    from ..services import cam4_source

    try:
        is_favorite = await cam4_source.is_favorite(auth_user, username, cookies)
    except Exception as e:
        logger.error("CAM4 is-following failed", username=username, error=str(e))
        raise HTTPException(status_code=502, detail=f"CAM4 check failed: {e}")
    return {"isFollowing": bool(is_favorite), "localOnly": False}


@router.post("/follow/{username}")
async def cam4_follow(username: str):
    auth_user, cookies = _require_auth()
    from ..services import cam4_source
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")
    result = await cam4_source.follow(auth_user, username, cookies)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "CAM4 follow failed"))
    try:
        status = await cam4_source.check_status(username)
    except Exception as e:
        logger.debug("CAM4 follow status check failed", username=username, error=str(e))
        status = {}
    thumb = status.get("thumbnail")
    await _db.upsert_followed_model(
        username=username,
        display_name=username,
        is_online=bool(status.get("is_online")),
        viewers=int(status.get("viewers") or 0),
        thumbnail_url=thumb,
        source_type="cam4",
        room_status=status.get("room_status"),
    )
    await _db.reconcile_model_sources_from_followed()
    return {"success": True, "localOnly": False, "action": "follow"}


@router.post("/unfollow/{username}")
async def cam4_unfollow(username: str):
    auth_user, cookies = _require_auth()
    from ..services import cam4_source
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")
    result = await cam4_source.unfollow(auth_user, username, cookies)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "CAM4 unfollow failed"))
    await _db.delete_followed_model(username, source_type="cam4")
    return {"success": True, "localOnly": False, "action": "unfollow"}


@router.post("/following/sync")
async def cam4_sync_following():
    """Synchronise les favoris CAM4 distants dans le cache local."""
    if _auth_service is None or _db is None:
        raise HTTPException(status_code=503, detail="CAM4 auth/database non initialisé")
    status = _auth_service.get_status()
    if not status.get("isLoggedIn"):
        raise HTTPException(status_code=401, detail="CAM4 session absente")

    from ..services import cam4_source

    try:
        items = await cam4_source.list_followed(_auth_service.get_cookies())
    except Exception as e:
        logger.error("CAM4 following sync failed", error=str(e))
        raise HTTPException(status_code=502, detail=f"CAM4 sync failed: {e}")

    synced = set()
    for item in items:
        username = item.get("username")
        if not username:
            continue
        await _db.upsert_followed_model(
            username=username,
            display_name=item.get("display_name") or username,
            is_online=bool(item.get("is_online", False)),
            viewers=int(item.get("viewers") or 0),
            thumbnail_url=item.get("thumbnail"),
            source_type="cam4",
            room_status=item.get("room_status"),
        )
        synced.add(username)

    await _db.reconcile_model_sources_from_followed()
    await _db.remove_unfollowed(synced, source_type="cam4")
    return {
        "synced": len(synced),
        "localOnly": False,
        "message": f"CAM4: {len(synced)} favoris synchronisés",
    }
