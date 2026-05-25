"""
API Router: Following management
"""

import asyncio
import os

from fastapi import APIRouter, HTTPException

from ..logger import logger
from ..providers.base import ProviderError

router = APIRouter(prefix="/api", tags=["following"])

# Set by main.py at startup
_chaturbate_api = None
_auth_service = None
_db = None
_provider_registry = None
_DEFAULT_RETENTION_DAYS = 30
_MAX_RETENTION_DAYS = 365


def init(chaturbate_api, auth_service, db, provider_registry=None):
    global _chaturbate_api, _auth_service, _db, _provider_registry
    _chaturbate_api = chaturbate_api
    _auth_service = auth_service
    _db = db
    _provider_registry = provider_registry


async def _get_default_retention_days() -> int:
    if not _db:
        return _DEFAULT_RETENTION_DAYS
    try:
        raw = await _db.get_setting("default_retention_days")
        retention_days = int(raw) if raw is not None else _DEFAULT_RETENTION_DAYS
        return max(0, min(_MAX_RETENTION_DAYS, retention_days))
    except (ValueError, TypeError):
        return _DEFAULT_RETENTION_DAYS


@router.get("/following")
async def get_following():
    """Returns all followed models (Chaturbate + CAM4 + autres plugins) avec
    statut online et flag isTracked."""
    try:
        # Login status par source (utilisé par le frontend pour afficher les
        # bons boutons "se connecter" / "synchroniser").
        per_source_logins: dict = {}
        any_logged_in = False
        if _auth_service:
            cb_status = _auth_service.get_status()
            per_source_logins["chaturbate"] = bool(cb_status.get("isLoggedIn"))
            any_logged_in = per_source_logins["chaturbate"] or any_logged_in

        # CAM4 status (lazy import pour éviter les cycles)
        try:
            from .. import main as app_main  # type: ignore
            cam4_svc = getattr(app_main, "cam4_auth_service", None)
            if cam4_svc is not None:
                per_source_logins["cam4"] = bool(cam4_svc.get_status().get("isLoggedIn"))
                any_logged_in = per_source_logins["cam4"] or any_logged_in
        except Exception:
            pass

        # Lire le cache local de tous les follows (toutes sources confondues)
        followed = []
        tracked_map = {}
        if _db:
            try:
                followed = await _db.get_all_followed()
                tracked_models = await _db.get_all_models()
                tracked_map = {m["username"]: m for m in tracked_models}
            except Exception as e:
                logger.warning("DB read failed in /api/following", error=str(e))
                followed = []
                tracked_map = {}

        for model in followed:
            tracked = tracked_map.get(model["username"])
            model["isTracked"] = tracked is not None
            model["is_recording"] = bool(tracked and tracked.get("is_recording"))
            # Surface cached room_status for UI (distinguer Private d'Offline)
            if tracked and tracked.get("room_status"):
                model["room_status"] = tracked.get("room_status")
            # Priorité: source_type sur la ligne followed > modèle tracké > chaturbate
            model["source_type"] = (
                model.get("source_type")
                or (tracked.get("source_type") if tracked else None)
                or "chaturbate"
            )

        online = [m for m in followed if m.get("is_online")]
        offline = [m for m in followed if not m.get("is_online")]

        return {
            "models": followed,
            "online": online,
            "offline": offline,
            "onlineCount": len(online),
            "offlineCount": len(offline),
            "isLoggedIn": any_logged_in,
            "perSource": per_source_logins,
            "message": None if any_logged_in else "Login required to view followed models",
        }
    except Exception as e:
        # Ne jamais renvoyer 500 sur cet endpoint : le front s'affiche mieux avec une
        # liste vide (qui sera re-peuplée au prochain fetch) qu'avec une erreur réseau
        logger.error("Erreur /api/following", error=str(e), exc_info=True)
        return {
            "models": [],
            "online": [],
            "offline": [],
            "onlineCount": 0,
            "offlineCount": 0,
            "isLoggedIn": False,
            "perSource": {},
            "message": "Temporary error, retrying...",
        }


