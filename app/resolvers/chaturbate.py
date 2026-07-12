import re
import html
import aiohttp
import requests
from typing import Optional
from urllib.parse import urljoin
from .base import ResolveError
from ..logger import logger
from ..core.config import CHATURBATE_REQUEST_TIMEOUT_SECONDS
from ..core.http_client import (
    aiohttp_client_session,
    aiohttp_request_kwargs,
    requests_proxy_kwargs,
)

# Rate limiting pour éviter HTTP 429
_last_request_time = 0
_min_delay_between_requests = 2.0  # 2 secondes entre chaque requête

# Optional ChaturbateAPI instance (set at startup)
_chaturbate_api = None


def _quality_field_order(max_height: Optional[int] = None):
    """Return Chaturbate HLS fields from preferred to fallback."""
    labels = {
        'hls_source_1080p': '1080p',
        'hls_source_hd': 'HD',
        'hls_source_high': 'High',
        'hls_source_720p': '720p',
        'hls_source': 'Standard',
    }

    if not max_height or max_height <= 0:
        order = [
            'hls_source_1080p', 'hls_source_hd',
            'hls_source_high', 'hls_source_720p', 'hls_source',
        ]
    else:
        order = []
        if max_height >= 1080:
            order.extend(['hls_source_1080p', 'hls_source_hd'])
        if max_height >= 720:
            order.extend(['hls_source_720p', 'hls_source_high'])
        order.append('hls_source')
        # Last-resort fallbacks keep recording available if Chaturbate omits
        # the exact capped field. Master playlists are still capped later.
        for field_name in [
            'hls_source_1080p', 'hls_source_hd',
            'hls_source_high', 'hls_source_720p',
        ]:
            if field_name not in order:
                order.append(field_name)

    return [(field_name, labels[field_name]) for field_name in order]


def set_chaturbate_api(api):
    """Set the ChaturbateAPI instance for authenticated resolution"""
    global _chaturbate_api
    _chaturbate_api = api


async def resolve_m3u8_async(username: str, max_height: Optional[int] = None) -> str:
    """
    Async M3U8 resolver with authentication support.
    Resolution chain:
    1. Authenticated get_edge_hls_url (if available)
    2. chatvideocontext API
    3. HTML scraping fallback

    Args:
        username: target model
        max_height: optional max resolution (e.g. 720). None = best available.
    """
    logger.subsection(f"Résolution M3U8 async - {username}")

    username = username.strip().lower()
    if not username or not re.match(r'^[a-z0-9_]+$', username):
        raise ResolveError("Nom d'utilisateur invalide")

    # Method 1: Authenticated edge HLS (best quality)
    if _chaturbate_api:
        try:
            hls_url = await _chaturbate_api.get_edge_hls_url(username)
            if hls_url:
                logger.success("M3U8 résolu via API authentifiée", username=username)
                return await _resolve_variant(hls_url, max_height=max_height)
        except Exception as e:
            logger.debug("Auth resolution failed, falling back", error=str(e))

    # Method 2 & 3: Fallback to async resolver (non-blocking for event loop)
    return await _resolve_m3u8_async_fallback(username, max_height=max_height)


