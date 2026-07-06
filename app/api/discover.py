"""API Router: Discover live models across registered providers."""

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query

from ..logger import logger
from ..core.config import CHATURBATE_REQUEST_TIMEOUT_SECONDS

router = APIRouter(prefix="/api", tags=["discover"])

_chaturbate_api = None
_db = None
_provider_registry = None
_pagination_total_cache: dict[tuple, tuple[float, int, int]] = {}
_PAGINATION_TOTAL_TTL_SECONDS = 300
_PROVIDER_DISABLED_SETTING = "disabled_providers"
_GENDER_ALIASES = {
    "female": "female",
    "f": "female",
    "females": "female",
    "girl": "female",
    "girls": "female",
    "woman": "female",
    "women": "female",
    "male": "male",
    "m": "male",
    "males": "male",
    "man": "male",
    "men": "male",
    "guy": "male",
    "guys": "male",
    "couple": "couple",
    "couples": "couple",
    "cpl": "couple",
    "maleFemale": "couple",
    "malefemale": "couple",
    "trans": "trans",
    "transgender": "trans",
    "ts": "trans",
    "tranny": "trans",
    "femaleTranny": "trans",
    "femaletranny": "trans",
    "transsexual": "trans",
}


def init(chaturbate_api, db, provider_registry=None):
    global _chaturbate_api, _db, _provider_registry
    _chaturbate_api = chaturbate_api
    _db = db
    _provider_registry = provider_registry
    _pagination_total_cache.clear()


async def _disabled_provider_sources() -> set[str]:
    if _db is None:
        return set()
    try:
        if hasattr(_db, "get_disabled_providers"):
            return set(await _db.get_disabled_providers())
        raw_value = await _db.get_setting(_PROVIDER_DISABLED_SETTING)
    except Exception:
        return set()
    try:
        parsed = json.loads(raw_value or "[]")
    except (TypeError, ValueError):
        return set()
    if not isinstance(parsed, list):
        return set()
    return {
        str(source_type or "").strip().lower()
        for source_type in parsed
        if str(source_type or "").strip()
    }


def _discover_providers(source: Optional[str], disabled_sources: Optional[set[str]] = None) -> list:
    if _provider_registry is None:
        return []
    disabled_sources = disabled_sources or set()
    requested = [
        item.strip().lower()
        for item in (source or "").split(",")
        if item.strip()
    ]
    if requested:
        providers = []
        for source_type in requested:
            if source_type in disabled_sources:
                continue
            if _provider_registry.has(source_type):
                providers.append(_provider_registry.get(source_type))
        return providers
    return [
        provider
        for provider in _provider_registry.all()
        if getattr(provider.capabilities, "can_discover", False)
        and provider.source_type not in disabled_sources
    ]


def _pagination_cache_key(
    source: Optional[str],
    gender: Optional[str],
    search: str,
    tags: List[str],
    sort_mode: str,
    limit: int,
    disabled_sources: set[str],
) -> tuple:
    requested_sources = tuple(
        item.strip().lower()
        for item in (source or "").split(",")
        if item.strip()
    )
    return (
        requested_sources or ("__all__",),
        (gender or "").strip().lower(),
        search,
        tuple(tags),
        sort_mode,
        int(limit),
        tuple(sorted(disabled_sources)),
    )


def _stable_pagination_totals(
    cache_key: tuple,
    page: int,
    total: int,
    total_pages: int,
) -> tuple[int, int]:
    now = time.monotonic()
    cached = _pagination_total_cache.get(cache_key)
    if cached and now - cached[0] >= _PAGINATION_TOTAL_TTL_SECONDS:
        cached = None
        _pagination_total_cache.pop(cache_key, None)

    if page <= 1 or cached is None:
        stable = (max(0, int(total)), max(1, int(total_pages or 1)))
        _pagination_total_cache[cache_key] = (now, stable[0], stable[1])
        return stable

    return cached[1], cached[2]


def _canonical_gender(value: object) -> Optional[str]:
    token = str(value or "").strip()
    if not token:
        return None
    compact = token.replace("_", "").replace("-", "").replace(" ", "")
    lowered = token.lower().replace("_", "-").strip()
    return (
        _GENDER_ALIASES.get(token)
        or _GENDER_ALIASES.get(compact)
        or _GENDER_ALIASES.get(lowered)
        or _GENDER_ALIASES.get(lowered.replace("-", ""))
    )


def _gender_tokens(values: List[object]) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        canonical = _canonical_gender(value)
        if canonical:
            tokens.add(canonical)
    return tokens


