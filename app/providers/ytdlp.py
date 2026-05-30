from __future__ import annotations

import asyncio
from typing import Any, Optional
from urllib.parse import quote_plus, urlparse

from ..logger import logger
from .base import (
    BaseProvider,
    ProviderCapabilities,
    ProviderError,
    ProviderOfflineError,
    ProviderPrivateError,
    ProviderStatus,
    ResolvedStream,
)
from .browser import DEFAULT_USER_AGENT


class YtDlpProvider(BaseProvider):
    def __init__(
        self,
        source_type: str,
        display_name: str,
        url_template: str,
        domains: tuple[str, ...],
        session_store=None,
        browser_fallback: Optional[BaseProvider] = None,
    ):
        super().__init__(session_store=session_store)
        self.source_type = source_type
        self.display_name = display_name
        self.url_template = url_template
        self.domains = domains
        self.browser_fallback = browser_fallback
        fallback_caps = getattr(browser_fallback, "capabilities", None)
        self.capabilities = ProviderCapabilities(
            can_login=bool(fallback_caps and fallback_caps.can_login),
            can_follow=bool(fallback_caps and fallback_caps.can_follow),
            can_sync_following=bool(fallback_caps and fallback_caps.can_sync_following),
            can_discover=bool(fallback_caps and fallback_caps.can_discover),
            uses_browser=bool(browser_fallback),
            uses_ytdlp=True,
        )

    def canonical_url(self, target: str) -> str:
        target = (target or "").strip()
        if target.startswith("http://") or target.startswith("https://"):
            return target
        return self.url_template.format(username=quote_plus(target))

    async def resolve_stream(
        self, target: str, max_height: Optional[int] = None
    ) -> ResolvedStream:
        page_url = self.canonical_url(target)
        try:
            info = await asyncio.to_thread(self._extract_info, page_url)
            stream_url, fmt_headers = self._select_media_url(info, max_height)
            headers = self._headers(page_url)
            headers.update(fmt_headers or {})
            return ResolvedStream(
                url=stream_url,
                headers=headers,
                source_type=self.source_type,
                is_live=True,
                room_status="public",
                viewers=int(info.get("view_count") or info.get("viewers") or 0),
                tags=self._tags(info),
                thumbnail=info.get("thumbnail"),
                title=info.get("title"),
            )
        except ProviderPrivateError as exc:
            if self._can_use_guarded_stripchat_fallback():
                return await self._resolve_stripchat_guarded_fallback(target, max_height, exc)
            raise
        except ProviderOfflineError as exc:
            if self._can_use_guarded_stripchat_fallback():
                return await self._resolve_stripchat_guarded_fallback(target, max_height, exc)
            raise
        except Exception as exc:
            if self._can_use_guarded_stripchat_fallback():
                return await self._resolve_stripchat_guarded_fallback(target, max_height, exc)
            if self._looks_offline(exc):
                raise ProviderOfflineError(str(exc)) from exc
            if self._looks_private(exc):
                raise ProviderPrivateError(str(exc)) from exc
            logger.debug(
                "yt-dlp provider resolve failed",
                source_type=self.source_type,
                target=target,
                error=str(exc),
            )
            if self.browser_fallback:
                return await self.browser_fallback.resolve_stream(target, max_height=max_height)
            raise ProviderError(f"yt-dlp n'a pas trouve de flux pour {self.display_name}/{target}") from exc

    def _can_use_guarded_stripchat_fallback(self) -> bool:
        return self.source_type == "stripchat" and bool(self.browser_fallback)

    async def _resolve_stripchat_guarded_fallback(
        self,
        target: str,
        max_height: Optional[int],
        original_exc: Exception,
    ) -> ResolvedStream:
        logger.debug(
            "yt-dlp Stripchat resolve failed; trying guarded public API fallback",
            target=target,
            error=str(original_exc),
        )
        assert self.browser_fallback is not None
        return await self.browser_fallback.resolve_stream(target, max_height=max_height)

    async def check_status(self, username: str) -> ProviderStatus:
        try:
            stream = await self.resolve_stream(username)
            return ProviderStatus(
                is_online=True,
                viewers=stream.viewers,
                room_status=stream.room_status or "public",
                hls_source=stream.url,
                thumbnail=stream.thumbnail,
                source_type=self.source_type,
                tags=list(stream.tags or []),
            )
        except ProviderPrivateError as exc:
            return ProviderStatus(False, room_status="private", source_type=self.source_type, detail=str(exc))
        except ProviderOfflineError as exc:
            return ProviderStatus(False, room_status="offline", source_type=self.source_type, detail=str(exc))

    async def login(self, username: str, password: str) -> dict[str, object]:
        if not self.browser_fallback or not self.capabilities.can_login:
            return {"success": False, "error": "Connexion non supportee"}
        return await self.browser_fallback.login(username, password)

    async def import_session(
        self,
        username: Optional[str] = None,
        cookie_header: Optional[str] = None,
        cookies: Optional[list[dict[str, Any]]] = None,
        local_storage: Optional[list[dict[str, Any]]] = None,
        user_agent: Optional[str] = None,
        x_bc: Optional[str] = None,
    ) -> dict[str, object]:
        if not self.browser_fallback or not self.capabilities.can_login:
            return {"success": False, "error": "Import de session non supporte"}
        return await self.browser_fallback.import_session(
            username=username,
            cookie_header=cookie_header,
            cookies=cookies,
            local_storage=local_storage,
            user_agent=user_agent,
            x_bc=x_bc,
        )

    async def logout(self) -> dict[str, object]:
        if self.browser_fallback:
            return await self.browser_fallback.logout()
        return await super().logout()

    async def list_live_models(self, **kwargs) -> dict[str, object]:
        if self.browser_fallback:
            return await self.browser_fallback.list_live_models(**kwargs)
        return await super().list_live_models(**kwargs)

    async def sync_following(self) -> list[dict[str, object]]:
        if self.browser_fallback and self.capabilities.can_sync_following:
            return await self.browser_fallback.sync_following()
        return await super().sync_following()

    async def follow(self, username: str) -> dict[str, object]:
        if self.browser_fallback and self.capabilities.can_follow:
            return await self.browser_fallback.follow(username)
        return await super().follow(username)

    async def unfollow(self, username: str) -> dict[str, object]:
        if self.browser_fallback and self.capabilities.can_follow:
            return await self.browser_fallback.unfollow(username)
        return await super().unfollow(username)

    async def is_following(self, username: str) -> bool:
        if self.browser_fallback and self.capabilities.can_follow:
            return await self.browser_fallback.is_following(username)
        return await super().is_following(username)

    def _extract_info(self, page_url: str) -> dict:
        try:
            from yt_dlp import YoutubeDL
        except Exception as exc:
            raise ProviderError("yt-dlp n'est pas installe") from exc

        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": 20,
            "format": "best",
        }
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(page_url, download=False)
        if not isinstance(info, dict):
            raise ProviderError("Extraction yt-dlp invalide")
        return info

    def _select_media_url(self, info: dict, max_height: Optional[int]) -> tuple[str, dict[str, str]]:
        candidates = []
        direct_url = info.get("url")
        if direct_url:
            candidates.append({
                "url": direct_url,
                "height": info.get("height") or 0,
                "tbr": info.get("tbr") or 0,
                "http_headers": info.get("http_headers") or {},
                "protocol": info.get("protocol") or "",
            })
        for fmt in info.get("formats") or []:
            url = fmt.get("url")
            if not url:
                continue
            protocol = (fmt.get("protocol") or "").lower()
            lower = url.lower()
            if ".m3u8" not in lower and ".mpd" not in lower and "m3u8" not in protocol and "dash" not in protocol:
                continue
            candidates.append(fmt)

        if not candidates:
            raise ProviderOfflineError("Aucun HLS/DASH public dans l'extraction")

        def score(fmt: dict) -> tuple[int, int, int]:
            height = int(fmt.get("height") or 0)
            tbr = int(fmt.get("tbr") or 0)
            fits = 1 if not max_height or height <= max_height or height == 0 else 0
            return (fits, height if fits else -height, tbr)

        selected = sorted(candidates, key=score, reverse=True)[0]
        url = selected.get("url")
        if not url:
            raise ProviderOfflineError("Format sans URL")
        return url, dict(selected.get("http_headers") or {})

    def _headers(self, page_url: str) -> dict[str, str]:
        parsed = urlparse(page_url)
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": page_url,
        }
        if origin:
            headers["Origin"] = origin
        return headers

    def _tags(self, info: dict) -> list[str]:
        values = []
        for key in ("tags", "categories"):
            raw = info.get(key) or []
            if isinstance(raw, str):
                raw = [raw]
            if isinstance(raw, list):
                values.extend(raw)
        seen = set()
        tags = []
        for value in values:
            tag = str(value or "").strip().strip("#").lower()
            if not tag or tag in seen or len(tag) > 48:
                continue
            seen.add(tag)
            tags.append(tag)
        return tags[:12]

    def _looks_offline(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return any(word in text for word in ("offline", "not live", "not online", "no live"))

    def _looks_private(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return any(word in text for word in ("private", "premium", "ticket", "group show"))
