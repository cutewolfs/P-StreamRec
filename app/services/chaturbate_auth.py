"""
Chaturbate Authentication Service
Handles login flow, cookie management, and session persistence
"""

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Optional, Dict, Any

import aiohttp
import bcrypt

from ..logger import logger
from ..core.config import OUTPUT_DIR, CHATURBATE_CSRFTOKEN, CHATURBATE_SESSIONID
from .flaresolverr import FlareSolverrClient


class ChaturbateAuthService:
    def __init__(self, db, flaresolverr: Optional[FlareSolverrClient] = None):
        self.db = db
        self.flaresolverr = flaresolverr
        self._session: Optional[aiohttp.ClientSession] = None
        self._cookies: Dict[str, str] = {}
        self._user_agent: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
        self._is_logged_in: bool = False
        self._username: Optional[str] = None
        self._last_error: Optional[str] = None
        self._cookies_file = OUTPUT_DIR / "cookies" / "chaturbate.json"
        self._lock = asyncio.Lock()

    async def initialize(self):
        """Load saved auth state from DB"""
        auth_state = await self.db.get_auth_state()
        if auth_state and auth_state.get("is_logged_in"):
            self._is_logged_in = True
            self._username = auth_state.get("username")
            if auth_state.get("session_cookies"):
                try:
                    self._cookies = json.loads(auth_state["session_cookies"])
                except (json.JSONDecodeError, TypeError):
                    pass

            # Also load from file backup
            if not self._cookies and self._cookies_file.exists():
                try:
                    with open(self._cookies_file, "r") as f:
                        self._cookies = json.load(f)
                except Exception:
                    pass

            if self._cookies:
                logger.info("Restored Chaturbate session from DB",
                           username=self._username)

        # Apply legacy cookie env vars as fallback
        if not self._cookies:
            if CHATURBATE_CSRFTOKEN:
                self._cookies["csrftoken"] = CHATURBATE_CSRFTOKEN
            if CHATURBATE_SESSIONID:
                self._cookies["sessionid"] = CHATURBATE_SESSIONID
            if self._cookies:
                logger.info("Using legacy cookie env vars as fallback")

    async def login(self, username: str, password: str) -> Dict[str, Any]:
        """
        Login to Chaturbate.
        1. GET chaturbate.com/ to extract CSRF token
        2. If 403 (Cloudflare): use FlareSolverr, retry
        3. POST /auth/login/ with credentials
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

                # Step 2: POST login
                login_url = "https://chaturbate.com/auth/login/"
                headers = {
                    "User-Agent": self._user_agent,
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

                cookie_header = "; ".join(
                    f"{k}={v}" for k, v in initial_cookies.items()
                )
                if cookie_header:
                    headers["Cookie"] = cookie_header

                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        login_url,
                        data=form_data,
                        headers=headers,
                        allow_redirects=False,
                        ssl=False,
                        timeout=aiohttp.ClientTimeout(total=30)
                    ) as resp:
                        # Successful login returns 302 redirect
                        if resp.status in (301, 302):
                            # Extract cookies from response
                            all_cookies = {}
                            all_cookies.update(initial_cookies)
                            for cookie in resp.cookies.values():
                                all_cookies[cookie.key] = cookie.value

                            # Check if we got a sessionid
                            if "sessionid" not in all_cookies:
                                self._last_error = "Login failed: no session cookie received"
                                await self._save_error(username, self._last_error)
                                return {"success": False, "error": self._last_error}

                            # Save state
                            self._cookies = all_cookies
                            self._is_logged_in = True
                            self._username = username
                            await self._save_state(username, password)

                            logger.success("Chaturbate login successful",
                                         username=username)
                            return {"success": True, "username": username}

                        elif resp.status == 200:
                            # 200 usually means login form re-rendered (wrong creds)
                            body = await resp.text()
                            if "error" in body.lower() or "incorrect" in body.lower():
                                self._last_error = "Invalid username or password"
                            else:
                                self._last_error = "Login failed (form re-rendered)"
                            await self._save_error(username, self._last_error)
                            return {"success": False, "error": self._last_error}

                        else:
                            self._last_error = f"Login failed with HTTP {resp.status}"
                            await self._save_error(username, self._last_error)
                            return {"success": False, "error": self._last_error}

            except Exception as e:
                self._last_error = f"Login error: {str(e)}"
                logger.error("Chaturbate login error", error=str(e), exc_info=True)
                await self._save_error(username, self._last_error)
                return {"success": False, "error": self._last_error}

    async def _extract_csrf_token(self) -> tuple:
        """Extract CSRF token from Chaturbate homepage"""
        url = "https://chaturbate.com/"
        headers = {
            "User-Agent": self._user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        cookies = {}

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(
                    url, headers=headers, ssl=False,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status == 403 and self.flaresolverr:
                        # Cloudflare block - use FlareSolverr
                        logger.info("Cloudflare detected, using FlareSolverr")
                        solution = await self.flaresolverr.solve_challenge(url)
                        if solution:
                            cookies.update(solution.get("cookies", {}))
                            self._user_agent = solution.get("user_agent", self._user_agent)
                            # Retry with solved cookies
                            headers["User-Agent"] = self._user_agent
                            cookie_header = "; ".join(
                                f"{k}={v}" for k, v in cookies.items()
                            )
                            headers["Cookie"] = cookie_header
                            async with session.get(
                                url, headers=headers, ssl=False,
                                timeout=aiohttp.ClientTimeout(total=15)
                            ) as retry_resp:
                                if retry_resp.status == 200:
                                    html = await retry_resp.text()
                                    for c in retry_resp.cookies.values():
                                        cookies[c.key] = c.value
                                    csrf = self._parse_csrf(html, cookies)
                                    return csrf, cookies
                        return None, {}

                    elif resp.status == 200:
                        html = await resp.text()
                        for c in resp.cookies.values():
                            cookies[c.key] = c.value
                        csrf = self._parse_csrf(html, cookies)
                        return csrf, cookies

                    else:
                        logger.error("Failed to load Chaturbate", status=resp.status)
                        return None, {}

            except Exception as e:
                logger.error("Error extracting CSRF token", error=str(e))
                return None, {}

    def _parse_csrf(self, html: str, cookies: dict) -> Optional[str]:
        """Parse CSRF token from HTML or cookies"""
        # Try HTML input field
        match = re.search(
            r'name=["\']csrfmiddlewaretoken["\']\s+value=["\']([^"\']+)["\']',
            html
        )
        if match:
            return match.group(1)

        # Try from cookies
        if "csrftoken" in cookies:
            return cookies["csrftoken"]

        # Try meta tag
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

        # Also save to file backup
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

        # Validate existing session
        is_valid = await self._validate_session()
        if is_valid:
            session = aiohttp.ClientSession()
            # Set cookies on session
            for name, value in self._cookies.items():
                session.cookie_jar.update_cookies(
                    {name: value},
                    aiohttp.URL("https://chaturbate.com/")
                )
            return session

        # Session expired - try re-login from saved credentials
        auth_state = await self.db.get_auth_state()
        if auth_state and auth_state.get("username"):
            logger.info("Session expired, attempting re-login")
            # We can't re-login without the password
            self._is_logged_in = False
            self._last_error = "Session expired, please re-login"
            return None

        return None

    async def _validate_session(self) -> bool:
        """Test if session is still valid by checking /followed-cams/"""
        if not self._cookies.get("sessionid"):
            return False

        try:
            headers = {
                "User-Agent": self._user_agent,
                "Cookie": "; ".join(f"{k}={v}" for k, v in self._cookies.items()),
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://chaturbate.com/followed-cams/",
                    headers=headers,
                    allow_redirects=False,
                    ssl=False,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    # 200 = valid session, 302 to login = expired
                    return resp.status == 200
        except Exception as e:
            logger.debug("Session validation error", error=str(e))
            return False

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