def _matches_gender_filter(
    item: Dict[str, Any],
    item_tags: List[str],
    requested_gender: Optional[str],
) -> bool:
    requested = _canonical_gender(requested_gender)
    if not requested:
        return True

    primary_tokens = _gender_tokens([
        item.get("gender"),
        item.get("gender_group"),
        item.get("genderGroup"),
        item.get("broadcastGender"),
        item.get("category"),
        item.get("main_category"),
    ])
    if primary_tokens:
        return requested in primary_tokens

    tag_tokens = _gender_tokens(item_tags)
    if "trans" in tag_tokens and requested != "trans":
        return False
    if "couple" in tag_tokens and requested != "couple":
        return False
    return requested in tag_tokens


async def _fetch_provider(
    provider,
    page: int,
    limit: int,
    gender: Optional[str],
    search: Optional[str],
    tags: Optional[List[str]],
    allow_browser: bool,
    exact_search_fallback: bool,
) -> Optional[Dict[str, Any]]:
    try:
        timeout = 25 if allow_browser else 14
        if getattr(provider, "source_type", "") == "chaturbate":
            timeout = max(timeout, CHATURBATE_REQUEST_TIMEOUT_SECONDS + (45 if allow_browser else 5))
        return await asyncio.wait_for(
            provider.list_live_models(
                page=page,
                limit=limit,
                gender=gender or "",
                search=search or "",
                tags=tags or [],
                allow_browser=allow_browser,
                exact_search_fallback=exact_search_fallback,
            ),
            timeout=timeout,
        )
    except Exception as e:
        logger.warning(
            "Discover provider échec",
            source_type=getattr(provider, "source_type", "unknown"),
            error=str(e),
        )
        return None


