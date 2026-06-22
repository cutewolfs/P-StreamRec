"""
API Router: Following management
"""

import json
import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ..following_sync import store_provider_following
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
_PRIVATE_ROOM_STATUSES = {
    "private",
    "group",
    "password_protected",
    "password protected",
    "hidden",
    "true_private",
    "private_spy",
}


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


def _provider_source_type(provider) -> str:
    return str(getattr(provider, "source_type", "") or "").strip().lower()


def _provider_display_name(provider, source_type: str) -> str:
    return getattr(provider, "display_name", None) or source_type or "Unknown"


def _registered_source_types() -> Optional[set[str]]:
    if not _provider_registry:
        return None
    return {
        source_type
        for provider in _provider_registry.all()
        for source_type in [_provider_source_type(provider)]
        if source_type
    }


async def _provider_login_summary(provider) -> dict:
    source_type = _provider_source_type(provider)
    summary = {
        "isLoggedIn": False,
        "username": None,
        "lastError": None,
        "hasCookies": False,
        "hasLocalStorage": False,
        "hasSession": False,
        "hasSavedSessionData": False,
        "hasSavedCredentials": False,
        "credentialsUpdatedAt": None,
    }

    caps = getattr(provider, "capabilities", None)
    if not getattr(caps, "can_login", False):
        summary["accountDisabled"] = True
        return summary

    auth = getattr(provider, "auth", None)
    if auth is not None and hasattr(auth, "get_status"):
        try:
            status = auth.get_status() or {}
            summary.update({
                "isLoggedIn": bool(status.get("isLoggedIn")),
                "username": status.get("username"),
                "lastError": status.get("lastError"),
                "hasCookies": bool(status.get("hasCookies") or status.get("isLoggedIn")),
            })
            summary["hasSession"] = bool(summary["isLoggedIn"])
            summary["hasSavedSessionData"] = bool(summary["hasCookies"])
        except Exception as exc:
            logger.debug("Provider auth status unavailable", source_type=source_type, error=str(exc))

    if _db and source_type:
        try:
            row = await _db.get_provider_session(source_type)
        except Exception as exc:
            logger.debug("Provider session status unavailable", source_type=source_type, error=str(exc))
            row = None
        if row:
            has_cookies = _stored_json_has_items(row.get("session_cookies"))
            has_local_storage = _stored_json_has_items(row.get("local_storage"))
            has_saved_session_data = bool(summary.get("hasCookies") or has_cookies or has_local_storage)
            summary["isLoggedIn"] = bool(summary["isLoggedIn"] or (row.get("is_logged_in") and has_saved_session_data))
            summary["username"] = summary["username"] or row.get("username") or row.get("credential_username")
            summary["lastError"] = summary["lastError"] or row.get("last_error")
            summary["hasCookies"] = bool(summary["hasCookies"] or has_cookies)
            summary["hasLocalStorage"] = has_local_storage
            summary["hasSavedSessionData"] = has_saved_session_data
            summary["hasSession"] = bool(summary["isLoggedIn"])
            summary["hasSavedCredentials"] = bool(
                row.get("credential_username") and row.get("credential_password")
            )
            summary["credentialsUpdatedAt"] = row.get("credentials_updated_at")

    return summary


def _stored_json_has_items(raw_value: object) -> bool:
    if not raw_value:
        return False
    try:
        parsed = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
    except Exception:
        return False
    if isinstance(parsed, list):
        return any(bool(item) for item in parsed)
    if isinstance(parsed, dict):
        return bool(parsed)
    return False


def _provider_capabilities(provider) -> dict:
    caps = getattr(provider, "capabilities", None)
    if not caps:
        return {}
    return {
        "can_login": bool(getattr(caps, "can_login", False)),
        "can_follow": bool(getattr(caps, "can_follow", False)),
        "can_sync_following": bool(getattr(caps, "can_sync_following", False)),
        "can_discover": bool(getattr(caps, "can_discover", False)),
        "can_stream": bool(getattr(caps, "can_stream", True)),
        "can_record": bool(getattr(caps, "can_record", True)),
        "uses_browser": bool(getattr(caps, "uses_browser", False)),
        "uses_ytdlp": bool(getattr(caps, "uses_ytdlp", False)),
    }


def _source_type_for_model(model: dict) -> str:
    return str(model.get("source_type") or model.get("platform") or "chaturbate").strip().lower()