async def _resolve_m3u8_async_fallback(username: str, max_height: Optional[int] = None) -> str:
    """Async fallback: try the chatvideocontext API, then scrape the HTML page.
    Uses aiohttp so the FastAPI event loop isn't blocked during resolution."""
    username = username.strip().lower()
    if not username or not re.match(r'^[a-z0-9_]+$', username):
        raise ResolveError("Nom d'utilisateur invalide")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Referer": "https://chaturbate.com/",
    }

    try:
        async with aiohttp_client_session() as session:
            # 1) API chatvideocontext
            api_url = f"https://chaturbate.com/api/chatvideocontext/{username}/"
            try:
                async with session.get(
                    api_url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=CHATURBATE_REQUEST_TIMEOUT_SECONDS),
                    ssl=False,
                    **aiohttp_request_kwargs(),
                ) as api_resp:
                    if api_resp.status == 200:
                        api_data = await api_resp.json(content_type=None)
                        best_m3u8 = None
                        for field_name, _quality_label in _quality_field_order(max_height):
                            if api_data.get(field_name):
                                best_m3u8 = api_data[field_name]
                                break
                        if best_m3u8:
                            return await _resolve_variant(best_m3u8, max_height=max_height)
            except Exception as e:
                logger.debug("Async API resolve failed, falling back to HTML", username=username, error=str(e))

            # 2) Fallback: parse HTML page
            html_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
            async with session.get(
                f"https://chaturbate.com/{username}/",
                headers=html_headers,
                timeout=aiohttp.ClientTimeout(total=CHATURBATE_REQUEST_TIMEOUT_SECONDS),
                ssl=False,
                **aiohttp_request_kwargs(),
            ) as resp:
                if resp.status != 200:
                    raise ResolveError(f"Impossible d'accéder à la page (HTTP {resp.status})")
                html_content = await resp.text()

        m3u8_patterns = [
            r'"(https?://[^"]*\.m3u8[^"]*)"',
            r"'(https?://[^']*\.m3u8[^']*)'",
            r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)',
        ]
        for pattern in m3u8_patterns:
            matches = re.findall(pattern, html_content, re.IGNORECASE)
            if matches:
                m3u8_url = matches[0] if not isinstance(matches[0], tuple) else matches[0][-1]
                m3u8_url = m3u8_url.replace("\\/", "/").replace("\\", "")
                m3u8_url = html.unescape(m3u8_url)
                m3u8_url = re.sub(r'u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), m3u8_url)
                m3u8_url = m3u8_url.rstrip('",;: \t\n\r')
                if m3u8_url.startswith("http") and ".m3u8" in m3u8_url:
                    return await _resolve_variant(m3u8_url, max_height=max_height)

        if "offline" in html_content.lower():
            raise ResolveError(f"{username} est hors ligne")
        raise ResolveError(f"Impossible de trouver le flux M3U8 pour {username}")
    except ResolveError:
        raise
    except Exception as e:
        raise ResolveError(f"Erreur réseau: {str(e)}")


def _parse_master_playlist(text: str):
    """Parse a master HLS playlist.

    Returns a list of {url, width, height, bandwidth} for each variant.
    Variants without RESOLUTION info keep height=0 (sorted last).
    """
    variants = []
    variant_index = 0
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            attrs = {}
            res_match = re.search(r'RESOLUTION=(\d+)x(\d+)', line)
            bw_match = re.search(r'BANDWIDTH=(\d+)', line)
            if res_match:
                attrs["width"] = int(res_match.group(1))
                attrs["height"] = int(res_match.group(2))
            if bw_match:
                attrs["bandwidth"] = int(bw_match.group(1))
            # Next non-comment line is the URL
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j].strip().startswith("#")):
                j += 1
            if j < len(lines):
                attrs["url"] = lines[j].strip()
                attrs["index"] = variant_index
                variant_index += 1
                variants.append(attrs)
                i = j + 1
                continue
        i += 1
    return variants


def _pick_variant_info(variants, max_height: Optional[int]):
    """Pick a variant entry given a max height constraint.

    - max_height None or <=0: highest resolution, highest bandwidth.
    - max_height set: best variant whose height <= max_height. Fallback to the
      lowest-resolution variant if all are taller.
    - On ties (same height), prefer highest bandwidth.
    """
    if not variants:
        return None

    def sort_key(v):
        return (v.get("height", 0), v.get("bandwidth", 0))

    if not max_height or max_height <= 0:
        return sorted(variants, key=sort_key, reverse=True)[0]

    eligible = [v for v in variants if v.get("height", 0) <= max_height]
    if eligible:
        return sorted(eligible, key=sort_key, reverse=True)[0]
    # Nothing fits — return the smallest to save bandwidth
    return sorted(variants, key=sort_key)[0]


