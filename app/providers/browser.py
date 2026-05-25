from __future__ import annotations

import asyncio
import html
import json
import os
import re
import time
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import quote_plus, unquote, urlencode, urljoin, urlparse

import aiohttp

from ..core.http_client import aiohttp_client_session, aiohttp_request_kwargs
from ..logger import logger
from .base import (
    BaseProvider,
    ProviderCapabilities,
    ProviderError,
    ProviderInteractionRequired,
    ProviderOfflineError,
    ProviderPrivateError,
    ResolvedStream,
)
from .sessions import ProviderSessionStore


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_MEDIA_URL_RE = re.compile(
    r"https?:\\?/\\?/[^\s\"'<>]+?\.(?:m3u8|mpd)(?:\?[^\s\"'<>]*)?",
    re.IGNORECASE,
)
_HLS_URL_FIELD_RE = re.compile(
    r"""["']hlsUrl["']\s*:\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_INTERACTION_RE = re.compile(r"captcha|2fa|two-factor|cloudflare|verify you are human", re.IGNORECASE)
_OFFLINE_RE = re.compile(r"offline|not currently online|not live|away", re.IGNORECASE)
_PRIVATE_RE = re.compile(r"private|ticket show|group show|premium", re.IGNORECASE)
_ANCHOR_RE = re.compile(r"<a\b(?P<attrs>[^>]*)>(?P<body>.*?)</a>", re.IGNORECASE | re.DOTALL)
_IMG_RE = re.compile(r"<img\b(?P<attrs>[^>]*)>", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(
    r"""([:\w-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?""",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_VIEWER_RE = re.compile(
    r"(?:(\d[\d,.\s]*(?:[kKmM])?)\s*(?:viewers?|watching|users?)|(?:viewers?|watching|users?)\D{0,24}(\d[\d,.\s]*(?:[kKmM])?))",
    re.IGNORECASE,
)
_VIEWER_KEY_RE = re.compile(
    r"""(?:
        ["']?(?:viewers?|viewerCount|viewer_count|numUsers|num_users|numViewers|watching)["']?\s*[:=]\s*["']?(\d[\d,.\s]*(?:[kKmM])?)["']?
        |
        data-(?:viewers?|viewer-count|num-users|watching)\s*=\s*["']?(\d[\d,.\s]*(?:[kKmM])?)["']?
    )""",
    re.IGNORECASE | re.VERBOSE,
)
_VIEWER_DATA_COUNT_RE = re.compile(
    r"""data-count\s*=\s*["']?(\d[\d,.\s]*(?:[kKmM])?)["']?[^>]{0,160}(?:viewers?|watching|users?|connection)""",
    re.IGNORECASE,
)
_VIEWER_CLASS_RE = re.compile(
    r"""(?:viewers?|watching|connection)[^<]{0,120}>\s*(\d[\d,.\s]*(?:[kKmM])?)\s*<""",
    re.IGNORECASE,
)
_HASHTAG_RE = re.compile(r"(?<![\w/])#([A-Za-z0-9][A-Za-z0-9_-]{1,47})")
_TAG_LINK_RE = re.compile(
    r"""href\s*=\s*["'][^"']*/(?:tag|tags|category|categories|hashtag|hashtags)/([^"'?#]+)""",
    re.IGNORECASE,
)
_TAG_FIELD_RE = re.compile(
    r"""["'](?:tags|hashtags|categories|keywords|interests)["']\s*:\s*(\[[^\]]{0,2500}\])""",
    re.IGNORECASE,
)
_TAG_ATTR_HINT_RE = re.compile(r"(?:^|[-_:])(tag|tags|category|categories|hashtag|hashtags|keyword|keywords|interest|interests)(?:$|[-_:])", re.IGNORECASE)
_TAG_SPLIT_RE = re.compile(r"[,;|]")
_GENERIC_TAG_STOPWORDS = {
    "account",
    "all",
    "chat",
    "free",
    "girl",
    "girls",
    "home",
    "html",
    "img",
    "live",
    "login",
    "medium",
    "model",
    "models",
    "online",
    "percent",
    "profile",
    "root",
    "search",
    "show",
    "shows",
    "signup",
    "standard",
    "stream",
    "streams",
    "video",
    "videos",
    "viewer",
    "viewers",
    "watching",
    "wrp",
    "cs_root",
    "livesnap",
    "livesnap parent",
    "livesnap1",
    "livesnap2",
}
_RESERVED_PROFILE_SEGMENTS = {
    "",
    "about",
    "account",
    "accounts",
    "2257",
    "all-models",
    "api",
    "become-a-model",
    "blog",
    "cams",
    "cam",
    "chat",
    "contact",
    "cookies-policy",
    "couple",
    "couples",
    "de",
    "dmca",
    "en",
    "es",
    "female",
    "fr",
    "girls",
    "help",
    "home",
    "hc",
    "it",
    "live",
    "login",
    "logout",
    "male",
    "members",
    "models",
    "new",
    "online",
    "performers",
    "parental-control",
    "privacy",
    "privacy.php",
    "profile",
    "search",
    "sex-cam",
    "signup",
    "support",
    "terms",
    "trans",
    "transgender",
    "videos",
}


def _clean_media_url(value: str) -> str:
    cleaned = html.unescape(value)
    cleaned = cleaned.replace("\\/", "/").replace("\\u002F", "/").replace("\\u003D", "=")
    return cleaned.strip()


def extract_media_urls(text: str) -> list[str]:
    seen = set()
    out = []
    for match in _MEDIA_URL_RE.finditer(text or ""):
        url = _clean_media_url(match.group(0))
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def extract_hls_url_fields(text: str) -> list[str]:
    seen = set()
    out = []
    for match in _HLS_URL_FIELD_RE.finditer(text or ""):
        url = _clean_media_url(match.group(1))
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def streamate_hls_manifest(payload: object) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    formats = payload.get("formats") if isinstance(payload.get("formats"), dict) else {}
    hls_format = formats.get("mp4-hls") if isinstance(formats.get("mp4-hls"), dict) else {}
    manifest = str(hls_format.get("manifest") or "").strip()
    if manifest.startswith("http://") or manifest.startswith("https://"):
        return manifest
    return None


def _parse_attrs(raw: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in _ATTR_RE.finditer(raw or ""):
        key = (match.group(1) or "").lower()
        value = next((g for g in match.groups()[1:] if g is not None), "")
        if key:
            attrs[key] = html.unescape(value or "")
    return attrs


def _strip_html(value: str) -> str:
    text = _TAG_RE.sub(" ", value or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _first_src(attrs: dict[str, str], base_url: str) -> Optional[str]:
    for key in ("data-src", "data-original", "data-lazy-src", "data-thumb", "src"):
        value = (attrs.get(key) or "").strip()
        if value:
            return urljoin(base_url, value)
    srcset = (attrs.get("srcset") or attrs.get("data-srcset") or "").strip()
    if srcset:
        first = srcset.split(",", 1)[0].strip().split(" ", 1)[0]
        if first:
            return urljoin(base_url, first)
    return None


def _first_image(fragment: str, base_url: str) -> Optional[str]:
    match = _IMG_RE.search(fragment or "")
    if not match:
        return None
    return _first_src(_parse_attrs(match.group("attrs")), base_url)


def _viewer_count(fragment: str) -> int:
    raw_fragment = html.unescape(fragment or "")
    data_count_match = _VIEWER_DATA_COUNT_RE.search(raw_fragment)
    if data_count_match:
        return _parse_count(data_count_match.group(1))
    key_match = _VIEWER_KEY_RE.search(raw_fragment)
    if key_match:
        return _parse_count(next((g for g in key_match.groups() if g), "0"))
    class_match = _VIEWER_CLASS_RE.search(raw_fragment)
    if class_match:
        return _parse_count(class_match.group(1))
    match = _VIEWER_RE.search(_strip_html(raw_fragment))
    if not match:
        return 0
    raw = next((g for g in match.groups() if g), "")
    return _parse_count(raw)


def _parse_count(raw: str) -> int:
    value = (raw or "").strip().lower().replace("\xa0", " ")
    match = re.search(r"(\d[\d,.\s]*)([km])?", value)
    if not match:
        return 0
    number = match.group(1).strip()
    suffix = match.group(2) or ""
    if suffix:
        number = number.replace(" ", "").replace(",", ".")
        try:
            parsed = float(number)
        except ValueError:
            return 0
        return int(parsed * (1000 if suffix == "k" else 1000000))
    digits = re.sub(r"[^\d]", "", number)
    return int(digits or 0)


def _normalize_tag(value: str) -> Optional[str]:
    tag = html.unescape(str(value or "")).strip().strip("#").strip()
    tag = re.sub(r"\s+", " ", tag)
    tag = tag.strip(".,;:!?()[]{}\"'")
    if not tag:
        return None
    lower = tag.lower()
    if lower in _GENERIC_TAG_STOPWORDS:
        return None
    if len(lower) < 2 or len(lower) > 48:
        return None
    if re.search(r"https?://|@|[<>/\\]", lower):
        return None
    if re.fullmatch(r"\d+", lower):
        return None
    return lower


def _normalize_tags(values: Iterable[object]) -> list[str]:
    seen = set()
    out = []
    for value in values:
        tag = _normalize_tag(str(value or ""))
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
        if len(out) >= 12:
            break
    return out


def _extract_json_tags(text: str) -> list[str]:
    values: list[object] = []
    for match in _TAG_FIELD_RE.finditer(text or ""):
        raw = html.unescape(match.group(1))
        try:
            parsed = json.loads(raw)
        except Exception:
            continue
        for item in parsed:
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, dict):
                for key in ("name", "slug", "label", "title", "value"):
                    if item.get(key):
                        values.append(item[key])
                        break
    return _normalize_tags(values)


def _extract_tags(fragment: str, attrs: Optional[dict[str, str]] = None) -> list[str]:
    values: list[object] = []
    text = fragment or ""
    attrs = attrs or {}
    for key, value in attrs.items():
        if _TAG_ATTR_HINT_RE.search(key):
            values.extend(part.strip() for part in _TAG_SPLIT_RE.split(value or "") if part.strip())
        if key in {"data-gender", "gender", "data-type", "data-category", "category"} and value:
            values.append(value)
        if key == "class" and value:
            values.extend(_tag_values_from_classes(value))
    for attr_match in re.finditer(
        r"""(data-gender|gender|data-type|data-category|category|class)\s*=\s*["']([^"']+)["']""",
        text,
        re.IGNORECASE,
    ):
        key = attr_match.group(1).lower()
        value = attr_match.group(2)
        if key == "class":
            values.extend(_tag_values_from_classes(value))
        else:
            values.extend(part.strip() for part in _TAG_SPLIT_RE.split(value) if part.strip())
    for match in _TAG_LINK_RE.finditer(text):
        path = unquote(match.group(1)).strip("/")
        segment = next((part for part in reversed(path.split("/")) if part), "")
        if segment:
            values.append(segment.replace("-cams", "").replace("-", " "))
    values.extend(match.group(1) for match in _HASHTAG_RE.finditer(_strip_html(text)))
    values.extend(_extract_json_tags(text))
    return _normalize_tags(values)


def _tag_values_from_classes(value: str) -> list[str]:
    values: list[str] = []
    for token in re.split(r"\s+", value or ""):
        token = token.strip("_ ")
        if not token:
            continue
        flag_match = re.search(r"flag[_-]?([A-Z][A-Za-z]+)", token)
        if flag_match:
            token = flag_match.group(1)
        elif token.startswith("flag"):
            token = token.removeprefix("flag").strip("_- ")
            if not token or re.fullmatch(r"[\d_\-\s]+", token):
                continue
        elif token.startswith("badge"):
            token = re.sub(r"(?<!^)([A-Z])", r" \1", token)
        elif not token.startswith(("hd", "new", "live", "vibra", "private", "voyeur", "exclusive")):
            continue
        if token.startswith("livesnap"):
            continue
        token = token.replace("_", " ").replace("-", " ")
        values.append(token)
    return values


_SUBJECT_KEYWORDS = {
    "anal": ("anal",),
    "asian": ("asian",),
    "bbw": ("bbw",),
    "big ass": ("big ass", "big butt"),
    "big tits": ("big tits", "big boobs"),
    "blonde": ("blonde",),
    "blowjob": ("blowjob",),
    "bondage": ("bondage",),
    "brunette": ("brunette",),
    "c2c": ("c2c", "cam2cam", "cam to cam"),
    "cosplay": ("cosplay",),
    "cum": ("cum",),
    "curvy": ("curvy",),
    "dance": ("dance", "dancing"),
    "dildo": ("dildo",),
    "ebony": ("ebony",),
    "exclusive": ("exclusive",),
    "feet": ("feet", "foot fetish"),
    "femdom": ("femdom",),
    "fetish": ("fetish",),
    "gamer": ("gamer", "gaming"),
    "goth": ("goth",),
    "hd": (" hd ", "high definition"),
    "joi": ("joi",),
    "latina": ("latina",),
    "latex": ("latex",),
    "lesbian": ("lesbian",),
    "lovense": ("lovense",),
    "milf": ("milf",),
    "pvt": ("pvt", "private"),
    "redhead": ("redhead",),
    "roleplay": ("roleplay", "role play"),
    "snapchat": ("snapchat",),
    "squirt": ("squirt",),
    "striptease": ("striptease",),
    "tattoos": ("tattoo",),
    "teen": ("teen", "18+"),
    "toy": ("toy", "toys"),
    "trans": ("trans",),
    "vibrator": ("vibrator",),
    "voyeur": ("voyeur",),
}


def _subject_keyword_tags(value: str) -> list[str]:
    text = f" {_strip_html(value).lower()} "
    found: list[str] = []
    for tag, needles in _SUBJECT_KEYWORDS.items():
        if any(needle in text for needle in needles):
            found.append(tag)
    return _normalize_tags(found)


def _page_metadata(text: str, page_url: str = "") -> dict[str, object]:
    thumb = _first_image(text or "", page_url) if page_url else None
    title_match = re.search(r"<title[^>]*>(.*?)</title>", text or "", re.IGNORECASE | re.DOTALL)
    title = _strip_html(title_match.group(1)) if title_match else None
    return {
        "viewers": _viewer_count(text or ""),
        "tags": _extract_tags(text or ""),
        "thumbnail": thumb,
        "title": title,
    }


def _anchor_context(html_text: str, match: re.Match) -> str:
    start = match.start()
    end = match.end()
    before_markers = [
        r'<div\b[^>]*(?:data-position|data-profile|data-username|data-name|data-gender|class="[^"]*(?:ls_thumb|lst_wrp|ModelCard|model_online|result-module__item))',
        r"<article\b",
        r"<li\b",
    ]
    context_start = start
    prefix = html_text[max(0, start - 5000):start]
    for pattern in before_markers:
        found = list(re.finditer(pattern, prefix, re.IGNORECASE | re.DOTALL))
        if found:
            context_start = max(context_start - len(prefix) + found[-1].start(), 0)
            break

    suffix = html_text[end:end + 5000]
    context_end = end + len(suffix)
    for pattern in (
        r'<div\b[^>]*(?:data-position|data-profile|data-username|class="[^"]*(?:ls_thumb|ModelCard|model_online|result-module__item))',
        r'<div\b[^>]*(?:data-name|data-gender|class="[^"]*(?:lst_wrp))',
        r"<article\b",
        r"<li\b",
    ):
        next_match = re.search(pattern, suffix, re.IGNORECASE | re.DOTALL)
        if next_match and next_match.start() > 80:
            context_end = end + next_match.start()
            break

    return html_text[context_start:context_end]


def _best_thumbnail(fragment: str, attrs: dict[str, str], base_url: str) -> Optional[str]:
    for key in ("data-thumb-image", "data-thumbnail", "data-preview", "data-image"):
        value = (attrs.get(key) or "").strip()
        if value:
            return urljoin(base_url, value)
    thumb = _first_image(fragment, base_url)
    if thumb and not thumb.startswith("data:image/svg+xml"):
        return thumb
    return thumb


def _subject_from_context(context: str) -> str:
    for pattern in (
        r"""class\s*=\s*["'][^"']*(?:subject|headline|topic)[^"']*["'][^>]*>(.*?)<""",
        r"""headlineMessage["']?\s*[:=]\s*["']([^"']+)["']""",
    ):
        match = re.search(pattern, context or "", re.IGNORECASE | re.DOTALL)
        if match:
            return _strip_html(match.group(1))[:240]
    return ""


class BrowserCaptureProvider(BaseProvider):
    capabilities = ProviderCapabilities(can_login=True, uses_browser=True)

    def __init__(
        self,
        source_type: str,
        display_name: str,
        url_templates: Iterable[str],
        domains: Iterable[str],
        session_store: Optional[ProviderSessionStore] = None,
        browser_root: Optional[Path] = None,
        login_templates: Optional[Iterable[str]] = None,
        discover_templates: Optional[Iterable[str]] = None,
        can_stream: bool = True,
        can_record: bool = True,
    ):
        super().__init__(session_store=session_store)
        self.source_type = source_type
        self.display_name = display_name
        self.url_templates = tuple(url_templates)
        self.domains = tuple(domains)
        self.browser_root = Path(browser_root or "data/provider-browser")
        self.login_templates = tuple(login_templates or self.url_templates)
        self.discover_templates = tuple(discover_templates or ())
        self.capabilities = ProviderCapabilities(
            can_login=True,
            can_discover=bool(self.discover_templates),
            can_stream=can_stream,
            can_record=can_record,
            uses_browser=True,
        )
        self._discover_cache: dict[tuple, tuple[float, dict]] = {}

    def canonical_url(self, target: str) -> str:
        target = (target or "").strip()
        if target.startswith("http://") or target.startswith("https://"):
            return target
        if not self.url_templates:
            return target
        return self.url_templates[0].format(username=quote_plus(target))

    def candidate_urls(self, target: str) -> list[str]:
        target = (target or "").strip()
        if target.startswith("http://") or target.startswith("https://"):
            return [target]
        encoded = quote_plus(target)
        return [template.format(username=encoded) for template in self.url_templates]

    def discover_urls(
        self,
        page: int,
        search: Optional[str],
        gender: Optional[str],
        tags: Optional[list[str]],
    ) -> list[str]:
        search = (search or "").strip()
        gender = (gender or "").strip().lower()
        first_tag = (tags or [""])[0] if tags else ""
        values = {
            "query": quote_plus(search),
            "username": quote_plus(search),
            "gender": quote_plus(gender),
            "tag": quote_plus(first_tag),
            "page": max(1, int(page or 1)),
        }
        urls: list[str] = []
        for template in self.discover_templates:
            if "{query}" in template and not search:
                continue
            try:
                url = template.format(**values)
            except KeyError:
                continue
            if url not in urls:
                urls.append(url)
        if search:
            for url in self.candidate_urls(search):
                if url not in urls:
                    urls.append(url)
        return urls

    async def list_live_models(self, **kwargs) -> dict[str, object]:
        if not self.discover_templates:
            return await super().list_live_models(**kwargs)

        page = max(1, int(kwargs.get("page") or 1))
        limit = max(1, int(kwargs.get("limit") or 24))
        gender = kwargs.get("gender") or None
        search = (kwargs.get("search") or "").strip()
        tags = kwargs.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        tags = [str(t).strip().lower() for t in (tags or []) if str(t).strip()]
        allow_browser = bool(kwargs.get("allow_browser", False))
        exact_search_fallback = bool(kwargs.get("exact_search_fallback", False))
        cache_key = (page, limit, gender or "", search, tuple(tags), allow_browser)
        ttl = int(os.getenv("PSTREAMREC_DISCOVER_CACHE_TTL", "90") or "90")
        cached = self._discover_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < ttl:
            return dict(cached[1])

        urls = self.discover_urls(page, search, gender, tags)
        items: list[dict[str, object]] = []
        for page_url in urls:
            html_text = await self._fetch_discover_html(page_url)
            if html_text:
                items.extend(self._parse_discover_models(html_text, page_url))

        if not items and allow_browser and urls:
            for page_url in urls[:2]:
                html_text = await self._fetch_discover_html_browser(page_url, search)
                if html_text:
                    items.extend(self._parse_discover_models(html_text, page_url))
                if items:
                    break

        if not items and search and exact_search_fallback:
            try:
                status = await self.check_status(search)
                if status.is_online:
                    items.append({
                        "username": search,
                        "display_name": search,
                        "thumbnail": status.thumbnail,
                        "viewers": status.viewers,
                        "subject": "",
                        "age": None,
                        "gender": "",
                        "is_online": True,
                        "tags": list(status.tags or []),
                        "room_status": status.room_status or "public",
                        "source_type": self.source_type,
                    })
            except Exception:
                pass

        items = self._dedupe_discover_models(items)
        if search:
            s = search.lower()
            items = [
                item for item in items
                if s in str(item.get("username") or "").lower()
                or s in str(item.get("display_name") or "").lower()
            ]
        if tags:
            items = [
                item for item in items
                if all(t in [str(x).lower() for x in (item.get("tags") or [])] for t in tags)
            ]

        items.sort(key=lambda item: int(item.get("viewers") or 0), reverse=True)
        total = len(items)
        total_pages = max(1, (total + limit - 1) // limit)
        start = (page - 1) * limit
        result = {
            "models": items[start:start + limit],
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
        }
        self._discover_cache[cache_key] = (time.monotonic(), result)
        return dict(result)

    async def resolve_stream(
        self, target: str, max_height: Optional[int] = None
    ) -> ResolvedStream:
        if self.source_type == "streamate":
            stream = await self._resolve_streamate_hls(target, max_height=max_height)
            if stream:
                return stream
        if self.source_type == "flirt4free":
            stream = await self._resolve_flirt4free_hls(target)
            if stream:
                return stream
        urls = self.candidate_urls(target)
        stream = await self._resolve_from_http(urls)
        if stream:
            return stream
        return await self._resolve_from_browser(urls, target)

    async def _resolve_streamate_hls(
        self,
        target: str,
        max_height: Optional[int] = None,
    ) -> Optional[ResolvedStream]:
        del max_height
        username = self._username_from_url(target) if target.startswith(("http://", "https://")) else (target or "").strip()
        username = (username or "").strip("@/ ")
        if not re.match(r"^[A-Za-z0-9_.-]{2,64}$", username):
            return None

        page_url = self.canonical_url(username)
        manifest_probe_url = f"https://manifest-server.naiadsystems.com/live/s:{quote_plus(username)}.json"
        cookie_header = await self._cookie_header(None)
        headers = self._stream_headers(page_url, cookie_header)
        headers["Accept"] = "application/json,text/plain,*/*"

        try:
            async with aiohttp_client_session(timeout=aiohttp.ClientTimeout(total=20)) as session:
                async with session.get(
                    manifest_probe_url,
                    headers=headers,
                    allow_redirects=True,
                    **aiohttp_request_kwargs(),
                ) as resp:
                    if resp.status == 404:
                        raise ProviderOfflineError(f"Aucun flux public Streamate pour {username}")
                    if resp.status in (401, 403):
                        raise ProviderInteractionRequired("Streamate demande une session ou une interaction")
                    if resp.status >= 400:
                        return None
                    payload = await resp.json(content_type=None)
                manifest_url = streamate_hls_manifest(payload)
                if not manifest_url:
                    return None
                async with session.get(
                    manifest_url,
                    headers=headers,
                    allow_redirects=True,
                    **aiohttp_request_kwargs(),
                ) as resp:
                    text = await resp.text(errors="ignore")
                    if resp.status >= 400 or "#EXTM3U" not in text:
                        return None
        except (ProviderOfflineError, ProviderInteractionRequired):
            raise
        except Exception as exc:
            logger.debug(
                "Streamate manifest probe failed",
                source_type=self.source_type,
                username=username,
                error=str(exc),
            )
            return None

        return ResolvedStream(
            url=manifest_url,
            headers=self._stream_headers(page_url, cookie_header),
            source_type=self.source_type,
            is_live=True,
            room_status="public",
            viewers=0,
            tags=[],
            thumbnail=None,
            title=None,
        )

    async def _resolve_from_http(self, urls: list[str]) -> Optional[ResolvedStream]:
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if self.session_store:
            cookie_header = await self.session_store.cookie_header(self.source_type)
            if cookie_header:
                headers["Cookie"] = cookie_header

        timeout = aiohttp.ClientTimeout(total=20)
        for page_url in urls:
            try:
                async with aiohttp_client_session(timeout=timeout) as session:
                    async with session.get(
                        page_url,
                        headers=headers,
                        allow_redirects=True,
                        **aiohttp_request_kwargs(),
                    ) as resp:
                        text = await resp.text(errors="ignore")
                        if resp.status >= 500:
                            continue
                        meta = _page_metadata(text, str(resp.url))
                        media_urls = extract_media_urls(text)
                        if media_urls:
                            stream_url = media_urls[0]
                            return ResolvedStream(
                                url=stream_url,
                                headers=self._stream_headers(str(resp.url), headers.get("Cookie")),
                                source_type=self.source_type,
                                is_live=True,
                                room_status="public",
                                viewers=int(meta.get("viewers") or 0),
                                tags=list(meta.get("tags") or []),
                                thumbnail=meta.get("thumbnail") or None,
                                title=meta.get("title") or None,
                            )
                        if resp.status in (401, 403):
                            continue
            except Exception as exc:
                logger.debug(
                    "Provider HTTP probe failed",
                    source_type=self.source_type,
                    url=page_url,
                    error=str(exc),
                )
        return None

    async def _fetch_discover_html(self, page_url: str) -> str:
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": page_url,
        }
        if self.session_store:
            cookie_header = await self.session_store.cookie_header(self.source_type)
            if cookie_header:
                headers["Cookie"] = cookie_header
        try:
            timeout = aiohttp.ClientTimeout(total=int(os.getenv("PSTREAMREC_DISCOVER_HTTP_TIMEOUT", "12") or "12"))
            async with aiohttp_client_session(timeout=timeout) as session:
                async with session.get(
                    page_url,
                    headers=headers,
                    allow_redirects=True,
                    **aiohttp_request_kwargs(),
                ) as resp:
                    if resp.status >= 500 or resp.status in (401, 403):
                        return ""
                    return await resp.text(errors="ignore")
        except Exception as exc:
            logger.debug(
                "Provider discover HTTP failed",
                source_type=self.source_type,
                url=page_url,
                error=str(exc),
            )
            return ""

    async def _fetch_discover_html_browser(self, page_url: str, search: str = "") -> str:
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except Exception:
            return ""

        timeout_seconds = int(os.getenv("PSTREAMREC_DISCOVER_BROWSER_TIMEOUT", "10") or "10")
        headless = os.getenv("PSTREAMREC_BROWSER_HEADLESS", "true").lower() not in {"0", "false", "no"}
        user_data_dir = self.browser_root / self.source_type
        user_data_dir.mkdir(parents=True, exist_ok=True)

        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                str(user_data_dir),
                headless=headless,
                user_agent=DEFAULT_USER_AGENT,
                viewport={"width": 1280, "height": 900},
            )
            try:
                await self._restore_cookies(context)
                page = context.pages[0] if context.pages else await context.new_page()
                json_payloads: list[str] = []
                capture_tasks: list[asyncio.Task] = []

                async def capture_json(response) -> None:
                    try:
                        headers = {k.lower(): v for k, v in response.headers.items()}
                        content_type = headers.get("content-type", "")
                        url = response.url
                        if "json" not in content_type and not re.search(
                            r"/(?:api|v\d+|ajax|models?|performers?|search|fallback)(?:[/?#]|$)",
                            url,
                            re.IGNORECASE,
                        ):
                            return
                        text = await response.text()
                        if not text or len(text) > 1500000:
                            return
                        stripped = text.lstrip()
                        if not stripped.startswith(("{", "[")):
                            return
                        json_payloads.append(
                            '<script type="application/json" data-pstreamrec-url="{}">{}</script>'.format(
                                html.escape(url, quote=True),
                                html.escape(text),
                            )
                        )
                    except Exception:
                        return

                def schedule_capture(response) -> None:
                    capture_tasks.append(asyncio.create_task(capture_json(response)))

                page.on("response", schedule_capture)
                try:
                    await page.goto(page_url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
                except PlaywrightTimeoutError:
                    pass
                except Exception as exc:
                    logger.debug(
                        "Provider discover browser navigation failed",
                        source_type=self.source_type,
                        url=page_url,
                        error=str(exc),
                    )
                    return ""
                await self._dismiss_common_prompts(page)
                if search:
                    await self._try_search(page, search)
                wait_ms = 5000 if self.source_type in {"myfreecams", "stripchat", "xcams"} else 1800
                await page.wait_for_timeout(wait_ms)
                if capture_tasks:
                    await asyncio.gather(*capture_tasks, return_exceptions=True)
                content = await page.content()
                if json_payloads:
                    content += "\n" + "\n".join(json_payloads)
                return content
            finally:
                await context.close()

    def _parse_discover_models(self, html_text: str, page_url: str) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        specialized_parser = {
            "camsoda": self._parse_camsoda_models,
            "cams": self._parse_cams_models,
            "flirt4free": self._parse_flirt4free_models,
            "livejasmin": self._parse_livejasmin_models,
            "myfreecams": self._parse_myfreecams_models,
            "stripchat": self._parse_stripchat_models,
            "streamate": self._parse_streamate_models,
            "xcams": self._parse_xcams_models,
        }.get(self.source_type)
        if specialized_parser:
            items.extend(specialized_parser(html_text, page_url))
            if items or self.source_type in {"camsoda", "cams", "flirt4free", "livejasmin", "myfreecams", "streamate", "xcams"}:
                return items
        for match in _ANCHOR_RE.finditer(html_text or ""):
            attrs = _parse_attrs(match.group("attrs"))
            href = attrs.get("href") or ""
            username = self._username_from_url(urljoin(page_url, href))
            if not username:
                continue
            body = match.group("body") or ""
            context = _anchor_context(html_text or "", match)
            context_attrs = {**_parse_attrs(context[:1200]), **attrs}
            display_name = (
                context_attrs.get("data-username")
                or context_attrs.get("data-profile")
                or context_attrs.get("data-name")
                or attrs.get("title")
                or attrs.get("aria-label")
                or _strip_html(body)
                or username
            )
            display_name = re.sub(r"\s+", " ", display_name).strip()
            if len(display_name) > 80 or display_name.lower() in _RESERVED_PROFILE_SEGMENTS:
                display_name = username
            thumbnail = _best_thumbnail(context or body, context_attrs, page_url)
            viewers = max(_viewer_count(body), _viewer_count(context))
            tags = _extract_tags(context or body, context_attrs)
            if not thumbnail and viewers <= 0:
                continue
            items.append({
                "username": username,
                "display_name": display_name,
                "thumbnail": thumbnail,
                "viewers": viewers,
                "subject": _subject_from_context(context),
                "age": None,
                "gender": "",
                "is_online": True,
                "tags": tags,
                "room_status": "public",
                "source_type": self.source_type,
            })
        return items

    def _parse_stripchat_models(self, html_text: str, page_url: str) -> list[dict[str, object]]:
        payloads = self._embedded_json_payloads(html_text)
        models: list[dict[str, object]] = []
        for payload in payloads:
            for block in self._find_dicts_with_key(payload, "models"):
                block_models = block.get("models")
                if isinstance(block_models, list):
                    models.extend(model for model in block_models if isinstance(model, dict))
        items: list[dict[str, object]] = []
        for model in models:
            username = str(model.get("username") or "").strip()
            if not username:
                continue
            status = str(model.get("status") or "").lower()
            if status and status != "public":
                room_status = status
            else:
                room_status = "public"
            tags = self._stripchat_tags(model)
            thumbnail = self._stripchat_thumbnail(model)
            items.append({
                "username": username,
                "display_name": username,
                "thumbnail": thumbnail,
                "viewers": int(model.get("viewersCount") or model.get("viewers") or 0),
                "subject": str(model.get("groupShowTopic") or ""),
                "age": None,
                "gender": str(model.get("genderGroup") or model.get("gender") or "").lower(),
                "is_online": bool(model.get("isOnline", model.get("isLive", True))),
                "tags": tags,
                "room_status": room_status,
                "source_type": self.source_type,
            })
        return self._dedupe_discover_models(items)

    def _stripchat_tags(self, model: dict[str, object]) -> list[str]:
        gender = str(model.get("gender") or model.get("broadcastGender") or model.get("genderGroup") or "").strip()
        gender_map = {
            "f": "female",
            "female": "female",
            "females": "female",
            "m": "male",
            "male": "male",
            "males": "male",
            "t": "trans",
            "trans": "trans",
            "femaleTranny": "trans",
            "tranny": "trans",
            "maleFemale": "couple",
            "group": "group",
        }
        values: list[object] = [gender_map.get(gender, gender)]
        broadcast_gender = str(model.get("broadcastGender") or "").strip()
        if broadcast_gender and broadcast_gender != gender:
            values.append(gender_map.get(broadcast_gender, broadcast_gender))
        country = str(model.get("country") or "").strip()
        if country:
            values.append(country)
        if model.get("isHd"):
            values.append("hd")
        if model.get("isVr"):
            values.append("vr")
        if model.get("isMobile"):
            values.append("mobile")
        if model.get("isNew"):
            values.append("new")
        if model.get("isLovense"):
            values.append("lovense")
        if model.get("isKiiroo"):
            values.append("kiiroo")
        if model.get("isNonNude"):
            values.append("non nude")
        if str(model.get("status") or "").lower() == "public":
            values.append("public")
        values.extend(_subject_keyword_tags(str(model.get("groupShowTopic") or "")))
        return _normalize_tags(values)

    def _stripchat_thumbnail(self, model: dict[str, object]) -> Optional[str]:
        model_id = str(model.get("id") or model.get("streamName") or "").strip()
        timestamp = str(model.get("snapshotTimestamp") or model.get("verifiedSnapshotTimestamp") or "").strip()
        if model_id and timestamp:
            return f"https://img.doppiocdn.net/snapshot/{model_id}/{timestamp}"
        for key in ("previewUrlThumbSmall", "avatarUrl"):
            value = str(model.get(key) or "").strip()
            if not value:
                continue
            if value.startswith(("http://", "https://")):
                return value
            if value.startswith("/"):
                return f"https://img.doppiocdn.net{value}"
        return None

    def _parse_camsoda_models(self, html_text: str, page_url: str) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        for match in re.finditer(
            r'<a\b(?P<attrs>[^>]*data-username\s*=\s*["\'][^"\']+["\'][^>]*)>(?P<body>.*?)(?=<a\b[^>]*data-username\s*=|</main>|<footer|\Z)',
            html_text or "",
            re.IGNORECASE | re.DOTALL,
        ):
            attrs = _parse_attrs(match.group("attrs"))
            username = (attrs.get("data-username") or "").strip()
            if not username:
                continue
            body = match.group("body") or ""
            subject = _subject_from_context(body)
            display_name_match = re.search(r"displayName[^>]*>(.*?)<", body, re.IGNORECASE | re.DOTALL)
            display_name = _strip_html(display_name_match.group(1)) if display_name_match else username
            tags = _normalize_tags(_extract_tags(match.group(0), attrs) + _subject_keyword_tags(subject) + ["female"])
            items.append({
                "username": username,
                "display_name": display_name or username,
                "thumbnail": _best_thumbnail(body, attrs, page_url),
                "viewers": _viewer_count(body),
                "subject": subject,
                "age": None,
                "gender": "female",
                "is_online": True,
                "tags": tags,
                "room_status": "public",
                "source_type": self.source_type,
            })
        return items

    def _parse_flirt4free_models(self, html_text: str, page_url: str) -> list[dict[str, object]]:
        home_models = self._flirt4free_homepage_models(html_text)
        if home_models:
            items = []
            for model in home_models:
                username = str(model.get("model_seo_name") or model.get("model_name") or "").strip()
                if not username:
                    continue
                room_status = str(model.get("room_status") or "").strip().lower()
                is_public = str(model.get("room_status_char") or "").upper() == "O" or "open" in room_status
                if not is_public:
                    continue
                category_tags = [
                    model.get("category_name"),
                    model.get("category_name_2"),
                    model.get("category_name_3"),
                    model.get("login_group_title"),
                    model.get("credit_tier"),
                    model.get("languages"),
                    model.get("country_code"),
                    "hd" if str(model.get("is_high_quality") or "") == "1" else "",
                    "new" if str(model.get("is_new") or "") not in {"", "0"} else "",
                    "fetish" if str(model.get("is_fetish") or "").upper() == "Y" else "",
                ]
                tags = _normalize_tags(category_tags)
                image_id = str(model.get("sample_image_id") or "").strip()
                thumbnail = None
                if image_id:
                    thumbnail = f"https://cdn5-images.vscdns.com/images/photos2/{image_id[-3:]}/{image_id}/small.jpg"
                items.append({
                    "username": username,
                    "display_name": str(model.get("display") or username),
                    "thumbnail": thumbnail,
                    "viewers": _parse_count(str(model.get("viewer_count") or model.get("viewers") or 0)),
                    "subject": str(model.get("scheduled_info", {}).get("title") or "") if isinstance(model.get("scheduled_info"), dict) else "",
                    "age": _parse_count(str(model.get("age") or 0)) or None,
                    "gender": "female",
                    "is_online": True,
                    "tags": tags or ["female", "public"],
                    "room_status": "public",
                    "source_type": self.source_type,
                })
            if items:
                return self._dedupe_discover_models(items)

        items: list[dict[str, object]] = []
        for match in re.finditer(
            r'<div\b(?P<attrs>[^>]*class\s*=\s*["\'][^"\']*model-container-home[^"\']*["\'][^>]*)>(?P<body>.*?)(?=<div\b[^>]*class\s*=\s*["\'][^"\']*model-container-home|</body>|\Z)',
            html_text or "",
            re.IGNORECASE | re.DOTALL,
        ):
            attrs = _parse_attrs(match.group("attrs"))
            body = match.group("body") or ""
            username_match = re.search(r'href\s*=\s*["\'][^"\']*[?&]model=([^"&\']+)', body, re.IGNORECASE)
            if not username_match:
                continue
            username = unquote(username_match.group(1)).strip()
            title_match = re.search(r'<img\b[^>]*\balt\s*=\s*["\']([^"\']+)["\']', body, re.IGNORECASE)
            display_name = html.unescape(title_match.group(1)).strip() if title_match else username.replace("-", " ")
            tags = ["female"]
            class_value = attrs.get("class") or ""
            if "partyRoom" in class_value:
                tags.append("group")
            if "openRoom" in class_value:
                tags.append("public")
            if "tip-controlled" in body or "lovense" in body.lower():
                tags.append("toy")
            if "new-model" in body or " new " in f" {class_value.lower()} ":
                tags.append("new")
            items.append({
                "username": username,
                "display_name": display_name,
                "thumbnail": _first_image(body, page_url),
                "viewers": _viewer_count(body),
                "subject": "",
                "age": None,
                "gender": "female",
                "is_online": True,
                "tags": _normalize_tags(tags),
                "room_status": "public",
                "source_type": self.source_type,
            })
        if items:
            return items
        for match in re.finditer(
            r'<div\b(?P<attrs>[^>]*class\s*=\s*["\'][^"\']*model-container[^"\']*["\'][^>]*)>(?P<body>.*?)(?=<div\b[^>]*class\s*=\s*["\'][^"\']*model-container|</body>|\Z)',
            html_text or "",
            re.IGNORECASE | re.DOTALL,
        ):
            body = match.group("body") or ""
            href_match = re.search(r'href\s*=\s*["\']/models/bios/([^/"\']+)/about\.php', body, re.IGNORECASE)
            if not href_match:
                continue
            slug = unquote(href_match.group(1)).strip()
            if not slug:
                continue
            title_match = re.search(r'<img\b[^>]*\balt\s*=\s*["\']([^"\']+)["\']', body, re.IGNORECASE)
            display_name = html.unescape(title_match.group(1)).strip() if title_match else slug.replace("-", " ")
            category_tags = [
                html.unescape(category).strip()
                for category in re.findall(r'title\s*=\s*["\']Category\s+([^"\']+)["\']', body, re.IGNORECASE)
            ]
            subject = _strip_html(body)[:240]
            tags = _normalize_tags(category_tags + _subject_keyword_tags(subject) + ["female"])
            items.append({
                "username": slug,
                "display_name": display_name,
                "thumbnail": _first_image(body, page_url),
                "viewers": _viewer_count(body),
                "subject": "",
                "age": None,
                "gender": "female",
                "is_online": True,
                "tags": tags,
                "room_status": "public",
                "source_type": self.source_type,
            })
        if not items:
            for match in _ANCHOR_RE.finditer(html_text or ""):
                attrs = _parse_attrs(match.group("attrs"))
                href = attrs.get("href") or ""
                href_match = re.search(r"/models/([^/\"']+)\.html?$", href, re.IGNORECASE)
                if not href_match:
                    continue
                username = unquote(href_match.group(1)).strip()
                body = match.group("body") or ""
                if not username or username.lower() in _RESERVED_PROFILE_SEGMENTS:
                    continue
                items.append({
                    "username": username,
                    "display_name": attrs.get("title") or _strip_html(body) or username,
                    "thumbnail": _first_image(body, page_url),
                    "viewers": _viewer_count(body),
                    "subject": "",
                    "age": None,
                    "gender": "female",
                    "is_online": True,
                    "tags": ["female"],
                    "room_status": "public",
                    "source_type": self.source_type,
                })
        return items

    async def _resolve_flirt4free_hls(self, target: str) -> Optional[ResolvedStream]:
        slug = self._flirt4free_slug(target)
        if not slug:
            return None

        pages = list(self.discover_urls(1, None, None, None))
        if not pages:
            pages = ["https://www.flirt4free.com/live/girls/"]
        pages.append(f"https://www.flirt4free.com/search?q={quote_plus(slug)}")

        matched: Optional[dict[str, object]] = None
        referer = "https://www.flirt4free.com/live/girls/"
        for page_url in pages:
            html_text = await self._fetch_discover_html(page_url)
            if not html_text:
                continue
            for model in self._flirt4free_homepage_models(html_text):
                if self._flirt4free_model_matches(model, slug):
                    matched = model
                    referer = page_url
                    break
            if matched:
                break
        if not matched:
            return None

        if str(matched.get("video_blocked") or "0") == "1" or str(matched.get("is_hls") or "") != "1":
            raise ProviderPrivateError("Flirt4Free ne fournit pas de HLS public pour ce modele")
        room_status = str(matched.get("room_status") or "").lower()
        if str(matched.get("room_status_char") or "").upper() != "O" and "open" not in room_status:
            raise ProviderPrivateError("Le modele Flirt4Free n'est pas en room publique")

        model_id = str(matched.get("model_id") or "").strip()
        video_host = str(matched.get("video_host") or "").strip()
        if not model_id or not video_host:
            return None

        stream_data = await self._fetch_flirt4free_stream_data(model_id, video_host, referer)
        media_rows = []
        data = stream_data.get("data") if isinstance(stream_data, dict) else {}
        if isinstance(data, dict):
            for key in ("llhls", "hls"):
                rows = data.get(key)
                if isinstance(rows, list):
                    media_rows.extend(row for row in rows if isinstance(row, dict))

        headers = self._stream_headers(referer, await self._provider_cookie_header())
        for row in media_rows:
            stream_url = str(row.get("url") or "").strip().replace("\\/", "/")
            if stream_url.startswith("//"):
                stream_url = "https:" + stream_url
            if not stream_url:
                continue
            if await self._is_valid_playlist(stream_url, headers):
                tags = _normalize_tags([
                    matched.get("category_name"),
                    matched.get("category_name_2"),
                    matched.get("category_name_3"),
                    matched.get("login_group_title"),
                    matched.get("credit_tier"),
                    "hd" if str(matched.get("is_high_quality") or "") == "1" else "",
                    "public",
                ])
                return ResolvedStream(
                    url=stream_url,
                    headers=headers,
                    source_type=self.source_type,
                    is_live=True,
                    room_status="public",
                    viewers=_parse_count(str(matched.get("viewer_count") or matched.get("viewers") or 0)),
                    tags=tags,
                    thumbnail=None,
                    title=str(matched.get("display") or slug),
                )
        raise ProviderOfflineError("Aucun HLS Flirt4Free public valide")

    async def _fetch_flirt4free_stream_data(self, model_id: str, video_host: str, referer: str) -> dict[str, object]:
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
            "Referer": referer,
        }
        cookie_header = await self._provider_cookie_header()
        if cookie_header:
            headers["Cookie"] = cookie_header
        url = "https://www.flirt4free.com/ws/chat/get-stream-urls.php?" + urlencode({
            "model_id": model_id,
            "video_host": video_host,
        })
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp_client_session(timeout=timeout) as session:
            async with session.get(url, headers=headers, **aiohttp_request_kwargs()) as resp:
                if resp.status >= 400:
                    raise ProviderOfflineError(f"Flirt4Free stream endpoint HTTP {resp.status}")
                payload = await resp.json(content_type=None)
        return payload if isinstance(payload, dict) else {}

    async def _is_valid_playlist(self, stream_url: str, headers: dict[str, str]) -> bool:
        timeout = aiohttp.ClientTimeout(total=12)
        try:
            async with aiohttp_client_session(timeout=timeout) as session:
                async with session.get(stream_url, headers=headers, **aiohttp_request_kwargs()) as resp:
                    if resp.status >= 400:
                        return False
                    head = await resp.content.read(256)
        except Exception:
            return False
        return head.lstrip().startswith(b"#EXTM3U") or b"<MPD" in head[:256]

    async def _provider_cookie_header(self) -> str:
        if not self.session_store:
            return ""
        try:
            return await self.session_store.cookie_header(self.source_type)
        except Exception:
            return ""

    def _flirt4free_homepage_models(self, html_text: str) -> list[dict[str, object]]:
        source = html_text or ""
        marker = source.find("window.__homePageData__")
        if marker >= 0:
            source = source[marker:]
        array_text = self._extract_js_array_after_key(source, "models")
        if not array_text:
            return []
        array_text = re.sub(r",\s*([\]}])", r"\1", array_text)
        try:
            data = json.loads(array_text)
        except Exception:
            return []
        return [item for item in data if isinstance(item, dict)]

    def _extract_js_array_after_key(self, text: str, key: str) -> str:
        key_match = re.search(rf"['\"]{re.escape(key)}['\"]\s*:\s*\[", text or "")
        if not key_match:
            return ""
        start = key_match.end() - 1
        depth = 0
        quote = ""
        escaped = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if quote:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == quote:
                    quote = ""
                continue
            if ch in {"'", '"'}:
                quote = ch
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return text[start:idx + 1]
        return ""

    def _flirt4free_slug(self, target: str) -> str:
        raw = (target or "").strip()
        if raw.startswith(("http://", "https://")):
            username = self._username_from_url(raw) or raw.rstrip("/").rsplit("/", 1)[-1]
        else:
            username = raw
        username = re.sub(r"\.html?$", "", username)
        return username.strip().lower().replace("_", "-")

    def _flirt4free_model_matches(self, model: dict[str, object], slug: str) -> bool:
        candidates = [
            model.get("model_seo_name"),
            str(model.get("model_name") or "").replace("_", "-"),
            str(model.get("display") or "").replace(" ", "-"),
        ]
        normalized = {str(value or "").strip().lower().replace("_", "-") for value in candidates}
        return slug in normalized

    def _parse_xcams_models(self, html_text: str, page_url: str) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        for match in re.finditer(
            r'<li\b(?P<attrs>[^>]*data-testid\s*=\s*["\']model-card["\'][^>]*)>(?P<body>.*?)(?=<li\b[^>]*data-testid\s*=\s*["\']model-card["\']|</ul>|\Z)',
            html_text or "",
            re.IGNORECASE | re.DOTALL,
        ):
            attrs = _parse_attrs(match.group("attrs"))
            body = match.group("body") or ""
            username = (attrs.get("data-cammer-nickname") or "").strip()
            if not username:
                profile_match = re.search(r'href\s*=\s*["\']/profile/([^/"\']+)/', body, re.IGNORECASE)
                username = unquote(profile_match.group(1)).strip() if profile_match else ""
            if not username:
                continue
            tags = ["female"]
            if "models__features--toy" in body or "toy--icon" in body:
                tags.append("toy")
            if "models__labels__item--new" in body:
                tags.append("new")
            if "chat-mode models__labels__item--free" in body or re.search(r">\s*FREE\s*<", body, re.IGNORECASE):
                tags.append("free")
            thumbnail = _first_image(body, page_url)
            items.append({
                "username": username,
                "display_name": username,
                "thumbnail": thumbnail,
                "viewers": _viewer_count(body),
                "subject": "",
                "age": None,
                "gender": "female",
                "is_online": True,
                "tags": _normalize_tags(tags),
                "room_status": "public",
                "source_type": self.source_type,
            })
        return items

    def _parse_cams_models(self, html_text: str, page_url: str) -> list[dict[str, object]]:
        data = self._next_data_json(html_text)
        compressed = self._find_first_dict(data, "compressedWonResponse") if data else None
        if not compressed:
            return []
        mapping = compressed.get("mapping") or []
        rows = compressed.get("models") or []
        items: list[dict[str, object]] = []
        for row in rows:
            if not isinstance(row, list):
                continue
            model = {key: row[idx] if idx < len(row) else None for idx, key in enumerate(mapping)}
            username = str(model.get("screen_name") or model.get("stream_name") or "").strip()
            if not username:
                continue
            gender = str(model.get("gender") or "").upper()
            tags: list[str] = []
            if gender == "F":
                tags.append("female")
            elif gender == "M":
                tags.append("male")
            elif gender == "TS":
                tags.append("trans")
            try:
                age = int(model.get("public_age") or 0)
            except (TypeError, ValueError):
                age = 0
            if 18 <= age <= 29:
                tags.append("18-29")
            elif 30 <= age <= 39:
                tags.append("30-39")
            elif 40 <= age <= 49:
                tags.append("40-49")
            elif age >= 50:
                tags.append("50+")
            if str(model.get("hq_enabled") or "") == "2":
                tags.append("hd")
            if str(model.get("vr") or "") == "1":
                tags.append("vr")
            image_id = str(model.get("image_pg") or "").strip()
            thumbnail = ""
            if image_id:
                thumbnail = f"https://images4.streamray.com/images/streamray/streams/{username.lower()}_640.gif"
            items.append({
                "username": username,
                "display_name": username,
                "thumbnail": thumbnail or None,
                "viewers": int(model.get("viewer_count") or model.get("viewers") or 0),
                "subject": "",
                "age": age or None,
                "gender": gender.lower(),
                "is_online": True,
                "tags": _normalize_tags(tags),
                "room_status": "public",
                "source_type": self.source_type,
            })
        return items

    def _parse_streamate_models(self, html_text: str, page_url: str) -> list[dict[str, object]]:
        payloads = self._embedded_json_payloads(html_text)
        performers: list[dict[str, object]] = []
        for payload in payloads:
            performers.extend(self._find_dicts_with_key(payload, "nickname"))
        items: list[dict[str, object]] = []
        for performer in performers:
            username = str(performer.get("nickname") or "").strip()
            if not username:
                continue
            subject = str(performer.get("headlineMessage") or performer.get("headline") or "")
            tags = [
                performer.get("categoryName"),
                performer.get("gender"),
                performer.get("country"),
            ]
            languages = performer.get("languages") or performer.get("languagesSpoken") or []
            if isinstance(languages, list):
                tags.extend(languages)
            if performer.get("new"):
                tags.append("new")
            if performer.get("hd") or performer.get("isHD"):
                tags.append("hd")
            tags = _normalize_tags(tags + _subject_keyword_tags(subject))
            thumbnail = (
                performer.get("thumbnail")
                or performer.get("thumbnailUrl")
                or performer.get("previewUrl")
                or performer.get("image")
            )
            items.append({
                "username": username,
                "display_name": username,
                "thumbnail": thumbnail,
                "viewers": int(performer.get("viewerCount") or performer.get("viewers") or 0),
                "subject": subject,
                "age": performer.get("age"),
                "gender": str(performer.get("gender") or ""),
                "is_online": bool(performer.get("online", True)),
                "tags": tags,
                "room_status": "public",
                "source_type": self.source_type,
            })
        if items:
            return self._dedupe_discover_models(items)
        return []

    def _embedded_json_payloads(self, html_text: str) -> list[object]:
        payloads: list[object] = []
        for match in re.finditer(r'<script\b[^>]*type\s*=\s*["\']application/json["\'][^>]*>(.*?)</script>', html_text or "", re.IGNORECASE | re.DOTALL):
            raw = html.unescape(match.group(1) or "").strip()
            if not raw:
                continue
            try:
                payloads.append(json.loads(raw))
            except Exception:
                continue
        return payloads

    def _next_data_json(self, html_text: str) -> Optional[dict]:
        match = re.search(r'<script\b[^>]*id\s*=\s*["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html_text or "", re.IGNORECASE | re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(html.unescape(match.group(1)))
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _find_first_dict(self, value: object, key: str) -> Optional[dict]:
        if isinstance(value, dict):
            if key in value and isinstance(value[key], dict):
                return value[key]
            for child in value.values():
                found = self._find_first_dict(child, key)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = self._find_first_dict(child, key)
                if found:
                    return found
        return None

    def _find_dicts_with_key(self, value: object, key: str) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        if isinstance(value, dict):
            if key in value:
                found.append(value)
            for child in value.values():
                found.extend(self._find_dicts_with_key(child, key))
        elif isinstance(value, list):
            for child in value:
                found.extend(self._find_dicts_with_key(child, key))
        return found

    def _dedupe_discover_models(self, items: list[dict[str, object]]) -> list[dict[str, object]]:
        by_username: dict[str, dict[str, object]] = {}
        for item in items:
            username = str(item.get("username") or "").strip().lower()
            if not username:
                continue
            existing = by_username.get(username)
            if not existing:
                by_username[username] = item
                continue
            if not existing.get("thumbnail") and item.get("thumbnail"):
                existing["thumbnail"] = item["thumbnail"]
            existing["viewers"] = max(int(existing.get("viewers") or 0), int(item.get("viewers") or 0))
            existing["tags"] = _normalize_tags(list(existing.get("tags") or []) + list(item.get("tags") or []))
        return list(by_username.values())

    def _parse_myfreecams_models(self, html_text: str, page_url: str) -> list[dict[str, object]]:
        global_tags = _normalize_tags(re.findall(r">([A-Za-z0-9][A-Za-z0-9_-]{1,47})</a>\s*<a[^>]*>\s*×", html_text or ""))
        items: list[dict[str, object]] = []
        for match in re.finditer(
            r'<div\b(?P<attrs>[^>]*(?:model_online|modelbox_)[^>]*)>(?P<body>.*?)(?=<div\b[^>]*(?:model_online|modelbox_)|</body>|\Z)',
            html_text or "",
            re.IGNORECASE | re.DOTALL,
        ):
            attrs = _parse_attrs(match.group("attrs"))
            body = match.group("body") or ""
            title_match = re.search(r"""title\s*=\s*["']Enter Chat Room of ([^"']+)["']""", body, re.IGNORECASE)
            username = title_match.group(1).strip() if title_match else ""
            if not username:
                text_lines = [line.strip() for line in _strip_html(body).split(" ") if line.strip()]
                username = next((line for line in text_lines if re.match(r"^[A-Za-z0-9_.-]{2,64}$", line)), "")
            if not username:
                continue
            if "%" in username or username.lower() in {"var", "username", "model"}:
                continue
            uid_match = re.search(r"""modelbox_(\d+)|data-uid\s*=\s*["']?(\d+)""", match.group(0), re.IGNORECASE)
            uid = next((g for g in (uid_match.groups() if uid_match else ()) if g), "")
            thumbnail = _best_thumbnail(body, attrs, page_url)
            if thumbnail and "%" in thumbnail:
                thumbnail = None
            if not thumbnail and uid:
                thumbnail = f"https://img.mfcimg.com/photos2/{uid[:3]}/{uid}/avatar.300x300.jpg"
            tags = _normalize_tags(global_tags + _extract_tags(body, attrs))
            items.append({
                "username": username,
                "display_name": username,
                "thumbnail": thumbnail,
                "viewers": _viewer_count(body),
                "subject": "",
                "age": None,
                "gender": "",
                "is_online": True,
                "tags": tags,
                "room_status": "public",
                "source_type": self.source_type,
            })
        return items

    def _parse_livejasmin_models(self, html_text: str, page_url: str) -> list[dict[str, object]]:
        match = re.search(
            r"listPagePerformers\s*=\s*(\[[\s\S]*?\]);",
            html_text or "",
            re.IGNORECASE,
        )
        if not match:
            return []
        try:
            performers = json.loads(match.group(1))
        except Exception:
            return []
        items = []
        for performer in performers:
            username = str(performer.get("display_name") or "").strip()
            if not username:
                continue
            willingnesses = performer.get("willingnesses") or {}
            tags = list(willingnesses.values()) if isinstance(willingnesses, dict) else []
            if performer.get("language"):
                tags.append(str(performer.get("language")).replace("lng_", ""))
            if performer.get("region"):
                tags.append(performer.get("region"))
            items.append({
                "username": username,
                "display_name": username,
                "thumbnail": performer.get("profilePictureUrl"),
                "viewers": int(performer.get("viewers") or performer.get("num_users") or 0),
                "subject": "",
                "age": None,
                "gender": performer.get("main_category") or "",
                "is_online": int(performer.get("status") or 0) == 1,
                "tags": _normalize_tags(tags),
                "room_status": "public",
                "source_type": self.source_type,
            })
        return items

    def _username_from_url(self, value: str) -> Optional[str]:
        parsed = urlparse(value or "")
        host = parsed.netloc.lower().removeprefix("www.")
        if self.domains and not any(host == d or host.endswith("." + d) for d in self.domains):
            return None
        path_parts = [unquote(p).strip() for p in parsed.path.split("/") if p.strip()]
        candidate = ""
        if self.source_type == "myfreecams":
            hash_parts = [unquote(p).strip() for p in parsed.fragment.split("/") if p.strip()]
            if hash_parts:
                candidate = hash_parts[0]
            elif path_parts and len(path_parts) >= 2 and path_parts[1] == "chat":
                candidate = path_parts[0]
            elif path_parts:
                candidate = path_parts[-1]
        elif self.source_type == "livejasmin":
            for marker in ("girls", "chat"):
                if marker in path_parts:
                    idx = path_parts.index(marker)
                    if len(path_parts) > idx + 1:
                        candidate = path_parts[idx + 1]
                        break
            if not candidate and path_parts:
                candidate = path_parts[-1]
        elif self.source_type == "streamate":
            if "cam" in path_parts:
                idx = path_parts.index("cam")
                if len(path_parts) > idx + 1:
                    candidate = path_parts[idx + 1]
            elif path_parts:
                candidate = path_parts[-1]
        elif self.source_type == "xcams":
            for marker in ("chat", "profile"):
                if marker in path_parts:
                    idx = path_parts.index(marker)
                    if len(path_parts) > idx + 1:
                        candidate = path_parts[idx + 1]
                        break
            if not candidate and path_parts:
                candidate = path_parts[-1]
        elif self.source_type == "flirt4free":
            if len(path_parts) >= 4 and path_parts[0] == "models" and path_parts[1] == "bios":
                candidate = path_parts[2]
            elif "models" in path_parts:
                idx = path_parts.index("models")
                if len(path_parts) > idx + 1:
                    candidate = path_parts[idx + 1]
            else:
                return None
            candidate = re.sub(r"\.html?$", "", candidate)
        else:
            if "model" in path_parts:
                idx = path_parts.index("model")
                if len(path_parts) > idx + 1:
                    candidate = path_parts[idx + 1]
            elif "models" in path_parts:
                idx = path_parts.index("models")
                if len(path_parts) > idx + 1:
                    candidate = path_parts[idx + 1]
            elif path_parts:
                candidate = path_parts[0]

        candidate = candidate.strip("@/ ").split("?", 1)[0].split("#", 1)[0]
        candidate = re.sub(r"\.html?$", "", candidate)
        if not re.match(r"^[A-Za-z0-9_.-]{2,64}$", candidate or ""):
            return None
        if candidate.lower() in _RESERVED_PROFILE_SEGMENTS:
            return None
        return candidate

    async def _resolve_from_browser(self, urls: list[str], target: str) -> ResolvedStream:
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except Exception as exc:
            raise ProviderError("Playwright n'est pas installe") from exc

        timeout_seconds = int(os.getenv("PSTREAMREC_BROWSER_CAPTURE_TIMEOUT", "25") or "25")
        headless = os.getenv("PSTREAMREC_BROWSER_HEADLESS", "true").lower() not in {"0", "false", "no"}
        user_data_dir = self.browser_root / self.source_type
        user_data_dir.mkdir(parents=True, exist_ok=True)
        captured: list[str] = []

        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                str(user_data_dir),
                headless=headless,
                user_agent=DEFAULT_USER_AGENT,
                viewport={"width": 1280, "height": 720},
            )
            try:
                await self._restore_cookies(context)
                page = context.pages[0] if context.pages else await context.new_page()

                def remember(url: str) -> None:
                    if self._is_media_url(url) and url not in captured:
                        captured.append(url)

                def remember_hls_field_urls(text: str) -> None:
                    for url in extract_hls_url_fields(text):
                        if url not in captured:
                            captured.append(url)

                try:
                    cdp_session = await context.new_cdp_session(page)
                    await cdp_session.send("Network.enable")

                    def remember_ws_frame(event) -> None:
                        payload = ((event.get("response") or {}).get("payloadData") or "")
                        remember_hls_field_urls(payload)
                        for url in extract_media_urls(payload):
                            remember(url)

                    cdp_session.on("Network.webSocketFrameReceived", remember_ws_frame)
                except Exception as exc:
                    logger.debug(
                        "Provider browser CDP capture unavailable",
                        source_type=self.source_type,
                        error=str(exc),
                    )

                page.on("request", lambda request: remember(request.url))
                page.on("response", lambda response: remember(response.url))

                last_html = ""
                for page_url in urls:
                    try:
                        await page.goto(page_url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
                    except PlaywrightTimeoutError:
                        pass
                    except Exception as exc:
                        logger.debug(
                            "Provider browser navigation failed",
                            source_type=self.source_type,
                            url=page_url,
                            error=str(exc),
                        )
                        continue

                    await self._dismiss_common_prompts(page)
                    if self.source_type == "flirt4free" and not target.startswith(("http://", "https://")):
                        await self._try_search(page, target)

                    deadline = time.monotonic() + max(5, timeout_seconds)
                    while time.monotonic() < deadline:
                        if captured:
                            await self._save_browser_state(context, target, is_logged_in=True)
                            stream_url = captured[0]
                            try:
                                last_html = await page.content()
                            except Exception:
                                pass
                            meta = _page_metadata(last_html, page.url)
                            return ResolvedStream(
                                url=stream_url,
                                headers=self._stream_headers(page.url, await self._cookie_header(context)),
                                source_type=self.source_type,
                                is_live=True,
                                room_status="public",
                                viewers=int(meta.get("viewers") or 0),
                                tags=list(meta.get("tags") or []),
                                thumbnail=meta.get("thumbnail") or None,
                                title=meta.get("title") or None,
                            )
                        try:
                            last_html = await page.content()
                            remember_hls_field_urls(last_html)
                            media_urls = extract_media_urls(last_html)
                            if media_urls:
                                await self._save_browser_state(context, target, is_logged_in=True)
                                meta = _page_metadata(last_html, page.url)
                                return ResolvedStream(
                                    url=media_urls[0],
                                    headers=self._stream_headers(page.url, await self._cookie_header(context)),
                                    source_type=self.source_type,
                                    is_live=True,
                                    room_status="public",
                                    viewers=int(meta.get("viewers") or 0),
                                    tags=list(meta.get("tags") or []),
                                    thumbnail=meta.get("thumbnail") or None,
                                    title=meta.get("title") or None,
                                )
                        except Exception:
                            pass
                        await page.wait_for_timeout(500)

                await self._save_browser_state(context, target, is_logged_in=False)
                self._raise_page_state(last_html)
                raise ProviderOfflineError(f"Aucun flux public trouve pour {self.display_name}/{target}")
            finally:
                await context.close()

    async def login(self, username: str, password: str) -> dict[str, object]:
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except Exception as exc:
            raise ProviderError("Playwright n'est pas installe") from exc

        headless = os.getenv("PSTREAMREC_BROWSER_HEADLESS", "true").lower() not in {"0", "false", "no"}
        user_data_dir = self.browser_root / self.source_type
        user_data_dir.mkdir(parents=True, exist_ok=True)
        login_urls = [
            template.format(username=quote_plus(username))
            for template in self.login_templates
        ]

        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                str(user_data_dir),
                headless=headless,
                user_agent=DEFAULT_USER_AGENT,
                viewport={"width": 1280, "height": 720},
            )
            try:
                await self._restore_cookies(context)
                page = context.pages[0] if context.pages else await context.new_page()
                for login_url in login_urls:
                    try:
                        await page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
                    except PlaywrightTimeoutError:
                        pass
                    except Exception:
                        continue
                    await self._dismiss_common_prompts(page)
                    await self._fill_login_form(page, username, password)
                    await page.wait_for_timeout(2500)
                    content = await page.content()
                    if _INTERACTION_RE.search(content):
                        await self._save_browser_state(context, username, is_logged_in=False, last_error="interaction_required")
                        raise ProviderInteractionRequired("Interaction manuelle requise dans le navigateur integre")
                    cookies = await context.cookies()
                    if cookies:
                        await self._save_browser_state(context, username, is_logged_in=True)
                        return {"success": True, "username": username}
                await self._save_browser_state(context, username, is_logged_in=False, last_error="login_failed")
                return {"success": False, "error": "Login form introuvable ou connexion refusee"}
            finally:
                await context.close()

    async def logout(self) -> dict[str, object]:
        if self.session_store:
            await self.session_store.clear(self.source_type)
        return {"success": True}

    def _stream_headers(self, page_url: str, cookie_header: Optional[str] = None) -> dict[str, str]:
        parsed = urlparse(page_url)
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": page_url,
        }
        if origin:
            headers["Origin"] = origin
        if cookie_header:
            headers["Cookie"] = cookie_header
        return headers

    def _is_media_url(self, value: str) -> bool:
        lower = (value or "").lower()
        return ".m3u8" in lower or ".mpd" in lower

    def _raise_page_state(self, content: str) -> None:
        text = content or ""
        if _INTERACTION_RE.search(text):
            raise ProviderInteractionRequired("Interaction manuelle requise")
        if _PRIVATE_RE.search(text):
            raise ProviderPrivateError("Le modele semble en show prive ou premium")
        if _OFFLINE_RE.search(text):
            raise ProviderOfflineError("Le modele semble hors ligne")

    async def _restore_cookies(self, context) -> None:
        if not self.session_store:
            return
        state = await self.session_store.get(self.source_type)
        cookies = state.get("cookies") or []
        if cookies:
            try:
                await context.add_cookies(cookies)
            except Exception:
                pass

    async def _save_browser_state(
        self,
        context,
        username: Optional[str],
        is_logged_in: bool,
        last_error: Optional[str] = None,
    ) -> None:
        if not self.session_store:
            return
        try:
            state = await context.storage_state()
        except Exception:
            state = {}
        await self.session_store.save(
            self.source_type,
            username=username,
            is_logged_in=is_logged_in,
            cookies=state.get("cookies") or [],
            local_storage=state.get("origins") or [],
            last_error=last_error,
        )

    async def _cookie_header(self, context) -> str:
        try:
            return ProviderSessionStore.cookies_to_header(await context.cookies())
        except Exception:
            return ""

    async def _dismiss_common_prompts(self, page) -> None:
        labels = re.compile(
            r"(^(accept|i agree|agree|continue|enter|yes|allow|j'accepte|accepter|ok)$|over 18|enter site)",
            re.IGNORECASE,
        )
        clicked = False
        for role in ("button", "link"):
            try:
                locator = page.get_by_role(role, name=labels)
                count = min(await locator.count(), 3)
                for idx in range(count):
                    try:
                        await locator.nth(idx).click(timeout=1000)
                        clicked = True
                    except Exception:
                        pass
            except Exception:
                pass
        if clicked:
            try:
                await page.wait_for_timeout(1000)
            except Exception:
                pass

    async def _try_search(self, page, target: str) -> None:
        selectors = [
            "input[type=search]",
            'input[name*="search" i]',
            'input[placeholder*="Search" i]',
            'input[placeholder*="model" i]',
        ]
        for selector in selectors:
            try:
                field = page.locator(selector).first
                if await field.count():
                    await field.fill(target, timeout=1000)
                    await field.press("Enter", timeout=1000)
                    await page.wait_for_timeout(2000)
                    return
            except Exception:
                continue

    async def _fill_login_form(self, page, username: str, password: str) -> None:
        user_selectors = [
            "input[type=email]",
            'input[name*="user" i]',
            'input[name*="login" i]',
            'input[name*="email" i]',
            "input[autocomplete=username]",
            "input[type=text]",
        ]
        pass_selectors = [
            "input[type=password]",
            'input[name*="pass" i]',
            "input[autocomplete=current-password]",
        ]
        for selector in user_selectors:
            try:
                field = page.locator(selector).first
                if await field.count():
                    await field.fill(username, timeout=1500)
                    break
            except Exception:
                pass
        for selector in pass_selectors:
            try:
                field = page.locator(selector).first
                if await field.count():
                    await field.fill(password, timeout=1500)
                    break
            except Exception:
                pass
        try:
            await page.locator("button[type=submit], input[type=submit]").first.click(timeout=1500)
        except Exception:
            try:
                await page.keyboard.press("Enter")
            except Exception:
                pass
