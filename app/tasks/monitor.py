"""
Tâche background: Monitoring continu des modèles
Vérifie l'état en ligne, génère les miniatures et met à jour SQLite
"""
import asyncio
import aiohttp
import json
import re
import subprocess
import os
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable
from datetime import datetime, timezone

if TYPE_CHECKING:
    from ..ffmpeg_runner import FFmpegManager
    from ..core.database import Database

from ..logger import logger
from ..core.config import (
    AUTO_RECORD_INTERVAL,
    MIN_RECORDING_BYTES,
    MIN_RECORDING_SECONDS,
    OUTPUT_DIR,
)
from ..core.http_client import aiohttp_client_session, aiohttp_request_kwargs

# Intervalle de vérification (en secondes)
CHECK_INTERVAL_SETTING_KEY = "check_interval_seconds"
MIN_CHECK_INTERVAL_SECONDS = 30
MAX_CHECK_INTERVAL_SECONDS = 3600
MONITOR_INTERVAL = AUTO_RECORD_INTERVAL
THUMBNAIL_UPDATE_INTERVAL = 300  # Miniature offline: toutes les 5 minutes
THUMBNAIL_UPDATE_INTERVAL_LIVE = 60  # Miniature live: toutes les 60s pour refléter l'activité
SHORT_RECORDING_PROBE_BYTES = max(MIN_RECORDING_BYTES, 64 * 1024 * 1024)


def normalize_check_interval_seconds(value, default: int = MONITOR_INTERVAL) -> int:
    try:
        interval = int(value)
    except (ValueError, TypeError):
        interval = default

    if interval < MIN_CHECK_INTERVAL_SECONDS:
        raise ValueError(f"check interval must be at least {MIN_CHECK_INTERVAL_SECONDS} seconds")
    if interval > MAX_CHECK_INTERVAL_SECONDS:
        raise ValueError(f"check interval must be at most {MAX_CHECK_INTERVAL_SECONDS} seconds")
    return interval


async def get_check_interval_seconds(db: 'Database') -> int:
    raw = await db.get_setting(CHECK_INTERVAL_SETTING_KEY)
    try:
        return normalize_check_interval_seconds(
            raw if raw is not None else AUTO_RECORD_INTERVAL
        )
    except ValueError as exc:
        logger.warning(
            "Intervalle de monitoring invalide, fallback env",
            task="monitor",
            value=raw,
            error=str(exc),
        )
        return normalize_check_interval_seconds(AUTO_RECORD_INTERVAL)

async def _check_live_via_cdn(session: aiohttp.ClientSession, username: str) -> bool:
    """Check if a model is live using the Chaturbate thumbnail CDN.

    This CDN endpoint is not behind Cloudflare and does not require cookies.
    A 200 response with a non-trivial body means the model is currently streaming.
    """
    url = f"https://roomimg.stream.highwebmedia.com/ri/{username}.jpg"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://chaturbate.com/",
    }
    try:
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
            ssl=False,
            **aiohttp_request_kwargs(),
        ) as resp:
            if resp.status == 200:
                content = await resp.read()
                return len(content) > 1000
    except Exception as e:
        logger.debug("Erreur fallback live CDN", username=username, error=str(e))
    return False


