from __future__ import annotations

import json
import time
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


def _cookies_to_dict(
    cookies: Optional[list[dict[str, Any]]] = None,
    cookie_header: Optional[str] = None,
) -> dict[str, str]:
    values: dict[str, str] = {}
    if cookie_header:
        for part in cookie_header.split(";"):
            if "=" not in part:
                continue
            name, value = part.split("=", 1)
            name = name.strip()
            if name:
                values[name] = value.strip()
    for cookie in cookies or []:
        if not isinstance(cookie, dict):
            continue
        name = cookie.get("name")
        value = cookie.get("value")
        if name and value is not None:
            values[str(name)] = str(value)
    return values


def _cookie_list(cookie_map: dict[str, str]) -> list[dict[str, str]]:
    return [{"name": name, "value": value} for name, value in cookie_map.items()]


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
        from ..resolvers.chaturbate import (
            resolve_llhls_master_playlist,
            resolve_m3u8_async,
        )

        status = ProviderStatus(False, source_type=self.source_type)
        if self.api:
            try:
                status = await self.check_status(target)
            except Exception:
                status = ProviderStatus(False, source_type=self.source_type)
        headers = self._headers(target)
        url = await resolve_m3u8_async(target, max_height=max_height)
        if not url:
            raise ProviderOfflineError(f"Aucun flux Chaturbate pour {target}")
        hls_master = await resolve_llhls_master_playlist(
            url,
            max_height=max_height,
            headers=headers,
        )
        ffmpeg_video_stream_index = (
            hls_master.get("video_stream_index") if hls_master else None
        )
        return ResolvedStream(
            url=url,
            headers=headers,
            source_type=self.source_type,
            ffmpeg_video_stream_index=(
                int(ffmpeg_video_stream_index)
                if ffmpeg_video_stream_index is not None else None
            ),
            hls_playlist_text=hls_master.get("text") if hls_master else None,
            hls_playlist_base_url=hls_master.get("base_url") if hls_master else None,
            hls_playlist_content_type=hls_master.get("content_type") if hls_master else None,
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

    async def import_session(
        self,
        username: Optional[str] = None,
        cookie_header: Optional[str] = None,
        cookies: Optional[list[dict[str, Any]]] = None,
        local_storage: Optional[list[dict[str, Any]]] = None,
        user_agent: Optional[str] = None,
        x_bc: Optional[str] = None,
    ) -> dict[str, Any]:
        if not self.auth:
            raise ProviderAuthError("Chaturbate auth service non initialise")
        cookie_map = _cookies_to_dict(cookies, cookie_header)
        if not cookie_map:
            return {"success": False, "error": "Chaturbate cookies are required; include sessionid and csrftoken from the same browser session"}
        if not cookie_map.get("sessionid"):
            return {"success": False, "error": "Chaturbate sessionid cookie is required for session import"}
        if user_agent:
            self.auth._user_agent = user_agent
        self.auth._cookies = cookie_map
        self.auth._username = (username or self.auth._username or "").strip() or None
        self.auth._last_error = None
        verified = await self.auth._validate_session()
        self.auth._is_logged_in = bool(verified)
        now = int(time.time())
        row = await self.auth.db.get_auth_state()
        saved_username = self.auth._username or (row or {}).get("username") or ""
        password_hash = (row or {}).get("password_hash") or ""
        validation_error = getattr(self.auth, "_last_validation_error", None) or getattr(self.auth, "_last_error", None)
        last_error = None if verified else (
            validation_error
            or "Imported Chaturbate session is not verified; include the same browser cookies and User-Agent"
        )
        if not verified:
            self.auth._last_error = last_error
        await self.auth.db.save_auth_state(
            username=saved_username,
            password_hash=password_hash,
            is_logged_in=bool(verified),
            session_cookies=json.dumps(cookie_map),
            cf_clearance=cookie_map.get("cf_clearance"),
            csrf_token=cookie_map.get("csrftoken"),
            last_login_at=now if verified else None,
            last_error=last_error,
        )
        if self.session_store:
            await self.session_store.save(
                self.source_type,
                username=saved_username or None,
                is_logged_in=bool(verified),
                cookies=cookies or _cookie_list(cookie_map),
                local_storage=local_storage or [],
                last_error=last_error,
            )
        if not verified:
            return {"success": False, "error": last_error}
        return {"success": True, "username": saved_username, "hasCookies": True}

    async def sync_following(self) -> list[dict[str, Any]]:
        if not self.api:
            raise ProviderAuthError("Chaturbate API non initialisee")
        self._require_verified_auth()
        return await self.api.get_followed_models()

    async def follow(self, username: str) -> dict[str, Any]:
        if not self.api:
            raise ProviderAuthError("Chaturbate API non initialisee")
        self._require_verified_auth()
        ok = await self.api.follow_model(username)
        return {"success": bool(ok)}

    async def unfollow(self, username: str) -> dict[str, Any]:
        if not self.api:
            raise ProviderAuthError("Chaturbate API non initialisee")
        self._require_verified_auth()
        ok = await self.api.unfollow_model(username)
        return {"success": bool(ok)}

    async def is_following(self, username: str) -> bool:
        if not self.api:
            return False
        if not self._has_verified_auth():
            return False
        return bool(await self.api.is_following(username))

    def _has_verified_auth(self) -> bool:
        if not self.auth:
            return False
        status = self.auth.get_status()
        cookies = self.auth.get_cookies()
        return bool(status.get("isLoggedIn") and cookies.get("sessionid"))

    def _require_verified_auth(self) -> None:
        if not self._has_verified_auth():
            raise ProviderAuthError("Connexion Chaturbate requise")

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

    async def import_session(
        self,
        username: Optional[str] = None,
        cookie_header: Optional[str] = None,
        cookies: Optional[list[dict[str, Any]]] = None,
        local_storage: Optional[list[dict[str, Any]]] = None,
        user_agent: Optional[str] = None,
        x_bc: Optional[str] = None,
    ) -> dict[str, Any]:
        if not self.auth:
            raise ProviderAuthError("CAM4 auth service non initialise")
        cookie_map = _cookies_to_dict(cookies, cookie_header)
        if not cookie_map:
            return {"success": False, "error": "CAM4 cookies are required"}
        session_username = (username or self.auth.get_status().get("username") or "").strip()
        if not session_username:
            return {"success": False, "error": "Username is required for CAM4 session import"}
        if user_agent:
            self.auth._user_agent = user_agent
        self.auth._cookies = cookie_map
        self.auth._username = session_username
        self.auth._last_error = None
        verified = await self.auth._validate_session()
        self.auth._is_logged_in = bool(verified)
        self.auth._last_login_at = int(time.time()) if verified else None
        last_error = None if verified else "Imported CAM4 session is not verified"
        if verified:
            await self.auth._persist_success(session_username)
        else:
            self.auth._last_error = last_error
            await self.auth._persist_error(session_username, last_error)
        if self.session_store:
            await self.session_store.save(
                self.source_type,
                username=session_username,
                is_logged_in=bool(verified),
                cookies=cookies or _cookie_list(cookie_map),
                local_storage=local_storage or [],
                last_error=last_error,
            )
        if not verified:
            return {"success": False, "error": last_error}
        return {"success": True, "username": session_username, "hasCookies": True}

    async def sync_following(self) -> list[dict[str, Any]]:
        from ..services import cam4_source

        if not self.auth:
            raise ProviderAuthError("CAM4 auth service non initialise")
        cookies = self.auth.get_cookies()
        if not cookies:
            raise ProviderAuthError("CAM4 session absente")
        try:
            return await cam4_source.list_followed(cookies)
        except cam4_source.CAM4FollowingError as exc:
            message = str(exc)
            if "session" in message.lower() or "login" in message.lower():
                raise ProviderAuthError(message) from exc
            raise ProviderError(message) from exc

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
