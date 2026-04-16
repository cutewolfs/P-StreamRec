"""
FlareSolverr REST client for Cloudflare bypass
"""

import asyncio
import time
import aiohttp
from typing import Optional, Dict, Any

from ..logger import logger
from ..core.config import FLARESOLVERR_URL, FLARESOLVERR_MAX_TIMEOUT


class FlareSolverrClient:
    def __init__(self, base_url: str = FLARESOLVERR_URL):
        self.base_url = base_url.rstrip("/")
        self._semaphore = asyncio.Semaphore(1)
        self._cached_cf_clearance: Optional[str] = None
        self._cached_user_agent: Optional[str] = None
        self._cache_expires_at: float = 0

    async def is_available(self) -> bool:
        """Check if FlareSolverr is healthy (shortcut, returns bool only)."""
        status = await self.check_status()
        return status["available"]

    async def check_status(self) -> Dict[str, Any]:
        """
        Check FlareSolverr health and return structured status.

        Returns dict with keys: available (bool), message (str), url (str), version (str|None).
        """
        try:
            async with aiohttp.ClientSession() as session:
                # FlareSolverr exposes no /health endpoint; the root "/" returns
                # {"msg": "FlareSolverr is ready!", ...} when the service is up.
                async with session.get(
                    f"{self.base_url}/",
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        return {
                            "available": False,
                            "message": f"HTTP {resp.status}",
                            "url": self.base_url,
                            "version": None,
                        }
                    try:
                        data = await resp.json()
                    except Exception:
                        return {
                            "available": False,
                            "message": "Invalid JSON response",
                            "url": self.base_url,
                            "version": None,
                        }
                    if data.get("msg") == "FlareSolverr is ready!":
                        return {
                            "available": True,
                            "message": "Ready",
                            "url": self.base_url,
                            "version": data.get("version"),
                        }
                    return {
                        "available": False,
                        "message": data.get("msg") or "Unknown status",
                        "url": self.base_url,
                        "version": data.get("version"),
                    }
        except asyncio.TimeoutError:
            logger.debug("FlareSolverr timeout", url=self.base_url)
            return {
                "available": False,
                "message": "Timeout after 10s (FlareSolverr may be starting up)",
                "url": self.base_url,
                "version": None,
            }
        except aiohttp.ClientConnectorError as e:
            logger.debug("FlareSolverr unreachable", url=self.base_url, error=str(e))
            return {
                "available": False,
                "message": f"Cannot connect: {e}",
                "url": self.base_url,
                "version": None,
            }
        except Exception as e:
            logger.debug("FlareSolverr error", error=str(e))
            return {
                "available": False,
                "message": f"Error: {e}",
                "url": self.base_url,
                "version": None,
            }

    async def solve_challenge(self, url: str) -> Optional[Dict[str, Any]]:
        """
        Solve a Cloudflare challenge via FlareSolverr.
        Returns dict with 'cookies' and 'user_agent' on success.
        """
        # Check cache first
        if self._cached_cf_clearance and time.time() < self._cache_expires_at:
            logger.debug("Using cached cf_clearance")
            return {
                "cookies": {"cf_clearance": self._cached_cf_clearance},
                "user_agent": self._cached_user_agent
            }

        async with self._semaphore:
            # Double-check cache after acquiring semaphore
            if self._cached_cf_clearance and time.time() < self._cache_expires_at:
                return {
                    "cookies": {"cf_clearance": self._cached_cf_clearance},
                    "user_agent": self._cached_user_agent
                }

            try:
                payload = {
                    "cmd": "request.get",
                    "url": url,
                    "maxTimeout": FLARESOLVERR_MAX_TIMEOUT
                }

                logger.info("Solving Cloudflare challenge via FlareSolverr", url=url)

                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{self.base_url}/v1",
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=FLARESOLVERR_MAX_TIMEOUT / 1000 + 10)
                    ) as resp:
                        if resp.status != 200:
                            logger.error("FlareSolverr error", status=resp.status)
                            return None

                        data = await resp.json()

                if data.get("status") != "ok":
                    logger.error("FlareSolverr challenge failed", message=data.get("message"))
                    return None

                solution = data.get("solution", {})
                cookies_list = solution.get("cookies", [])
                user_agent = solution.get("userAgent", "")

                # Extract cookies into a dict
                cookies = {}
                for cookie in cookies_list:
                    cookies[cookie["name"]] = cookie["value"]

                # Cache cf_clearance (valid for ~15 minutes, cache for 10)
                cf_clearance = cookies.get("cf_clearance")
                if cf_clearance:
                    self._cached_cf_clearance = cf_clearance
                    self._cached_user_agent = user_agent
                    self._cache_expires_at = time.time() + 600  # 10 minutes

                logger.success("Cloudflare challenge solved", cookies_count=len(cookies))
                return {
                    "cookies": cookies,
                    "user_agent": user_agent
                }

            except asyncio.TimeoutError:
                logger.error("FlareSolverr timeout")
                return None
            except Exception as e:
                logger.error("FlareSolverr error", error=str(e), exc_info=True)
                return None

    def invalidate_cache(self):
        """Force cache invalidation"""
        self._cached_cf_clearance = None
        self._cached_user_agent = None
        self._cache_expires_at = 0