@router.post("/following/sync")
async def sync_following():
    """
    Force re-sync followed models from every connected provider that supports
    remote follow sync. Kept on the legacy route for the Following page and
    older clients.
    """
    if not _db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    if _provider_registry:
        timeout = float(os.getenv("PSTREAMREC_FOLLOW_SYNC_TIMEOUT", "45") or "45")
        results = []
        total_synced = 0

        for provider in _provider_registry.all():
            caps = getattr(provider, "capabilities", None)
            if not caps or not getattr(caps, "can_sync_following", False):
                continue

            source_type = getattr(provider, "source_type", "") or ""
            display_name = getattr(provider, "display_name", source_type) or source_type
            try:
                items = await asyncio.wait_for(provider.sync_following(), timeout=timeout)
                synced = await _store_provider_following(source_type, items or [])
                total_synced += synced
                results.append({
                    "sourceType": source_type,
                    "displayName": display_name,
                    "synced": synced,
                    "status": "ok",
                })
            except asyncio.TimeoutError:
                logger.warning("Provider following sync timeout", source_type=source_type, timeout=timeout)
                results.append({
                    "sourceType": source_type,
                    "displayName": display_name,
                    "synced": 0,
                    "status": "timeout",
                    "detail": f"Sync timed out after {int(timeout)}s",
                })
            except ProviderError as exc:
                results.append({
                    "sourceType": source_type,
                    "displayName": display_name,
                    "synced": 0,
                    "status": "error",
                    "detail": str(exc),
                })
            except Exception as exc:
                logger.error("Provider following sync failed", source_type=source_type, error=str(exc), exc_info=True)
                results.append({
                    "sourceType": source_type,
                    "displayName": display_name,
                    "synced": 0,
                    "status": "error",
                    "detail": str(exc),
                })

        await _db.reconcile_model_sources_from_followed()
        ok_count = sum(1 for item in results if item.get("status") == "ok")
        return {
            "synced": total_synced,
            "results": results,
            "message": f"Synced {total_synced} followed models from {ok_count} provider(s)",
        }

    if not _auth_service or not _chaturbate_api:
        raise HTTPException(status_code=503, detail="Services not initialized")

    status = _auth_service.get_status()
    if not status.get("isLoggedIn"):
        raise HTTPException(status_code=401, detail="Login required")

    # Fetch from Chaturbate
    models = await _chaturbate_api.get_followed_models()

    if not models:
        # Diagnose the reason so users know what to fix
        cookies = _auth_service.api.auth.get_cookies() if hasattr(_auth_service, 'api') else {}
        if not _chaturbate_api.auth.get_cookies().get("sessionid"):
            reason = "Chaturbate session expired — please log in again from Settings"
        else:
            reason = "Chaturbate returned 0 followed models (session may be invalid or rate-limited)"
        return {"synced": 0, "message": reason}

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
            source_type="chaturbate",
            room_status=model.get("room_status"),
        )
        synced_usernames.add(model["username"])

    # Remove models no longer followed
    await _db.remove_unfollowed(synced_usernames)

    return {"synced": len(models), "message": f"Synced {len(models)} followed models"}


async def _store_provider_following(source_type: str, items: list[dict]) -> int:
    synced_usernames = set()
    for item in items:
        username = item.get("username")
        if not username:
            continue
        thumbnail = item.get("thumbnail_url") or item.get("thumbnail")
        is_online = bool(item.get("is_online", item.get("isOnline", False)))
        if source_type == "chaturbate" and not is_online and thumbnail and "roomimg.stream.highwebmedia.com" in thumbnail:
            thumbnail = None
        await _db.upsert_followed_model(
            username=username,
            display_name=item.get("display_name") or username,
            is_online=is_online,
            viewers=int(item.get("viewers") or 0),
            thumbnail_url=thumbnail,
            source_type=source_type,
            room_status=item.get("room_status") or item.get("roomStatus"),
        )
        synced_usernames.add(username)

    if source_type == "chaturbate":
        await _db.remove_unfollowed(synced_usernames, source_type=source_type)
    return len(synced_usernames)


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

    followed = await _db.get_followed_model(username)
    source_type = (followed or {}).get("source_type") or "chaturbate"

    # Add to models table
    await _db.add_or_update_model(
        username=username,
        auto_record=True,
        record_quality="best",
        retention_days=await _get_default_retention_days(),
        source_type=source_type,
    )

    return {"message": f"{username} added to tracking", "tracked": True}