def _pick_variant(variants, max_height: Optional[int]):
    picked = _pick_variant_info(variants, max_height)
    return picked["url"] if picked else None


async def resolve_llhls_master_playlist(
    m3u8_url: str,
    max_height: Optional[int] = None,
    headers: Optional[dict[str, str]] = None,
) -> Optional[dict[str, object]]:
    """Fetch a Chaturbate LL-HLS master once and return playlist metadata."""
    if "llhls.m3u8" not in (m3u8_url or "").lower():
        return None

    request_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://chaturbate.com/",
        "Origin": "https://chaturbate.com",
    }
    request_headers.update(headers or {})

    try:
        async with aiohttp_client_session() as session:
            async with session.get(
                m3u8_url,
                headers=request_headers,
                timeout=aiohttp.ClientTimeout(total=CHATURBATE_REQUEST_TIMEOUT_SECONDS),
                ssl=False,
                **aiohttp_request_kwargs(),
            ) as resp:
                if resp.status != 200:
                    logger.debug(
                        "Chaturbate LL-HLS master fetch failed",
                        status=resp.status,
                    )
                    return None
                text = await resp.text()
                content_type = resp.headers.get("Content-Type", "")
                base_url = str(resp.url)
    except Exception as e:
        logger.debug("Could not fetch Chaturbate LL-HLS master", error=str(e))
        return None

    variants = _parse_master_playlist(text)
    picked = _pick_variant_info(variants, max_height)
    return {
        "video_stream_index": int(picked.get("index", 0)) if picked else None,
        "text": text,
        "base_url": base_url,
        "content_type": content_type,
    }


async def resolve_llhls_video_stream_index(
    m3u8_url: str,
    max_height: Optional[int] = None,
    headers: Optional[dict[str, str]] = None,
) -> Optional[int]:
    """Return the FFmpeg 0:v:N index for a Chaturbate LL-HLS master playlist."""
    metadata = await resolve_llhls_master_playlist(
        m3u8_url,
        max_height=max_height,
        headers=headers,
    )
    if not metadata:
        return None
    value = metadata.get("video_stream_index")
    return int(value) if value is not None else None


async def _resolve_variant(m3u8_url: str, max_height: Optional[int] = None) -> str:
    """If URL is a traditional master playlist, pick a variant according to max_height.

    Only operates on playlist.m3u8 (non-LL-HLS muxed streams).
    LL-HLS edge URLs (llhls.m3u8) carry separate audio rendition groups that are
    only resolvable from the master playlist — ffmpeg must receive the master URL
    so it can map both the video variant and the audio rendition. Passing a
    video-only chunk URL to ffmpeg results in silent recordings.
    """
    if 'playlist.m3u8' not in m3u8_url:
        return m3u8_url

    try:
        async with aiohttp_client_session() as session:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            }
            async with session.get(
                m3u8_url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=CHATURBATE_REQUEST_TIMEOUT_SECONDS), ssl=False,
                **aiohttp_request_kwargs(),
            ) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    variants = _parse_master_playlist(text)
                    picked = _pick_variant(variants, max_height)
                    if picked:
                        logger.debug("HLS variant picked",
                                    max_height=max_height,
                                    variant=picked,
                                    candidates=len(variants))
                        return urljoin(m3u8_url, picked)
    except Exception as e:
        logger.debug("Could not extract variant from playlist", error=str(e))

    return m3u8_url


# Backwards-compatible alias
async def _resolve_best_quality(m3u8_url: str) -> str:
    return await _resolve_variant(m3u8_url, max_height=None)


