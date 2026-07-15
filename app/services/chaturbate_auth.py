"""
Chaturbate Authentication Service
Handles login flow, cookie management, and session persistence
Uses curl_cffi (impersonate=chrome) to match real browser TLS/JA3 fingerprint
"""

import asyncio
import json
import re
import time
from typing import Optional, Dict, Any

import aiohttp
import bcrypt
from curl_cffi.requests import AsyncSession

from ..logger import logger
from ..core.config import (
    OUTPUT_DIR,
    CHATURBATE_CSRFTOKEN,
    CHATURBATE_REQUEST_TIMEOUT_SECONDS,
    CHATURBATE_SESSIONID,
)
from ..core.http_client import aiohttp_client_session, aiohttp_request_kwargs
from .flaresolverr import FlareSolverrClient


_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_CHATURBATE_AUTH_COOKIE_NAMES = {"sessionid", "csrftoken"}
_IMPERSONATE_TARGET = "chrome124"  # curl_cffi impersonation profile


def _is_cloudflare_cookie(name: str) -> bool:
    lower = str(name or "").strip().lower()
    return lower == "cf_clearance" or lower.startswith("__cf") or lower.startswith("cf_")


def merge_flaresolverr_cookies(
    cookies: Dict[str, str],
    solved_cookies: Dict[str, Any],
) -> bool:
    """Merge FlareSolverr cookies without replacing authenticated CTB cookies."""
    changed = False
    existing_by_lower = {str(k).lower(): k for k in cookies}

    for raw_name, raw_value in (solved_cookies or {}).items():
        name = str(raw_name or "").strip()
        if not name:
            continue
        lower = name.lower()
        if lower in _CHATURBATE_AUTH_COOKIE_NAMES:
            continue

        existing_key = existing_by_lower.get(lower)
        if _is_cloudflare_cookie(name):
            target = existing_key or name
            cookies[target] = str(raw_value)
            existing_by_lower[lower] = target
            changed = True
        elif existing_key is None:
            cookies[name] = str(raw_value)
            existing_by_lower[lower] = name
            changed = True

    return changed