async def check_model_status(
    session: aiohttp.ClientSession,
    username: str,
    csrftoken: str = None,
    auth_cookies: dict | None = None,
) -> dict:
    """Vérifie le statut d'un modèle via l'API Chaturbate.

    Cookies priority:
    1. ``auth_cookies`` (authenticated session stored in DB, injected by the
       ChaturbateAuthService via the builtin plugin or monitor task)
    2. ``CHATURBATE_*`` environment variables (legacy fallback)

    Without auth cookies Chaturbate redirects ``/api/chatvideocontext/`` to the
    login page and the check silently fails (see GH #11).

    When the chatvideocontext API is blocked by Cloudflare TLS fingerprinting
    (connection reset, error code 0), the CDN thumbnail endpoint is used as a
    reliable fallback to detect liveness without any Cloudflare dependency.
    """
    try:
        url = f"https://chaturbate.com/api/chatvideocontext/{username}/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://chaturbate.com/",
            "Origin": "https://chaturbate.com",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

        cookies: dict[str, str] = {}

        if auth_cookies:
            cookies.update(auth_cookies)

        if csrftoken and "csrftoken" not in cookies:
            cookies["csrftoken"] = csrftoken

        # Legacy env-var fallback: only fill slots the authenticated session did
        # not already provide.
        affkey_env = os.getenv("CHATURBATE_AFFKEY")
        sessionid_env = os.getenv("CHATURBATE_SESSIONID")
        if affkey_env and "affkey" not in cookies:
            cookies["affkey"] = affkey_env
        if sessionid_env and "sessionid" not in cookies:
            cookies["sessionid"] = sessionid_env

        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
            ssl=False,
            **aiohttp_request_kwargs(),
        ) as response:
            if response.status == 200:
                data = await response.json()
                
                # Log les données de l'API pour débogage
                logger.debug("Réponse API Chaturbate", 
                           username=username,
                           room_status=data.get("room_status"),
                           has_hls=bool(data.get("hls_source")),
                           num_users=data.get("num_users", 0))
                
                # Détection améliorée du statut en ligne
                room_status = data.get("room_status", "")
                hls_source = data.get("hls_source")
                
                # Un modèle est en ligne si :
                # 1. Il a un flux HLS disponible OU
                # 2. Le room_status est "public" OU
                # 3. Le room_status est "away" (temporairement absent mais toujours en ligne)
                is_online = (
                    bool(hls_source) or
                    room_status in ["public", "away"]
                )

                viewers = data.get("num_users", 0)

                return {
                    "is_online": is_online,
                    "viewers": viewers,
                    "hls_source": hls_source,
                    "room_status": room_status or None,
                    "tags": data.get("tags") or data.get("room_tags") or [],
                }
    except Exception as e:
        logger.debug("Erreur vérification statut modèle", username=username, error=str(e))

    # Fallback: chatvideocontext is blocked by Cloudflare TLS fingerprinting.
    # Use the thumbnail CDN which has no CF protection to detect liveness.
    try:
        is_live = await _check_live_via_cdn(session, username)
        if is_live:
            logger.debug("Statut détecté via CDN (fallback CF)", username=username, is_online=True)
        return {
            "is_online": is_live,
            "viewers": 0,
            "hls_source": None,
            "room_status": "public" if is_live else None,
            "tags": [],
        }
    except Exception as e:
        logger.debug("Erreur fallback CDN", username=username, error=str(e))

    return {
        "is_online": False,
        "viewers": 0,
        "hls_source": None,
        "room_status": None,
        "tags": [],
    }

