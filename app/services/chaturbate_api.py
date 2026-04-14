"""
Chaturbate API Client
Authenticated API calls for model discovery, following, and stream resolution
"""

import asyncio
import re
import time
from typing import Optional, List, Dict, Any

import aiohttp

from ..logger import logger
from ..core.config import CB_REQUEST_DELAY
from .chaturbate_auth import ChaturbateAuthService
from .flaresolverr import FlareSolverrClient


class ChaturbateAPI:
    def __init__(
        self,
        auth_service: ChaturbateAuthService,
        flaresolverr: Optional[FlareSolverrClient] = None
    ):
        self.auth = auth_service
        self.flaresolverr = flaresolverr
        self._semaphore = asyncio.Semaphore(2)
        self._last_request_time: float = 0

    async def _rate_limit(self):
        """Apply rate limiting between requests"""
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < CB_REQUEST_DELAY:
            await asyncio.sleep(CB_REQUEST_DELAY - elapsed)
        self._last_request_time = time.time()

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
    ) -> Optional[aiohttp.ClientResponse]:
        """Make an HTTP request with rate limiting, retry on 403"""
        async with self._semaphore:
            await self._rate_limit()

            if headers is None:
                headers = self._get_headers()

            async with aiohttp.ClientSession() as session:
                try:
                    async with session.request(
                        method, url, headers=headers, ssl=False,
                        timeout=aiohttp.ClientTimeout(total=15),
                        **kwargs
                    ) as resp:
                        if resp.status == 403 and self.flaresolverr:
                            # Cloudflare block - try FlareSolverr
                            logger.info("403 detected, attempting FlareSolverr bypass")
                            solution = await self.flaresolverr.solve_challenge(url)
                            if solution:
                                cookies = solution.get("cookies", {})
                                new_ua = solution.get("user_agent", "")
                                headers["User-Agent"] = new_ua
                                cookie_parts = []
                                for k, v in self.auth.get_cookies().items():
                                    cookie_parts.append(f"{k}={v}")
                                for k, v in cookies.items():
                                    cookie_parts.append(f"{k}={v}")
                                headers["Cookie"] = "; ".join(cookie_parts)

                                # Retry
                                await self._rate_limit()
                                async with session.request(
                                    method, url, headers=headers, ssl=False,
                                    timeout=aiohttp.ClientTimeout(total=15),
                                    **kwargs
                                ) as retry_resp:
                                    # Read body before response context exits
                                    body = await retry_resp.read()
                                    return _FakeResponse(
                                        retry_resp.status,
                                        body,
                                        retry_resp.headers,
                                        retry_resp.content_type
                                    )

                        # Read body before context exits
                        body = await resp.read()
                        return _FakeResponse(
                            resp.status, body, resp.headers, resp.content_type
                        )

                except Exception as e:
                    logger.error("Request error", url=url, error=str(e))
                    return None

    async def get_live_models(
        self,
        page: int = 1,
        limit: int = 24,
        gender: str = "",
        search: str = ""
    ) -> Dict[str, Any]:
        """
        Fetch live models from Chaturbate.
        Uses the roomlist API or scrapes the homepage.
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
            if search:
                api_url += f"&keywords={search}"

            resp = await self._request("GET", api_url)

            if resp and resp.status == 200:
                try:
                    data = resp.json()
                    rooms = data.get("rooms", [])
                    total = data.get("total_count", len(rooms))

                    models = []
                    for room in rooms:
                        models.append({
                            "username": room.get("username", ""),
                            "display_name": room.get("display_name", ""),
                            "thumbnail": room.get("img", ""),
                            "viewers": room.get("num_users", 0),
                            "subject": room.get("subject", ""),
                            "age": room.get("age", None),
                            "gender": room.get("gender", ""),
                            "is_online": True,
                            "tags": room.get("tags", []),
                        })

                    total_pages = max(1, (total + limit - 1) // limit)

                    return {
                        "models": models,
                        "total": total,
                        "page": page,
                        "limit": limit,
                        "total_pages": total_pages,
                    }
                except Exception as e:
                    logger.debug("API roomlist parse error", error=str(e))

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

        resp = await self._request("GET", url)
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
        Fetch all followed models using Chaturbate's roomlist API.
        Uses follow=true for online models, follow=true&offline=true for offline.
        """
        if not self.auth.get_cookies().get("sessionid"):
            return []

        models = []
        seen = set()

        headers = self._get_headers()
        headers["Accept"] = "application/json"
        headers["X-Requested-With"] = "XMLHttpRequest"
        headers["Referer"] = "https://chaturbate.com/followed-cams/"

        # Fetch online followed models
        online_models = await self._fetch_followed_page(headers, offline=False)
        for item in online_models:
            username = item.get("username", "")
            if username and username not in seen:
                seen.add(username)
                models.append(self._parse_room_item(item, is_online=True))

        # Fetch offline followed models (with pagination)
        offline_models = await self._fetch_followed_page(headers, offline=True)
        for item in offline_models:
            username = item.get("username", "")
            if username and username not in seen:
                seen.add(username)
                models.append(self._parse_room_item(item, is_online=False))

        logger.debug("Fetched followed models",
                     total=len(models),
                     online=len(online_models),
                     offline=len(offline_models))
        return models

    async def _fetch_followed_page(
        self, headers: Dict[str, str], offline: bool = False
    ) -> List[Dict[str, Any]]:
        """Fetch one category (online or offline) of followed models with pagination."""
        all_rooms = []
        limit = 90
        offset = 0

        while True:
            params = f"limit={limit}&offset={offset}&follow=true"
            if offline:
                params += "&offline=true"
            url = f"https://chaturbate.com/api/ts/roomlist/room-list/?{params}"

            resp = await self._request("GET", url, headers=headers)
            if not resp or resp.status != 200:
                logger.debug("Followed roomlist API failed",
                            offline=offline,
                            status=resp.status if resp else None)
                break

            try:
                data = resp.json()
            except Exception:
                break

            rooms = data.get("rooms", [])
            total_count = data.get("total_count", 0)

            all_rooms.extend(rooms)

            offset += limit
            if not rooms or offset >= total_count:
                break

            await self._rate_limit()

        return all_rooms

    @staticmethod
    def _parse_room_item(item: Dict[str, Any], is_online: bool = True) -> Dict[str, Any]:
        """Parse a room item from the roomlist API into our model format."""
        username = item.get("username", "")
        thumb = item.get("img", "")
        if thumb and thumb.startswith("//"):
            thumb = "https:" + thumb
        if not thumb:
            thumb = f"https://roomimg.stream.highwebmedia.com/ri/{username}.jpg"

        return {
            "username": username,
            "display_name": item.get("display_name") or username,
            "is_online": is_online or item.get("current_show") == "public",
            "viewers": item.get("num_users", 0),
            "thumbnail_url": thumb,
            "tags": item.get("tags", []),
            "subject": item.get("room_subject") or item.get("subject", ""),
            "gender": item.get("gender", ""),
            "num_followers": item.get("num_followers", 0),
        }

    async def _toggle_follow(self, username: str, action: str) -> bool:
        """Follow or unfollow a model on Chaturbate (requires auth)"""
        if not self.auth.get_cookies().get("sessionid"):
            logger.warning(f"Cannot {action}: not logged in")
            return False

        try:
            url = f"https://chaturbate.com/follow/{action}/{username}/"
            headers = self._get_headers()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["X-Requested-With"] = "XMLHttpRequest"
            headers["Referer"] = f"https://chaturbate.com/{username}/"

            csrf = self.auth.get_cookies().get("csrftoken", "")
            if csrf:
                headers["X-CSRFToken"] = csrf

            data = f"room_slug={username}"
            resp = await self._request("POST", url, headers=headers, data=data)

            if resp and resp.status == 200:
                logger.info(f"{action.capitalize()}ed model on Chaturbate", username=username)
                return True
            logger.warning(f"Failed to {action} model", username=username,
                          status=resp.status if resp else None)
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
                    "hls_source_hd", "hls_source_high",
                    "hls_source_720p", "hls_source_1080p",
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