class ChaturbateAuthService:
    def __init__(self, db, flaresolverr: Optional[FlareSolverrClient] = None):
        self.db = db
        self.flaresolverr = flaresolverr
        self._session: Optional[aiohttp.ClientSession] = None
        self._cookies: Dict[str, str] = {}
        # Kept for compatibility with other services reading get_user_agent();
        # curl_cffi's impersonate profile sets the *real* UA/TLS internally,
        # this string is just what we report to callers/DB.
        self._user_agent: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
        self._is_logged_in: bool = False
        self._username: Optional[str] = None
        self._last_error: Optional[str] = None
        self._last_validation_error: Optional[str] = None
        self._cookies_file = OUTPUT_DIR / "cookies" / "chaturbate.json"
        self._lock = asyncio.Lock()

    def _apply_flaresolverr_solution(
        self,
        headers: Dict[str, str],
        cookies: Dict[str, str],
        solution: Dict[str, Any],
    ) -> bool:
        solved_cookies = solution.get("cookies") or {}
        user_agent = solution.get("user_agent") or ""
        if user_agent:
            self._user_agent = user_agent
            headers["User-Agent"] = self._user_agent
        merged_cookies = merge_flaresolverr_cookies(cookies, solved_cookies)
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        return bool(merged_cookies or user_agent)

    async def _prepare_flaresolverr_headers(
        self,
        url: str,
        headers: Dict[str, str],
        cookies: Dict[str, str],
    ) -> bool:
        if not self.flaresolverr:
            return False
        solution = await self.flaresolverr.solve_challenge(url)
        if not solution:
            return False
        return self._apply_flaresolverr_solution(headers, cookies, solution)

    async def initialize(self):
        """Load saved auth state from DB"""
        auth_state = await self.db.get_auth_state()
        if auth_state:
            self._username = auth_state.get("username")
            self._last_error = auth_state.get("last_error")
            if auth_state.get("session_cookies"):
                try:
                    self._cookies = json.loads(auth_state["session_cookies"])
                except (json.JSONDecodeError, TypeError):
                    pass

            if not self._cookies and self._cookies_file.exists():
                try:
                    with open(self._cookies_file, "r") as f:
                        self._cookies = json.load(f)
                except Exception:
                    pass

            if self._cookies:
                self._is_logged_in = False
                if bool(auth_state.get("is_logged_in")):
                    self._is_logged_in = await self._validate_session()
                if self._is_logged_in:
                    logger.info("Restored verified Chaturbate session from DB",
                               username=self._username)
                elif auth_state.get("is_logged_in"):
                    self._last_error = "Session expired, please re-login"
                    await self.db.save_auth_state(
                        username=self._username or "",
                        password_hash=auth_state.get("password_hash") or "",
                        is_logged_in=False,
                        session_cookies=json.dumps(self._cookies),
                        cf_clearance=self._cookies.get("cf_clearance"),
                        csrf_token=self._cookies.get("csrftoken"),
                        last_login_at=auth_state.get("last_login_at"),
                        last_error=self._last_error,
                    )

        if not self._cookies:
            if CHATURBATE_CSRFTOKEN:
                self._cookies["csrftoken"] = CHATURBATE_CSRFTOKEN
            if CHATURBATE_SESSIONID:
                self._cookies["sessionid"] = CHATURBATE_SESSIONID
            if self._cookies:
                logger.info("Using legacy cookie env vars as fallback")
                self._is_logged_in = await self._validate_session()
                if not self._is_logged_in:
                    self._last_error = "Legacy Chaturbate cookies are not verified"

    async def login(self, username: str, password: str) -> Dict[str, Any]:
        """
        Login to Chaturbate.
        1. GET chaturbate.com/ via curl_cffi (chrome impersonation) to extract CSRF token
        2. If still blocked: use FlareSolverr, retry
        3. POST /auth/login/ with credentials via curl_cffi
        4. Save cookies to DB + file
        """
        async with self._lock:
            try:
                logger.info("Starting Chaturbate login", username=username)
                self._last_error = None

                # Step 1: Get CSRF token
                csrf_token, initial_cookies = await self._extract_csrf_token()

                if not csrf_token:
                    self._last_error = "Could not extract CSRF token from Chaturbate"
                    await self._save_error(username, self._last_error)
                    return {"success": False, "error": self._last_error}

                # Step 2: POST login (curl_cffi impersonating Chrome TLS/JA3)
                login_url = "https://chaturbate.com/auth/login/"
                headers = {
                    "Referer": "https://chaturbate.com/",
                    "Origin": "https://chaturbate.com",
                    "Content-Type": "application/x-www-form-urlencoded",
                }

                form_data = {
                    "username": username,
                    "password": password,
                    "csrfmiddlewaretoken": csrf_token,
                    "next": "/",
                }

                async with AsyncSession(impersonate=_IMPERSONATE_TARGET) as session:
                    resp = await session.post(
                        login_url,
                        data=form_data,
                        headers=headers,
                        cookies=initial_cookies,
                        allow_redirects=False,
                        timeout=30,
                    )

                    if resp.status_code in (301, 302):
                        all_cookies = {}
                        all_cookies.update(initial_cookies)
                        for name, value in resp.cookies.items():
                            all_cookies[name] = value

                        if "sessionid" not in all_cookies:
                            self._last_error = "Login failed: no session cookie received"
                            await self._save_error(username, self._last_error)
                            return {"success": False, "error": self._last_error}

                        self._cookies = all_cookies
                        self._username = username
                        self._is_logged_in = False
                        if not await self._validate_session():
                            validation_error = (
                                self._last_validation_error
                                or self._last_error
                                or "Login session could not be verified"
                            )
                            self._cookies = {}
                            self._username = None
                            self._last_error = validation_error
                            await self._save_error(username, validation_error)
                            return {"success": False, "error": validation_error}

                        self._is_logged_in = True
                        await self._save_state(username, password)

                        logger.success("Chaturbate login successful", username=username)
                        return {"success": True, "username": username}

                    elif resp.status_code == 200:
                        body = resp.text
                        if "error" in body.lower() or "incorrect" in body.lower():
                            self._last_error = "Invalid username or password"
                        else:
                            self._last_error = "Login failed (form re-rendered)"
                        await self._save_error(username, self._last_error)
                        return {"success": False, "error": self._last_error}

                    else:
                        self._last_error = f"Login failed with HTTP {resp.status_code}"
                        await self._save_error(username, self._last_error)
                        return {"success": False, "error": self._last_error}

            except Exception as e:
                self._last_error = f"Login error: {str(e)}"
                logger.error("Chaturbate login error", error=str(e), exc_info=True)
                await self._save_error(username, self._last_error)
                return {"success": False, "error": self._last_error}

    async def _extract_csrf_token(self) -> tuple:
        """Extract CSRF token from Chaturbate homepage via curl_cffi (Chrome impersonation)."""
        url = "https://chaturbate.com/"
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        cookies: Dict[str, str] = {}

        try:
            async with AsyncSession(impersonate=_IMPERSONATE_TARGET) as session:
                resp = await session.get(
                    url, headers=headers,
                    allow_redirects=False,
                    timeout=15,
                )

                if resp.status_code == 403 or resp.status_code in _REDIRECT_STATUSES:
                    logger.info(
                        "Chaturbate protection detected, using FlareSolverr",
                        status=resp.status_code,
                    )
                    fs_headers: Dict[str, str] = {}
                    if await self._prepare_flaresolverr_headers(url, fs_headers, cookies):
                        retry_cookies = {**cookies}
                        retry_resp = await session.get(
                            url, headers=fs_headers,
                            cookies=retry_cookies,
                            allow_redirects=False,
                            timeout=15,
                        )
                        if retry_resp.status_code == 200:
                            html = retry_resp.text
                            for name, value in retry_resp.cookies.items():
                                cookies[name] = value
                            csrf = self._parse_csrf(html, cookies)
                            return csrf, cookies
                    return None, {}

                elif resp.status_code == 200:
                    html = resp.text
                    for name, value in resp.cookies.items():
                        cookies[name] = value
                    if self.flaresolverr and "cf_clearance" not in cookies:
                        csrf = self._parse_csrf(html, cookies)
                        fs_headers = {}
                        if await self._prepare_flaresolverr_headers(url, fs_headers, cookies):
                            retry_resp = await session.get(
                                url, headers=fs_headers,
                                cookies=cookies,
                                allow_redirects=False,
                                timeout=15,
                            )
                            if retry_resp.status_code == 200:
                                retry_html = retry_resp.text
                                for name, value in retry_resp.cookies.items():
                                    cookies[name] = value
                                retry_csrf = self._parse_csrf(retry_html, cookies)
                                return retry_csrf or csrf, cookies
                        return csrf, cookies
                    csrf = self._parse_csrf(html, cookies)
                    return csrf, cookies

                else:
                    logger.error("Failed to load Chaturbate", status=resp.status_code)
                    return None, {}

        except Exception as e:
            logger.error("Error extracting CSRF token", error=str(e))
            return None, {}

    def _parse_csrf(self, html: str, cookies: dict) -> Optional[str]:
        """Parse CSRF token from HTML or cookies"""
        match = re.search(
            r'name=["\']csrfmiddlewaretoken["\']\s+value=["\']([^"\']+)["\']',
            html
        )
        if match:
            return match.group(1)

        if "csrftoken" in cookies:
            return cookies["csrftoken"]

        match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
        if match:
            return match.group(1)

        logger.warning("CSRF token not found in HTML or cookies")
        return None

    async def _save_state(self, username: str, password: str):
        """Save auth state to DB and file"""
        password_hash = bcrypt.hashpw(
            password.encode("utf-8"), bcrypt.gensalt()
        ).decode("utf-8")

        cookies_json = json.dumps(self._cookies)
        now = int(time.time())

        await self.db.save_auth_state(
            username=username,
            password_hash=password_hash,
            is_logged_in=True,
            session_cookies=cookies_json,
            cf_clearance=self._cookies.get("cf_clearance"),
            csrf_token=self._cookies.get("csrftoken"),
            last_login_at=now,
            last_error=None
        )

        try:
            self._cookies_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._cookies_file, "w") as f:
                json.dump(self._cookies, f, indent=2)
        except Exception as e:
            logger.debug("Could not save cookies file", error=str(e))

    async def _save_error(self, username: str, error: str):
        """Save error state"""
        try:
            await self.db.save_auth_state(
                username=username,
                password_hash="",
                is_logged_in=False,
                last_error=error
            )
        except Exception:
            pass

    async def ensure_session(self) -> Optional[aiohttp.ClientSession]:
        """Get an authenticated aiohttp session, re-login if expired"""
        if not self._is_logged_in or not self._cookies:
            return None

        is_valid = await self._validate_session()
        if is_valid:
            session = aiohttp.ClientSession(trust_env=True)
            for name, value in self._cookies.items():
                session.cookie_jar.update_cookies(
                    {name: value},
                    aiohttp.URL("https://chaturbate.com/")
                )
            return session

        auth_state = await self.db.get_auth_state()
        if auth_state and auth_state.get("username"):
            logger.info("Session expired, attempting re-login")
            self._is_logged_in = False
            self._last_error = "Session expired, please re-login"
            return None

        return None

    async def _validate_session(self) -> bool:
        """Test if session is still valid with Chaturbate's authenticated APIs."""
        valid, reason = await self._validate_session_detail()
        self._last_validation_error = reason
        if not valid and reason:
            self._last_error = reason
        return valid

    async def _validate_session_detail(self) -> tuple[bool, Optional[str]]:
        """Validate the current cookie jar via curl_cffi (Chrome impersonation)."""
        if not self._cookies.get("sessionid"):
            return False, "Chaturbate sessionid cookie is missing"

        try:
            headers = {
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://chaturbate.com/followed-cams/",
            }
            async with AsyncSession(impersonate=_IMPERSONATE_TARGET) as session:
                resp = await session.get(
                    "https://chaturbate.com/api/ts/chatmessages/pm_users/?offset=0",
                    headers=headers,
                    cookies=self._cookies,
                    allow_redirects=False,
                    timeout=CHATURBATE_REQUEST_TIMEOUT_SECONDS,
                )
                if resp.status_code == 200:
                    return True, None
                if resp.status_code in {301, 302, 303, 307, 308}:
                    return False, "Chaturbate session validation redirected to login"
                if resp.status_code == 403:
                    return False, "Chaturbate returned 403 while validating the imported session"

                resp2 = await session.get(
                    "https://chaturbate.com/followed-cams/",
                    headers=headers,
                    cookies=self._cookies,
                    allow_redirects=False,
                    timeout=CHATURBATE_REQUEST_TIMEOUT_SECONDS,
                )
                if resp2.status_code == 200:
                    return True, None
                if resp2.status_code in {301, 302, 303, 307, 308}:
                    return False, "Chaturbate followed-cams redirected to login"
                if resp2.status_code == 403:
                    return False, "Chaturbate returned 403 on followed-cams; import the same browser cookies and User-Agent"
                return False, f"Chaturbate session validation failed with HTTP {resp2.status_code}"
        except Exception as e:
            logger.debug("Session validation error", error=str(e))
            return False, f"Chaturbate session validation error: {e}"

    async def logout(self):
        """Clear session and saved state"""
        self._is_logged_in = False
        self._username = None
        self._cookies = {}
        self._last_error = None

        await self.db.clear_auth_state()

        try:
            if self._cookies_file.exists():
                self._cookies_file.unlink()
        except Exception:
            pass

        logger.info("Chaturbate session cleared")

    def get_status(self) -> Dict[str, Any]:
        """Get current auth status"""
        return {
            "isLoggedIn": self._is_logged_in,
            "username": self._username,
            "lastError": self._last_error,
            "hasCookies": bool(self._cookies),
        }

    def get_cookies(self) -> Dict[str, str]:
        """Get current cookies for use by other services"""
        return dict(self._cookies)

    def get_user_agent(self) -> str:
        """Get current user agent"""
        return self._user_agent