def _is_private_model(model: dict) -> bool:
    return (model.get("room_status") or model.get("roomStatus") or "").lower() in _PRIVATE_ROOM_STATUSES


def _model_viewers(model: dict) -> int:
    if not bool(model.get("is_online")) or _is_private_model(model):
        return 0
    try:
        return int(model.get("viewers") or 0)
    except (TypeError, ValueError):
        return 0


def _model_status_rank(model: dict) -> int:
    if bool(model.get("is_online")) and not _is_private_model(model):
        return 0
    if _is_private_model(model):
        return 1
    return 2


def _sort_following_models(models: list[dict]) -> list[dict]:
    return sorted(
        models,
        key=lambda model: (
            -_model_viewers(model),
            _model_status_rank(model),
            _source_type_for_model(model),
            str(model.get("username") or model.get("name") or "").lower(),
        ),
    )


def _provider_counts(models: list[dict]) -> dict:
    online = [m for m in models if bool(m.get("is_online")) or _is_private_model(m)]
    return {
        "totalCount": len(models),
        "onlineCount": len(online),
        "offlineCount": len(models) - len(online),
    }


async def _build_provider_summaries(followed: list[dict], any_logged_in: bool) -> tuple[list[dict], dict, bool]:
    by_source: dict[str, list[dict]] = {}
    for model in followed:
        by_source.setdefault(_source_type_for_model(model), []).append(model)

    providers = []
    per_source_logins = {}
    seen_sources = set()

    if _provider_registry:
        for provider in _provider_registry.all():
            source_type = _provider_source_type(provider)
            if not source_type:
                continue
            status = await _provider_login_summary(provider)
            caps = _provider_capabilities(provider)
            models = by_source.get(source_type, [])
            seen_sources.add(source_type)
            per_source_logins[source_type] = bool(status.get("isLoggedIn"))
            any_logged_in = any_logged_in or bool(status.get("isLoggedIn"))
            providers.append({
                "sourceType": source_type,
                "displayName": _provider_display_name(provider, source_type),
                "capabilities": caps,
                "status": status,
                **_provider_counts(models),
            })

    if not _provider_registry:
        for source_type, models in sorted(by_source.items()):
            if source_type in seen_sources:
                continue
            per_source_logins[source_type] = per_source_logins.get(source_type, False)
            providers.append({
                "sourceType": source_type,
                "displayName": source_type.capitalize(),
                "capabilities": {},
                "status": {
                    "isLoggedIn": False,
                    "username": None,
                    "lastError": None,
                    "hasCookies": False,
                    "hasSavedCredentials": False,
                    "credentialsUpdatedAt": None,
                },
                **_provider_counts(models),
            })

    return providers, per_source_logins, any_logged_in


