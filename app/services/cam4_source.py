"""
Source intégrée CAM4.

Expose les fonctions resolve / check_status / list_live_models / list_followed
en dicts, pour que main.py et les routers les appellent directement quand
source_type == "cam4" (en remplacement de l'ancien plugin qui passait par
le PluginManager).
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

import aiohttp


PROFILE_PAGE_URL = "https://www.cam4.com/{username}"
BROADCAST_API_URL = "https://www.cam4.com/rest/v1.0/profile/{username}/streamInfo"

_M3U8_RE = re.compile(r"https?://[^\s\"'<>]+\.m3u8[^\s\"'<>]*", re.IGNORECASE)
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{2,32}$")
_BROADCAST_MARKER_RE = re.compile(r'"BroadcastItem:\d+":')
_TAG_MARKER_RE = re.compile(r'"BroadcastTag:([^"]+)":\{([^}]+)\}')

_DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class CAM4Error(Exception):
    pass


class CAM4ResolveError(CAM4Error):
    pass


class CAM4StatusError(CAM4Error):
    pass


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _extract_balanced(html: str, start_idx: int) -> Optional[str]:
    n = len(html)
    i = start_idx
    while i < n and html[i] != "{":
        i += 1
    if i >= n:
        return None
    obj_start = i
    depth = 0
    in_str = False
    while i < n:
        c = html[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return html[obj_start:i + 1]
        i += 1
    return None


def _decode_unicode_slashes(text: str) -> str:
    return text.replace("\\u002F", "/").replace("\\u003D", "=")


def _parse_broadcasts(html: str) -> List[Dict[str, Any]]:
    tag_names: Dict[str, str] = {}
    for m in _TAG_MARKER_RE.finditer(html):
        body = m.group(2)
        slug_m = re.search(r'"slug":"([^"]+)"', body)
        name_m = re.search(r'"i18nValue":"([^"]*)"', body) or re.search(
            r'"name":"([^"]+)"', body
        )
        if slug_m and name_m:
            tag_names[slug_m.group(1)] = name_m.group(1)

    items: List[Dict[str, Any]] = []
    seen: set = set()
    for m in _BROADCAST_MARKER_RE.finditer(html):
        obj_text = _extract_balanced(html, m.end())
        if not obj_text:
            continue
        try:
            obj = json.loads(obj_text)
        except json.JSONDecodeError:
            continue

        username = (obj.get("username") or "").strip()
        if not username or username.lower() in seen:
            continue
        seen.add(username.lower())

        viewers = int(obj.get("viewers") or 0)
        preview = obj.get("preview") or {}
        thumb = preview.get("poster") or obj.get("profileImageURL") or None
        if isinstance(thumb, str):
            thumb = _decode_unicode_slashes(thumb)
        broadcast_type = obj.get("broadcastType")
        show_type = (obj.get("showType") or "").upper()

        if show_type == "PUBLIC_SHOW" or not show_type:
            room_status = "public"
        elif show_type == "GROUP_SHOW":
            room_status = "group"
        else:
            room_status = show_type.lower().replace("_show", "")

        tag_slugs = re.findall(r'"__ref":"BroadcastTag:([^"]+)"', obj_text)
        tag_labels = [tag_names.get(s, s) for s in tag_slugs]

        items.append(
            {
                "username": username,
                "display_name": username,
                "thumbnail": thumb,
                "viewers": viewers,
                "subject": None,
                "age": None,
                "gender": broadcast_type,
                "is_online": room_status == "public",
                "tags": tag_labels,
                "source_type": "cam4",
                "room_status": room_status,
            }
        )
    return items


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def _fetch_html(
    url: str,
    cookies: Optional[Dict[str, str]] = None,
    user_agent: str = _DEFAULT_UA,
) -> Optional[str]:
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, allow_redirects=True) as resp:
                if resp.status != 200:
                    return None
                return await resp.text()
    except Exception:
        return None


async def _fetch_stream_info(
    username: str, user_agent: str = _DEFAULT_UA
) -> Dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=15)
    url = BROADCAST_API_URL.format(username=username)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"User-Agent": user_agent}) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json(content_type=None)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def _scrape_hls(username: str, user_agent: str = _DEFAULT_UA) -> Optional[str]:
    timeout = aiohttp.ClientTimeout(total=15)
    url = PROFILE_PAGE_URL.format(username=username)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"User-Agent": user_agent}) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()
        m = _M3U8_RE.search(html)
        return m.group(0) if m else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_username(target: str) -> bool:
    if not target:
        return False
    return bool(_USERNAME_RE.match(target.strip()))


async def resolve(target: str, max_height: Optional[int] = None) -> str:
    username = target.strip().lower()
    if not validate_username(username):
        raise CAM4ResolveError(f"Username CAM4 invalide: '{target}'")

    info = await _fetch_stream_info(username)
    hls = info.get("cdnURL") or info.get("hlsPlaylistUrl") or info.get("edgeURL")
    if not hls:
        hls = await _scrape_hls(username)
    if not hls:
        raise CAM4ResolveError(f"Aucun M3U8 trouvé pour CAM4/{username}")
    return hls


async def check_status(username: str) -> Dict[str, Any]:
    if not validate_username(username):
        raise CAM4StatusError(f"Username CAM4 invalide: '{username}'")
    try:
        info = await _fetch_stream_info(username.strip().lower())
    except Exception as e:
        raise CAM4StatusError(f"Échec check_status CAM4 '{username}': {e}")

    hls = info.get("cdnURL") or info.get("hlsPlaylistUrl") or info.get("edgeURL")
    is_online = bool(
        info.get("isLive")
        or info.get("isCamming")
        or info.get("online")
        or hls
    )
    viewers = int(info.get("viewerCount") or info.get("viewers") or 0)
    return {
        "is_online": is_online,
        "viewers": viewers,
        "hls_source": hls,
        "room_status": info.get("showType") or info.get("status") or None,
    }


async def list_live_models(
    page: int = 1,
    limit: int = 24,
    gender: Optional[str] = None,
    search: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    gender_map = {
        "female": "female",
        "male": "male",
        "couple": "couple",
        "trans": "trans",
    }
    path = gender_map.get((gender or "").lower(), "")

    first_tag = ""
    if tags:
        t = tags[0].strip().lower()
        if re.match(r"^[a-z0-9_-]{2,40}$", t):
            first_tag = t

    if path and first_tag:
        url = f"https://www.cam4.com/{path}/{first_tag}"
    elif first_tag:
        url = f"https://www.cam4.com/{first_tag}"
    elif path:
        url = f"https://www.cam4.com/{path}"
    else:
        url = "https://www.cam4.com/"

    html = await _fetch_html(url)
    if not html:
        return {"models": [], "total": 0, "page": page, "limit": limit, "total_pages": 1}

    items = _parse_broadcasts(html)

    if search:
        s = search.strip().lower()
        if s:
            items = [it for it in items if s in (it["username"] or "").lower()]

    items.sort(key=lambda it: int(it.get("viewers") or 0), reverse=True)

    total = len(items)
    total_pages = max(1, (total + limit - 1) // limit)
    start = (page - 1) * limit
    end = start + limit
    return {
        "models": items[start:end],
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
    }


async def list_followed(cookies: Dict[str, str]) -> List[Dict[str, Any]]:
    """Liste les favoris de l'utilisateur authentifié.

    Endpoint: /friends_favorites (d'après l'app CAM4, routeDefinitions.favorites).
    /favorites sans s final redirige vers /error-no-profile qui inline des
    suggestions — ne PAS utiliser, ça produit de faux positifs.
    `showOfflineBroadcasters=true` inclut les favoris offline.
    """
    if not cookies:
        return []
    html = await _fetch_html(
        "https://www.cam4.com/friends_favorites?showOfflineBroadcasters=true",
        cookies=cookies,
    )
    if not html:
        return []
    return _parse_broadcasts(html)


# ---------------------------------------------------------------------------
# Favoris (follow/unfollow) — REST /rest/v1.0/favorites/{username}/{performerName}
# ---------------------------------------------------------------------------


_FAVORITE_URL = "https://www.cam4.com/rest/v1.0/favorites/{username}/{performerName}"


async def _favorite_request(
    method: str,
    auth_username: str,
    target: str,
    cookies: Dict[str, str],
) -> aiohttp.ClientResponse:
    url = _FAVORITE_URL.format(
        username=auth_username.strip().lower(),
        performerName=target.strip(),
    )
    headers = {
        "User-Agent": _DEFAULT_UA,
        "Accept": "application/json",
        "Origin": "https://www.cam4.com",
        "Referer": f"https://www.cam4.com/{target}",
        "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
    }
    # POST/DELETE exigent un Content-Type et un body vide — sans ça CAM4
    # renvoie 415 Unsupported Media Type.
    kwargs = {}
    if method in ("POST", "DELETE"):
        headers["Content-Type"] = "application/json"
        kwargs["data"] = b""
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(
            method, url, headers=headers, allow_redirects=False, **kwargs
        ) as resp:
            body = await resp.text()
            return resp.status, body


async def is_favorite(
    auth_username: str, target: str, cookies: Dict[str, str]
) -> bool:
    """True si `target` est un favori de l'utilisateur authentifié."""
    if not cookies or not auth_username:
        return False
    try:
        status, body = await _favorite_request("GET", auth_username, target, cookies)
    except Exception:
        return False
    if status != 200:
        return False
    b = (body or "").strip().lower()
    if b in {"true", '"true"'}:
        return True
    if b in {"false", '"false"'}:
        return False
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return False
    if isinstance(data, bool):
        return data
    if isinstance(data, dict):
        # Valeurs plausibles: {"favorite": true}, {"status": "FAVORITE"}, ...
        if isinstance(data.get("favorite"), bool):
            return data["favorite"]
        if isinstance(data.get("isFavorite"), bool):
            return data["isFavorite"]
        st = str(data.get("status") or "").upper()
        if st == "FAVORITE":
            return True
    return False


async def follow(
    auth_username: str, target: str, cookies: Dict[str, str]
) -> Dict[str, Any]:
    if not cookies or not auth_username:
        return {"success": False, "error": "CAM4 session absente"}
    try:
        status, body = await _favorite_request("POST", auth_username, target, cookies)
    except Exception as e:
        return {"success": False, "error": f"Erreur réseau: {e}"}
    if status in (200, 201, 204):
        return {"success": True}
    return {"success": False, "error": body or f"HTTP {status}", "code": status}


async def unfollow(
    auth_username: str, target: str, cookies: Dict[str, str]
) -> Dict[str, Any]:
    if not cookies or not auth_username:
        return {"success": False, "error": "CAM4 session absente"}
    try:
        status, body = await _favorite_request("DELETE", auth_username, target, cookies)
    except Exception as e:
        return {"success": False, "error": f"Erreur réseau: {e}"}
    if status in (200, 204):
        return {"success": True}
    return {"success": False, "error": body or f"HTTP {status}", "code": status}
