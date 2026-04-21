"""
API Router: Discover live models.

Agrège Chaturbate + CAM4 (deux sources intégrées). Le premier tag est envoyé
aux API natives (filtrage côté source), les tags supplémentaires et la
blacklist sont appliqués localement. L'interleave round-robin empêche un
catalogue plus gros (Chaturbate ~7000) d'écraser le plus petit (CAM4 ~60).
"""

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query

from ..logger import logger
from ..services import cam4_source

router = APIRouter(prefix="/api", tags=["discover"])

_chaturbate_api = None
_db = None


def init(chaturbate_api, db):
    global _chaturbate_api, _db
    _chaturbate_api = chaturbate_api
    _db = db


async def _fetch_chaturbate(
    page: int,
    limit: int,
    gender: Optional[str],
    search: Optional[str],
    first_tag: str,
) -> Optional[Dict[str, Any]]:
    if _chaturbate_api is None:
        return None
    try:
        return await _chaturbate_api.get_live_models(
            page=page,
            limit=limit,
            gender=gender or "",
            search=search or "",
            tag=first_tag,
        )
    except Exception as e:
        logger.warning("Discover Chaturbate échec", error=str(e))
        return None


async def _fetch_cam4(
    page: int,
    limit: int,
    gender: Optional[str],
    search: Optional[str],
    tags: Optional[List[str]],
) -> Optional[Dict[str, Any]]:
    try:
        return await cam4_source.list_live_models(
            page=page,
            limit=limit,
            gender=gender,
            search=search,
            tags=tags,
        )
    except Exception as e:
        logger.warning("Discover CAM4 échec", error=str(e))
        return None


@router.get("/discover")
async def discover_models(
    page: int = Query(1, ge=1),
    limit: int = Query(24, ge=1, le=100),
    gender: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    tags: Optional[str] = Query(None),
):
    """Liste agrégée Chaturbate + CAM4 (rooms publiques uniquement)."""
    included_tags: List[str] = []
    if tags:
        included_tags = [t.strip().lower() for t in tags.split(",") if t.strip()]
    first_tag = included_tags[0] if included_tags else ""
    extra_tags = included_tags[1:] if len(included_tags) > 1 else []

    n_sources = 2  # chaturbate + cam4
    per_source_limit = max(1, (limit + n_sources - 1) // n_sources)

    cb_result, cam4_result = await asyncio.gather(
        _fetch_chaturbate(page, per_source_limit, gender, search, first_tag),
        _fetch_cam4(page, per_source_limit, gender, search, included_tags or None),
    )

    blacklisted_tags: List[str] = []
    if _db:
        try:
            blacklisted_tags = await _db.get_blacklisted_tags()
        except Exception:
            blacklisted_tags = []
    blacklisted_set = {t.lower() for t in blacklisted_tags}

    def _filter_list(result: Optional[Dict[str, Any]], source: str) -> List[Dict[str, Any]]:
        if result is None:
            return []
        out: List[Dict[str, Any]] = []
        for item in result.get("models", []):
            # Room status (public par défaut pour Chaturbate qui renseigne
            # current_show). On n'exclut QUE les rooms non publiques.
            rs = (item.get("room_status") or "public").lower()
            if rs != "public" or not item.get("is_online", True):
                continue
            item_tags_lower = [t.lower() for t in (item.get("tags") or [])]
            if blacklisted_set and any(bt in item_tags_lower for bt in blacklisted_set):
                continue
            if extra_tags and not all(t in item_tags_lower for t in extra_tags):
                continue
            item = dict(item)
            item["source_type"] = source
            out.append(item)
        return out

    cb_items = _filter_list(cb_result, "chaturbate")
    cam4_items = _filter_list(cam4_result, "cam4")

    plugin_totals = []
    plugin_total_pages = []
    for r in (cb_result, cam4_result):
        if r is None:
            continue
        plugin_totals.append(int(r.get("total") or 0))
        plugin_total_pages.append(int(r.get("total_pages") or 1))

    # Interleave round-robin pour mélanger les deux sources.
    combined: List[Dict[str, Any]] = []
    iters = [iter(cam4_items), iter(cb_items)]
    while len(combined) < limit and any(iters):
        next_iters = []
        for it in iters:
            try:
                combined.append(next(it))
                if len(combined) >= limit:
                    break
                next_iters.append(it)
            except StopIteration:
                continue
        iters = next_iters

    total_combined = sum(plugin_totals)
    total_pages = max(plugin_total_pages) if plugin_total_pages else 1

    if _db:
        try:
            tracked_models = await _db.get_all_models()
            tracked_set = {m["username"] for m in tracked_models}
            followed_models = await _db.get_all_followed()
            followed_set = {m["username"] for m in followed_models}
            for model in combined:
                model["isTracked"] = model["username"] in tracked_set
                model["isFollowed"] = model["username"] in followed_set
        except Exception:
            pass

    return {
        "models": combined,
        "total": total_combined,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
    }