@router.get("/discover")
async def discover_models(
    page: int = Query(1, ge=1),
    limit: int = Query(24, ge=1, le=100),
    source: Optional[str] = Query(None),
    gender: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    tags: Optional[str] = Query(None),
    sort: Optional[str] = Query("viewers"),
):
    """Liste agrégée de toutes les sources Discover (rooms publiques uniquement)."""
    included_tags: List[str] = []
    if tags:
        included_tags = [t.strip().lower() for t in tags.split(",") if t.strip()]

    search_lower = (search or "").strip().lower()
    sort_mode = (sort or "viewers").strip().lower()

    disabled_sources = await _disabled_provider_sources()
    providers = _discover_providers(source, disabled_sources)
    if not providers:
        return {"models": [], "total": 0, "page": page, "limit": limit, "total_pages": 1}
    explicit_source = bool((source or "").strip())
    # En vue "All sources", on récupère un volume fixe par provider puis on
    # trie/pagine localement. Sinon le nombre total de pages change à chaque
    # Next parce que certains providers recalculent total_pages avec le limit
    # temporaire qu'on leur passe.
    per_source_limit = limit if explicit_source else 100
    provider_page = page if explicit_source else 1
    allow_browser = explicit_source

    results = await asyncio.gather(*[
        _fetch_provider(
            provider,
            provider_page,
            per_source_limit,
            gender,
            search,
            included_tags or None,
            allow_browser=allow_browser,
            exact_search_fallback=explicit_source,
        )
        for provider in providers
    ])

    blacklisted_tags: List[str] = []
    if _db:
        try:
            blacklisted_tags = await _db.get_blacklisted_tags()
        except Exception:
            blacklisted_tags = []
    blacklisted_set = {t.lower() for t in blacklisted_tags}

    capabilities = {
        provider.source_type: provider.capabilities
        for provider in providers
    }

    def _fallback_tags(item: Dict[str, Any]) -> List[str]:
        values = [
            item.get("gender"),
            item.get("room_status") or "public",
        ]
        age = item.get("age")
        try:
            age_value = int(age or 0)
        except (TypeError, ValueError):
            age_value = 0
        if 18 <= age_value <= 29:
            values.append("18-29")
        elif 30 <= age_value <= 39:
            values.append("30-39")
        elif 40 <= age_value <= 49:
            values.append("40-49")
        elif age_value >= 50:
            values.append("50+")
        seen = set()
        tags_out: List[str] = []
        for value in values:
            tag = str(value or "").strip().lower()
            if not tag or tag in seen:
                continue
            seen.add(tag)
            tags_out.append(tag)
        return tags_out

    def _filter_list(result: Optional[Dict[str, Any]], source_type: str) -> List[Dict[str, Any]]:
        if result is None:
            return []
        out: List[Dict[str, Any]] = []
        for item in result.get("models", []):
            # Room status (public par défaut pour Chaturbate qui renseigne
            # current_show). On n'exclut QUE les rooms non publiques.
            rs = (item.get("room_status") or "public").lower()
            if rs != "public" or not item.get("is_online", True):
                continue
            item_tags = [str(tag).strip().lower() for tag in (item.get("tags") or []) if str(tag).strip()]
            if not item_tags:
                item_tags = _fallback_tags(item)
            item_tags_lower = [t.lower() for t in item_tags]
            if not _matches_gender_filter(item, item_tags_lower, gender):
                continue
            if blacklisted_set and any(bt in item_tags_lower for bt in blacklisted_set):
                continue
            # Filtrage strict: tous les tags demandés doivent être présents.
            # Sans ce garde, CAM4 (qui fournit rarement des tags) polluait la
            # liste filtrée avec des items hors-sujet.
            if included_tags and not all(t in item_tags_lower for t in included_tags):
                continue
            # Filtrage strict: le search doit matcher l'username. Le backend
            # Chaturbate fait un match full-text sur keywords (subject inclus)
            # qui renvoie des non-pertinents; on restreint ici.
            if search_lower:
                uname = (item.get("username") or "").lower()
                dname = (item.get("display_name") or "").lower()
                if search_lower not in uname and search_lower not in dname:
                    continue
            item = dict(item)
            item["viewers"] = int(item.get("viewers") or 0)
            item["tags"] = item_tags
            item["source_type"] = item.get("source_type") or source_type
            item_caps = capabilities.get(item["source_type"])
            stream_available = bool(getattr(item_caps, "can_stream", True))
            record_available = bool(getattr(item_caps, "can_record", stream_available))
            if not explicit_source and not stream_available:
                continue
            item["stream_available"] = stream_available
            item["record_available"] = record_available
            item["can_follow"] = bool(getattr(item_caps, "can_follow", False) or stream_available or record_available)
            out.append(item)
        return out

    grouped_items: List[List[Dict[str, Any]]] = []
    for provider, result in zip(providers, results):
        grouped_items.append(_filter_list(result, provider.source_type))

    provider_statuses: List[Dict[str, Any]] = []
    for provider, result, items in zip(providers, results, grouped_items):
        if result is None:
            provider_statuses.append({
                "source_type": provider.source_type,
                "display_name": provider.display_name,
                "status": "error",
                "detail": "Provider did not return a Discover response.",
                "count": 0,
                "total": 0,
            })
            continue
        provider_can_stream = bool(getattr(provider.capabilities, "can_stream", True))
        provider_status = str(result.get("provider_status") or ("ok" if items else "empty"))
        provider_detail = str(result.get("provider_detail") or "")
        if not provider_can_stream and result.get("models"):
            provider_status = "discover_only"
            provider_detail = (
                provider_detail
                or "Discover is available, but this provider did not expose a public FFmpeg-readable stream."
            )
        provider_statuses.append({
            "source_type": provider.source_type,
            "display_name": provider.display_name,
            "status": provider_status,
            "detail": provider_detail,
            "count": len(items),
            "total": int(result.get("total") or 0),
        })

    plugin_totals = []
    plugin_total_pages = []
    for r in results:
        if r is None:
            continue
        plugin_totals.append(int(r.get("total") or 0))
        plugin_total_pages.append(int(r.get("total_pages") or 1))

    # Classement global: une room à 900 viewers doit passer devant une room à
    # 50 viewers, quelle que soit la source. En vue agrégée, on prend la page 1
    # de chaque provider avec assez de candidats pour trier puis paginer ici.
    ranked_items = [item for group in grouped_items for item in group]
    if not explicit_source and sort_mode == "viewers":
        positive_items = [item for item in ranked_items if int(item.get("viewers") or 0) > 0]
        if positive_items:
            ranked_items = [
                item for item in ranked_items
                if int(item.get("viewers") or 0) > 0
            ]
    if sort_mode == "newest":
        ranked_items.sort(key=lambda m: (m.get("age") or 99))
    else:
        ranked_items.sort(key=lambda m: int(m.get("viewers") or 0), reverse=True)

    if explicit_source:
        combined = ranked_items[:limit]
    else:
        start = (page - 1) * limit
        combined = ranked_items[start:start + limit]

    total_combined = sum(plugin_totals)
    if explicit_source:
        total_pages = max(plugin_total_pages) if plugin_total_pages else 1
    else:
        total_combined = len(ranked_items)
        total_pages = max(1, (total_combined + limit - 1) // limit)
    total_combined, total_pages = _stable_pagination_totals(
        _pagination_cache_key(source, gender, search_lower, included_tags, sort_mode, limit, disabled_sources),
        page,
        total_combined,
        total_pages,
    )

    if _db:
        try:
            tracked_models = await _db.get_all_models()
            tracked_set = {
                (m["username"], m.get("source_type") or "chaturbate")
                for m in tracked_models
            }
            followed_models = await _db.get_all_followed()
            followed_set = {
                (m["username"], m.get("source_type") or "chaturbate")
                for m in followed_models
            }
            for model in combined:
                key = (model["username"], model.get("source_type") or "chaturbate")
                model["isTracked"] = key in tracked_set
                model["isFollowed"] = key in followed_set
        except Exception:
            pass

    return {
        "models": combined,
        "total": total_combined,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
        "provider_statuses": provider_statuses,
    }