def resolve_m3u8(username: str) -> str:
    """
    Résolveur Chaturbate ultra-simplifié et fiable.
    Utilise l'API puis fallback sur HTML si nécessaire.
    """
    logger.subsection(f"Résolution M3U8 - {username}")

    username = username.strip().lower()
    if not username or not re.match(r'^[a-z0-9_]+$', username):
        logger.error("Nom d'utilisateur invalide", username=username)
        raise ResolveError("Nom d'utilisateur invalide")

    logger.debug("Username validé", username=username)

    try:
        # MÉTHODE 1: Essayer l'API Chaturbate d'abord (meilleure qualité)
        api_url = f"https://chaturbate.com/api/chatvideocontext/{username}/"
        logger.progress("Tentative via API Chaturbate", username=username, url=api_url)

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Referer": "https://chaturbate.com/",
        }

        api_resp = requests.get(
            api_url,
            headers=headers,
            timeout=CHATURBATE_REQUEST_TIMEOUT_SECONDS,
            **requests_proxy_kwargs(),
        )
        if api_resp.status_code == 200:
            api_data = api_resp.json()

            # Logger TOUS les champs HLS disponibles pour debugging
            hls_fields = {k: v[:80] if isinstance(v, str) else v for k, v in api_data.items() if 'hls' in k.lower() or 'm3u8' in str(v).lower()}
            logger.debug("Champs HLS disponibles dans API", username=username, hls_fields=hls_fields)

            # Chercher la meilleure qualité disponible
            # Tester plusieurs noms possibles pour haute qualité
            best_m3u8 = None
            quality_source = None

            for field_name, quality_label in _quality_field_order(None):
                if api_data.get(field_name):
                    best_m3u8 = api_data[field_name]
                    quality_source = f"{field_name} ({quality_label})"
                    logger.success("M3U8 trouvé via API", username=username, quality=quality_source)
                    break

            if best_m3u8:
                # ASTUCE: Si c'est un playlist.m3u8, charger et prendre la dernière ligne (meilleure qualité)
                if 'playlist.m3u8' in best_m3u8:
                    try:
                        logger.debug("Extraction meilleure qualité du playlist", username=username)
                        playlist_resp = requests.get(
                            best_m3u8,
                            headers=headers,
                            timeout=CHATURBATE_REQUEST_TIMEOUT_SECONDS,
                            **requests_proxy_kwargs(),
                        )
                        if playlist_resp.status_code == 200:
                            lines = playlist_resp.text.strip().split('\n')
                            # La dernière ligne non-vide qui n'est pas un commentaire est la meilleure qualité
                            for line in reversed(lines):
                                line = line.strip()
                                if line and not line.startswith('#'):
                                    # C'est un chemin relatif, construire l'URL complète
                                    base_url = best_m3u8.rsplit('/', 1)[0]
                                    best_m3u8 = f"{base_url}/{line}"
                                    logger.success("Meilleure qualité extraite du playlist", username=username)
                                    break
                    except Exception as e:
                        logger.warning("Impossible d'extraire meilleure qualité du playlist, utilisation URL brute",
                                     username=username, error=str(e))

                logger.success("M3U8 résolu via API", username=username)
                return best_m3u8

            logger.debug("Pas de HLS dans API, fallback sur HTML", username=username)

        # MÉTHODE 2: Fallback sur parsing HTML
        url = f"https://chaturbate.com/{username}/"
        logger.progress("Fallback: Récupération page HTML", username=username, url=url)

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        resp = requests.get(
            url,
            headers=headers,
            timeout=CHATURBATE_REQUEST_TIMEOUT_SECONDS,
            **requests_proxy_kwargs(),
        )
        logger.debug("Réponse HTTP reçue", username=username, status_code=resp.status_code)

        if resp.status_code != 200:
            logger.error("Erreur HTTP", username=username, status_code=resp.status_code)
            raise ResolveError(f"Impossible d'accéder à la page (HTTP {resp.status_code})")

        html_content = resp.text
        logger.debug("Page HTML récupérée", username=username, size_chars=len(html_content))

        # Chercher le M3U8 avec patterns multiples et variés
        m3u8_patterns = [
            # URLs directes entre guillemets
            r'"(https?://[^"]*\.m3u8[^"]*)"',
            r"'(https?://[^']*\.m3u8[^']*)'",
            # Dans variables JavaScript
            r'hls_source["\s:=]+(["\'])(https?://[^"\']+\.m3u8[^"\']*)\1',
            r'hlsSource["\s:=]+(["\'])(https?://[^"\']+\.m3u8[^"\']*)\1',
            r'm3u8["\s:=]+(["\'])(https?://[^"\']+\.m3u8[^"\']*)\1',
            # URL encodée (avec antislash)
            r'(https?:\\?/\\?/[^"\'\\s]+\.m3u8[^"\'\\s]*)',
            # Dans JSON
            r'"url"["\s:]+(["\'])(https?://[^"\']+\.m3u8[^"\']*)\1',
            # Pattern large pour tout .m3u8
            r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)',
        ]

        logger.debug("Recherche M3U8 avec patterns",
                    username=username,
                    pattern_count=len(m3u8_patterns))

        for i, pattern in enumerate(m3u8_patterns, 1):
            logger.debug("Test pattern regex", username=username, pattern_num=f"{i}/{len(m3u8_patterns)}")
            matches = re.findall(pattern, html_content, re.IGNORECASE)

            if matches:
                logger.debug("Pattern match trouvé", username=username, pattern_index=i, matches=len(matches))
                # Prendre le premier match (ou le groupe capturé)
                if isinstance(matches[0], tuple):
                    # Si c'est un tuple (groupes capturés), prendre le dernier élément non vide
                    m3u8_url = [g for g in matches[0] if g and 'http' in g][0] if matches[0] else matches[0][-1]
                else:
                    m3u8_url = matches[0]

                # Nettoyer l'URL
                m3u8_url = m3u8_url.replace("\\/", "/").replace("\\", "")

                # Décoder les entités Unicode (u002D = -, u0022 = ", etc.)
                m3u8_url = html.unescape(m3u8_url)

                # Remplacer les codes Unicode hexadécimaux
                def decode_unicode(match):
                    return chr(int(match.group(1), 16))
                m3u8_url = re.sub(r'u([0-9a-fA-F]{4})', decode_unicode, m3u8_url)

                # Supprimer les caractères parasites à la fin
                m3u8_url = m3u8_url.rstrip('",;: \t\n\r')

                logger.debug("M3U8 candidat trouvé", username=username)

                if m3u8_url.startswith("http") and ".m3u8" in m3u8_url:
                    logger.success("M3U8 résolu avec succès", username=username, pattern=i)
                    return m3u8_url
                else:
                    logger.debug("URL candidat invalide", username=username)

        # Si pas trouvé, vérifier si hors ligne
        logger.warning("Aucun M3U8 trouvé, vérification statut", username=username)

        html_lower = html_content.lower()
        if "offline" in html_lower:
            logger.info("Utilisateur détecté hors ligne", username=username)
            raise ResolveError(f"{username} est hors ligne")

        # Debug: rechercher 'hls' et 'm3u8' dans le HTML
        hls_count = html_lower.count('hls')
        m3u8_count = html_lower.count('m3u8')

        logger.debug("Analyse HTML", username=username, size=len(html_content), hls_count=hls_count, m3u8_count=m3u8_count)

        logger.error("M3U8 non trouvé", username=username)
        raise ResolveError(f"Impossible de trouver le flux M3U8 pour {username}")

    except requests.RequestException as e:
        logger.error("Erreur réseau lors de la résolution",
                    username=username,
                    exc_info=True,
                    error=str(e))
        raise ResolveError(f"Erreur réseau: {str(e)}")
    except ResolveError:
        raise
    except Exception as e:
        logger.critical("Erreur inattendue dans le resolver",
                       username=username,
                       exc_info=True,
                       error=str(e))
        raise ResolveError(f"Erreur inattendue: {str(e)}")
