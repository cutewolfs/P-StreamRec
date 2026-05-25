from __future__ import annotations

import json
from typing import Any, Optional


class ProviderSessionStore:
    """SQLite-backed browser/session state for generic providers."""

    def __init__(self, db):
        self.db = db

    async def get(self, source_type: str) -> dict[str, Any]:
        row = await self.db.get_provider_session(source_type)
        if not row:
            return {}
        cookies = self._load_json(row.get("session_cookies"), [])
        local_storage = self._load_json(row.get("local_storage"), [])
        return {
            **row,
            "cookies": cookies,
            "localStorage": local_storage,
        }

    async def save(
        self,
        source_type: str,
        username: Optional[str] = None,
        is_logged_in: bool = False,
        cookies: Any = None,
        local_storage: Any = None,
        last_error: Optional[str] = None,
    ) -> None:
        await self.db.save_provider_session(
            source_type=source_type,
            username=username,
            is_logged_in=is_logged_in,
            session_cookies=json.dumps(cookies or []),
            local_storage=json.dumps(local_storage or []),
            last_error=last_error,
        )

    async def clear(self, source_type: str) -> None:
        await self.db.clear_provider_session(source_type)

    async def cookie_header(self, source_type: str) -> str:
        state = await self.get(source_type)
        return self.cookies_to_header(state.get("cookies"))

    @staticmethod
    def cookies_to_header(cookies: Any) -> str:
        if not cookies:
            return ""
        if isinstance(cookies, dict):
            return "; ".join(f"{key}={value}" for key, value in cookies.items())
        parts = []
        if isinstance(cookies, list):
            for cookie in cookies:
                if not isinstance(cookie, dict):
                    continue
                name = cookie.get("name")
                value = cookie.get("value")
                if name and value is not None:
                    parts.append(f"{name}={value}")
        return "; ".join(parts)

    @staticmethod
    def _load_json(value: Optional[str], default: Any) -> Any:
        if not value:
            return default
        try:
            return json.loads(value)
        except Exception:
            return default
