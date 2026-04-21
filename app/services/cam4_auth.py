"""
CAM4 Authentication Service.

Login programmatique via le REST interne de CAM4:
    POST https://www.cam4.com/rest/v2.0/login
    Content-Type: application/json
    Body: {"username": "...", "password": "..."}

Sur 200, on récupère les cookies de session (JSESSIONID, cam4_SESSION_ID, etc.)
et on les stocke pour les requêtes authentifiées suivantes (favorites, etc.).
Sur 403, l'API renvoie {"status": "INVALID_CREDENTIALS"|"twofa_required"|...}.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, Optional

import aiohttp

from ..logger import logger


_PREFIX = "cam4:"

LOGIN_URL = "https://www.cam4.com/rest/v2.0/login"
HOMEPAGE_URL = "https://www.cam4.com/"
LOGIN_PAGE_URL = "https://www.cam4.com/login"


class CAM4AuthService:
    def __init__(self, db):
        self.db = db
        self._cookies: Dict[str, str] = {}
        self._username: Optional[str] = None
        self._last_error: Optional[str] = None
        self._last_login_at: Optional[int] = None
        self._user_agent: str = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

    async def initialize(self) -> None:
        raw = await self.db.get_setting(f"{_PREFIX}session_cookies")
        if raw:
            try:
                self._cookies = json.loads(raw)
            except json.JSONDecodeError:
                self._cookies = {}
        self._username = await self.db.get_setting(f"{_PREFIX}username")
        ts = await self.db.get_setting(f"{_PREFIX}last_login_at")
        try:
            self._last_login_at = int(ts) if ts else None
        except (TypeError, ValueError):
            self._last_login_at = None
        self._last_error = await self.db.get_setting(f"{_PREFIX}last_error")
        if self._cookies:
            logger.info("CAM4 session restaurée depuis la DB", username=self._username)

    async def login(self, username: str, password: str) -> Dict[str, Any]:
        """Login programmatique via POST /rest/v2.0/login. Retourne
        {success, username?, error?}."""
        username = (username or "").strip()
        password = password or ""
        if not username or not password:
            err = "Username et password requis"
            self._last_error = err
            await self._persist_error(username, err)
            return {"success": False, "error": err}

        try:
            async with aiohttp.ClientSession() as session:
                # 1) Seed la session en chargeant la page de login (cookies initiaux)
                try:
                    await session.get(
                        LOGIN_PAGE_URL,
                        headers={"User-Agent": self._user_agent},
                        timeout=aiohttp.ClientTimeout(total=15),
                    )
                except Exception:
                    pass

                # 2) POST login JSON
                headers = {
                    "User-Agent": self._user_agent,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Origin": "https://www.cam4.com",
                    "Referer": LOGIN_PAGE_URL,
                    "X-Redirect-To": HOMEPAGE_URL,
                }
                payload = {"username": username, "password": password}
                async with session.post(
                    LOGIN_URL,
                    json=payload,
                    headers=headers,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    body_text = await resp.text()
                    try:
                        body = json.loads(body_text) if body_text else {}
                    except json.JSONDecodeError:
                        body = {}

                    if resp.status == 200 or resp.status == 201:
                        # Login réussi: agrège tous les cookies de la jar
                        cookies: Dict[str, str] = {}
                        for c in session.cookie_jar:
                            if c.key:
                                cookies[c.key] = c.value
                        if not cookies:
                            err = "Login OK mais aucun cookie reçu"
                            self._last_error = err
                            await self._persist_error(username, err)
                            return {"success": False, "error": err}

                        self._cookies = cookies
                        self._username = username
                        self._last_login_at = int(time.time())
                        self._last_error = None
                        await self._persist_success(username)
                        logger.success("CAM4 login réussi", username=username)
                        return {"success": True, "username": username}

                    # Erreur normalisée
                    status = (body.get("status") or "").lower()
                    details = body.get("details") or body_text or f"HTTP {resp.status}"
                    err_map = {
                        "invalid_credentials": "Identifiants incorrects",
                        "invalid_client_credentials": "Identifiants incorrects",
                        "twofa_required": "2FA requis (non supporté)",
                        "consecutive_login_failed": "Trop de tentatives, réessayez plus tard",
                        "temporary_banned": "Compte temporairement bloqué",
                        "client_banned": "Compte banni",
                        "invalid_captcha": "CAPTCHA requis — connectez-vous d'abord dans le navigateur",
                        "unverified_user": "Compte non vérifié",
                        "invalid_username": "Username invalide",
                    }
                    err = err_map.get(status, details)
                    self._last_error = err
                    await self._persist_error(username, err)
                    return {"success": False, "error": err, "code": status or None}

        except asyncio.TimeoutError:
            err = "Timeout en contactant CAM4"
            self._last_error = err
            await self._persist_error(username, err)
            return {"success": False, "error": err}
        except Exception as e:
            err = f"Erreur réseau: {e}"
            self._last_error = err
            await self._persist_error(username, err)
            logger.error("CAM4 login exception", error=str(e), exc_info=True)
            return {"success": False, "error": err}

    async def logout(self) -> None:
        self._cookies = {}
        self._username = None
        self._last_login_at = None
        self._last_error = None
        await self.db.set_setting(f"{_PREFIX}session_cookies", "")
        await self.db.set_setting(f"{_PREFIX}username", "")
        await self.db.set_setting(f"{_PREFIX}last_login_at", "")
        await self.db.set_setting(f"{_PREFIX}last_error", "")
        logger.info("CAM4 session effacée")

    def get_cookies(self) -> Dict[str, str]:
        return dict(self._cookies)

    def get_user_agent(self) -> str:
        return self._user_agent

    def get_status(self) -> Dict[str, Any]:
        return {
            "isLoggedIn": bool(self._cookies),
            "username": self._username,
            "lastError": self._last_error,
            "lastLoginAt": self._last_login_at,
            "hasCookies": bool(self._cookies),
        }

    async def _persist_success(self, username: str) -> None:
        await self.db.set_setting(f"{_PREFIX}session_cookies", json.dumps(self._cookies))
        await self.db.set_setting(f"{_PREFIX}username", username)
        await self.db.set_setting(f"{_PREFIX}last_login_at", str(self._last_login_at or 0))
        await self.db.set_setting(f"{_PREFIX}last_error", "")

    async def _persist_error(self, username: str, error: str) -> None:
        await self.db.set_setting(f"{_PREFIX}last_error", error)