@router.get("/following")
async def get_following():
    """Returns all followed models (Chaturbate + CAM4 + autres plugins) avec
    statut online et flag isTracked."""
    try:
        # Login status par source (fallback legacy si le registre provider
        # n'est pas encore initialisé).
        per_source_logins: dict = {}
        any_logged_in = False
        if _auth_service and not _provider_registry:
            cb_status = _auth_service.get_status()
            per_source_logins["chaturbate"] = bool(cb_status.get("isLoggedIn"))
            any_logged_in = per_source_logins["chaturbate"] or any_logged_in

        # CAM4 status (lazy import pour éviter les cycles)
        if not _provider_registry:
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
                registered_sources = _registered_source_types()
                followed = await _db.get_all_followed()
                tracked_models = await _db.get_all_models()
                if registered_sources is not None:
                    followed = [
                        item for item in followed
                        if _source_type_for_model(item) in registered_sources
                    ]
                    tracked_models = [
                        item for item in tracked_models
                        if _source_type_for_model(item) in registered_sources
                    ]
                tracked_map = {
                    (
                        m["username"],
                        _source_type_for_model(m),
                    ): m
                    for m in tracked_models
                }
            except Exception as e:
                logger.warning("DB read failed in /api/following", error=str(e))
                followed = []
                tracked_map = {}

        for model in followed:
            model_source = _source_type_for_model(model)
            tracked = tracked_map.get((model["username"], model_source))
            if not tracked and model_source == "chaturbate":
                tracked = tracked_map.get((model["username"], ""))
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

        followed = _sort_following_models(followed)
        providers, registry_logins, any_logged_in = await _build_provider_summaries(followed, any_logged_in)
        per_source_logins.update(registry_logins)
        by_provider = {}
        for model in followed:
            by_provider.setdefault(_source_type_for_model(model), []).append(model)

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
            "providers": providers,
            "byProvider": by_provider,
            "message": None,
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
    Sync all providers that expose a verified remote following API.
    Providers without remote sync keep their local follows untouched.
    """
    if not _db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    if _provider_registry:
        results = []
        total_synced = 0
        for provider in _provider_registry.all():
            caps = getattr(provider, "capabilities", None)
            if not getattr(caps, "can_sync_following", False):
                continue
            source_type = _provider_source_type(provider)
            display_name = _provider_display_name(provider, source_type)
            try:
                items = await asyncio.wait_for(provider.sync_following(), timeout=60)
                stored = await _store_provider_following(source_type, items)
                synced = stored["synced"]
                total_synced += synced
                results.append({
                    "sourceType": source_type,
                    "displayName": display_name,
                    "synced": synced,
                    "trusted": stored["trusted"],
                    "skippedReason": stored["skippedReason"],
                    "success": True,
                })
            except asyncio.TimeoutError:
                logger.warning("Provider following sync timeout", source_type=source_type)
                results.append({
                    "sourceType": source_type,
                    "displayName": display_name,
                    "synced": 0,
                    "success": False,
                    "error": "Sync timeout",
                })
            except ProviderError as exc:
                logger.warning("Provider following sync failed", source_type=source_type, error=str(exc))
                results.append({
                    "sourceType": source_type,
                    "displayName": display_name,
                    "synced": 0,
                    "success": False,
                    "error": str(exc),
                })
            except Exception as exc:
                logger.error("Provider following sync error", source_type=source_type, error=str(exc), exc_info=True)
                results.append({
                    "sourceType": source_type,
                    "displayName": display_name,
                    "synced": 0,
                    "success": False,
                    "error": str(exc),
                })
        await _db.reconcile_model_sources_from_followed()
        if not results:
            return {
                "synced": 0,
                "results": [],
                "localOnly": True,
                "message": "No provider exposes remote following sync; local follows only",
            }
        return {
            "synced": total_synced,
            "results": results,
            "localOnly": False,
            "message": f"Synced {total_synced} remote follows",
        }

    if not _chaturbate_api or not _auth_service:
        raise HTTPException(status_code=503, detail="Chaturbate API not initialized")
    status = _auth_service.get_status()
    if not status.get("isLoggedIn"):
        raise HTTPException(status_code=401, detail="Chaturbate session absente")
    try:
        items = await _chaturbate_api.get_followed_models()
    except Exception as exc:
        logger.error("Chaturbate following sync failed", error=str(exc), exc_info=True)
        raise HTTPException(status_code=502, detail=f"Chaturbate sync failed: {exc}")
    stored = await _store_provider_following("chaturbate", items)
    synced = stored["synced"]
    await _db.reconcile_model_sources_from_followed()
    return {
        "synced": synced,
        "trusted": stored["trusted"],
        "skippedReason": stored["skippedReason"],
        "results": [{
            "sourceType": "chaturbate",
            "displayName": "Chaturbate",
            "synced": synced,
            "trusted": stored["trusted"],
            "skippedReason": stored["skippedReason"],
            "success": True,
        }],
        "localOnly": False,
        "message": f"Chaturbate: {synced} follows synced",
    }


async def _store_provider_following(source_type: str, items: list[dict]) -> dict:
    return await store_provider_following(_db, source_type, items)


@router.post("/following/{username}/track")
async def track_followed_model(
    username: str,
    source_type: Optional[str] = Query(None, alias="source_type"),
    source: Optional[str] = Query(None),
):
    """
    Add a followed model to P-StreamRec models table for recording.
    """
    if not _db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    requested_source = (source_type or source or "").strip().lower() or None
    if requested_source and _provider_registry and requested_source not in (_registered_source_types() or set()):
        raise HTTPException(status_code=404, detail=f"Source '{requested_source}' non disponible")

    # Check if already tracked
    existing = await _db.get_model(username, source_type=requested_source)
    if existing:
        return {"message": f"{username} is already tracked", "alreadyTracked": True}

    followed = await _db.get_followed_model(username, source_type=requested_source)
    source_type = (followed or {}).get("source_type") or requested_source or "chaturbate"

    # Add to models table
    await _db.add_or_update_model(
        username=username,
        auto_record=True,
        record_quality="best",
        retention_days=await _get_default_retention_days(),
        source_type=source_type,
    )

    return {"message": f"{username} added to tracking", "tracked": True}
