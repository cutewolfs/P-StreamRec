from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


class ProviderError(Exception):
    """Base error for provider integrations."""

    room_status = "error"


class ProviderOfflineError(ProviderError):
    room_status = "offline"


class ProviderPrivateError(ProviderError):
    room_status = "private"


class ProviderAuthError(ProviderError):
    room_status = "auth_required"


class ProviderInteractionRequired(ProviderError):
    room_status = "interaction_required"


@dataclass(frozen=True)
class ProviderCapabilities:
    can_login: bool = False
    can_follow: bool = False
    can_sync_following: bool = False
    can_discover: bool = False
    can_stream: bool = True
    can_record: bool = True
    uses_browser: bool = False
    uses_ytdlp: bool = False


@dataclass
class ResolvedStream:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    source_type: str = ""
    is_live: bool = True
    room_status: Optional[str] = None
    viewers: int = 0
    tags: list[str] = field(default_factory=list)
    thumbnail: Optional[str] = None
    title: Optional[str] = None

    def safe_headers(self) -> dict[str, str]:
        hidden = {"authorization", "cookie", "set-cookie", "x-csrf-token"}
        return {
            key: value
            for key, value in (self.headers or {}).items()
            if key.lower() not in hidden
        }


@dataclass
class ProviderStatus:
    is_online: bool
    viewers: int = 0
    room_status: Optional[str] = None
    hls_source: Optional[str] = None
    thumbnail: Optional[str] = None
    source_type: str = ""
    tags: list[str] = field(default_factory=list)
    detail: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "is_online": bool(self.is_online),
            "viewers": int(self.viewers or 0),
            "room_status": self.room_status,
            "hls_source": self.hls_source,
            "thumbnail": self.thumbnail,
            "source_type": self.source_type,
            "tags": list(self.tags or []),
            "detail": self.detail,
        }


class BaseProvider:
    source_type = ""
    display_name = ""
    domains: tuple[str, ...] = ()
    capabilities = ProviderCapabilities()

    def __init__(self, session_store=None):
        self.session_store = session_store

    def metadata(self) -> dict[str, Any]:
        return {
            "sourceType": self.source_type,
            "displayName": self.display_name or self.source_type,
            "domains": list(self.domains),
            "capabilities": asdict(self.capabilities),
        }

    def canonical_url(self, target: str) -> str:
        return target

    async def resolve_stream(
        self, target: str, max_height: Optional[int] = None
    ) -> ResolvedStream:
        raise ProviderError(f"{self.display_name or self.source_type} ne supporte pas resolve_stream")

    async def check_status(self, username: str) -> ProviderStatus:
        try:
            stream = await self.resolve_stream(username)
            return ProviderStatus(
                is_online=stream.is_live,
                viewers=stream.viewers,
                room_status=stream.room_status or "public",
                hls_source=stream.url,
                thumbnail=stream.thumbnail,
                source_type=self.source_type,
                tags=list(stream.tags or []),
            )
        except ProviderPrivateError as exc:
            return ProviderStatus(False, room_status="private", source_type=self.source_type, detail=str(exc))
        except ProviderInteractionRequired as exc:
            return ProviderStatus(False, room_status="interaction_required", source_type=self.source_type, detail=str(exc))
        except ProviderAuthError as exc:
            return ProviderStatus(False, room_status="auth_required", source_type=self.source_type, detail=str(exc))
        except ProviderOfflineError as exc:
            return ProviderStatus(False, room_status="offline", source_type=self.source_type, detail=str(exc))

    async def list_live_models(self, **kwargs) -> dict[str, Any]:
        return {"models": [], "total": 0, "page": kwargs.get("page", 1), "limit": kwargs.get("limit", 24), "total_pages": 1}

    async def login(self, username: str, password: str) -> dict[str, Any]:
        raise ProviderError(f"Connexion non supportee pour {self.display_name or self.source_type}")

    async def logout(self) -> dict[str, Any]:
        if self.session_store:
            await self.session_store.clear(self.source_type)
        return {"success": True}

    async def sync_following(self) -> list[dict[str, Any]]:
        raise ProviderError(f"Synchronisation non supportee pour {self.display_name or self.source_type}")

    async def follow(self, username: str) -> dict[str, Any]:
        raise ProviderError(f"Follow distant non supporte pour {self.display_name or self.source_type}")

    async def unfollow(self, username: str) -> dict[str, Any]:
        raise ProviderError(f"Unfollow distant non supporte pour {self.display_name or self.source_type}")

    async def is_following(self, username: str) -> bool:
        return False
