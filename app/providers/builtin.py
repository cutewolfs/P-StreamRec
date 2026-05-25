from __future__ import annotations

from typing import Any, Optional

from .base import (
    BaseProvider,
    ProviderAuthError,
    ProviderCapabilities,
    ProviderError,
    ProviderOfflineError,
    ProviderStatus,
    ResolvedStream,
)
from .browser import DEFAULT_USER_AGENT


class ChaturbateProvider(BaseProvider):
    source_type = "chaturbate"
    display_name = "Chaturbate"
    domains = ("chaturbate.com", "highwebmedia.com", "mmcdn.com")
    capabilities = ProviderCapabilities(
        can_login=True,
        can_follow=True,
        can_sync_following=True,
        can_discover=True,
    )

    def __init__(self, api=None, auth=None, session_store=None):
        super().__init__(session_store=session_store)
        self.api = api
        self.auth = auth

    def canonical_url(self, target: str) -> str:
        target = (target or "").strip().strip("/")
        if target.startswith("http://") or target.startswith("https://"):
            return target
        return f"https://chaturbate.com/{target}/"

    async def resolve_stream(
        self, target: str, max_height: Optional[int] = None
    ) -> ResolvedStream:
        from ..resolvers.chaturbate import resolve_m3u8_async

        status = ProviderStatus(False, source_type=self.source_type)
        if self.api:
            try:
                status = await self.check_status(target)
            except Exception:
                status = ProviderStatus(False, source_type=self.source_type)
        url = await resolve_m3u8_async(target, max_height=max_height)
        if not url:
            raise ProviderOfflineError(f"Aucun flux Chaturbate pour {target}")
        return ResolvedStream(
            url=url,
            headers=self._headers(target),
            source_type=self.source_type,
            is_live=True,
            room_status="public",
            viewers=int(status.viewers or 0),
            tags=list(status.tags or []),
            thumbnail=status.thumbnail,
        )

    async def check_status(self, username: str) -> ProviderStatus:
        if self.api:
            data = await self.api.check_status(username)
            meta = {}
            if bool(data.get("is_online")) and (not data.get("tags") or not int(data.get("viewers") or 0)):
                meta = await self._discover_metadata(username)
            return ProviderStatus(
                is_online=bool(data.get("is_online")),
                viewers=int(data.get("viewers") or meta.get("viewers") or 0),
                room_status=data.get("room_status") or meta.get("room_status"),
                hls_source=data.get("hls_source"),
                thumbnail=meta.get("thumbnail"),
                source_type=self.source_type,
                tags=list(data.get("tags") or meta.get("tags") or []),
            )
        return await super().check_status(username)

    async def _discover_metadata(self, username: str) -> dict[str, Any]:
        if not self.api:
            return {}
        try:
            data = await self.api.get_live_models(page=1, limit=12, search=username, tag="")
        except Exception:
            return {}
        needle = (username or "").strip().lower()
        for item in data.get("models") or []:
            if str(item.get("username") or "").strip().lower() == needle:
                return item
        return {}

    async def list_live_models(self, **kwargs) -> dict[str, Any]:
        if not self.api:
            return await super().list_live_models(**kwargs)
        tags = kwargs.get("tags") or []
        first_tag = tags[0] if isinstance(tags, list) and tags else kwargs.get("tag", "")
        data = await self.api.get_live_models(
            page=kwargs.get("page", 1),
            limit=kwargs.get("limit", 24),
            gender=kwargs.get("gender") or "",
            search=kwargs.get("search") or "",
            tag=first_tag or "",
        )
        for item in data.get("models") or []:
            item["source_type"] = self.source_type
        return data

    async def login(self, username: str, password: str) -> dict[str, Any]:
        if not self.auth:
            raise ProviderAuthError("Chaturbate auth service non initialise")
        return await self.auth.login(username, password)

    async def logout(self) -> dict[str, Any]:
        if self.auth:
            await self.auth.logout()
        return {"success": True}

    async def sync_following(self) -> list[dict[str, Any]]:
        if not self.api:
            raise ProviderAuthError("Chaturbate API non initialisee")
        return await self.api.get_followed_models()

    async def follow(self, username: str) -> dict[str, Any]:
        if not self.api:
            raise ProviderAuthError("Chaturbate API non initialisee")
        ok = await self.api.follow_model(username)
        return {"success": bool(ok)}

    async def unfollow(self, username: str) -> dict[str, Any]:
        if not self.api:
            raise ProviderAuthError("Chaturbate API non initialisee")
        ok = await self.api.unfollow_model(username)
        return {"success": bool(ok)}

    async def is_following(self, username: str) -> bool:
        if not self.api:
            return False
        return bool(await self.api.is_following(username))

    def _headers(self, target: str) -> dict[str, str]:
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": self.canonical_url(target),
            "Origin": "https://chaturbate.com",
            "Connection": "keep-alive",
        }
        if self.auth:
            try:
                cookies = self.auth.get_cookies()
            except Exception:
                cookies = {}
            if cookies:
                headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in cookies.items())
        return headers


