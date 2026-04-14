"""
API Router: Discover live models
"""

from fastapi import APIRouter, Query
from typing import Optional, List

router = APIRouter(prefix="/api", tags=["discover"])

# Set by main.py at startup
_chaturbate_api = None
_db = None


def init(chaturbate_api, db):
    global _chaturbate_api, _db
    _chaturbate_api = chaturbate_api
    _db = db


@router.get("/discover")
async def discover_models(
    page: int = Query(1, ge=1),
    limit: int = Query(24, ge=1, le=100),
    gender: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    tags: Optional[str] = Query(None),
):
    """
    Get live models from Chaturbate.
    Works without login (unauthenticated scraping), enhanced with login.
    Supports tag filtering (comma-separated) and blacklist from settings.
    """
    if not _chaturbate_api:
        return {
            "models": [],
            "total": 0,
            "page": page,
            "limit": limit,
            "total_pages": 1,
        }

    result = await _chaturbate_api.get_live_models(
        page=page,
        limit=limit,
        gender=gender or "",
        search=search or ""
    )

    # Parse included tags filter
    included_tags = []
    if tags:
        included_tags = [t.strip().lower() for t in tags.split(",") if t.strip()]

    # Get blacklisted tags from settings
    blacklisted_tags = []
    if _db:
        blacklisted_tags = await _db.get_blacklisted_tags()

    # Filter models by tags
    models = result.get("models", [])
    if included_tags or blacklisted_tags:
        filtered = []
        for model in models:
            model_tags = [t.lower() for t in model.get("tags", [])]

            # Check blacklist: skip if any blacklisted tag is present
            if blacklisted_tags and any(bt in model_tags for bt in blacklisted_tags):
                continue

            # Check included tags: keep only if all included tags are present
            if included_tags and not all(it in model_tags for it in included_tags):
                continue

            filtered.append(model)
        models = filtered
        result["models"] = models
        result["total"] = len(models)

    # Add isFollowed and isTracked flags
    if _db:
        tracked_models = await _db.get_all_models()
        tracked_set = {m["username"] for m in tracked_models}

        followed_models = await _db.get_all_followed()
        followed_set = {m["username"] for m in followed_models}

        for model in result.get("models", []):
            model["isTracked"] = model["username"] in tracked_set
            model["isFollowed"] = model["username"] in followed_set

    return result