async def generate_thumbnail_from_stream(
    username: str,
    session_id: str,
    output_dir: Path,
    ffmpeg_path: str = "ffmpeg"
) -> str | None:
    """Génère une miniature depuis le stream HLS en cours"""
    try:
        session_dir = output_dir / "sessions" / session_id
        m3u8_file = session_dir / "stream.m3u8"
        
        if not m3u8_file.exists():
            return None
        
        # Dossier pour les miniatures live
        live_thumbs_dir = output_dir / "thumbnails" / "live"
        live_thumbs_dir.mkdir(parents=True, exist_ok=True)
        thumb_path = live_thumbs_dir / f"{username}.jpg"
        
        # Générer la miniature
        process = await asyncio.create_subprocess_exec(
            ffmpeg_path, "-i", str(m3u8_file),
            "-vframes", "1",
            "-vf", "scale=280:-1",
            "-y",
            str(thumb_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        
        await asyncio.wait_for(process.wait(), timeout=10)
        
        if thumb_path.exists():
            return str(thumb_path)
    
    except Exception as e:
        logger.debug("Erreur génération miniature stream", username=username, error=str(e))
    
    return None

async def generate_thumbnail_from_recording(
    username: str,
    output_dir: Path,
    ffmpeg_path: str = "ffmpeg"
) -> str | None:
    """Génère une miniature depuis la dernière rediffusion"""
    try:
        records_dir = output_dir / "records" / username
        
        if not records_dir.exists():
            return None
        
        # Trouver la dernière rediffusion
        ts_files = sorted(records_dir.rglob("*.ts"), key=lambda p: p.stat().st_mtime, reverse=True)
        
        if not ts_files:
            return None
        
        latest_recording = ts_files[0]
        
        # Dossier pour les miniatures offline
        offline_thumbs_dir = output_dir / "thumbnails" / "offline"
        offline_thumbs_dir.mkdir(parents=True, exist_ok=True)
        thumb_path = offline_thumbs_dir / f"{username}.jpg"
        
        # Ne régénérer que si la miniature n'existe pas ou est plus ancienne que l'enregistrement
        if thumb_path.exists() and thumb_path.stat().st_mtime > latest_recording.stat().st_mtime:
            return str(thumb_path)
        
        # Extraire une frame au milieu de la vidéo
        process = await asyncio.create_subprocess_exec(
            ffmpeg_path, "-ss", "00:00:30",
            "-i", str(latest_recording),
            "-vframes", "1",
            "-vf", "scale=280:-1",
            "-y",
            str(thumb_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        
        await asyncio.wait_for(process.wait(), timeout=15)
        
        if thumb_path.exists():
            return str(thumb_path)
    
    except Exception as e:
        logger.debug("Erreur génération miniature offline", username=username, error=str(e))
    
    return None

async def download_thumbnail_from_chaturbate(
    session: aiohttp.ClientSession,
    username: str,
    output_dir: Path
) -> str | None:
    """Télécharge la miniature depuis Chaturbate"""
    try:
        img_urls = [
            f"https://roomimg.stream.highwebmedia.com/ri/{username}.jpg",
            f"https://cbjpeg.stream.highwebmedia.com/stream?room={username}&f=.jpg",
        ]
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://chaturbate.com/",
        }
        
        for img_url in img_urls:
            try:
                async with session.get(
                    img_url,
                    headers=headers,
                    timeout=5,
                    **aiohttp_request_kwargs(),
                ) as response:
                    if response.status == 200:
                        content = await response.read()
                        
                        if len(content) > 1000:
                            # Sauvegarder la miniature
                            cb_thumbs_dir = output_dir / "thumbnails" / "chaturbate"
                            cb_thumbs_dir.mkdir(parents=True, exist_ok=True)
                            thumb_path = cb_thumbs_dir / f"{username}.jpg"
                            
                            with open(thumb_path, 'wb') as f:
                                f.write(content)
                            
                            return str(thumb_path)
            except:
                continue
    
    except Exception as e:
        logger.debug("Erreur téléchargement miniature Chaturbate", username=username, error=str(e))
    
    return None

async def get_video_duration(file_path: Path, ffmpeg_path: str = "ffmpeg") -> int:
    """Récupère la durée d'une vidéo avec ffprobe"""
    try:
        # Utiliser ffprobe pour récupérer la durée
        ffprobe_path = ffmpeg_path.replace("ffmpeg", "ffprobe")
        
        process = await asyncio.create_subprocess_exec(
            ffprobe_path,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(file_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        
        if process.returncode == 0 and stdout:
            duration_str = stdout.decode().strip()
            if duration_str:
                return int(float(duration_str))
    
    except Exception as e:
        logger.debug("Erreur récupération durée vidéo", file_path=str(file_path), error=str(e))
    
    return 0


_RECORDED_AT_TAG_KEYS = (
    "creation_time",
    "com.apple.quicktime.creationdate",
    "date_utc",
    "date-utc",
    "encoded_date",
    "tagged_date",
    "creation_date",
    "creationdate",
    "date",
)

_REFERENCE_MONTHS = {
    "jan": 1,
    "january": 1,
    "janvier": 1,
    "feb": 2,
    "february": 2,
    "fev": 2,
    "fevr": 2,
    "fevrier": 2,
    "mar": 3,
    "march": 3,
    "mars": 3,
    "apr": 4,
    "april": 4,
    "avr": 4,
    "avril": 4,
    "may": 5,
    "mai": 5,
    "jun": 6,
    "june": 6,
    "juin": 6,
    "jul": 7,
    "july": 7,
    "juillet": 7,
    "aug": 8,
    "august": 8,
    "aout": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "septembre": 9,
    "oct": 10,
    "october": 10,
    "octobre": 10,
    "nov": 11,
    "november": 11,
    "novembre": 11,
    "dec": 12,
    "december": 12,
    "decembre": 12,
}

_REFERENCE_MONTH_PATTERN = "|".join(sorted(_REFERENCE_MONTHS, key=len, reverse=True))
_REFERENCE_TIME_SUFFIX = (
    r"(?:"
    r"(?:[\s_t,.;@-]+|[\s_]*a[\s_]+|[\s_]*at[\s_]+|[\s_]*vers[\s_]+)"
    r"([01]?\d|2[0-3])"
    r"(?:[:h._-]?([0-5]\d))?"
    r"(?:[:h._-]?([0-5]\d))?"
    r"\s*(am|pm)?"
    r")?"
)


def _normalize_metadata_key(key: object) -> str:
    return re.sub(r"[\s_-]+", "", str(key or "").strip().lower())


def _parse_metadata_timestamp(value: object) -> int | None:
    raw = str(value or "").strip().strip("\x00")
    if not raw or raw.upper() in {"N/A", "NONE", "NULL"}:
        return None

    candidate = raw
    if candidate.upper().startswith("UTC "):
        candidate = candidate[4:].strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    if re.search(r"[+-]\d{4}$", candidate):
        candidate = f"{candidate[:-5]}{candidate[-5:-2]}:{candidate[-2:]}"
    if re.match(r"^\d{4}:\d{2}:\d{2}", candidate):
        candidate = candidate.replace(":", "-", 2)

    formats = (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    )

    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        for fmt in formats:
            try:
                parsed = datetime.strptime(candidate, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return int(parsed.timestamp())
    except (OverflowError, OSError, ValueError):
        return None


def _normalize_reference_text(value: object) -> str:
    raw = str(value or "").strip().strip("\x00")
    normalized = unicodedata.normalize("NFKD", raw)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower()


def _reference_timestamp_from_parts(
    year: object,
    month: object,
    day: object,
    hour: object = None,
    minute: object = None,
    second: object = None,
    meridiem: object = None,
) -> int | None:
    try:
        hour_value = int(hour) if hour not in (None, "") else 0
        minute_value = int(minute) if minute not in (None, "") else 0
        second_value = int(second) if second not in (None, "") else 0
        marker = str(meridiem or "").lower()
        if marker == "pm" and hour_value < 12:
            hour_value += 12
        elif marker == "am" and hour_value == 12:
            hour_value = 0

        parsed = datetime(
            int(year),
            int(month),
            int(day),
            hour_value,
            minute_value,
            second_value,
        )
    except (TypeError, ValueError, OverflowError):
        return None

    try:
        return int(parsed.timestamp())
    except (OverflowError, OSError, ValueError):
        return None


def reference_timestamp_from_text(text: object) -> int | None:
    """Extract a local timestamp from a content title or user-supplied filename."""
    candidate = _normalize_reference_text(text)
    if not candidate:
        return None

    compact_datetime = re.search(
        r"(?<!\d)(\d{4})(\d{2})(\d{2})([01]\d|2[0-3])([0-5]\d)([0-5]\d)?(?!\d)",
        candidate,
    )
    if compact_datetime:
        parsed = _reference_timestamp_from_parts(
            compact_datetime.group(1),
            compact_datetime.group(2),
            compact_datetime.group(3),
            compact_datetime.group(4),
            compact_datetime.group(5),
            compact_datetime.group(6),
        )
        if parsed is not None:
            return parsed

    compact_date = re.search(
        r"(?<!\d)(\d{4})(\d{2})(\d{2})"
        + _REFERENCE_TIME_SUFFIX
        + r"(?!\d)",
        candidate,
    )
    if compact_date:
        parsed = _reference_timestamp_from_parts(
            compact_date.group(1),
            compact_date.group(2),
            compact_date.group(3),
            compact_date.group(4),
            compact_date.group(5),
            compact_date.group(6),
            compact_date.group(7),
        )
        if parsed is not None:
            return parsed

    year_first = re.search(
        r"(?<!\d)(\d{4})[-_. /](\d{1,2})[-_. /](\d{1,2})"
        + _REFERENCE_TIME_SUFFIX
        + r"(?!\d)",
        candidate,
    )
    if year_first:
        parsed = _reference_timestamp_from_parts(
            year_first.group(1),
            year_first.group(2),
            year_first.group(3),
            year_first.group(4),
            year_first.group(5),
            year_first.group(6),
            year_first.group(7),
        )
        if parsed is not None:
            return parsed

    day_first = re.search(
        r"(?<!\d)(\d{1,2})[-_. /](\d{1,2})[-_. /](\d{4})"
        + _REFERENCE_TIME_SUFFIX
        + r"(?!\d)",
        candidate,
    )
    if day_first:
        parsed = _reference_timestamp_from_parts(
            day_first.group(3),
            day_first.group(2),
            day_first.group(1),
            day_first.group(4),
            day_first.group(5),
            day_first.group(6),
            day_first.group(7),
        )
        if parsed is not None:
            return parsed

    day_month_name = re.search(
        rf"(?<!\w)(\d{{1,2}})(?:st|nd|rd|th)?[\s._-]+({_REFERENCE_MONTH_PATTERN})"
        rf"[\s._,-]+(\d{{4}})"
        + _REFERENCE_TIME_SUFFIX
        + r"(?!\d)",
        candidate,
    )
    if day_month_name:
        parsed = _reference_timestamp_from_parts(
            day_month_name.group(3),
            _REFERENCE_MONTHS.get(day_month_name.group(2)),
            day_month_name.group(1),
            day_month_name.group(4),
            day_month_name.group(5),
            day_month_name.group(6),
            day_month_name.group(7),
        )
        if parsed is not None:
            return parsed

    month_name_first = re.search(
        rf"(?<!\w)({_REFERENCE_MONTH_PATTERN})[\s._-]+(\d{{1,2}})(?:st|nd|rd|th)?"
        rf"[,]?[\s._-]+(\d{{4}})"
        + _REFERENCE_TIME_SUFFIX
        + r"(?!\d)",
        candidate,
    )
    if month_name_first:
        parsed = _reference_timestamp_from_parts(
            month_name_first.group(3),
            _REFERENCE_MONTHS.get(month_name_first.group(1)),
            month_name_first.group(2),
            month_name_first.group(4),
            month_name_first.group(5),
            month_name_first.group(6),
            month_name_first.group(7),
        )
        if parsed is not None:
            return parsed

    return None


def _parse_video_recorded_at(probe_data: dict[str, Any]) -> int | None:
    tag_maps: list[dict[str, Any]] = []
    format_tags = (probe_data.get("format") or {}).get("tags")
    if isinstance(format_tags, dict):
        tag_maps.append(format_tags)
    for stream in probe_data.get("streams") or []:
        if isinstance(stream, dict) and isinstance(stream.get("tags"), dict):
            tag_maps.append(stream["tags"])

    wanted_keys = [_normalize_metadata_key(key) for key in _RECORDED_AT_TAG_KEYS]
    for wanted_key in wanted_keys:
        for tags in tag_maps:
            for key, value in tags.items():
                if _normalize_metadata_key(key) == wanted_key:
                    parsed = _parse_metadata_timestamp(value)
                    if parsed is not None:
                        return parsed

    for tags in tag_maps:
        for key, value in tags.items():
            normalized = _normalize_metadata_key(key)
            if "date" in normalized or ("creation" in normalized and "time" in normalized):
                parsed = _parse_metadata_timestamp(value)
                if parsed is not None:
                    return parsed
    return None


async def get_video_recorded_at(file_path: Path, ffmpeg_path: str = "ffmpeg") -> int | None:
    """Read the media/container recorded date from ffprobe metadata tags."""
    try:
        ffprobe_path = ffmpeg_path.replace("ffmpeg", "ffprobe")

        process = await asyncio.create_subprocess_exec(
            ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format_tags:stream_tags",
            "-of",
            "json",
            str(file_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        if process.returncode == 0 and stdout:
            return _parse_video_recorded_at(json.loads(stdout.decode("utf-8", errors="replace")))
    except Exception as e:
        logger.debug("Erreur récupération date metadata vidéo", file_path=str(file_path), error=str(e))

    return None


def recording_timestamp_from_filename(filename: str) -> int | None:
    path = Path(filename)
    for value in (path.stem, path.name):
        parsed = reference_timestamp_from_text(value)
        if parsed is not None:
            return parsed
    return None


def _iter_reference_texts(file_path: Path, reference_texts: Iterable[object] | None = None):
    seen: set[str] = set()
    for value in (*tuple(reference_texts or ()), file_path.stem, file_path.name):
        normalized = str(value or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            yield normalized


async def get_media_created_at(
    file_path: Path,
    ffmpeg_path: str = "ffmpeg",
    fallback_timestamp: int | None = None,
    reference_texts: Iterable[object] | None = None,
) -> int:
    for value in _iter_reference_texts(file_path, reference_texts):
        reference_at = reference_timestamp_from_text(value)
        if reference_at is not None:
            return reference_at

    recorded_at = await get_video_recorded_at(file_path, ffmpeg_path)
    if recorded_at is not None:
        return recorded_at
    if fallback_timestamp is not None:
        return int(fallback_timestamp)
    try:
        return int(file_path.stat().st_mtime)
    except OSError:
        return int(datetime.now(tz=timezone.utc).timestamp())


async def generate_recording_thumbnail(
    ts_file: Path,
    output_dir: Path,
    username: str,
    ffmpeg_path: str = "ffmpeg"
) -> str | None:
    """Génère une miniature pour un enregistrement"""
    try:
        # Dossier pour les miniatures d'enregistrements
        thumbs_dir = output_dir / "thumbnails" / username
        thumbs_dir.mkdir(parents=True, exist_ok=True)
        thumb_path = thumbs_dir / f"{ts_file.stem}.jpg"
        
        # Ne pas régénérer si existe déjà
        if thumb_path.exists():
            return str(thumb_path)
        
        # Extraire une frame à 30 secondes du début
        process = await asyncio.create_subprocess_exec(
            ffmpeg_path,
            "-ss", "00:00:30",
            "-i", str(ts_file),
            "-vframes", "1",
            "-vf", "scale=320:-1",
            "-y",
            str(thumb_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        
        await asyncio.wait_for(process.wait(), timeout=15)
        
        if thumb_path.exists():
            return str(thumb_path)
    
    except Exception as e:
        logger.debug("Erreur génération miniature enregistrement", 
                    username=username, 
                    filename=ts_file.name, 
                    error=str(e))
    
    return None


async def _record_dirs_for_username(db: 'Database', username: str, output_dir: Path) -> list[Path]:
    records_root = output_dir / "records"
    candidates: list[Path] = []
    try:
        model = await db.get_model(username)
    except Exception:
        model = None

    record_path = (model or {}).get("record_path")
    if record_path:
        try:
            relative = Path(str(record_path))
            if not relative.is_absolute() and not any(part in {"", ".", ".."} for part in relative.parts):
                candidates.append(records_root / relative)
        except Exception:
            pass
    else:
        candidates.append(records_root / username)

    candidates.extend([
        records_root / username / "videos" / "record",
        records_root / username,
    ])

    unique: list[Path] = []
    seen: set[str] = set()
    root = records_root.resolve()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            if not resolved.is_relative_to(root):
                continue
            key = str(resolved)
        except Exception:
            continue
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


async def update_recordings_cache(db: 'Database', username: str, output_dir: Path, ffmpeg_path: str = "ffmpeg"):
    """Met à jour le cache des enregistrements dans SQLite"""
    try:
        import time
        records_root = output_dir / "records"
        records_dirs = await _record_dirs_for_username(db, username, output_dir)

        # Purge des rows DB orphelines : un fichier TS et MP4 qui n'existent
        # plus sur disque doivent être retirés de la DB, sinon le recording
        # reste affiché sur /recordings sans moyen de le supprimer depuis
        # l'UI (ex: suppression manuelle du fichier, volume réinitialisé).
        # Safety: on ne purge QUE si le dossier parent /records existe (sinon
        # volume démonté, on ne veut rien effacer).
        if records_root.exists():
            existing_recs = await db.get_recordings(username)
            for rec in existing_recs:
                if rec.get('media_kind') == 'import':
                    continue
                ts_path_str = rec.get('file_path')
                mp4_path_str = rec.get('mp4_path')
                ts_exists = bool(ts_path_str) and Path(ts_path_str).exists()
                mp4_exists = bool(mp4_path_str) and Path(mp4_path_str).exists()
                if not ts_exists and not mp4_exists:
                    await db.delete_recording(username, rec['filename'])
                    logger.info(
                        "Row recording orpheline supprimée",
                        username=username,
                        filename=rec['filename'],
                        task="monitor",
                    )

        records_dirs = [records_dir for records_dir in records_dirs if records_dir.exists()]
        if not records_dirs:
            return

        async def cleanup_short_recording(ts_file: Path, existing_rec: dict | None, seconds_since_modification: float) -> bool:
            stat = ts_file.stat()
            cached_duration = int((existing_rec or {}).get('duration_seconds') or 0)
            duration_seconds = cached_duration

            if seconds_since_modification < 120:
                return False

            should_probe_duration = (
                stat.st_size == 0
                or stat.st_size <= SHORT_RECORDING_PROBE_BYTES
                or (0 < duration_seconds < MIN_RECORDING_SECONDS)
            )

            if should_probe_duration and duration_seconds == 0 and stat.st_size > 0:
                duration_seconds = await get_video_duration(ts_file, ffmpeg_path)

            too_short = (
                stat.st_size == 0
                or (duration_seconds == 0 and stat.st_size < MIN_RECORDING_BYTES)
                or (0 < duration_seconds < MIN_RECORDING_SECONDS)
            )

            if not too_short:
                return False

            mp4_path = ts_file.with_suffix('.mp4')
            thumb_path = output_dir / "thumbnails" / username / f"{ts_file.stem}.jpg"
            deleted = []
            for path, label in ((ts_file, "TS"), (mp4_path, "MP4"), (thumb_path, "thumbnail")):
                if path.exists():
                    try:
                        path.unlink()
                        deleted.append(label)
                    except Exception as e:
                        logger.error(
                            "Erreur suppression fragment recording",
                            username=username,
                            filename=path.name,
                            error=str(e),
                        )

            await db.delete_recording(username, ts_file.name)
            logger.warning(
                "Recording trop court supprimé",
                username=username,
                filename=ts_file.name,
                duration_seconds=duration_seconds,
                file_size=stat.st_size,
                min_seconds=MIN_RECORDING_SECONDS,
                min_bytes=MIN_RECORDING_BYTES,
                deleted=deleted,
            )
            return True

        for ts_file in (ts_file for records_dir in records_dirs for ts_file in records_dir.glob("*.ts")):
            stat = ts_file.stat()
            
            # Récupérer la durée actuelle depuis la DB
            existing_recordings = await db.get_recordings(username)
            existing_rec = next((r for r in existing_recordings if r['filename'] == ts_file.name), None)
            seconds_since_modification = time.time() - stat.st_mtime

            if await cleanup_short_recording(ts_file, existing_rec, seconds_since_modification):
                continue
            
            # Calculer la durée uniquement si elle n'est pas déjà en cache ou est à 0
            duration_seconds = 0
            if existing_rec:
                duration_seconds = existing_rec.get('duration_seconds', 0)
            
            if duration_seconds == 0:
                # Vérifier que le fichier est stable (pas modifié depuis 120s)
                # pour éviter de calculer la durée sur un fichier en cours d'écriture
                if seconds_since_modification >= 120:
                    # Calculer la durée avec ffprobe
                    duration_seconds = await get_video_duration(ts_file, ffmpeg_path)
                    logger.debug("Durée calculée", username=username, filename=ts_file.name, duration=duration_seconds)
                else:
                    logger.debug("Fichier pas encore stable, skip calcul durée", 
                               username=username, 
                               filename=ts_file.name,
                               seconds_since_modification=int(seconds_since_modification))
            
            # Générer la miniature si elle n'existe pas
            thumbnail_path = None
            if existing_rec:
                thumbnail_path = existing_rec.get('thumbnail_path')
            
            if not thumbnail_path or not Path(thumbnail_path).exists():
                thumbnail_path = await generate_recording_thumbnail(ts_file, output_dir, username, ffmpeg_path)
                if thumbnail_path:
                    logger.debug("Miniature générée", username=username, filename=ts_file.name, thumb=thumbnail_path)
            
            # Générer recording_id si c'est un nouvel enregistrement
            recording_id = None
            if existing_rec:
                recording_id = existing_rec.get('recording_id')
            
            if not recording_id:
                # Extraire le timestamp du nom de fichier (format: YYYYMMDD_HHMMSS_xxx.ts)
                # Sinon générer un nouveau recording_id
                recording_id = f"{username}_{ts_file.stem}"

            existing_created_at = int((existing_rec or {}).get('created_at') or 0)
            created_at = existing_created_at
            filename_created_at = recording_timestamp_from_filename(ts_file.name)
            if (
                created_at in {0, int(stat.st_mtime)}
                or (filename_created_at is not None and created_at != filename_created_at)
            ):
                created_at = await get_media_created_at(
                    ts_file,
                    ffmpeg_path,
                    fallback_timestamp=int(stat.st_mtime),
                )
            
            await db.add_or_update_recording(
                username=username,
                filename=ts_file.name,
                file_path=str(ts_file),
                file_size=stat.st_size,
                recording_id=recording_id,
                duration_seconds=duration_seconds,
                thumbnail_path=thumbnail_path,
                created_at=created_at,
            )
    
    except Exception as e:
        logger.debug("Erreur mise à jour cache enregistrements", username=username, error=str(e))

async def monitor_models_task(
    db: 'Database',
    manager: 'FFmpegManager',
    ffmpeg_path: str = "ffmpeg",
    chaturbate_auth=None,
    cam4_auth=None,
    provider_registry=None,
):
    """
    Tâche de monitoring en arrière-plan.

    Pour chaque modèle trackée, vérifie son statut via le registre provider
    quand il est disponible, avec les anciens chemins Chaturbate/CAM4 en
    fallback.

    ``chaturbate_auth`` fournit les cookies authentifiés pour check_model_status
    Chaturbate (évite le redirect login, GH #11). ``cam4_auth`` est reservé
    pour d'éventuels besoins futurs côté CAM4.
    """
    logger.background_task("monitor", "Démarrage du monitoring continu")

    csrftoken = os.getenv("CHATURBATE_CSRFTOKEN")
    if csrftoken:
        logger.info("CSRF token détecté", has_token=True)

    await db.initialize()

    async def sleep_until_next_check():
        try:
            interval = await get_check_interval_seconds(db)
        except Exception as exc:
            logger.warning(
                "Impossible de lire l'intervalle de monitoring",
                task="monitor",
                error=str(exc),
            )
            interval = MONITOR_INTERVAL
        await asyncio.sleep(interval)

    async with aiohttp_client_session() as session:
        while True:
            try:
                models = await db.get_all_models()

                if not models:
                    await sleep_until_next_check()
                    continue

                logger.debug("Vérification des modèles", count=len(models))

                active_sessions = manager.list_status()

                for model in models:
                    username = model['username']
                    source_type = model.get('source_type') or 'chaturbate'
                    if source_type == 'chaturbate':
                        try:
                            followed = await db.get_followed_model(username)
                            followed_source = (followed or {}).get('source_type') or ''
                            if followed_source and followed_source != 'chaturbate':
                                source_type = followed_source
                        except Exception:
                            pass

                    try:
                        if provider_registry is not None and provider_registry.has(source_type):
                            try:
                                status_obj = await provider_registry.get(source_type).check_status(username)
                                status = status_obj.as_dict() if hasattr(status_obj, "as_dict") else dict(status_obj)
                            except Exception as e:
                                logger.debug(
                                    "Provider check_status error",
                                    source_type=source_type,
                                    username=username,
                                    error=str(e),
                                )
                                status = {'is_online': False, 'viewers': 0, 'hls_source': None, 'room_status': None, 'tags': []}
                        elif source_type == "cam4":
                            try:
                                from ..services import cam4_source
                                status = await cam4_source.check_status(username)
                            except Exception as e:
                                logger.debug(
                                    "CAM4 check_status error",
                                    username=username,
                                    error=str(e),
                                )
                                status = {'is_online': False, 'viewers': 0, 'hls_source': None, 'room_status': None, 'tags': []}
                        else:
                            # Chaturbate: check direct avec cookies authentifiés
                            auth_cookies = (
                                chaturbate_auth.get_cookies()
                                if chaturbate_auth is not None
                                else None
                            )
                            status = await check_model_status(
                                session,
                                username,
                                csrftoken,
                                auth_cookies=auth_cookies,
                            )
                        
                        # Vérifier si en cours d'enregistrement
                        active_session = next(
                            (s for s in active_sessions if s.get('person') == username and s.get('running')),
                            None
                        )
                        is_recording = active_session is not None
                        
                        # Générer/mettre à jour la miniature. Les modèles live
                        # sont rafraîchis plus souvent (60s) pour refléter
                        # l'activité sur les pages Discover / Following.
                        thumbnail_path = None
                        last_thumbnail_update = model.get('thumbnail_updated_at') or 0
                        thumb_interval = (
                            THUMBNAIL_UPDATE_INTERVAL_LIVE if status['is_online']
                            else THUMBNAIL_UPDATE_INTERVAL
                        )
                        needs_thumbnail_update = (
                            datetime.now().timestamp() - last_thumbnail_update > thumb_interval
                        )
                        
                        if needs_thumbnail_update:
                            # 1) HTTP download from provider (zero CPU). Only for Chaturbate;
                            #    CAM4 has its own flow via download in followed/model APIs.
                            if status['is_online'] and source_type == 'chaturbate':
                                thumbnail_path = await download_thumbnail_from_chaturbate(
                                    session,
                                    username,
                                    OUTPUT_DIR
                                )

                            # 2) Fallback: ffmpeg extract 1 frame from our local HLS playlist
                            if not thumbnail_path and is_recording and active_session:
                                thumbnail_path = await generate_thumbnail_from_stream(
                                    username,
                                    active_session['id'],
                                    OUTPUT_DIR,
                                    ffmpeg_path
                                )

                            # 3) Offline fallback: extract from latest recording
                            if not thumbnail_path:
                                thumbnail_path = await generate_thumbnail_from_recording(
                                    username,
                                    OUTPUT_DIR,
                                    ffmpeg_path
                                )
                        
                        # Mettre à jour le statut dans la DB
                        await db.update_model_status(
                            username=username,
                            is_online=status['is_online'],
                            viewers=status['viewers'],
                            is_recording=is_recording,
                            thumbnail_path=thumbnail_path,
                            room_status=status.get('room_status'),
                            source_type=source_type,
                        )
                        
                        # Mettre à jour le cache des enregistrements
                        await update_recordings_cache(db, username, OUTPUT_DIR, ffmpeg_path)
                        
                        logger.debug("Modèle mis à jour",
                                   username=username,
                                   is_online=status['is_online'],
                                   is_recording=is_recording,
                                   viewers=status['viewers'])
                    
                    except Exception as e:
                        logger.error("Erreur monitoring modèle",
                                   username=username,
                                   error=str(e),
                                   exc_info=True)
                        continue
                
                # Attendre avant la prochaine vérification
                await sleep_until_next_check()
            
            except Exception as e:
                logger.error("Erreur dans monitor task",
                           error=str(e),
                           exc_info=True)
                await sleep_until_next_check()