class CAM4Provider(BaseProvider):
    source_type = "cam4"
    display_name = "CAM4"
    domains = ("cam4.com",)
    capabilities = ProviderCapabilities(
        can_login=True,
        can_follow=True,
        can_sync_following=True,
        can_discover=True,
    )

    def __init__(self, auth=None, session_store=None):
        super().__init__(session_store=session_store)
        self.auth = auth

    def canonical_url(self, target: str) -> str:
        target = (target or "").strip().strip("/")
        if target.startswith("http://") or target.startswith("https://"):
            return target
        return f"https://www.cam4.com/{target}"

    async def resolve_stream(
        self, target: str, max_height: Optional[int] = None
    ) -> ResolvedStream:
        from ..services import cam4_source

        status_data: dict[str, Any] = {}
        try:
            status_data = await cam4_source.check_status(target)
        except Exception:
            status_data = {}
        url = status_data.get("hls_source") or await cam4_source.resolve(target, max_height=max_height)
        if not url:
            raise ProviderOfflineError(f"Aucun flux CAM4 pour {target}")
        return ResolvedStream(
            url=url,
            headers=self._headers(target),
            source_type=self.source_type,
            is_live=True,
            room_status="public",
            viewers=int(status_data.get("viewers") or 0),
            tags=list(status_data.get("tags") or []),
            thumbnail=status_data.get("thumbnail"),
        )

    async def check_status(self, username: str) -> ProviderStatus:
        from ..services import cam4_source

        data = await cam4_source.check_status(username)
        return ProviderStatus(
            is_online=bool(data.get("is_online")),
            viewers=int(data.get("viewers") or 0),
            room_status=data.get("room_status"),
            hls_source=data.get("hls_source"),
            source_type=self.source_type,
            tags=list(data.get("tags") or []),
        )

    async def list_live_models(self, **kwargs) -> dict[str, Any]:
        from ..services import cam4_source

        data = await cam4_source.list_live_models(
            page=kwargs.get("page", 1),
            limit=kwargs.get("limit", 24),
            gender=kwargs.get("gender") or None,
            search=kwargs.get("search") or None,
            tags=kwargs.get("tags") or None,
        )
        for item in data.get("models") or []:
            item["source_type"] = self.source_type
        return data

    async def login(self, username: str, password: str) -> dict[str, Any]:
        if not self.auth:
            raise ProviderAuthError("CAM4 auth service non initialise")
        return await self.auth.login(username, password)

    async def logout(self) -> dict[str, Any]:
        if self.auth:
            await self.auth.logout()
        return {"success": True}

    async def sync_following(self) -> list[dict[str, Any]]:
        from ..services import cam4_source

        if not self.auth:
            raise ProviderAuthError("CAM4 auth service non initialise")
        cookies = self.auth.get_cookies()
        if not cookies:
            raise ProviderAuthError("CAM4 session absente")
        return await cam4_source.list_followed(cookies)

    async def follow(self, username: str) -> dict[str, Any]:
        from ..services import cam4_source

        auth_user, cookies = self._auth_context()
        return await cam4_source.follow(auth_user, username, cookies)

    async def unfollow(self, username: str) -> dict[str, Any]:
        from ..services import cam4_source

        auth_user, cookies = self._auth_context()
        return await cam4_source.unfollow(auth_user, username, cookies)

    async def is_following(self, username: str) -> bool:
        from ..services import cam4_source

        try:
            auth_user, cookies = self._auth_context()
        except ProviderError:
            return False
        return bool(await cam4_source.is_favorite(auth_user, username, cookies))

    def _auth_context(self) -> tuple[str, dict[str, str]]:
        if not self.auth:
            raise ProviderAuthError("CAM4 auth service non initialise")
        status = self.auth.get_status()
        cookies = self.auth.get_cookies()
        auth_user = status.get("username")
        if not auth_user or not cookies:
            raise ProviderAuthError("CAM4 session absente")
        return auth_user, cookies

    def _headers(self, target: str) -> dict[str, str]:
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": self.canonical_url(target),
            "Origin": "https://www.cam4.com",
        }
        if self.auth:
            cookies = self.auth.get_cookies()
            if cookies:
                headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in cookies.items())
        return headers
