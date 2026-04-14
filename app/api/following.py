"""
API Router: Following management
"""

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api", tags=["following"])

# Set by main.py at startup
_chaturbate_api = None
_auth_service = None
_db = None


def init(chaturbate_api, auth_service, db):
    global _chaturbate_api, _auth_service, _db
    _chaturbate_api = chaturbate_api
    _auth_service = auth_service
    _db = db


@router.get("/following")
async def get_following():
    """
    Get followed models with online status and isTracked flag.
    Requires Chaturbate login.
    """
    if not _auth_service:
        raise HTTPException(status_code=503, detail="Auth service not initialized")

    status = _auth_service.get_status()
    if not status.get("isLoggedIn"):
        return {
            "models": [],
            "isLoggedIn": False,
            "message": "Login required to view followed models"
        }

    # Get from local DB cache first
    followed = await _db.get_all_followed()

    # Add isTracked and is_recording flags from models table
    tracked_models = await _db.get_all_models()
    tracked_map = {m["username"]: m for m in tracked_models}

    for model in followed:
        tracked = tracked_map.get(model["username"])
        model["isTracked"] = tracked is not None
        model["is_recording"] = bool(tracked and tracked.get("is_recording"))

    # Split into online/offline
    online = [m for m in followed if m.get("is_online")]
    offline = [m for m in followed if not m.get("is_online")]

    return {
        "models": followed,
        "online": online,
        "offline": offline,
        "onlineCount": len(online),
        "offlineCount": len(offline),
        "isLoggedIn": True,
    }


@router.post("/following/sync")
async def sync_following():
    """
    Force re-sync followed models from Chaturbate.
    """
    if not _auth_service or not _chaturbate_api:
        raise HTTPException(status_code=503, detail="Services not initialized")

    status = _auth_service.get_status()
    if not status.get("isLoggedIn"):
        raise HTTPException(status_code=401, detail="Login required")

    # Fetch from Chaturbate
    models = await _chaturbate_api.get_followed_models()

    if not models:
        return {"synced": 0, "message": "No models found or fetch failed"}

    # Upsert all models (preserves old thumbnail_url via COALESCE when new value is None)
    synced_usernames = set()
    for model in models:
        thumb = model.get("thumbnail_url")
        is_online = model.get("is_online", False)
        # For offline models with only a fallback URL, pass None so COALESCE keeps the old thumbnail
        if not is_online and thumb and "roomimg.stream.highwebmedia.com" in thumb:
            thumb = None
        await _db.upsert_followed_model(
            username=model["username"],
            display_name=model.get("display_name"),
            is_online=is_online,
            viewers=model.get("viewers", 0),
            thumbnail_url=thumb,
        )
        synced_usernames.add(model["username"])

    # Remove models no longer followed
    await _db.remove_unfollowed(synced_usernames)

    return {"synced": len(models), "message": f"Synced {len(models)} followed models"}


@router.post("/following/{username}/track")
async def track_followed_model(username: str):
    """
    Add a followed model to P-StreamRec models table for recording.
    """
    if not _db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    # Check if already tracked
    existing = await _db.get_model(username)
    if existing:
        return {"message": f"{username} is already tracked", "alreadyTracked": True}

    # Add to models table
    await _db.add_or_update_model(
        username=username,
        auto_record=True,
        record_quality="best",
        retention_days=30
    )

    return {"message": f"{username} added to tracking", "tracked": True}
