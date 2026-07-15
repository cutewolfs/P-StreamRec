"""
Chaturbate API Client
Authenticated API calls for model discovery, following, and stream resolution
"""

import asyncio
import json
import re
import time
from html import unescape
from typing import Optional, List, Dict, Any
from curl_cffi.requests import AsyncSession

import aiohttp

from ..logger import logger
from ..core.config import (
    CB_REQUEST_DELAY,
    CHATURBATE_REQUEST_TIMEOUT_SECONDS,
    PSTREAMREC_MAX_FOLLOW_SYNC_ITEMS,
)
from ..core.http_client import aiohttp_client_session, aiohttp_request_kwargs
from .chaturbate_auth import ChaturbateAuthService, merge_flaresolverr_cookies
from .flaresolverr import FlareSolverrClient


_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class FollowedSyncResult(list):
    def __init__(
        self,
        values=(),
        trusted: bool = True,
        skipped_reason: Optional[str] = None,
        authoritative: bool = True,
    ):
        super().__init__(values)
        self.trusted = trusted
        self.skipped_reason = skipped_reason
        self.authoritative = bool(authoritative and trusted)


class ChaturbateAPI:
    def __init__(
        self,
        auth_service: ChaturbateAuthService,
        flaresolverr: Optional[FlareSolverrClient] = None
    ):
        self.auth = auth_service
        self.flaresolverr = flaresolverr
        self._semaphore = asyncio.Semaphore(2)
        self._rate_lock = asyncio.Lock()
        self._last_request_time: float = 0

    def _apply_flaresolverr_solution(
        self,
        headers: Dict[str, str],
        solution: Dict[str, Any],
    ) -> bool:
        cookies = solution.get("cookies") or {}
        user_agent = solution.get("user_agent") or ""
        if user_agent:
            headers["User-Agent"] = user_agent
            if hasattr(self.auth, "_user_agent"):
                self.auth._user_agent = user_agent

        merged_cookies = self.auth.get_cookies()
        changed_cookies = merge_flaresolverr_cookies(merged_cookies, cookies)
        if merged_cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in merged_cookies.items())
            if hasattr(self.auth, "_cookies"):
                self.auth._cookies = dict(merged_cookies)
        return bool(changed_cookies or user_agent)

    async def _prepare_flaresolverr_retry_headers(
        self,
        url: str,
        headers: Dict[str, str],
    ) -> bool:
        if not self.flaresolverr:
            return False
        solution = await self.flaresolverr.solve_challenge(url)
        if not solution:
            return False
        return self._apply_flaresolverr_solution(headers, solution)

    async def _rate_limit(self):
        """Apply rate limiting between requests"""
        async with self._rate_lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            if elapsed < CB_REQUEST_DELAY:
                await asyncio.sleep(CB_REQUEST_DELAY - elapsed)
            self._last_request_time = time.monotonic()

    def _get_headers(self) -> Dict[str, str]:
        """Get headers with auth cookies if available"""
        headers = {
            "User-Agent": self.auth.get_user_agent(),
            "Accept": "application/json, text/html",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://chaturbate.com/",
            "Origin": "https://chaturbate.com",
        }

        cookies = self.auth.get_cookies()
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

        return headers

    async def _request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict] = None,
        **kwargs
    ) -> Optional["_FakeResponse"]:
        """Make an HTTP request with rate limiting, using curl_cffi to bypass
        Cloudflare TLS fingerprinting, with FlareSolverr fallback on 403."""
        async with self._semaphore:
            await self._rate_limit()

            if headers is None:
                headers = self._get_headers()

            curl_kwargs = {}
            if "data" in kwargs:
                curl_kwargs["data"] = kwargs.pop("data")
            if "json" in kwargs:
                curl_kwargs["json"] = kwargs.pop("json")
            if "params" in kwargs:
                curl_kwargs["params"] = kwargs.pop("params")
            if "cookies" in kwargs:
                curl_kwargs["cookies"] = kwargs.pop("cookies")

            try:
                async with AsyncSession() as session:
                    resp = await session.request(
                        method,
                        url,
                        headers=headers,
                        impersonate="chrome120",
                        timeout=CHATURBATE_REQUEST_TIMEOUT_SECONDS,
                        **curl_kwargs,
                    )

                    if resp.status_code == 403 or resp.status_code in _REDIRECT_STATUSES:
                        if self.flaresolverr:
                            logger.info(
                                "Chaturbate protection detected, attempting FlareSolverr bypass",
                                status=resp.status_code,
                            )
                            solved = await self._prepare_flaresolverr_retry_headers(url, headers)

                            if solved:
                                await self._rate_limit()
                                retry_resp = await session.request(
                                    method,
                                    url,
                                    headers=headers,
                                    impersonate="chrome120",
                                    timeout=CHATURBATE_REQUEST_TIMEOUT_SECONDS,
                                    **curl_kwargs,
                                )
                                return _FakeResponse(
                                    retry_resp.status_code,
                                    retry_resp.content,
                                    retry_resp.headers,
                                    retry_resp.headers.get("content-type"),
                                )

                    return _FakeResponse(
                        resp.status_code,
                        resp.content,
                        resp.headers,
                        resp.headers.get("content-type"),
                    )

            except Exception as e:
                logger.error("Request error", url=url, error=str(e))
                return None

    async def check_status(self, username: str) -> Dict[str, Any]:
        """Statut Chaturbate d'un username. Renvoie un dict normalisé.

        Utilise le chaturbate_auth du service pour fournir des cookies
        authentifiés à monitor.check_model_status().
        """
        from ..tasks.monitor import check_model_status

        csrftoken = None
        try:
            csrftoken = self.auth.get_cookies().get("csrftoken")
        except Exception:
            csrftoken = None

        auth_cookies = None
        try:
            cookies = self.auth.get_cookies()
            if cookies:
                auth_cookies = cookies
        except Exception:
            auth_cookies = None

        async with aiohttp_client_session() as session:
            data = await check_model_status(
                session, username, csrftoken, auth_cookies=auth_cookies
            )
        return {
            "is_online": bool(data.get("is_online", False)),
            "viewers": int(data.get("viewers", 0) or 0),
            "hls_source": data.get("hls_source"),
            "room_status": data.get("room_status"),
            "tags": list(data.get("tags") or []),
        }

    async def get_live_models(
        self,
        page: int = 1,
        limit: int = 24,
        gender: str = "",
        search: str = "",
        tag: str = ""
    ) -> Dict[str, Any]:
        """
        Fetch live models from Chaturbate.
        Uses the roomlist API or scrapes the homepage.

        tag: filter by a single tag via the native API (e.g. "french", "18").
            Le filtrage natif est crucial : sans ça le total_count reflète
            l'ensemble et la pagination calculée côté backend est fausse.
        """
        try:
            # Try the internal API first
            offset = (page - 1) * limit
            api_url = (
                f"https://chaturbate.com/api/ts/roomlist/room-list/"
                f"?limit={limit}&offset={offset}"
            )
            if gender:
                gender_map = {
                    "female": "f",
                    "male": "m",
                    "couple": "c",
                    "trans": "t",
                }
                g = gender_map.get(gender.lower(), "")
                if g:
                    api_url += f"&genders={g}"
            # Chaturbate n'a pas de paramètre `tag` dédié sur cet endpoint; le
            # seul moyen de filtrer est `keywords` (recherche full-text, qui
            # matche sur les tags et le subject). Si on a un tag, on le
            # concatène au search dans keywords.
            combined_keywords = " ".join(x for x in (tag, search) if x).strip()
            if combined_keywords:
                from urllib.parse import quote_plus
                api_url += f"&keywords={quote_plus(combined_keywords)}"

            headers = self._get_headers()
            headers["Accept"] = "application/json"
            headers["X-Requested-With"] = "XMLHttpRequest"

            resp = await self._request("GET", api_url, headers=headers, allow_redirects=False)

            if resp and resp.status == 200:
                parsed = self._parse_roomlist_response(resp, page, limit)
                if parsed is not None:
                    return parsed
            elif resp:
                logger.debug("Chaturbate roomlist API failed", status=resp.status)

            # Fallback: scrape homepage
            return await self._scrape_live_models(page, limit, gender, search)

        except Exception as e:
            logger.error("Error fetching live models", error=str(e))
            return {
                "models": [],
                "total": 0,
                "page": page,
                "limit": limit,
                "total_pages": 1,
            }

    @staticmethod
    def _as_int(value: Any, default: int = 0) -> int:
        try:
            if value is None or value == "":
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_optional_int(value: Any) -> Optional[int]:
        try:
            if value is None or value == "":
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _roomlist_total(cls, data: Dict[str, Any], default: int) -> tuple[int, Optional[str]]:
        for key in ("total_count", "totalCount", "num_total", "numTotal", "total"):
            if key in data:
                return cls._as_int(data.get(key), default), key
        return default, None

    @staticmethod
    def _normalize_tags(value: Any) -> List[str]:
        if isinstance(value, list):
            return [str(tag).strip() for tag in value if str(tag).strip()]
        if isinstance(value, str):
            return [tag for tag in re.split(r"[\s,#]+", value) if tag]
        return []

    @staticmethod
    def _normalize_thumbnail(value: Any, username: str) -> str:
        thumbnail = str(value or "").strip()
        if thumbnail.startswith("//"):
            thumbnail = "https:" + thumbnail
        if not thumbnail and username:
            thumbnail = f"https://roomimg.stream.highwebmedia.com/ri/{username}.jpg"
        return thumbnail

    @classmethod
    def _parse_public_room_item(cls, room: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(room, dict):
            return None

        username = str(room.get("username") or room.get("room") or room.get("slug") or "").strip()
        if not username:
            return None

        room_status = (
            room.get("current_show")
            or room.get("room_status")
            or room.get("label")
            or "public"
        )
        subject = room.get("room_subject")
        if subject is None:
            subject = room.get("subject", "")

        return {
            "username": username,
            "display_name": str(room.get("display_name") or username),
            "thumbnail": cls._normalize_thumbnail(
                room.get("img") or room.get("thumbnail") or room.get("thumbnail_url"),
                username,
            ),
            "viewers": cls._as_int(
                room.get("num_users")
                if room.get("num_users") is not None
                else room.get("viewers", room.get("num_viewers", 0))
            ),
            "subject": str(subject or ""),
            "age": cls._as_optional_int(
                room.get("age") if room.get("age") is not None else room.get("display_age")
            ),
            "gender": str(room.get("gender") or ""),
            "is_online": True,
            "tags": cls._normalize_tags(room.get("tags", [])),
            "room_status": str(room_status or "public"),
        }

    @classmethod
    def _parse_roomlist_payload(
        cls,
        data: Any,
        page: int,
        limit: int,
    ) -> Dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("roomlist payload is not an object")

        rooms = data.get("rooms", [])
        if isinstance(rooms, dict):
            for key in ("rooms", "results", "items"):
                nested = rooms.get(key)
                if isinstance(nested, list):
                    rooms = nested
                    break
            else:
                rooms = list(rooms.values())
        if not isinstance(rooms, list):
            raise ValueError("roomlist rooms is not a list")

        models = []
        for room in rooms:
            model = cls._parse_public_room_item(room)
            if model:
                models.append(model)

        total, _total_key = cls._roomlist_total(data, len(models))
        total_pages = max(1, (total + limit - 1) // limit)

        return {
            "models": models,
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
        }

    @classmethod
    def _parse_roomlist_response(
        cls,
        resp: Any,
        page: int,
        limit: int,
    ) -> Optional[Dict[str, Any]]:
        content_type = str(getattr(resp, "content_type", "") or "").lower()
        body_preview = resp.text().lstrip()[:120]
        if "json" not in content_type and not body_preview.startswith(("{", "[")):
            logger.debug(
                "Chaturbate roomlist API returned non-JSON response",
                content_type=content_type,
            )
            return None

        try:
            data = resp.json()
        except Exception as e:
            logger.debug(
                "Chaturbate roomlist JSON decode failed",
                error=str(e),
                content_type=content_type,
            )
            return None

        try:
            return cls._parse_roomlist_payload(data, page, limit)
        except Exception as e:
            logger.debug("Chaturbate roomlist payload ignored", error=str(e))
            return None

    async def _scrape_live_models(
        self,
        page: int,
        limit: int,
        gender: str,
        search: str
    ) -> Dict[str, Any]:
        """Fallback: scrape Chaturbate homepage for live models"""
        url = "https://chaturbate.com/"
        if gender:
            gender_map = {
                "female": "female-cams/",
                "male": "male-cams/",
                "couple": "couple-cams/",
                "trans": "trans-cams/",
            }
            url += gender_map.get(gender.lower(), "")
        if search:
            url = f"https://chaturbate.com/tags/{search}/"

        if page > 1:
            url += f"?page={page}"

        resp = await self._request("GET", url, allow_redirects=False)
        if not resp or resp.status != 200:
            return {
                "models": [],
                "total": 0,
                "page": page,
                "limit": limit,
                "total_pages": 1,
            }

        html = resp.text()
        models = []

        # Parse room list from HTML
        room_pattern = re.compile(
            r'<li[^>]*class="[^"]*room_list_room[^"]*"[^>]*>'
            r'.*?data-room="([^"]+)"'
            r'.*?<img[^>]*src="([^"]*)"'
            r'.*?<span[^>]*class="[^"]*cams[^"]*"[^>]*>(\d*)</span>',
            re.DOTALL
        )

        for match in room_pattern.finditer(html):
            username = match.group(1)
            thumbnail = match.group(2)
            viewers_str = match.group(3)
            viewers = int(viewers_str) if viewers_str else 0

            if thumbnail.startswith("//"):
                thumbnail = "https:" + thumbnail

            models.append({
                "username": username,
                "display_name": username,
                "thumbnail": thumbnail,
                "viewers": viewers,
                "subject": "",
                "age": None,
                "gender": gender or "",
                "is_online": True,
                "tags": [],
            })

        # Estimate total pages from pagination
        total_pages_match = re.search(
            r'class="[^"]*endless_page_link[^"]*"[^>]*>(\d+)</a>\s*<li[^>]*class="[^"]*next',
            html
        )
        total_pages = int(total_pages_match.group(1)) if total_pages_match else page

        return {
            "models": models[:limit],
            "total": len(models),
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
        }

    async def get_followed_models(self) -> List[Dict[str, Any]]:
        """
        Fetch a complete followed-model snapshot from Chaturbate.

        Prefer the authenticated JSON roomlist because the HTML followed-cams
        page is redirected to a login/age wall for some regions. Chaturbate can
        silently ignore ``follow=true`` and return the global roomlist, so JSON
        results are authoritative only when every raw room explicitly reports
        ``is_following: true`` and both online/offline categories paginate
        completely. HTML remains a non-authoritative fallback: it may refresh
        visible statuses, but it must never delete cached offline follows.
        """
        status_getter = getattr(self.auth, "get_status", None)
        status = status_getter() if callable(status_getter) else None
        if (
            not self.auth.get_cookies().get("sessionid")
            or (isinstance(status, dict) and status.get("isLoggedIn") is not True)
        ):
            return FollowedSyncResult(
                [],
                trusted=False,
                skipped_reason="Chaturbate session is not verified",
                authoritative=False,
            )

        headers = self._get_headers()
        roomlist_result = await self._fetch_followed_roomlist_api(headers)
        if roomlist_result.trusted:
            return roomlist_result

        html_result = await self._fetch_followed_html(headers)
        if html_result.trusted:
            logger.info(
                "Chaturbate followed roomlist unavailable; using HTML fallback",
                reason=roomlist_result.skipped_reason,
            )
            return html_result

        reasons = [
            reason
            for reason in (
                roomlist_result.skipped_reason,
                html_result.skipped_reason,
            )
            if reason
        ]
        return self._untrusted_followed_result("; HTML fallback: ".join(reasons))

    async def _fetch_followed_html(self, headers: Dict[str, str]) -> FollowedSyncResult:
        html_headers = dict(headers)
        html_headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        html_headers["Referer"] = "https://chaturbate.com/followed-cams/"

        models: List[Dict[str, Any]] = []
        seen = set()

        page = 1
        while True:
            url = "https://chaturbate.com/followed-cams/"
            if page > 1:
                url = f"{url}?page={page}"

            resp = await self._request("GET", url, headers=html_headers, allow_redirects=False)
            if not resp:
                return self._followed_failure("Chaturbate followed-cams request failed")
            if resp.status in _REDIRECT_STATUSES:
                return self._followed_failure("Chaturbate followed-cams redirected to login")
            if resp.status == 403:
                return self._followed_failure("Chaturbate followed-cams returned 403")
            if resp.status != 200:
                return self._followed_failure(f"Chaturbate followed-cams returned HTTP {resp.status}")

            html = resp.text()
            if self._looks_like_login_or_challenge(html):
                return self._followed_failure("Chaturbate followed-cams returned a login or challenge page")
            if page == 1 and not self._looks_like_followed_page_context(html):
                return self._followed_failure("Chaturbate followed-cams page shape is not recognized")

            page_items = self._parse_followed_html(html)
            if not page_items and page == 1 and not self._looks_like_empty_followed_page(html):
                return self._followed_failure("Chaturbate followed-cams page shape is not recognized")
            if page_items and self._has_next_followed_page(html):
                return self._followed_failure(
                    "Chaturbate followed-cams HTML pagination is ambiguous"
                )

            for item in page_items:
                username = item.get("username", "")
                key = username.lower()
                if username and key not in seen:
                    seen.add(key)
                    models.append(item)
                    if len(models) > PSTREAMREC_MAX_FOLLOW_SYNC_ITEMS:
                        return self._followed_failure(
                            f"Chaturbate followed-cams exceeded safety limit ({PSTREAMREC_MAX_FOLLOW_SYNC_ITEMS})"
                        )

            if not self._has_next_followed_page(html):
                break
            page += 1
            if page > 200:
                return self._followed_failure("Chaturbate followed-cams pagination exceeded safety limit")

        logger.debug("Fetched followed models", total=len(models), source="followed-cams")
        return FollowedSyncResult(models, trusted=True, authoritative=False)

    async def _fetch_followed_roomlist_api(self, headers: Dict[str, str]) -> FollowedSyncResult:
        api_headers = dict(headers)
        api_headers["Accept"] = "application/json"
        api_headers["X-Requested-With"] = "XMLHttpRequest"
        api_headers["Referer"] = "https://chaturbate.com/followed-cams/"

        models: List[Dict[str, Any]] = []
        seen = set()
        limit = 90

        for offline in (False, True):
            offset = 0
            expected_total: Optional[int] = None
            category_seen = set()

            while True:
                params = f"limit={limit}&offset={offset}&follow=true"
                if offline:
                    params += "&offline=true"
                url = f"https://chaturbate.com/api/ts/roomlist/room-list/?{params}"
                resp = await self._request("GET", url, headers=api_headers, allow_redirects=False)
                if not resp:
                    return self._followed_failure("Chaturbate followed roomlist request failed")
                if resp.status in _REDIRECT_STATUSES:
                    return self._followed_failure("Chaturbate followed roomlist redirected to login")
                if resp.status == 403:
                    return self._followed_failure("Chaturbate followed roomlist returned 403")
                if resp.status != 200:
                    return self._followed_failure(f"Chaturbate followed roomlist returned HTTP {resp.status}")

                try:
                    data = resp.json()
                except Exception as e:
                    logger.debug(
                        "Chaturbate followed roomlist JSON decode failed",
                        error=str(e),
                        content_type=str(getattr(resp, "content_type", "") or ""),
                    )
                    return self._followed_failure("Chaturbate followed roomlist response is not recognized")

                if not isinstance(data, dict) or "total_count" not in data:
                    return self._followed_failure("Chaturbate followed roomlist total is not recognized")
                total = self._strict_nonnegative_int(data.get("total_count"))
                if total is None:
                    return self._followed_failure("Chaturbate followed roomlist total is not recognized")
                if expected_total is None:
                    expected_total = total
                elif total != expected_total:
                    return self._followed_failure("Chaturbate followed roomlist total changed during pagination")
                if total > PSTREAMREC_MAX_FOLLOW_SYNC_ITEMS:
                    return self._followed_failure(
                        f"Chaturbate followed roomlist exceeded safety limit ({PSTREAMREC_MAX_FOLLOW_SYNC_ITEMS})"
                    )

                page_rooms = data.get("rooms")
                if not isinstance(page_rooms, list):
                    return self._followed_failure("Chaturbate followed roomlist rooms are not recognized")
                if len(page_rooms) > limit or offset + len(page_rooms) > total:
                    return self._followed_failure("Chaturbate followed roomlist total is inconsistent")
                if not page_rooms and offset < total:
                    return self._followed_failure("Chaturbate followed roomlist ended before its reported total")

                for room in page_rooms:
                    if not isinstance(room, dict) or room.get("is_following") is not True:
                        return self._followed_failure(
                            "Chaturbate ignored the followed roomlist filter"
                        )
                    item = self._parse_room_item(room, is_online=not offline)
                    username = str(item.get("username") or "").strip()
                    key = username.lower()
                    if not username or not re.match(r"^[A-Za-z0-9_]+$", username):
                        return self._followed_failure("Chaturbate followed roomlist contains an invalid room")
                    if key in category_seen:
                        return self._followed_failure("Chaturbate followed roomlist contains duplicate rooms")
                    category_seen.add(key)
                    if key not in seen:
                        seen.add(key)
                        models.append(item)
                        if len(models) > PSTREAMREC_MAX_FOLLOW_SYNC_ITEMS:
                            return self._followed_failure(
                                f"Chaturbate followed roomlist exceeded safety limit ({PSTREAMREC_MAX_FOLLOW_SYNC_ITEMS})"
                            )

                next_offset = offset + len(page_rooms)
                if next_offset == total:
                    break
                if len(page_rooms) < limit:
                    return self._followed_failure("Chaturbate followed roomlist pagination is incomplete")
                offset = next_offset

        logger.debug("Fetched followed models", total=len(models), source="followed-roomlist")
        return FollowedSyncResult(models, trusted=True, authoritative=True)

    @staticmethod
    def _strict_nonnegative_int(value: Any) -> Optional[int]:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value >= 0 else None
        if isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
            return int(value.strip())
        return None

    @staticmethod
    def _followed_failure(reason: str) -> FollowedSyncResult:
        return FollowedSyncResult(
            [],
            trusted=False,
            skipped_reason=reason,
            authoritative=False,
        )

    @staticmethod
    def _untrusted_followed_result(reason: str) -> FollowedSyncResult:
        logger.warning("Chaturbate following sync skipped", reason=reason)
        return FollowedSyncResult(
            [],
            trusted=False,
            skipped_reason=reason,
            authoritative=False,
        )

    @staticmethod
    def _html_attr(fragment: str, name: str) -> str:
        match = re.search(
            rf'\b{name}\s*=\s*(["\'])(.*?)\1',
            fragment or "",
            re.IGNORECASE | re.DOTALL,
        )
        return unescape(match.group(2)).strip() if match else ""

    @staticmethod
    def _looks_like_login_or_challenge(html: str) -> bool:
        lower = (html or "").lower()
        if "cloudflare" in lower and ("challenge" in lower or "cf-browser-verification" in lower):
            return True
        if "/auth/login" in lower or ("name=\"password\"" in lower and "csrfmiddlewaretoken" in lower):
            return True
        return "login_required" in lower or "please log in" in lower

    @staticmethod
    def _looks_like_empty_followed_page(html: str) -> bool:
        lower = (html or "").lower()
        empty_markers = (
            "haven't followed",
            "not following anyone",
            "not yet followed",
            "no followed",
            "no followed cams",
            "no favorites",
            "you have not followed",
            "you aren't following",
        )
        return any(marker in lower for marker in empty_markers)

    @staticmethod
    def _looks_like_followed_page_context(html: str) -> bool:
        lower = (html or "").lower()
        markers = (
            "followed-cams",
            "followed cams",
            "followedpage",
            "follow=true",
            "my followed",
            "models you follow",
            "rooms you follow",
        )
        return any(marker in lower for marker in markers)

    @staticmethod
    def _looks_like_followed_roomlist_shell(html: str) -> bool:
        lower = (html or "").lower()
        has_roomlist_root = (
            'id="roomlist_root"' in lower
            or "id='roomlist_root'" in lower
            or 'data-testid="room-list"' in lower
            or "data-testid='room-list'" in lower
        )
        has_followed_context = (
            "followed-cams" in lower
            or "followedpage" in lower
            or "follow=true" in lower
        )
        return has_roomlist_root and has_followed_context

    @classmethod
    def _has_next_followed_page(cls, html: str) -> bool:
        lower = (html or "").lower()
        if 'rel="next"' in lower or "rel='next'" in lower:
            return True
        next_link = re.search(
            r'<a\b[^>]*href=["\'][^"\']*(?:\?|&)page=\d+[^"\']*["\'][^>]*>\s*(?:next|&rsaquo;|›)',
            html or "",
            re.IGNORECASE | re.DOTALL,
        )
        return bool(next_link)

    @classmethod
    def _parse_followed_html(cls, html: str) -> List[Dict[str, Any]]:
        models: List[Dict[str, Any]] = []
        for match in re.finditer(r"<li\b(?P<attrs>[^>]*)>(?P<body>.*?)</li>", html or "", re.IGNORECASE | re.DOTALL):
            attrs = match.group("attrs") or ""
            if "room_list_room" not in attrs:
                continue
            body = match.group("body") or ""
            username = (
                cls._html_attr(attrs, "data-room")
                or cls._html_attr(attrs, "data-room-name")
                or cls._html_attr(attrs, "data-username")
            )
            if not username:
                href_match = re.search(r'href=["\']/([A-Za-z0-9_]+)/?["\']', body, re.IGNORECASE)
                username = href_match.group(1) if href_match else ""
            username = username.strip().strip("/")
            if not username or not re.match(r"^[A-Za-z0-9_]+$", username):
                continue

            img_attrs = ""
            img_match = re.search(r"<img\b([^>]*)>", body, re.IGNORECASE | re.DOTALL)
            if img_match:
                img_attrs = img_match.group(1)
            thumbnail = cls._normalize_thumbnail(
                cls._html_attr(img_attrs, "src")
                or cls._html_attr(img_attrs, "data-src")
                or cls._html_attr(img_attrs, "data-original")
                or cls._html_attr(img_attrs, "data-image")
                or cls._html_attr(attrs, "data-image"),
                username,
            )
            display_name = (
                cls._html_attr(img_attrs, "alt")
                or cls._html_attr(attrs, "title")
                or username
            )
            viewers_match = re.search(
                r'class=["\'][^"\']*\bcams\b[^"\']*["\'][^>]*>\s*([0-9,]+)',
                body,
                re.IGNORECASE | re.DOTALL,
            ) or re.search(r"\b([0-9,]+)\s+(?:viewers|cams)\b", body, re.IGNORECASE)
            viewers = cls._as_int(viewers_match.group(1).replace(",", "")) if viewers_match else 0
            room_status = (
                cls._html_attr(attrs, "data-room-status")
                or cls._html_attr(attrs, "data-current-show")
                or None
            )
            classes = cls._html_attr(attrs, "class").lower()
            is_offline = "offline" in classes or (room_status or "").lower() == "offline"
            is_private = "private" in classes or (room_status or "").lower() in {"private", "group"}
            is_online = not is_offline and not is_private
            if not room_status:
                room_status = "offline" if is_offline else "private" if is_private else "public"

            models.append({
                "username": username,
                "display_name": display_name,
                "is_online": is_online,
                "viewers": viewers if is_online else 0,
                "thumbnail_url": thumbnail,
                "room_status": room_status,
                "tags": [],
                "subject": "",
                "gender": "",
                "num_followers": 0,
            })
        models.extend(cls._parse_followed_embedded_json(html))
        return cls._dedupe_followed_items(models)

    @classmethod
    def _parse_followed_embedded_json(cls, html: str) -> List[Dict[str, Any]]:
        models: List[Dict[str, Any]] = []
        scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", html or "", re.IGNORECASE | re.DOTALL)
        for script in scripts:
            text = unescape(script or "").strip()
            if not text or len(text) > 2_000_000:
                continue
            json_values = cls._extract_json_values(text)
            for value in json_values:
                cls._collect_followed_json_models(value, models)
        return models

    @classmethod
    def _extract_json_values(cls, text: str) -> List[Any]:
        values: List[Any] = []
        decoder = json.JSONDecoder()
        candidates = []
        stripped = text.strip()
        if stripped.startswith(("{", "[")):
            candidates.append(stripped)
        for match in re.finditer(r"=\s*({.*?});", text, re.DOTALL):
            candidates.append(match.group(1))

        for candidate in candidates:
            try:
                value, _ = decoder.raw_decode(candidate)
            except Exception:
                continue
            values.append(value)
        return values

    @classmethod
    def _collect_followed_json_models(cls, value: Any, models: List[Dict[str, Any]]) -> None:
        if isinstance(value, list):
            for item in value:
                cls._collect_followed_json_models(item, models)
            return
        if not isinstance(value, dict):
            return

        item = cls._followed_json_item(value)
        if item:
            models.append(item)

        for child in value.values():
            if isinstance(child, (dict, list)):
                cls._collect_followed_json_models(child, models)

    @classmethod
    def _followed_json_item(cls, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        username = str(
            item.get("username")
            or item.get("room")
            or item.get("room_slug")
            or item.get("slug")
            or ""
        ).strip().strip("/")
        if not username or not re.match(r"^[A-Za-z0-9_]+$", username):
            return None

        has_followed_shape = any(
            key in item
            for key in (
                "is_online",
                "isOnline",
                "current_show",
                "room_status",
                "num_users",
                "viewers",
                "thumbnail",
                "thumbnail_url",
                "img",
            )
        )
        if not has_followed_shape:
            return None

        room_status = str(
            item.get("room_status")
            or item.get("roomStatus")
            or item.get("current_show")
            or item.get("status")
            or ""
        ).strip().lower()
        is_private = room_status in {"private", "group", "ticket", "hidden"}
        is_online = bool(item.get("is_online", item.get("isOnline", room_status == "public"))) and not is_private
        thumbnail = cls._normalize_thumbnail(
            item.get("thumbnail_url") or item.get("thumbnail") or item.get("img"),
            username,
        )
        return {
            "username": username,
            "display_name": str(item.get("display_name") or item.get("displayName") or username),
            "is_online": is_online,
            "viewers": cls._as_int(item.get("viewers", item.get("num_users", 0))) if is_online else 0,
            "thumbnail_url": thumbnail,
            "room_status": room_status or ("public" if is_online else "offline"),
            "tags": cls._normalize_tags(item.get("tags", [])),
            "subject": str(item.get("subject") or item.get("room_subject") or ""),
            "gender": str(item.get("gender") or ""),
            "num_followers": cls._as_int(item.get("num_followers"), 0),
        }

    @staticmethod
    def _dedupe_followed_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        deduped: List[Dict[str, Any]] = []
        seen = set()
        for item in items:
            username = str(item.get("username") or "").strip()
            key = username.lower()
            if not username or key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    @classmethod
    def _followed_roomlist_item(cls, model: Dict[str, Any]) -> Dict[str, Any]:
        username = str(model.get("username") or "").strip()
        room_status = str(model.get("room_status") or "public").strip().lower()
        is_private = room_status in {"private", "group", "ticket", "hidden"}
        is_online = bool(model.get("is_online", True)) and not is_private
        thumbnail = model.get("thumbnail_url") or model.get("thumbnail")
        return {
            "username": username,
            "display_name": str(model.get("display_name") or username),
            "is_online": is_online,
            "viewers": cls._as_int(model.get("viewers"), 0) if is_online else 0,
            "thumbnail_url": thumbnail,
            "room_status": room_status or ("public" if is_online else "offline"),
            "tags": cls._normalize_tags(model.get("tags", [])),
            "subject": str(model.get("subject") or ""),
            "gender": str(model.get("gender") or ""),
            "num_followers": cls._as_int(model.get("num_followers"), 0),
        }

    @staticmethod
    def _parse_room_item(item: Dict[str, Any], is_online: bool = True) -> Dict[str, Any]:
        """Parse a room item from the roomlist API into our model format."""
        username = item.get("username", "")
        thumb = (
            item.get("img")
            or item.get("thumbnail")
            or item.get("thumbnail_url")
            or item.get("thumbnailUrl")
            or item.get("image_url")
            or item.get("imageUrl")
            or ""
        )
        if thumb and thumb.startswith("//"):
            thumb = "https:" + thumb
        if not thumb:
            thumb = f"https://roomimg.stream.highwebmedia.com/ri/{username}.jpg"
        room_status = item.get("current_show") or item.get("room_status") or None

        return {
            "username": username,
            "display_name": item.get("display_name") or username,
            "is_online": is_online or item.get("current_show") == "public",
            "viewers": item.get("num_users", 0),
            "thumbnail_url": thumb,
            "room_status": room_status,
            "tags": item.get("tags", []),
            "subject": item.get("room_subject") or item.get("subject", ""),
            "gender": item.get("gender", ""),
            "num_followers": item.get("num_followers", 0),
        }

    async def _toggle_follow(self, username: str, action: str) -> bool:
        """Follow or unfollow a model on Chaturbate (requires auth).

        Verifies the action actually took effect by re-checking is_following()
        afterwards, because Chaturbate sometimes returns 200 even when an
        anti-bot layer silently drops the request.
        """
        if not self.auth.get_cookies().get("sessionid"):
            logger.warning(f"Cannot {action}: not logged in")
            return False

        try:
            room_url = f"https://chaturbate.com/{username}/"
            headers = self._get_headers()
            headers["Accept"] = "*/*"
            headers["X-Requested-With"] = "XMLHttpRequest"
            headers["Referer"] = room_url

            # Prime room cookies before the AJAX follow endpoint, matching the
            # browser flow used by CTBrec.
            await self._request("GET", room_url, headers=headers)

            url = f"https://chaturbate.com/follow/{action}/{username}/"
            csrf = self.auth.get_cookies().get("csrftoken", "")
            if csrf:
                headers["X-CSRFToken"] = csrf

            resp = await self._request("POST", url, headers=headers, data=b"")

            if not resp or resp.status != 200:
                logger.warning(f"Failed to {action} model", username=username,
                            status=resp.status if resp else None)
                return False

            # Verify the state server-side (anti-bot sometimes returns 200 then drops)
            expected = (action == "follow")
            actual = await self.is_following(username)
            if actual != expected:
                logger.warning(
                    f"{action} returned 200 but state did not change (silent failure)",
                    username=username, expected=expected, actual=actual,
                )
                return False

            logger.info(f"{action.capitalize()}ed model on Chaturbate", username=username)
            return True
        except Exception as e:
            logger.error(f"Error {action}ing model", username=username, error=str(e))
        return False

    async def follow_model(self, username: str) -> bool:
        """Follow a model on Chaturbate (requires auth)"""
        return await self._toggle_follow(username, "follow")

    async def unfollow_model(self, username: str) -> bool:
        """Unfollow a model on Chaturbate (requires auth)"""
        return await self._toggle_follow(username, "unfollow")

    async def is_following(self, username: str) -> bool:
        """Check if currently following a model on Chaturbate"""
        if not self.auth.get_cookies().get("sessionid"):
            return False

        try:
            url = f"https://chaturbate.com/api/chatvideocontext/{username}/"
            resp = await self._request("GET", url)
            if resp and resp.status == 200:
                data = resp.json()
                return data.get("following", False)
        except Exception as e:
            logger.debug("Error checking follow status", username=username, error=str(e))
        return False

    async def get_edge_hls_url(self, username: str) -> Optional[str]:
        """
        POST /get_edge_hls_url_ajax/ (authenticated, better quality)
        Fallback to chatvideocontext API
        """
        # Method 1: Authenticated edge HLS
        if self.auth.get_cookies().get("sessionid"):
            try:
                url = "https://chaturbate.com/get_edge_hls_url_ajax/"
                headers = self._get_headers()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                headers["X-Requested-With"] = "XMLHttpRequest"

                csrf = self.auth.get_cookies().get("csrftoken", "")
                if csrf:
                    headers["X-CSRFToken"] = csrf

                data = f"room_slug={username}&bandwidth=high"

                resp = await self._request(
                    "POST", url, headers=headers, data=data
                )

                if resp and resp.status == 200:
                    result = resp.json()
                    hls_url = result.get("url")
                    if hls_url:
                        logger.debug("Got edge HLS URL",
                                username=username, source="edge_ajax")
                        return hls_url
            except Exception as e:
                logger.debug("Edge HLS failed", username=username, error=str(e))

        # Method 2: chatvideocontext API
        try:
            api_url = f"https://chaturbate.com/api/chatvideocontext/{username}/"
            resp = await self._request("GET", api_url)

            if resp and resp.status == 200:
                data = resp.json()

                # Check quality sources in priority order
                for field in [
                    "hls_source_1080p", "hls_source_hd",
                    "hls_source_high", "hls_source_720p",
                    "hls_source"
                ]:
                    if data.get(field):
                        logger.debug("Got HLS URL from API",
                                username=username, field=field)
                        return data[field]
        except Exception as e:
            logger.debug("API HLS failed", username=username, error=str(e))

        return None


class _FakeResponse:
    """Holds response data after the aiohttp context has exited"""

    def __init__(self, status: int, body: bytes, headers: Any, content_type: str):
        self.status = status
        self._body = body
        self.headers = headers
        self.content_type = content_type

    def json(self) -> Any:
        import json as _json
        return _json.loads(self._body)

    def text(self) -> str:
        return self._body.decode("utf-8", errors="replace")
