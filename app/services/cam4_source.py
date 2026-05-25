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

from ..core.http_client import aiohttp_client_session, aiohttp_request_kwargs


PROFILE_PAGE_URL = "https://www.cam4.com/{username}"
BROADCAST_API_URL = "https://www.cam4.com/rest/v1.0/profile/{username}/streamInfo"

_M3U8_RE = re.compile(r"https?://[^\s\"'<>]+\.m3u8[^\s\"'<>]*", re.IGNORECASE)
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{2,32}$")
_BROADCAST_MARKER_RE = re.compile(r'"BroadcastItem:\d+":')
_TAG_MARKER_RE = re.compile(r'"BroadcastTag:([^"]+)":\{([^}]+)\}')
_ATTR_RE = re.compile(
    r"""([:\w-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?""",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")

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


def _strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", value or "")).strip()


def _parse_attrs(raw: str) -> Dict[str, str]:
    attrs: Dict[str, str] = {}
    for match in _ATTR_RE.finditer(raw or ""):
        key = (match.group(1) or "").lower()
        value = next((g for g in match.groups()[1:] if g is not None), "")
        if key:
            attrs[key] = value or ""
    return attrs


def _normalize_tags(values: List[Any]) -> List[str]:
    seen = set()
    tags: List[str] = []
    for value in values:
        if isinstance(value, dict):
            value = value.get("name") or value.get("label") or value.get("slug") or value.get("value")
        tag = str(value or "").strip().strip("#").lower()
        tag = re.sub(r"\s+", " ", tag)
        if not tag or tag in seen or len(tag) > 48:
            continue
        seen.add(tag)
        tags.append(tag)
        if len(tags) >= 12:
            break
    return tags


def _tags_from_stream_info(info: Dict[str, Any]) -> List[str]:
    values: List[Any] = []
    for key in ("tags", "tagLabels", "tag_labels", "categories", "interests"):
        raw = info.get(key) or []
        if isinstance(raw, str):
            values.extend(part.strip() for part in raw.split(",") if part.strip())
        elif isinstance(raw, list):
            values.extend(raw)
    return _normalize_tags(values)


def _first_image_url(fragment: str) -> Optional[str]:
    img_match = re.search(r"<img\b([^>]*)>", fragment or "", re.IGNORECASE | re.DOTALL)
    if not img_match:
        return None
    attrs = _parse_attrs(img_match.group(1))
    for key in ("data-src", "data-original", "data-thumb", "src"):
        value = (attrs.get(key) or "").strip()
        if value and not value.startswith("data:image"):
            return _decode_unicode_slashes(value)
    return None


def _parse_card_viewers(fragment: str) -> int:
    for pattern in (
        r"""data-count\s*=\s*["']?(\d[\d,.\s]*(?:[kKmM])?)["']?[^>]{0,180}(?:Viewers?|Watching|Users?|Connection)""",
        r"""(?:Viewers?|Watching|Users?|Connection)[^<>"']{0,120}>\s*(\d[\d,.\s]*(?:[kKmM])?)\s*<""",
        r"""(\d[\d,.\s]*(?:[kKmM])?)\s*(?:viewers?|watching|users?)""",
    ):
        match = re.search(pattern, fragment or "", re.IGNORECASE | re.DOTALL)
        if match:
            raw = match.group(1).strip().lower().replace(",", ".").replace(" ", "")
            suffix = raw[-1:] if raw[-1:] in {"k", "m"} else ""
            number = raw[:-1] if suffix else raw
            try:
                parsed = float(number) if suffix else int(re.sub(r"[^\d]", "", number) or 0)
            except ValueError:
                return 0
            if suffix == "k":
                return int(parsed * 1000)
            if suffix == "m":
                return int(parsed * 1000000)
            return int(parsed)
    return 0


def _parse_rendered_cards(html: str) -> List[Dict[str, Any]]:
    cards: List[Dict[str, Any]] = []
    pattern = re.compile(
        r"""<div\b(?P<attrs>[^>]*(?:data-position|data-profile)[^>]*)>(?P<body>.*?)(?=<div\b[^>]*(?:data-position|data-profile)|</body>|\Z)""",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(html or ""):
        attrs = _parse_attrs(match.group("attrs"))
        body = match.group("body") or ""
        username = (attrs.get("data-profile") or attrs.get("data-username") or "").strip()
        if not username:
            link_match = re.search(r"""href\s*=\s*["']/([A-Za-z0-9_]{2,32})(?:["'/?#])""", body, re.IGNORECASE)
            username = link_match.group(1) if link_match else ""
        if not username:
            continue

        tag_values: List[str] = []
        for tag_match in re.finditer(r"""href\s*=\s*["'][^"']*/tags/([^"'?#]+)""", body, re.IGNORECASE):
            path = tag_match.group(1).strip("/")
            segment = next((part for part in reversed(path.split("/")) if part), "")
            if segment:
                tag_values.append(segment.replace("-", " "))

        cards.append(
            {
                "username": username,
                "display_name": username,
                "thumbnail": _first_image_url(body),
                "viewers": _parse_card_viewers(match.group(0)),
                "subject": None,
                "age": None,
                "gender": attrs.get("data-gender") or attrs.get("data-broadcast-type") or "",
                "is_online": True,
                "tags": _normalize_tags(
                    tag_values
                    + [attrs.get("data-gender"), attrs.get("data-broadcast-type"), "public"]
                ),
                "source_type": "cam4",
                "room_status": "public",
            }
        )
    return cards


def _merge_broadcast_metadata(items: List[Dict[str, Any]], extra_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_username = {str(item.get("username") or "").lower(): item for item in items if item.get("username")}
    for extra in extra_items:
        key = str(extra.get("username") or "").lower()
        if not key:
            continue
        existing = by_username.get(key)
        if not existing:
            by_username[key] = extra
            items.append(extra)
            continue
        if not existing.get("thumbnail") and extra.get("thumbnail"):
            existing["thumbnail"] = extra["thumbnail"]
        existing["viewers"] = max(int(existing.get("viewers") or 0), int(extra.get("viewers") or 0))
        existing["tags"] = _normalize_tags(list(existing.get("tags") or []) + list(extra.get("tags") or []))
    return items


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
        tag_labels = _normalize_tags([tag_names.get(s, s) for s in tag_slugs] + [broadcast_type, "public"])

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
    return _merge_broadcast_metadata(items, _parse_rendered_cards(html))


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
        async with aiohttp_client_session(timeout=timeout) as session:
            async with session.get(
                url,
                headers=headers,
                allow_redirects=True,
                **aiohttp_request_kwargs(),
            ) as resp:
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
        async with aiohttp_client_session(timeout=timeout) as session:
            async with session.get(
                url,
                headers={"User-Agent": user_agent},
                **aiohttp_request_kwargs(),
            ) as resp:
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
        async with aiohttp_client_session(timeout=timeout) as session:
            async with session.get(
                url,
                headers={"User-Agent": user_agent},
                **aiohttp_request_kwargs(),
            ) as resp:
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
        "tags": _tags_from_stream_info(info),
        "thumbnail": info.get("previewImageURL") or info.get("profileImageURL"),
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
    async with aiohttp_client_session(timeout=timeout) as session:
        async with session.request(
            method,
            url,
            headers=headers,
            allow_redirects=False,
            **aiohttp_request_kwargs(),
            **kwargs
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
