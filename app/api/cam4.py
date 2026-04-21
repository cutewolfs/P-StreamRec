"""
API Router: CAM4 session management + followed models.

Login programmatique via POST /rest/v2.0/login (endpoint interne CAM4). Les
cookies retournés sont persistés par CAM4AuthService et réutilisés pour les
requêtes authentifiées (favorites, etc.).
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
    from ..services import cam4_source
    auth_user, cookies = _require_auth()
    result = await cam4_source.is_favorite(auth_user, username, cookies)
    return {"isFollowing": bool(result)}


@router.post("/follow/{username}")
async def cam4_follow(username: str):
    from ..services import cam4_source
    auth_user, cookies = _require_auth()
    result = await cam4_source.follow(auth_user, username, cookies)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Follow failed"))

    # Mise à jour immédiate de la table followed_models pour que le modèle
    # apparaisse sur la page /following sans attendre un sync.
    if _db is not None:
        try:
            status = await cam4_source.check_status(username)
            thumb = None
            if status.get("hls_source"):
                thumb = f"https://snapshots.xcdnpro.com/thumbnails/{username}"
            await _db.upsert_followed_model(
                username=username,
                display_name=username,
                is_online=bool(status.get("is_online")),
                viewers=int(status.get("viewers") or 0),
                thumbnail_url=thumb,
                source_type="cam4",
            )
        except Exception as e:
            logger.debug("CAM4 upsert après follow échec", username=username, error=str(e))
    return result


@router.post("/unfollow/{username}")
async def cam4_unfollow(username: str):
    from ..services import cam4_source
    auth_user, cookies = _require_auth()
    result = await cam4_source.unfollow(auth_user, username, cookies)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Unfollow failed"))

    if _db is not None:
        try:
            await _db.delete_followed_model(username, source_type="cam4")
        except Exception as e:
            logger.debug("CAM4 delete après unfollow échec", username=username, error=str(e))
    return result


@router.post("/following/sync")
async def cam4_sync_following():
    """Synchronise les favoris CAM4 du compte connecté vers la DB locale
    (table followed_models, source_type=cam4)."""
    if _auth_service is None or _db is None:
        raise HTTPException(status_code=503, detail="Services CAM4 non initialisés")

    status = _auth_service.get_status()
    if not status.get("isLoggedIn"):
        raise HTTPException(status_code=401, detail="CAM4 session absente")

    from ..services import cam4_source
    cookies = _auth_service.get_cookies()
    try:
        items = await cam4_source.list_followed(cookies)
    except Exception as e:
        logger.error("CAM4 list_followed échec", error=str(e), exc_info=True)
        raise HTTPException(status_code=502, detail=f"Échec CAM4 list_followed: {e}")

    synced = set()
    for item in items:
        await _db.upsert_followed_model(
            username=item["username"],
            display_name=item.get("display_name") or item["username"],
            is_online=bool(item.get("is_online", False)),
            viewers=int(item.get("viewers") or 0),
            thumbnail_url=item.get("thumbnail"),
            source_type="cam4",
        )
        synced.add(item["username"])

    # On ne supprime PAS les follows CAM4 absents: sans état fiable de la page
    # /favorites (redirection silencieuse si la session expire), un scrape
    # raté vidrait la DB. L'utilisateur peut supprimer manuellement.
    return {"synced": len(synced), "message": f"CAM4: {len(synced)} favoris synchronisés"}
