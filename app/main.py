import re
import mimetypes
from pathlib import Path
from typing import Any, Optional, Tuple
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlparse, urlunparse
import os
import asyncio
import aiohttp
import json
import queue
import shutil
import subprocess
import sys
import time
from datetime import datetime
import secrets
import hashlib
import http.client
import socket as raw_socket
from html import unescape

from fastapi import FastAPI, HTTPException, Request, Cookie, Response
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from .browser_recorder import BrowserCaptureManager
from .ffmpeg_runner import FFmpegManager
from .logger import logger
from .core.database import Database
from .core.config import MIN_RECORDING_BYTES, MIN_RECORDING_SECONDS
from .core.utils import format_bytes
from .following_sync import store_provider_following
from .core.http_client import aiohttp_client_session, aiohttp_request_kwargs
from .tasks.monitor import (
    CHECK_INTERVAL_SETTING_KEY,
    get_check_interval_seconds,
    get_media_created_at,
    get_video_duration,
    monitor_models_task,
    normalize_check_interval_seconds,
)
from .tasks.convert import auto_convert_recordings_task
from .tasks.media_imports import (
    DEFAULT_MIN_AGE_SECONDS,
    DIRECT_PLAYABLE_EXTENSIONS,
    MediaImportManager,
    create_playable_mp4_copy,
    generate_import_thumbnail,
    media_imports_task,
    mp4_needs_faststart_repair,
    remove_import_record,
    stable_import_recording_id,
    SUPPORTED_VIDEO_EXTENSIONS,
    title_from_filename,
)
from .recording_names import (
    ALLOWED_FILENAME_FORMATS,
    FILENAME_FORMAT_TIMESTAMP,
    normalize_filename_format,
)
from .services.flaresolverr import DEFAULT_FLARE_SERVICE_URL, FlareSolverrClient
from .services.chaturbate_auth import ChaturbateAuthService
from .services.chaturbate_api import ChaturbateAPI
from .services.cam4_auth import CAM4AuthService
from .services import cam4_source
from .providers import (
    ProviderAuthError,
    ProviderError,
    ProviderInteractionRequired,
    ProviderOfflineError,
    ProviderPrivateError,
    ProviderRegistry,
    ProviderStatus,
    ResolvedStream,
    create_provider_registry,
)
from .providers.sessions import ProviderSessionStore
from .api import auth as auth_router
from .api import cam4 as cam4_router
from .api import discover as discover_router
from .api import following as following_router

# Environment
BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(BASE_DIR / "data")))
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")
HLS_TIME = int(os.getenv("HLS_TIME", "4"))
HLS_LIST_SIZE = int(os.getenv("HLS_LIST_SIZE", "6"))
CB_RESOLVER_ENABLED = os.getenv("CB_RESOLVER_ENABLED", "false").lower() in {"1", "true", "yes"}
PASSWORD = os.getenv("PASSWORD", "")  # Mot de passe optionnel
CHATURBATE_USERNAME = os.getenv("CHATURBATE_USERNAME", "")
CHATURBATE_PASSWORD = os.getenv("CHATURBATE_PASSWORD", "")
RECORDING_RANGE_CHUNK_SIZE = int(os.getenv("RECORDING_RANGE_CHUNK_SIZE", str(8 * 1024 * 1024)))
RECORDING_INITIAL_METADATA_MAX_BYTES = int(
    os.getenv("RECORDING_INITIAL_METADATA_MAX_BYTES", str(128 * 1024 * 1024))
)
MEDIA_IMPORTS_ENABLED = os.getenv("PSTREAMREC_MEDIA_IMPORTS", "false").lower() in {"1", "true", "yes"}
MEDIA_LIBRARY_METADATA_MIN_AGE_SECONDS = DEFAULT_MIN_AGE_SECONDS
MEDIA_LIBRARY_VIDEO_EXTENSIONS = SUPPORTED_VIDEO_EXTENSIONS
MEDIA_LIBRARY_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
MEDIA_LIBRARY_AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".ogg", ".wav", ".flac"}
MEDIA_LIBRARY_EXTENSIONS = (
    MEDIA_LIBRARY_VIDEO_EXTENSIONS
    | MEDIA_LIBRARY_IMAGE_EXTENSIONS
    | MEDIA_LIBRARY_AUDIO_EXTENSIONS
)
MEDIA_LIBRARY_BROWSER_PLAYABLE_VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm"}
PROFILE_IMAGES_DIR = OUTPUT_DIR / "profile-images"
PROFILE_IMAGE_MAX_BYTES = int(os.getenv("PSTREAMREC_PROFILE_IMAGE_MAX_BYTES", str(8 * 1024 * 1024)))
BABEPEDIA_BASE_URL = "https://www.babepedia.com"
BABEPEDIA_USER_AGENT = os.getenv(
    "PSTREAMREC_PROFILE_IMAGE_USER_AGENT",
    "P-StreamRec/0.1 (+https://github.com/Raccommode/P-StreamRec)",
)
MEDIA_LIBRARY_TEMP_SUFFIXES = (
    ".tmp",
    ".part",
    ".partial",
    ".download",
    ".crdownload",
)
IVS_PLAYER_ASSET_BASE_URL = "https://player.live-video.net/1.4.1"
IVS_PLAYER_ASSETS = {
    "amazon-ivs-player.min.js": "application/javascript",
    "amazon-ivs-worker.min.js": "application/javascript",
    "amazon-ivs-wasmworker.min.js": "application/javascript",
    "amazon-ivs-wasmworker.min.wasm": "application/wasm",
}
_IVS_PLAYER_ASSET_CACHE: dict[str, dict[str, object]] = {}

# Docker constants
DOCKER_SOCKET = '/var/run/docker.sock'
DOCKER_IMAGE = 'ghcr.io/raccommode/p-streamrec'


class _UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP connection over Unix domain socket (for Docker API)."""
    def __init__(self, socket_path, timeout=30):
        super().__init__('localhost', timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        self.sock = raw_socket.socket(raw_socket.AF_UNIX, raw_socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def _docker_api(method, path, body=None, timeout=30):
    """Send a request to the Docker Engine API via Unix socket."""
    conn = _UnixHTTPConnection(DOCKER_SOCKET, timeout=timeout)
    headers = {}
    body_bytes = None
    if body is not None:
        body_bytes = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
    conn.request(method, path, body=body_bytes, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    status = resp.status
    conn.close()
    return status, data


def _get_container_id():
    """Detect the current Docker container ID."""
    hostname = raw_socket.gethostname()
    if hostname and len(hostname) >= 12:
        try:
            int(hostname[:12], 16)
            return hostname[:12]
        except ValueError:
            pass
    for path in ('/proc/self/cgroup', '/proc/self/mountinfo'):
        try:
            with open(path, 'r') as f:
                for line in f:
                    if '/docker/' in line:
                        for part in reversed(line.strip().split('/')):
                            if len(part) >= 12:
                                try:
                                    int(part[:12], 16)
                                    return part[:12]
                                except ValueError:
                                    continue
        except FileNotFoundError:
                continue
    return None


def _normalize_version(version: Optional[str]) -> str:
    return (version or "").strip().lstrip("v").strip()


def _version_parts(version: str) -> Optional[Tuple[int, ...]]:
    match = re.search(r"\d+(?:\.\d+)*", _normalize_version(version))
    if not match:
        return None
    return tuple(int(part) for part in match.group(0).split("."))


def _is_update_available(current_version: str, latest_version: str) -> bool:
    current = _normalize_version(current_version)
    latest = _normalize_version(latest_version)
    if not current or current.lower() in {"dev", "local"} or not latest:
        return False
    if current == latest:
        return False

    current_parts = _version_parts(current)
    latest_parts = _version_parts(latest)
    if current_parts and latest_parts:
        width = max(len(current_parts), len(latest_parts))
        current_parts = current_parts + (0,) * (width - len(current_parts))
        latest_parts = latest_parts + (0,) * (width - len(latest_parts))
        return latest_parts > current_parts

    return current != latest


# Ensure dirs
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logger.info("Répertoire de sortie", path=str(OUTPUT_DIR))
logger.info("FFmpeg path", path=FFMPEG_PATH)
logger.info("HLS Configuration", hls_time=HLS_TIME, hls_list_size=HLS_LIST_SIZE)
logger.info("Chaturbate Resolver", enabled=CB_RESOLVER_ENABLED)
logger.info("Local media imports", enabled=MEDIA_IMPORTS_ENABLED)
if PASSWORD:
    logger.info("Authentification activée", protected=True)
else:
    logger.info("Authentification désactivée", protected=False)

app = FastAPI(title="P-StreamRec", version="0.1.0")

# Gestionnaire de sessions simples (en mémoire)
active_sessions = set()

def generate_session_token() -> str:
    """Génère un token de session sécurisé"""
    return secrets.token_urlsafe(32)

def verify_password(provided_password: str) -> bool:
    """Vérifie si le mot de passe fourni correspond"""
    return provided_password == PASSWORD

def is_authenticated(session_token: Optional[str]) -> bool:
    """Vérifie si la session est valide"""
    if not PASSWORD:
        return True  # Pas d'authentification requise
    return session_token in active_sessions

# Middleware d'authentification
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # Routes publiques (pas besoin d'authentification)
    public_paths = ["/login", "/api/login", "/favicon.ico"]
    public_prefixes = ["/static/", "/api/chaturbate/status", "/api/proxy/hls/"]

    if request.url.path in public_paths or any(
        request.url.path.startswith(p) for p in public_prefixes
    ):
        return await call_next(request)
    
    # Si pas de mot de passe configuré, laisser passer
    if not PASSWORD:
        return await call_next(request)
    
    # Vérifier le token de session
    session_token = request.cookies.get("session_token")
    
    if not is_authenticated(session_token):
        # Rediriger vers la page de login
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                status_code=401,
                content={"detail": "Non authentifié"}
            )
        return RedirectResponse(url="/login", status_code=303)
    
    return await call_next(request)

# Middleware pour logger toutes les requêtes
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    
    # Log requête
    logger.api_request(request.method, request.url.path)
    
    # Traiter requête
    response = await call_next(request)
    
    # Log réponse
    duration_ms = (time.time() - start_time) * 1000
    logger.api_response(response.status_code, request.url.path, duration_ms)
    
    return response

# Configuration CORS permissive pour Docker/self-hosted deployments
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Autoriser toutes les origines
    allow_credentials=False,  # Pas de credentials avec wildcard origin
    allow_methods=["*"],  # Autoriser toutes les méthodes (GET, POST, etc.)
    allow_headers=["*"],  # Autoriser tous les headers
)

# Register API routers
app.include_router(auth_router.router)
app.include_router(cam4_router.router)
app.include_router(discover_router.router)
app.include_router(following_router.router)

# Static mounts — desactiver le cache pour que les reload JS/CSS soient pris
# en compte immédiatement en dev (volume mount). Le surcoût bandwidth est
# negligeable pour des fichiers locaux.
@app.middleware("http")
async def _no_cache_static(request: Request, call_next):
    response = await call_next(request)
    no_cache_paths = {"/", "/discover", "/following", "/recordings", "/media", "/settings", "/wiki"}
    if request.url.path.startswith("/static/") or request.url.path in no_cache_paths:
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

def _recording_media_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in {".mp4", ".m4v", ".mov"}:
        return "video/mp4"
    if ext == ".webm":
        return "video/webm"
    if ext == ".mkv":
        return "video/x-matroska"
    if ext == ".avi":
        return "video/x-msvideo"
    return "video/mp2t"


def _recording_headers(filename: str, file_size: int) -> dict:
    return {
        "Content-Length": str(file_size),
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'inline; filename="{filename}"',
        "Cache-Control": "public, max-age=3600",
    }


def _mp4_faststart_metadata_end(file_path: Path, file_size: int) -> Optional[int]:
    """Return the byte offset after an initial MP4 moov box, when it is before media data."""
    max_metadata_bytes = max(RECORDING_INITIAL_METADATA_MAX_BYTES, 0)
    if file_size <= 0 or max_metadata_bytes <= 0:
        return None

    probe_limit = min(file_size, max_metadata_bytes)
    offset = 0
    try:
        with open(file_path, "rb") as f:
            while offset + 8 <= probe_limit:
                f.seek(offset)
                header = f.read(16)
                if len(header) < 8:
                    return None

                box_size = int.from_bytes(header[0:4], "big")
                box_type = header[4:8]
                header_size = 8
                if box_size == 1:
                    if len(header) < 16:
                        return None
                    box_size = int.from_bytes(header[8:16], "big")
                    header_size = 16
                elif box_size == 0:
                    box_size = file_size - offset

                if box_size < header_size:
                    return None

                box_end = offset + box_size
                if box_end > file_size:
                    return None
                if box_type == b"moov":
                    return box_end if box_end <= max_metadata_bytes else None
                if box_type in {b"mdat", b"moof"}:
                    return None

                offset = box_end
    except OSError:
        return None

    return None


def _initial_open_range_chunk_size(file_path: Path, filename: str, file_size: int, range_header: str) -> Optional[int]:
    range_spec = range_header[len("bytes="):].strip() if range_header.startswith("bytes=") else ""
    if range_spec != "0-":
        return None
    if Path(filename).suffix.lower() not in {".mp4", ".m4v", ".mov"}:
        return None

    metadata_end = _mp4_faststart_metadata_end(file_path, file_size)
    if not metadata_end:
        return None
    return max(RECORDING_RANGE_CHUNK_SIZE, metadata_end)


def _parse_byte_range(
    range_header: str,
    file_size: int,
    open_ended_chunk_size: Optional[int] = None,
) -> Optional[tuple[int, int]]:
    if file_size <= 0 or not range_header.startswith("bytes="):
        return None

    range_spec = range_header[len("bytes="):].strip()
    if "," in range_spec or "-" not in range_spec:
        return None

    start_text, end_text = range_spec.split("-", 1)
    try:
        if start_text == "":
            suffix_length = int(end_text)
            if suffix_length <= 0:
                return None
            start = max(file_size - suffix_length, 0)
            end = file_size - 1
        else:
            start = int(start_text)
            if end_text:
                end = int(end_text)
            else:
                # Browsers commonly request "bytes=N-" for video. Returning a
                # bounded chunk keeps very long replays responsive and avoids a
                # single multi-GB response through Docker/proxy layers.
                chunk_size = (
                    open_ended_chunk_size
                    if open_ended_chunk_size is not None
                    else RECORDING_RANGE_CHUNK_SIZE
                )
                end = start + max(chunk_size, 1) - 1
            end = min(end, file_size - 1)
    except ValueError:
        return None

    if start < 0 or start >= file_size or end < start:
        return None
    return start, end


async def _serve_video_file_with_ranges(request: Request, file_path: Path, filename: str):
    """Serve a local video path with browser-friendly Range/HEAD support."""
    from fastapi.responses import StreamingResponse

    if not file_path.exists() or not file_path.is_file():
        logger.error("Fichier vidéo introuvable", path=str(file_path))
        raise HTTPException(status_code=404, detail="Media not found")

    file_size = file_path.stat().st_size
    logger.file_operation("Lecture", str(file_path), size=file_size)

    media_type = _recording_media_type(filename)
    base_headers = _recording_headers(filename, file_size)
    range_header = request.headers.get("range")

    if range_header:
        open_ended_chunk_size = _initial_open_range_chunk_size(
            file_path,
            filename,
            file_size,
            range_header,
        )
        byte_range = _parse_byte_range(
            range_header,
            file_size,
            open_ended_chunk_size=open_ended_chunk_size,
        )
        if not byte_range:
            return Response(
                status_code=416,
                headers={
                    "Content-Range": f"bytes */{file_size}",
                    "Accept-Ranges": "bytes",
                }
            )

        start, end = byte_range
        chunk_size = end - start + 1

        async def range_file_stream():
            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = chunk_size
                while remaining > 0:
                    read_size = min(remaining, 64 * 1024)
                    data = f.read(read_size)
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        headers = {
            **base_headers,
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(chunk_size),
        }

        if request.method == "HEAD":
            return Response(status_code=206, media_type=media_type, headers=headers)

        return StreamingResponse(
            range_file_stream(),
            status_code=206,
            media_type=media_type,
            headers=headers
        )

    if request.method == "HEAD":
        return Response(status_code=200, media_type=media_type, headers=base_headers)

    return FileResponse(str(file_path), media_type=media_type, headers=base_headers)


def _media_library_kind(path: Path) -> Optional[str]:
    ext = path.suffix.lower()
    if ext in MEDIA_LIBRARY_IMAGE_EXTENSIONS:
        return "image"
    if ext in MEDIA_LIBRARY_VIDEO_EXTENSIONS:
        return "video"
    if ext in MEDIA_LIBRARY_AUDIO_EXTENSIONS:
        return "audio"
    return None


def _is_media_library_file(path: Path) -> bool:
    if not path.is_file():
        return False
    name = path.name
    if name.startswith(".") or any(name.endswith(suffix) for suffix in MEDIA_LIBRARY_TEMP_SUFFIXES):
        return False
    if any(part.startswith(".") for part in path.parts):
        return False
    return _media_library_kind(path) is not None


def _quote_url_path(path_value: str) -> str:
    return "/".join(quote(part, safe="") for part in Path(path_value).parts)


def _content_disposition(filename: str, disposition: str = "inline") -> str:
    fallback = re.sub(r'[^A-Za-z0-9._ -]+', "_", filename).strip() or "media"
    return f'{disposition}; filename="{fallback}"; filename*=UTF-8\'\'{quote(filename)}'


def _resolve_library_media_path(username: str, file_path: str) -> Path:
    if (
        not username
        or ".." in username
        or "/" in username
        or "\\" in username
        or "\x00" in username
        or "\x00" in file_path
        or "\\" in file_path
    ):
        raise HTTPException(status_code=400, detail="Invalid media path")

    relative = Path(file_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise HTTPException(status_code=400, detail="Invalid media path")

    records_root = (OUTPUT_DIR / "records").resolve()
    profile_root = (records_root / username).resolve()
    candidate = (profile_root / relative).resolve()

    if not candidate.is_relative_to(profile_root) or not candidate.is_relative_to(records_root):
        raise HTTPException(status_code=403, detail="Media path is not allowed")
    if _media_library_kind(candidate) is None:
        raise HTTPException(status_code=400, detail="Unsupported media format")
    return candidate


def _validate_media_profile_username(username: str) -> str:
    username = (username or "").strip()
    if (
        not username
        or username in {".", ".."}
        or "/" in username
        or "\\" in username
        or "\x00" in username
    ):
        raise HTTPException(status_code=400, detail="Invalid media profile")
    return username


def _media_profile_dir(username: str) -> Path:
    username = _validate_media_profile_username(username)
    records_root = (OUTPUT_DIR / "records").resolve()
    profile_dir = (records_root / username).resolve()
    if not profile_dir.is_relative_to(records_root):
        raise HTTPException(status_code=403, detail="Media profile is not allowed")
    return profile_dir


def _list_media_profile_folders() -> set[str]:
    records_root = OUTPUT_DIR / "records"
    if not records_root.exists():
        return set()

    profiles: set[str] = set()
    for child in records_root.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        try:
            _validate_media_profile_username(child.name)
        except HTTPException:
            continue
        profiles.add(child.name)
    return profiles


async def _repair_truncated_media_profile_usernames() -> int:
    try:
        profiles = await db.get_all_media_profiles()
        profile_names = {
            profile["username"]
            for profile in profiles
            if profile.get("username")
        }
        if not profile_names:
            return 0
        model_names = {
            model["username"]
            for model in await db.get_all_models()
            if model.get("username")
        }
        folder_names = _list_media_profile_folders()
        candidate_names = model_names | folder_names
        repaired = 0
        for profile_name in sorted(profile_names):
            candidates = [
                candidate
                for candidate in candidate_names
                if candidate != profile_name
                and candidate not in profile_names
                and (candidate.startswith("_") or candidate.endswith("_"))
                and candidate.strip("_") == profile_name
            ]
            if len(candidates) != 1:
                continue
            if await db.rename_media_profile(profile_name, candidates[0]):
                repaired += 1
                profile_names.discard(profile_name)
                profile_names.add(candidates[0])
                logger.info(
                    "Profil media repare apres troncature underscore",
                    old_username=profile_name,
                    new_username=candidates[0],
                )
        return repaired
    except Exception as exc:
        logger.debug("Reparation profils media tronques ignoree", error=str(exc))
        return 0


def _media_library_url(username: str, relative_path: str, download: bool = False) -> str:
    url = f"/streams/library/{quote(username, safe='')}/{_quote_url_path(relative_path)}"
    if download:
        return f"{url}?download=1"
    return url


def _model_for_media_profile(username: str, models: list[dict]) -> Optional[dict]:
    selected = None
    for model in models:
        if model.get("username") != username:
            continue
        if selected is None:
            selected = model
        if (model.get("source_type") or model.get("sourceType")) == "chaturbate":
            return model
    return selected


def _normalize_profile_image_url(value: object) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Profile image URL must be an HTTP URL")
    return raw


def _normalize_profile_source_url(value: object) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Profile image source URL must be an HTTP URL")
    return raw


def _is_local_profile_image_url(value: object) -> bool:
    raw = str(value or "").strip()
    return raw.startswith("/api/media-profiles/") and "/profile-image" in raw


def _profile_image_local_url(username: str, image_path: object) -> str:
    raw = str(image_path or "").strip()
    if not raw:
        return ""
    path = Path(raw)
    try:
        if not path.exists() or not path.is_file() or not _path_is_inside_output(path):
            return ""
        version = int(path.stat().st_mtime)
    except OSError:
        return ""
    return f"/api/media-profiles/{quote(username, safe='')}/profile-image?v={version}"


def _media_profile_image_url(profile: dict) -> str:
    username = profile.get("username") or ""
    local_url = _profile_image_local_url(username, profile.get("profile_image_path"))
    if local_url:
        return local_url
    remote_url = str(profile.get("profile_image_url") or "").strip()
    if remote_url.startswith("http://") or remote_url.startswith("https://"):
        return remote_url
    return ""


def _media_profile_formatted(profile: Optional[dict]) -> dict:
    profile = profile or {}
    birth_date = profile.get("birth_date") or ""
    profile_image_url = _media_profile_image_url(profile)
    profile_image_source_url = profile.get("profile_image_source_url") or ""
    return {
        "displayName": profile.get("display_name") or "",
        "firstName": profile.get("first_name") or "",
        "lastName": profile.get("last_name") or "",
        "age": profile.get("age"),
        "birthDate": birth_date,
        "birth_date": birth_date,
        "profileImageUrl": profile_image_url,
        "profile_image_url": profile_image_url,
        "profileImageSourceUrl": profile_image_source_url,
        "profile_image_source_url": profile_image_source_url,
        "address": profile.get("address") or "",
        "city": profile.get("city") or "",
        "region": profile.get("region") or "",
        "postalCode": profile.get("postal_code") or "",
        "country": profile.get("country") or "",
        "aliases": profile.get("aliases") or "",
        "tags": profile.get("tags") or "",
        "notes": profile.get("notes") or "",
        "socialUrls": profile.get("social_urls") or [],
        "streamUrls": profile.get("stream_urls") or [],
        "profileUrls": profile.get("profile_urls") or [],
    }


def _recording_source_session_key(profile_username: str, source_type: str, channel_username: str) -> str:
    return ":".join([
        _validate_media_profile_username(profile_username),
        (source_type or "chaturbate").strip().lower(),
        _validate_media_profile_username(channel_username),
    ])


def _channel_username_from_url(value: object) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    parts = [unquote(part).strip() for part in parsed.path.split("/") if part.strip()]
    if not parts:
        return None
    ignored = {"b", "chat", "en", "fr", "room", "rooms", "videochat"}
    for part in parts:
        if part.lower() not in ignored:
            return part.lstrip("@")
    return parts[-1].lstrip("@")


def _canonical_stream_url(source_type: str, channel_username: str, channel_url: Optional[str] = None) -> str:
    raw_url = str(channel_url or "").strip()
    if raw_url:
        parsed = urlparse(raw_url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return raw_url
    channel = quote(channel_username.strip().lstrip("@"), safe="")
    normalized_source = (source_type or "chaturbate").strip().lower()
    if normalized_source == "cam4":
        return f"https://www.cam4.com/{channel}"
    if normalized_source == "chaturbate":
        return f"https://chaturbate.com/{channel}/"
    try:
        if provider_registry.has(normalized_source):
            return provider_registry.get(normalized_source).canonical_url(channel_username)
    except Exception:
        pass
    return raw_url


def _normalize_live_channel_username(value: object, channel_url: object = None) -> str:
    raw = str(value or "").strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        raw = _channel_username_from_url(raw) or ""
    if not raw:
        raw = _channel_username_from_url(channel_url) or ""
    raw = raw.strip().lstrip("@")
    return _validate_media_profile_username(raw)


def _profile_source_response(source: dict) -> dict:
    source_type = (source.get("source_type") or source.get("sourceType") or "chaturbate").strip().lower()
    channel_username = source.get("channel_username") or source.get("channelUsername") or source.get("username") or ""
    profile_username = source.get("profile_username") or source.get("profileUsername") or ""
    record_path = source.get("record_path") or _default_record_path(profile_username or channel_username)
    retention_value = source.get("retention_days")
    if retention_value is None:
        retention_value = source.get("retentionDays", 30)
    try:
        retention_days = int(retention_value if retention_value is not None else 30)
    except (TypeError, ValueError):
        retention_days = 30
    return {
        "id": source.get("id"),
        "profileUsername": profile_username,
        "profile_username": profile_username,
        "sourceType": source_type,
        "source_type": source_type,
        "channelUsername": channel_username,
        "channel_username": channel_username,
        "channelUrl": source.get("channel_url") or source.get("channelUrl") or _canonical_stream_url(source_type, channel_username),
        "channel_url": source.get("channel_url") or source.get("channelUrl") or _canonical_stream_url(source_type, channel_username),
        "autoRecord": bool(source.get("auto_record", source.get("autoRecord", False))),
        "auto_record": bool(source.get("auto_record", source.get("autoRecord", False))),
        "recordQuality": source.get("record_quality") or source.get("recordQuality") or "best",
        "record_quality": source.get("record_quality") or source.get("recordQuality") or "best",
        "retentionDays": retention_days,
        "retention_days": retention_days,
        "recordPath": record_path,
        "record_path": record_path,
        "sessionKey": _recording_source_session_key(profile_username or channel_username, source_type, channel_username) if channel_username else "",
    }


async def _media_profile_stream_sources(username: str) -> list[dict]:
    username = _validate_media_profile_username(username)
    sources = await db.get_media_profile_sources(username)
    if sources:
        return [_profile_source_response(source) for source in sources]

    model = await db.get_model(username)
    if not model:
        return []
    source_type = await _infer_source_type(username, model)
    return [_profile_source_response({
        "profile_username": username,
        "source_type": source_type,
        "channel_username": username,
        "channel_url": _canonical_stream_url(source_type, username),
        "auto_record": bool(model.get("auto_record", False)),
        "record_quality": model.get("record_quality") or await _get_default_record_quality(),
        "retention_days": model.get("retention_days", await _get_default_retention_days()),
        "record_path": _record_path_from_model(model, username),
    })]


async def _normalize_profile_source_payload(
    profile_username: str,
    raw_source: dict,
    default_auto_record: bool = False,
) -> dict:
    raw_source = raw_source or {}
    source_type = _normalize_source_type(
        raw_source.get("sourceType")
        or raw_source.get("source_type")
        or _source_type_from_url(str(raw_source.get("channelUrl") or raw_source.get("channel_url") or raw_source.get("url") or ""))
        or "chaturbate"
    ) or "chaturbate"
    if source_type not in _available_source_types():
        raise HTTPException(status_code=400, detail=f"Source '{source_type}' is not available")

    channel_url = raw_source.get("channelUrl") or raw_source.get("channel_url") or raw_source.get("url") or raw_source.get("sourceUrl")
    channel_username = _normalize_live_channel_username(
        raw_source.get("channelUsername")
        or raw_source.get("channel_username")
        or raw_source.get("modelUsername")
        or raw_source.get("model_username")
        or raw_source.get("target")
        or raw_source.get("username"),
        channel_url,
    )
    retention_days = _normalize_retention_days(
        raw_source.get("retentionDays") if "retentionDays" in raw_source else raw_source.get("retention_days"),
        await _get_default_retention_days(),
    )
    record_quality = (
        raw_source.get("recordQuality")
        or raw_source.get("record_quality")
        or await _get_default_record_quality()
    )
    auto_record = bool(raw_source.get("autoRecord", raw_source.get("auto_record", default_auto_record)))
    record_path = _normalize_record_path(
        raw_source.get("recordPath") or raw_source.get("record_path") or _default_record_path(profile_username),
        profile_username,
    )
    return {
        "profile_username": profile_username,
        "source_type": source_type,
        "channel_username": channel_username,
        "channel_url": _canonical_stream_url(source_type, channel_username, channel_url),
        "auto_record": auto_record,
        "record_quality": record_quality,
        "retention_days": retention_days,
        "record_path": record_path,
    }


def _normalize_birth_date(value: object) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Birth date must use YYYY-MM-DD")
    return parsed.strftime("%Y-%m-%d")


def _babepedia_slug(value: str) -> str:
    raw = re.sub(r"\s+", "_", str(value or "").strip())
    raw = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip(".-")
    return raw


def _babepedia_candidate_urls(username: str, profile: dict, body: dict) -> list[str]:
    candidates: list[str] = []

    def add_url(value: object):
        raw = str(value or "").strip()
        if not raw:
            return
        parsed = urlparse(raw)
        if parsed.scheme in {"http", "https"} and parsed.netloc and raw not in candidates:
            candidates.append(raw)

    def add_query(value: object):
        raw = str(value or "").strip()
        if not raw:
            return
        slug = _babepedia_slug(raw)
        if slug:
            add_url(f"{BABEPEDIA_BASE_URL}/babe/{quote(slug, safe='')}")

    add_url(body.get("sourceUrl") or body.get("source_url"))
    for url in body.get("profileUrls") or body.get("profile_urls") or profile.get("profile_urls") or []:
        if "babepedia." in str(url).lower():
            add_url(url)

    add_query(body.get("query"))
    add_query(profile.get("display_name"))
    first_last = " ".join(
        part for part in [profile.get("first_name"), profile.get("last_name")] if part
    )
    add_query(first_last)
    for alias in re.split(r"[,;\n]+", str(profile.get("aliases") or "")):
        add_query(alias)
    add_query(username)
    return candidates


def _direct_image_url(value: object) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if Path(parsed.path).suffix.lower() in MEDIA_LIBRARY_IMAGE_EXTENSIONS:
        return raw
    return None


def _html_meta_content(html: str, key: str) -> Optional[str]:
    for tag in re.findall(r"<meta\b[^>]*>", html or "", flags=re.IGNORECASE):
        if re.search(
            r"(?:property|name)\s*=\s*['\"]" + re.escape(key) + r"['\"]",
            tag,
            flags=re.IGNORECASE,
        ):
            match = re.search(r"content\s*=\s*['\"]([^'\"]+)['\"]", tag, flags=re.IGNORECASE)
            if match:
                return unescape(match.group(1).strip())
    return None


def _extract_babepedia_image_url(html: str, page_url: str) -> Optional[str]:
    for key in ("og:image", "twitter:image", "twitter:image:src"):
        image_url = _html_meta_content(html, key)
        if image_url:
            return urljoin(page_url, image_url)

    focused = re.search(
        r"<img\b[^>]*(?:class|id)\s*=\s*['\"][^'\"]*(?:profile|babe|thumb|photo)[^'\"]*['\"][^>]*>",
        html or "",
        flags=re.IGNORECASE,
    )
    if focused:
        match = re.search(r"\bsrc\s*=\s*['\"]([^'\"]+)['\"]", focused.group(0), flags=re.IGNORECASE)
        if match:
            return urljoin(page_url, unescape(match.group(1).strip()))

    return None


async def _fetch_text_url(url: str) -> str:
    timeout = aiohttp.ClientTimeout(total=20)
    headers = {"User-Agent": BABEPEDIA_USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    async with aiohttp_client_session(timeout=timeout, headers=headers) as session:
        async with session.get(url, **aiohttp_request_kwargs()) as response:
            if response.status >= 400:
                raise HTTPException(status_code=404, detail="Profile image source not found")
            return await response.text(errors="ignore")


def _profile_image_extension(url: str, content_type: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in MEDIA_LIBRARY_IMAGE_EXTENSIONS:
        return ".jpg" if suffix == ".jpeg" else suffix
    guessed = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip())
    if guessed == ".jpe":
        guessed = ".jpg"
    if guessed in MEDIA_LIBRARY_IMAGE_EXTENSIONS:
        return ".jpg" if guessed == ".jpeg" else guessed
    return ".jpg"


async def _download_profile_image(username: str, image_url: str) -> dict:
    timeout = aiohttp.ClientTimeout(total=30)
    headers = {"User-Agent": BABEPEDIA_USER_AGENT, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
    async with aiohttp_client_session(timeout=timeout, headers=headers) as session:
        async with session.get(image_url, **aiohttp_request_kwargs()) as response:
            if response.status >= 400:
                raise HTTPException(status_code=404, detail="Profile image could not be downloaded")

            content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type and not content_type.startswith("image/"):
                raise HTTPException(status_code=400, detail="Profile image source did not return an image")

            extension = _profile_image_extension(str(response.url), content_type)
            PROFILE_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
            destination = PROFILE_IMAGES_DIR / f"{username}{extension}"
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            size = 0
            with temporary.open("wb") as fh:
                async for chunk in response.content.iter_chunked(1024 * 256):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > PROFILE_IMAGE_MAX_BYTES:
                        temporary.unlink(missing_ok=True)
                        raise HTTPException(status_code=413, detail="Profile image is too large")
                    fh.write(chunk)
            temporary.replace(destination)

    return {
        "path": str(destination),
        "size": size,
        "contentType": content_type or mimetypes.guess_type(destination.name)[0] or "image/jpeg",
    }


async def _resolve_profile_image_from_babepedia(username: str, profile: dict, body: dict) -> dict:
    direct_url = _direct_image_url(body.get("profileImageUrl") or body.get("profile_image_url"))
    if direct_url:
        return {"imageUrl": direct_url, "sourceUrl": body.get("sourceUrl") or body.get("source_url") or direct_url}

    candidates = _babepedia_candidate_urls(username, profile, body)
    if not candidates:
        raise HTTPException(status_code=404, detail="No Babepedia profile image candidate found")

    last_error: Optional[Exception] = None
    for candidate in candidates:
        direct = _direct_image_url(candidate)
        if direct:
            return {"imageUrl": direct, "sourceUrl": direct}

        try:
            html = await _fetch_text_url(candidate)
            image_url = _extract_babepedia_image_url(html, candidate)
            if image_url:
                return {"imageUrl": image_url, "sourceUrl": candidate}
        except HTTPException as e:
            last_error = e
        except Exception as e:
            last_error = e
            logger.debug("Résolution image profil Babepedia échouée", url=candidate, error=str(e))

    if isinstance(last_error, HTTPException):
        raise last_error
    raise HTTPException(status_code=404, detail="No Babepedia profile image found")


def _profile_image_update_from_body(body: dict, existing: dict) -> dict:
    has_image_url = "profileImageUrl" in body or "profile_image_url" in body
    raw_image_url = body.get("profileImageUrl") if "profileImageUrl" in body else body.get("profile_image_url")
    has_source_url = "profileImageSourceUrl" in body or "profile_image_source_url" in body
    raw_source_url = (
        body.get("profileImageSourceUrl")
        if "profileImageSourceUrl" in body
        else body.get("profile_image_source_url")
    )

    image_path = existing.get("profile_image_path")
    image_url = existing.get("profile_image_url")
    source_url = existing.get("profile_image_source_url")

    if has_image_url:
        if _is_local_profile_image_url(raw_image_url):
            image_url = existing.get("profile_image_url")
            image_path = existing.get("profile_image_path")
        elif str(raw_image_url or "").strip():
            image_url = _normalize_profile_image_url(raw_image_url)
            image_path = None
        else:
            image_url = None
            image_path = None

    if has_source_url:
        source_url = _normalize_profile_source_url(raw_source_url) if str(raw_source_url or "").strip() else None

    return {
        "profile_image_url": image_url,
        "profile_image_source_url": source_url,
        "profile_image_path": image_path,
    }


async def _media_profile_payload(username: str) -> dict:
    username = _validate_media_profile_username(username)
    profile_dir = _media_profile_dir(username)
    profile = await db.get_media_profile(username)
    model = await db.get_model(username)
    default_quality = await _get_default_record_quality()
    default_retention = await _get_default_retention_days()
    stream_sources = await _media_profile_stream_sources(username)
    primary_source = stream_sources[0] if stream_sources else None
    source_type = (primary_source or {}).get("sourceType") or (await _infer_source_type(username, model) if model else "chaturbate")
    auto_record = bool((primary_source or {}).get("autoRecord", False)) if primary_source else (bool(model.get("auto_record", False)) if model else False)
    record_quality = (primary_source or {}).get("recordQuality") or (model or {}).get("record_quality") or default_quality
    retention_days = (primary_source or {}).get("retentionDays")
    if retention_days is None:
        retention_days = (model or {}).get("retention_days", default_retention)

    return {
        "username": username,
        "folderExists": profile_dir.exists() and profile_dir.is_dir(),
        "deleteUrl": f"/api/media-profiles/{quote(username, safe='')}",
        **_media_profile_formatted(profile),
        "sourceType": source_type,
        "source_type": source_type,
        "autoRecord": auto_record,
        "recordQuality": record_quality,
        "retentionDays": retention_days,
        "streamSources": stream_sources,
        "stream_sources": stream_sources,
        **_record_path_fields(username, model),
    }


def _media_library_placeholder_title(filename: str) -> str:
    title = Path(filename).stem.replace("_", " ").replace("-", " ").strip()
    return re.sub(r"\s+", " ", title) or filename


def _recording_thumb_url(username: str, rec: Optional[dict], path: Path) -> Optional[str]:
    thumb_value = (rec or {}).get("thumbnail_path")
    if thumb_value:
        thumb_path = Path(thumb_value)
        if thumb_path.exists():
            return f"/api/recording-thumbnail/{quote(username, safe='')}/{quote(thumb_path.name, safe='')}"

    generated = OUTPUT_DIR / "thumbnails" / username / f"{path.stem}.jpg"
    if generated.exists():
        return f"/api/recording-thumbnail/{quote(username, safe='')}/{quote(generated.name, safe='')}"
    return None


async def _ensure_media_library_video_metadata(
    username: str,
    relative_path: str,
    media_path: Path,
    rec: Optional[dict],
    stat: os.stat_result,
) -> Optional[dict]:
    if _media_library_kind(media_path) != "video":
        return rec
    if time.time() - stat.st_mtime < MEDIA_LIBRARY_METADATA_MIN_AGE_SECONDS:
        return rec

    current = dict(rec or {})
    duration_seconds = int(current.get("duration_seconds") or 0)
    thumbnail_url = _recording_thumb_url(username, current, media_path)
    source_mtime = int(stat.st_mtime)
    current_created_at = int(current.get("created_at") or 0)
    recording_id = current.get("recording_id") or stable_import_recording_id(username, relative_path)
    media_kind = (current.get("media_kind") or "import").strip().lower() or "import"
    title = current.get("title") or title_from_filename(media_path.name)
    needs_faststart_repair = media_kind == "import" and mp4_needs_faststart_repair(media_path)
    playable_points_to_source = _resolved_path_key(current.get("playable_path")) == _resolved_path_key(str(media_path))
    source_changed = (
        int(current.get("source_mtime") or 0) != source_mtime
        or int(current.get("file_size") or 0) != stat.st_size
    )
    needs_playable_copy = (
        media_kind == "import"
        and (
            (
                media_path.suffix.lower() not in DIRECT_PLAYABLE_EXTENSIONS
                and (not current.get("playable_path") or source_changed)
            )
            or (
                needs_faststart_repair
                and (
                    not current.get("playable_path")
                    or playable_points_to_source
                    or source_changed
                )
            )
        )
    )
    created_at = current_created_at or source_mtime
    if current_created_at in {0, source_mtime}:
        created_at = await get_media_created_at(
            media_path,
            FFMPEG_PATH,
            fallback_timestamp=source_mtime,
            reference_texts=[title],
        )
    if duration_seconds > 0 and thumbnail_url and current_created_at == created_at and not needs_playable_copy:
        return rec

    if duration_seconds <= 0:
        duration_seconds = await get_video_duration(media_path, FFMPEG_PATH)

    thumbnail_path = current.get("thumbnail_path") if thumbnail_url else None
    if not thumbnail_path:
        thumbnail_path = await generate_import_thumbnail(
            media_path,
            OUTPUT_DIR,
            username,
            recording_id,
            FFMPEG_PATH,
        )

    playable_path = current.get("playable_path")
    playable_size = current.get("playable_size")
    mp4_path = current.get("mp4_path")
    mp4_size = current.get("mp4_size")
    import_status = current.get("import_status") or ("ready" if media_kind == "import" else None)
    import_error = current.get("import_error")
    if media_kind == "import" and source_changed:
        playable_path = None
        playable_size = None
        mp4_path = None
        mp4_size = None

    if needs_faststart_repair and media_kind == "import":
        cached_path = OUTPUT_DIR / "media_imports" / username / f"{recording_id}.mp4"
        if cached_path.exists() and not source_changed:
            playable_path = str(cached_path)
            playable_size = cached_path.stat().st_size
            mp4_path = str(cached_path)
            mp4_size = playable_size
            import_status = "ready"
            import_error = None
        else:
            playable_path = None

    if not playable_path and media_path.suffix.lower() in DIRECT_PLAYABLE_EXTENSIONS and not needs_faststart_repair:
        playable_path = str(media_path)
        playable_size = stat.st_size
    elif not playable_path and media_kind == "import":
        ok, converted_path, error = await create_playable_mp4_copy(
            media_path,
            OUTPUT_DIR,
            username,
            recording_id,
            FFMPEG_PATH,
        )
        if ok and converted_path:
            playable_path = str(converted_path)
            playable_size = converted_path.stat().st_size
            if converted_path != media_path:
                mp4_path = str(converted_path)
                mp4_size = playable_size
            import_status = "ready"
            import_error = None
        else:
            import_status = "failed"
            import_error = error or "Conversion failed"

    protected_from_retention = bool(current.get("protected_from_retention"))
    if media_kind == "import":
        protected_from_retention = True

    await db.add_or_update_recording(
        username=username,
        filename=media_path.name,
        file_path=str(media_path),
        file_size=stat.st_size,
        recording_id=recording_id,
        duration_seconds=duration_seconds,
        thumbnail_path=thumbnail_path,
        mp4_path=mp4_path,
        mp4_size=mp4_size,
        is_converted=bool(current.get("is_converted") or playable_path),
        media_kind=media_kind,
        title=title,
        import_status=import_status,
        import_error=import_error,
        source_mtime=source_mtime,
        playable_path=playable_path,
        playable_size=playable_size,
        protected_from_retention=protected_from_retention,
        created_at=created_at,
    )

    current.update({
        "username": username,
        "filename": media_path.name,
        "file_path": str(media_path),
        "file_size": stat.st_size,
        "recording_id": recording_id,
        "duration_seconds": duration_seconds,
        "thumbnail_path": thumbnail_path or current.get("thumbnail_path"),
        "mp4_path": mp4_path,
        "mp4_size": mp4_size,
        "is_converted": bool(current.get("is_converted") or playable_path),
        "media_kind": media_kind,
        "title": title,
        "import_status": import_status,
        "import_error": import_error,
        "source_mtime": source_mtime,
        "playable_path": playable_path,
        "playable_size": playable_size,
        "protected_from_retention": protected_from_retention,
        "created_at": created_at,
    })
    return current


def _media_library_stats(items: list[dict]) -> dict:
    stats = {
        "total": len(items),
        "videos": 0,
        "images": 0,
        "audio": 0,
        "totalSize": 0,
    }
    for item in items:
        stats["totalSize"] += int(item.get("size") or 0)
        if item.get("type") == "video":
            stats["videos"] += 1
        elif item.get("type") == "image":
            stats["images"] += 1
        elif item.get("type") == "audio":
            stats["audio"] += 1
    return stats


def _normalize_watched_threshold(value, default: int = 90) -> int:
    try:
        threshold = int(value)
    except (ValueError, TypeError):
        threshold = default
    return max(0, min(100, threshold))


async def _get_watched_threshold() -> int:
    raw = await db.get_setting("auto_delete_threshold")
    return _normalize_watched_threshold(raw if raw is not None else 90)


def _playback_progress(position_seconds: float, duration_seconds: float) -> int:
    try:
        position = float(position_seconds)
        duration = float(duration_seconds)
    except (ValueError, TypeError):
        return 0
    if duration <= 0 or position <= 0:
        return 0
    return max(0, min(100, round((position / duration) * 100)))


def _is_playback_watched(
    position_seconds: float,
    duration_seconds: float,
    watched_threshold: int,
) -> bool:
    try:
        position = float(position_seconds)
        duration = float(duration_seconds)
    except (ValueError, TypeError):
        return False
    if duration <= 0 or position <= 0:
        return False
    return _playback_progress(position, duration) >= watched_threshold or position >= max(0, duration - 2)


def _attach_media_playback_state(
    item: dict,
    playback_position: Optional[dict],
    watched_threshold: int,
) -> None:
    if item.get("type") != "video":
        item.update({
            "playbackPosition": 0,
            "playbackDuration": 0,
            "playbackProgress": 0,
            "watchedThreshold": watched_threshold,
            "isWatched": False,
            "watchedAt": None,
        })
        return

    position = float((playback_position or {}).get("position_seconds") or 0)
    duration = float(
        (playback_position or {}).get("duration_seconds")
        or item.get("duration")
        or 0
    )
    progress = _playback_progress(position, duration)
    stored_watched_at = (playback_position or {}).get("watched_at")
    is_watched = bool(stored_watched_at) or _is_playback_watched(position, duration, watched_threshold)

    item.update({
        "playbackPosition": position,
        "playbackDuration": duration,
        "playbackProgress": progress,
        "watchedThreshold": watched_threshold,
        "isWatched": is_watched,
        "watchedAt": stored_watched_at or ((playback_position or {}).get("updated_at") if is_watched else None),
    })


def _media_library_lazy_record(
    username: str,
    relative_path: str,
    media_path: Path,
    stat: os.stat_result,
) -> dict:
    """Return enough recording-like metadata for listing without probing media."""
    return {
        "username": username,
        "filename": media_path.name,
        "file_path": str(media_path),
        "file_size": stat.st_size,
        "recording_id": stable_import_recording_id(username, relative_path),
        "duration_seconds": 0,
        "media_kind": "library",
        "title": title_from_filename(media_path.name),
        "source_mtime": int(stat.st_mtime),
        "playable_path": str(media_path)
        if media_path.suffix.lower() in DIRECT_PLAYABLE_EXTENSIONS
        else None,
        "playable_size": stat.st_size
        if media_path.suffix.lower() in DIRECT_PLAYABLE_EXTENSIONS
        else None,
        "created_at": int(stat.st_mtime),
    }


async def _scan_media_library_items(
    profile_username: Optional[str] = None,
    refresh_metadata: bool = True,
) -> list[dict]:
    from .core.utils import format_bytes

    records_root = OUTPUT_DIR / "records"
    if not records_root.exists():
        return []

    if profile_username:
        selected_username = _validate_media_profile_username(profile_username)
        selected_dir = records_root / selected_username
        profile_dirs = [selected_dir] if selected_dir.is_dir() else []
    else:
        profile_dirs = [
            profile_dir
            for profile_dir in sorted(records_root.iterdir(), key=lambda p: p.name.lower())
            if profile_dir.is_dir() and not profile_dir.name.startswith(".")
        ]
    recordings_by_username = await db.get_recordings_for_usernames([profile_dir.name for profile_dir in profile_dirs])

    items: list[dict] = []
    for profile_dir in profile_dirs:
        username = profile_dir.name
        profile_root = profile_dir.resolve()
        recordings = recordings_by_username.get(username, [])
        recordings_by_path: dict[str, dict] = {}
        recordings_by_filename: dict[str, dict] = {}

        for rec in recordings:
            filename = rec.get("filename")
            if filename:
                recordings_by_filename.setdefault(filename, rec)
            for key in ("file_path", "mp4_path", "playable_path"):
                value = rec.get(key)
                if not value:
                    continue
                try:
                    recordings_by_path.setdefault(str(Path(value).resolve()), rec)
                except Exception:
                    continue

        for media_path in profile_dir.rglob("*"):
            if not _is_media_library_file(media_path):
                continue
            try:
                resolved = media_path.resolve()
                if not resolved.is_relative_to(profile_root):
                    continue
                stat = media_path.stat()
                relative_path = media_path.relative_to(profile_dir).as_posix()
            except Exception:
                continue

            kind = _media_library_kind(media_path)
            if not kind:
                continue

            rec = recordings_by_path.get(str(resolved))
            if rec is None and len(Path(relative_path).parts) == 1:
                rec = recordings_by_filename.get(media_path.name)
            if kind == "video" and refresh_metadata:
                rec = await _ensure_media_library_video_metadata(
                    username,
                    relative_path,
                    media_path,
                    rec,
                    stat,
                )
            elif kind == "video" and rec is None:
                rec = _media_library_lazy_record(username, relative_path, media_path, stat)

            imported_recording_id = (rec or {}).get("recording_id")
            media_kind = (rec or {}).get("media_kind")
            is_imported = bool(rec and media_kind == "import")
            if kind == "video" and is_imported and imported_recording_id:
                url = f"/streams/media/{quote(imported_recording_id, safe='')}"
                download_url = f"{url}?download=1"
                browser_playable = bool((rec or {}).get("playable_path")) or media_path.suffix.lower() in MEDIA_LIBRARY_BROWSER_PLAYABLE_VIDEO_EXTENSIONS
            else:
                url = _media_library_url(username, relative_path)
                download_url = _media_library_url(username, relative_path, download=True)
                browser_playable = kind != "video" or media_path.suffix.lower() in MEDIA_LIBRARY_BROWSER_PLAYABLE_VIDEO_EXTENSIONS
            thumbnail = url if kind == "image" else _recording_thumb_url(username, rec, media_path)
            created_at = int((rec or {}).get("created_at") or stat.st_mtime)
            duration_seconds = int((rec or {}).get("duration_seconds") or 0)
            recording_id = imported_recording_id
            item_id_seed = f"{username}\0{relative_path}"
            item_id = hashlib.sha256(item_id_seed.encode("utf-8")).hexdigest()[:16]

            items.append({
                "id": item_id,
                "recordingId": recording_id,
                "username": username,
                "filename": media_path.name,
                "relativePath": relative_path,
                "title": (rec or {}).get("title") or _media_library_placeholder_title(media_path.name),
                "type": kind,
                "extension": media_path.suffix.lower().lstrip("."),
                "mimeType": mimetypes.guess_type(media_path.name)[0] or (
                    _recording_media_type(media_path.name) if kind == "video" else "application/octet-stream"
                ),
                "size": stat.st_size,
                "sizeFormatted": format_bytes(stat.st_size),
                "createdAt": created_at,
                "modifiedAt": int(stat.st_mtime),
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "duration": duration_seconds,
                "durationStr": _format_duration_label(duration_seconds) if duration_seconds else "",
                "url": url,
                "downloadUrl": download_url,
                "deleteUrl": f"/api/media-library/{quote(username, safe='')}/{_quote_url_path(relative_path)}",
                "thumbnail": thumbnail,
                "browserPlayable": browser_playable,
                "isImported": is_imported,
                "isRecording": bool(rec and (media_kind or "recording") == "recording"),
            })

    return items


# Route protégée pour les enregistrements
def _recording_candidate_paths(rec: dict) -> list[Path]:
    candidates: list[Path] = []
    if rec.get("is_converted") and rec.get("mp4_path"):
        candidates.append(Path(rec["mp4_path"]))
    for key in ("playable_path", "file_path", "mp4_path"):
        value = rec.get(key)
        if value:
            path = Path(value)
            if path not in candidates:
                candidates.append(path)
    return candidates


def _select_recording_path(rec: dict, requested_filename: Optional[str] = None) -> Optional[Path]:
    candidates = _recording_candidate_paths(rec)
    if requested_filename:
        for path in candidates:
            if path.name == requested_filename and path.exists() and path.is_file():
                return path
    for path in candidates:
        if path.exists() and path.is_file():
            return path
    return None


def _assert_recording_path_is_safe(path: Path):
    if not _path_is_inside_output(path):
        logger.warning("Chemin recording hors volume refusé", path=str(path))
        raise HTTPException(status_code=403, detail="Recording path is not allowed")


def _assert_recording_not_active(username: str, path: Path):
    target_key = _resolved_path_key(str(path))
    for session in _all_recording_statuses():
        if not (session.get("person") == username and session.get("running")):
            continue
        active_key = _resolved_path_key(session.get("record_path") or "")
        if target_key and active_key == target_key:
            logger.warning("Accès bloqué à enregistrement en cours", username=username, path=str(path))
            raise HTTPException(
                status_code=403,
                detail="This recording is in progress. Watch the live stream instead.",
            )


async def _serve_recording_from_record(
    request: Request,
    rec: dict,
    requested_filename: Optional[str] = None,
):
    username = rec.get("username") or ""
    path = _select_recording_path(rec, requested_filename)
    if not path:
        raise HTTPException(status_code=404, detail="Recording not found")
    _assert_recording_path_is_safe(path)
    _assert_recording_not_active(username, path)
    return await _serve_video_file_with_ranges(request, path, path.name)


@app.api_route("/streams/recordings/{recording_id}", methods=["GET", "HEAD"])
async def serve_recording_by_id(request: Request, recording_id: str):
    """Sert un enregistrement via son ID, même si le fichier est en sous-dossier."""
    if ".." in recording_id or "/" in recording_id:
        raise HTTPException(status_code=400, detail="Invalid recording ID")
    rec = await db.get_recording_by_id(recording_id)
    if not rec or rec.get("media_kind") == "import":
        raise HTTPException(status_code=404, detail="Recording not found")
    return await _serve_recording_from_record(request, rec)


@app.api_route("/streams/records/{username}/{filename}", methods=["GET", "HEAD"])
async def serve_recording_protected(request: Request, username: str, filename: str):
    """Sert un enregistrement (TS ou MP4) avec support HTTP Range pour les gros fichiers"""
    logger.api_request(request.method, f"/streams/records/{username}/{filename}")

    # Sécurité: vérifier le nom de fichier
    if (
        ".." in username
        or "/" in username
        or ".." in filename
        or "/" in filename
        or not filename.lower().endswith((".ts", ".mp4", ".webm"))
    ):
        logger.warning("Tentative d'accès fichier invalide", username=username, filename=filename)
        raise HTTPException(status_code=400, detail="Invalid filename")

    for rec in await db.get_recordings(username):
        if rec.get("media_kind") == "import":
            continue
        if (
            rec.get("filename") == filename
            or Path(rec.get("file_path") or "").name == filename
            or Path(rec.get("mp4_path") or "").name == filename
            or Path(rec.get("playable_path") or "").name == filename
        ):
            return await _serve_recording_from_record(request, rec, filename)

    # Servir le fichier
    file_path = OUTPUT_DIR / "records" / username / filename
    _assert_recording_not_active(username, file_path)
    return await _serve_video_file_with_ranges(request, file_path, filename)


@app.api_route("/streams/media/{recording_id}", methods=["GET", "HEAD"])
async def serve_imported_media(request: Request, recording_id: str, download: bool = False):
    """Sert un média importé via son ID stable, sans exposer de chemin disque."""
    if ".." in recording_id or "/" in recording_id:
        raise HTTPException(status_code=400, detail="Invalid media ID")

    rec = await db.get_recording_by_id(recording_id)
    if not rec or rec.get("media_kind") != "import":
        raise HTTPException(status_code=404, detail="Media not found")

    path_value = rec.get("file_path") if download else (rec.get("playable_path") or rec.get("file_path"))
    if not path_value:
        raise HTTPException(status_code=404, detail="Media file not found")

    file_path = Path(path_value)
    if file_path.suffix.lower() == ".ts":
        raise HTTPException(status_code=400, detail="TS files are not supported in Media")
    output_root = OUTPUT_DIR.resolve()
    try:
        resolved = file_path.resolve()
        if not resolved.is_relative_to(output_root):
            raise HTTPException(status_code=403, detail="Media path is not allowed")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid media path")

    return await _serve_video_file_with_ranges(request, file_path, file_path.name)


@app.api_route("/streams/library/{username}/{file_path:path}", methods=["GET", "HEAD"])
async def serve_library_media(request: Request, username: str, file_path: str, download: bool = False):
    """Sert un fichier média présent dans /records/<profil>/ avec validation de chemin."""
    media_path = _resolve_library_media_path(username, file_path)
    if not media_path.exists() or not media_path.is_file():
        raise HTTPException(status_code=404, detail="Media not found")

    kind = _media_library_kind(media_path)
    media_type = mimetypes.guess_type(media_path.name)[0] or (
        _recording_media_type(media_path.name) if kind == "video" else "application/octet-stream"
    )

    if download:
        return FileResponse(
            str(media_path),
            media_type=media_type,
            headers={
                "Content-Disposition": _content_disposition(media_path.name, "attachment"),
                "Cache-Control": "private, max-age=0",
            },
        )

    if kind == "video":
        return await _serve_video_file_with_ranges(request, media_path, media_path.name)

    return FileResponse(
        str(media_path),
        media_type=media_type,
        headers={
            "Content-Disposition": _content_disposition(media_path.name),
            "Cache-Control": "public, max-age=3600",
        },
    )

# Mount pour les sessions HLS live uniquement
app.mount("/streams/sessions", StaticFiles(directory=str(OUTPUT_DIR / "sessions")), name="streams_sessions")
app.mount("/streams/thumbnails", StaticFiles(directory=str(OUTPUT_DIR / "thumbnails")), name="streams_thumbnails")

manager = FFmpegManager(str(OUTPUT_DIR), ffmpeg_path=FFMPEG_PATH, hls_time=HLS_TIME, hls_list_size=HLS_LIST_SIZE)
browser_capture_manager = BrowserCaptureManager(str(OUTPUT_DIR))


@app.get("/streams/browser/{session_id}/{filename}")
async def serve_browser_live_stream(session_id: str, filename: str):
    session = browser_capture_manager.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session browser introuvable")
    if filename not in {"live.webm", "live.mp4"} or filename != f"live.{getattr(session, 'file_extension', 'webm')}":
        raise HTTPException(status_code=404, detail="Flux browser introuvable")
    q = session.subscribe()

    async def chunks():
        try:
            while session.is_running() or not q.empty():
                try:
                    chunk = await asyncio.to_thread(q.get, True, 2)
                except queue.Empty:
                    if not session.is_running():
                        break
                    continue
                yield chunk
        finally:
            session.unsubscribe(q)
            if not session.record:
                browser_capture_manager.stop_live_if_idle(session_id)

    return StreamingResponse(
        chunks(),
        media_type="video/mp4" if filename.endswith(".mp4") else "video/webm",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )

# Database SQLite
DB_FILE = OUTPUT_DIR / "streamrec.db"
db = Database(DB_FILE)
browser_capture_manager.session_store = ProviderSessionStore(db)
media_import_manager: Optional[MediaImportManager] = None

# Chaturbate API (initialized at startup)
chaturbate_api: Optional[ChaturbateAPI] = None
flaresolverr_client: Optional[FlareSolverrClient] = None

# Plugin manager (initialized at startup)
# Set at startup via setup_services
cam4_auth_service: Optional[CAM4AuthService] = None

SOURCE_TYPES = {"chaturbate", "cam4"}
provider_registry = create_provider_registry(db, output_dir=OUTPUT_DIR)
SOURCE_TYPES = provider_registry.source_types()
_HLS_PROXY_CACHE: dict[str, dict] = {}
_HLS_PROXY_REVERSE: dict[tuple, str] = {}
_HLS_PROXY_TTL_SECONDS = int(os.getenv("PSTREAMREC_HLS_PROXY_TTL_SECONDS", "900"))
_HLS_MOUFLON_PSCH_RE = re.compile(r"^#EXT-X-MOUFLON:PSCH:([^:]+):(.+)$", re.IGNORECASE)
_HLS_MOUFLON_URI_RE = re.compile(r"^#EXT-X-MOUFLON:URI:(https?://.+)$", re.IGNORECASE)


def _parse_auto_record_users_env(raw_value: Optional[str]) -> Tuple[list[str], int]:
    """Parse AUTO_RECORD_USERS, preserving order and dropping duplicates."""
    users: list[str] = []
    seen: set[str] = set()
    skipped = 0

    if not raw_value:
        return users, skipped

    for item in (raw_value or "").split(","):
        username = item.strip()
        if not username:
            skipped += 1
            continue
        dedupe_key = username.lower()
        if dedupe_key in seen:
            skipped += 1
            continue
        seen.add(dedupe_key)
        users.append(username)

    return users, skipped


async def _import_auto_record_users_from_env() -> dict[str, int]:
    """Import AUTO_RECORD_USERS into SQLite so the auto-record loop can see it."""
    users, skipped = _parse_auto_record_users_env(os.getenv("AUTO_RECORD_USERS", ""))
    imported = 0
    updated = 0
    created = 0

    for username in users:
        existing = await db.get_model(username)
        if existing:
            retention_days = existing.get("retention_days")
            await db.add_or_update_model(
                username=username,
                display_name=existing.get("display_name"),
                auto_record=True,
                record_quality=existing.get("record_quality") or "best",
                retention_days=retention_days if retention_days is not None else 30,
                source_type=existing.get("source_type") or "chaturbate",
            )
            updated += 1
        else:
            await db.add_or_update_model(
                username=username,
                display_name=username,
                auto_record=True,
                record_quality=await _get_default_record_quality(),
                retention_days=await _get_default_retention_days(),
                source_type="chaturbate",
            )
            created += 1
        imported += 1

    if imported or skipped:
        logger.info(
            "AUTO_RECORD_USERS imported",
            imported=imported,
            created=created,
            updated=updated,
            skipped=skipped,
        )

    return {
        "imported": imported,
        "created": created,
        "updated": updated,
        "skipped": skipped,
    }


_SOURCE_TYPE_ALIASES = {}


def _normalize_source_type(source_type: Optional[str]) -> Optional[str]:
    value = (source_type or "").strip().lower()
    if not value or value == "auto":
        return None
    return _SOURCE_TYPE_ALIASES.get(value, value)


def _available_source_types() -> set[str]:
    types = provider_registry.source_types()
    return types or set(SOURCE_TYPES)


def _source_type_from_url(target: str) -> Optional[str]:
    try:
        hostname = (urlparse(target).hostname or "").lower().rstrip(".")
    except Exception:
        return None
    if not hostname:
        return None
    for provider in provider_registry.all():
        for domain in provider.domains:
            clean = domain.lower().rstrip(".")
            if hostname == clean or hostname.endswith(f".{clean}"):
                return provider.source_type
    return None


async def _infer_source_type(
    username: Optional[str],
    model: Optional[dict] = None,
) -> str:
    """Pick the platform for a username, repairing old CAM4 rows when possible."""
    model_source = _normalize_source_type(model.get("source_type") if model else None)
    if model_source and model_source != "chaturbate":
        return model_source

    if username:
        try:
            followed = await db.get_followed_model(username)
            followed_source = _normalize_source_type(
                followed.get("source_type") if followed else None
            )
            if followed_source and (
                not model_source
                or (model_source == "chaturbate" and followed_source != "chaturbate")
            ):
                return followed_source
        except Exception:
            pass

    return model_source or "chaturbate"


def _provider_for(source_type: str):
    if provider_registry.has(source_type):
        return provider_registry.get(source_type)
    raise ValueError(f"source_type inconnu: {source_type}")


_BROWSER_CAPTURE_SOURCES = {"livejasmin", "xcams"}


def _supports_browser_capture(source_type: str) -> bool:
    return (source_type or "").strip().lower() in _BROWSER_CAPTURE_SOURCES


async def _ensure_browser_capture_session(source_type: str) -> None:
    return


def _provider_error_detail(source_type: str, target: str, exc: Exception) -> str:
    if isinstance(exc, ProviderInteractionRequired):
        return f"{source_type}/{target}: interaction manuelle requise (CAPTCHA/2FA/challenge)"
    if isinstance(exc, ProviderPrivateError):
        return f"{source_type}/{target}: flux prive ou payant non supporte"
    if isinstance(exc, ProviderAuthError):
        return f"{source_type}/{target}: connexion requise"
    if isinstance(exc, ProviderOfflineError):
        return f"{source_type}/{target}: modele hors ligne ou flux public introuvable"
    return f"{source_type}/{target}: {exc}"


async def _resolve_stream(source_type: str, target: str, max_height: Optional[int]) -> ResolvedStream:
    provider = _provider_for(source_type)
    if not getattr(provider.capabilities, "can_stream", True):
        raise ProviderError(
            f"{provider.display_name} est disponible en Discover uniquement: "
            "aucun flux public lisible par FFmpeg n'a ete valide."
        )
    stream = await provider.resolve_stream(target, max_height=max_height)
    if not stream.source_type:
        stream.source_type = source_type
    return stream


async def _resolve_m3u8(source_type: str, target: str, max_height: Optional[int]) -> Optional[str]:
    """Backward-compatible URL-only resolver."""
    stream = await _resolve_stream(source_type, target, max_height)
    return stream.url


def _prune_hls_proxy_cache() -> None:
    now = time.time()
    expired = [
        token for token, entry in _HLS_PROXY_CACHE.items()
        if entry.get("expires_at", 0) <= now
    ]
    for token in expired:
        entry = _HLS_PROXY_CACHE.pop(token, None)
        cache_key = entry.get("cache_key") if entry else None
        if cache_key and _HLS_PROXY_REVERSE.get(cache_key) == token:
            _HLS_PROXY_REVERSE.pop(cache_key, None)


def _hls_proxy_cache_key(
    url: str,
    headers: Optional[dict[str, str]],
    suffix: str,
) -> tuple:
    header_items = tuple(
        sorted((str(key), str(value)) for key, value in (headers or {}).items())
    )
    return (url, header_items, suffix)


def _hls_proxy_path_suffix(url: str) -> str:
    parsed = urlparse(url or "")
    path = parsed.path
    if re.search(r"(?:^|[&;])flags=segment(?:$|[&;])", parsed.query or "", re.IGNORECASE):
        return ".mp4"
    suffix = os.path.splitext(path)[1].lower()
    if suffix == ".pts":
        return ".ts"
    if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix or ""):
        return suffix
    return ""


def _url_with_missing_query_params(url: str, params: dict[str, str]) -> str:
    parsed = urlparse(url or "")
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    existing = {key for key, _ in pairs}
    for key, value in params.items():
        if key not in existing and value:
            pairs.append((key, value))
    return urlunparse(parsed._replace(query=urlencode(pairs)))


def _register_hls_proxy_url(
    url: str,
    headers: Optional[dict[str, str]] = None,
    suffix: Optional[str] = None,
) -> str:
    _prune_hls_proxy_cache()
    resolved_suffix = suffix if suffix is not None else _hls_proxy_path_suffix(url)
    cache_key = _hls_proxy_cache_key(url, headers, resolved_suffix)
    now = time.time()
    existing_token = _HLS_PROXY_REVERSE.get(cache_key)
    existing = _HLS_PROXY_CACHE.get(existing_token or "")
    if existing and existing.get("expires_at", 0) > now:
        existing["expires_at"] = now + max(60, _HLS_PROXY_TTL_SECONDS)
        return f"/api/proxy/hls/{existing_token}{resolved_suffix}"

    token = secrets.token_urlsafe(24)
    _HLS_PROXY_CACHE[token] = {
        "url": url,
        "headers": dict(headers or {}),
        "cache_key": cache_key,
        "created_at": now,
        "expires_at": now + max(60, _HLS_PROXY_TTL_SECONDS),
    }
    _HLS_PROXY_REVERSE[cache_key] = token
    return f"/api/proxy/hls/{token}{resolved_suffix}"


def _register_cached_hls_body(
    url: str,
    body: bytes,
    content_type: str = "",
    suffix: str = ".mp4",
) -> str:
    _prune_hls_proxy_cache()
    token = secrets.token_urlsafe(24)
    now = time.time()
    _HLS_PROXY_CACHE[token] = {
        "url": url,
        "headers": {},
        "body": body,
        "content_type": content_type,
        "created_at": now,
        "expires_at": now + max(60, _HLS_PROXY_TTL_SECONDS),
    }
    return f"/api/proxy/hls/{token}{suffix}"


def _hls_segment_header_variants(headers: Optional[dict[str, str]]) -> list[dict[str, str]]:
    primary = dict(headers or {})
    variants = [primary]
    without_cookie = {
        key: value
        for key, value in primary.items()
        if key.lower() != "cookie"
    }
    if without_cookie != primary:
        variants.append(without_cookie)
    return variants


def _single_segment_hls_info(text: str) -> Optional[tuple[str, str, str, str]]:
    target_duration = "1"
    version = "3"
    duration = "1.0"
    pending_duration: Optional[str] = None
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#EXT-X-TARGETDURATION:"):
            target_duration = stripped.split(":", 1)[1].strip() or target_duration
        elif stripped.startswith("#EXT-X-VERSION:"):
            version = stripped.split(":", 1)[1].strip() or version
        elif stripped.startswith("#EXTINF:"):
            pending_duration = stripped.split(":", 1)[1].split(",", 1)[0].strip() or duration
        elif not stripped.startswith("#"):
            return stripped, pending_duration or duration, target_duration, version
    return None


async def _rewrite_prefetched_single_segment_playlist(
    text: str,
    base_url: str,
    headers: Optional[dict[str, str]],
    entry: dict,
    session,
) -> str:
    segment_info = _single_segment_hls_info(text)
    if not segment_info:
        return _rewrite_hls_playlist(text, base_url, headers=headers, live_sequence=0)

    raw_uri, duration, target_duration, version = segment_info
    raw_url = urljoin(base_url, raw_uri)
    proxy_url = None
    for attempt_headers in _hls_segment_header_variants(headers):
        try:
            async with session.get(
                raw_url,
                headers=attempt_headers,
                allow_redirects=True,
                **aiohttp_request_kwargs(),
            ) as resp:
                body = await resp.read()
                if resp.status < 400 and body:
                    proxy_url = _register_cached_hls_body(
                        str(resp.url),
                        body,
                        resp.headers.get("Content-Type", "video/mp4"),
                    )
                    break
        except Exception:
            continue

    if not proxy_url:
        proxy_url = _register_hls_proxy_url(raw_url, headers=headers, suffix=".mp4")

    seq = int(entry.get("next_media_sequence") or 0)
    history = list(entry.get("live_segments") or [])
    history.append({"seq": seq, "duration": duration, "url": proxy_url})
    history = history[-6:]
    entry["live_segments"] = history
    entry["next_media_sequence"] = seq + 1

    lines = [
        "#EXTM3U",
        f"#EXT-X-TARGETDURATION:{target_duration}",
        f"#EXT-X-VERSION:{version}",
        f"#EXT-X-MEDIA-SEQUENCE:{history[0]['seq']}",
    ]
    for segment in history:
        lines.append(f"#EXTINF:{segment['duration']},")
        lines.append(segment["url"])
    return "\n".join(lines) + "\n"


def _rewrite_hls_playlist(
    text: str,
    base_url: str,
    headers: Optional[dict[str, str]] = None,
    live_sequence: Optional[int] = None,
) -> str:
    def proxy_url(raw_uri: str) -> str:
        return _register_hls_proxy_url(urljoin(base_url, raw_uri), headers=headers)

    def proxy_absolute_url(url: str) -> str:
        return _register_hls_proxy_url(url, headers=headers)

    media_sequence_written = False
    pending_program_date_time: Optional[str] = None
    pending_mouflon_uri: Optional[str] = None
    pending_variant = False
    skip_mouflon_full_segment = False
    variant_index = 0
    mouflon_keys: list[tuple[str, str]] = []
    has_mouflon_parts = "#EXT-X-MOUFLON:URI:" in (text or "") and "#EXT-X-PART:" in (text or "")
    rewritten = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            rewritten.append(line)
            continue
        if stripped.startswith("#"):
            upper = stripped.upper()
            psch_match = _HLS_MOUFLON_PSCH_RE.match(stripped)
            if psch_match:
                mouflon_keys.append((psch_match.group(1).strip(), psch_match.group(2).strip()))
                rewritten.append(line)
                continue
            uri_match = _HLS_MOUFLON_URI_RE.match(stripped)
            if uri_match:
                pending_mouflon_uri = uri_match.group(1).strip()
                continue
            # Hls.js can chase generic LL-HLS parts/preload hints faster than
            # a server-side proxy can refresh provider tokens. Stripchat's
            # Mouflon playlists are different: the full media.mp4 placeholders
            # 404, while the part URLs in the preceding Mouflon tags are valid.
            if (
                upper.startswith("#EXT-X-SERVER-CONTROL:")
                or upper.startswith("#EXT-X-PART-INF:")
                or upper.startswith("#EXT-X-RENDITION-REPORT:")
            ):
                continue
            if upper.startswith("#EXT-X-PART:"):
                if has_mouflon_parts and pending_mouflon_uri:
                    duration_match = re.search(r"DURATION=([0-9.]+)", stripped, re.IGNORECASE)
                    rewritten.append(f"#EXTINF:{duration_match.group(1) if duration_match else '0.5'},")
                    rewritten.append(proxy_absolute_url(pending_mouflon_uri))
                pending_mouflon_uri = None
                continue
            if upper.startswith("#EXT-X-PRELOAD-HINT:"):
                pending_mouflon_uri = None
                continue
            if upper.startswith("#EXT-X-MOUFLON:EXT-REF:"):
                continue
            if upper.startswith("#EXT-X-STREAM-INF:"):
                pending_variant = True
                rewritten.append(line)
                continue
            if upper.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                pending_program_date_time = line
                continue
            if upper.startswith("#EXTINF:"):
                if has_mouflon_parts:
                    skip_mouflon_full_segment = True
                    pending_program_date_time = None
                    continue
                if pending_program_date_time:
                    rewritten.append(pending_program_date_time)
                    pending_program_date_time = None
                rewritten.append(line)
                continue
            if live_sequence is not None:
                if upper.startswith("#EXT-X-PLAYLIST-TYPE:") or upper == "#EXT-X-ENDLIST":
                    continue
                if upper.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                    rewritten.append(f"#EXT-X-MEDIA-SEQUENCE:{live_sequence}")
                    media_sequence_written = True
                    continue
            if "URI=\"" in stripped:
                line = re.sub(
                    r'URI="([^"]+)"',
                    lambda match: f'URI="{proxy_url(match.group(1))}"',
                    line,
                )
            rewritten.append(line)
            continue
        raw_uri = pending_mouflon_uri or stripped
        pending_mouflon_uri = None
        if skip_mouflon_full_segment:
            skip_mouflon_full_segment = False
            continue
        absolute_url = urljoin(base_url, raw_uri)
        if pending_variant:
            query_keys = {key for key, _ in parse_qsl(urlparse(absolute_url).query, keep_blank_values=True)}
            if mouflon_keys and "pkey" not in query_keys:
                version, pkey = mouflon_keys[min(variant_index, len(mouflon_keys) - 1)]
                absolute_url = _url_with_missing_query_params(
                    absolute_url,
                    {"playlistType": "lowLatency", "psch": version, "pkey": pkey},
                )
            variant_index += 1
            pending_variant = False
        rewritten.append(proxy_absolute_url(absolute_url))
    if live_sequence is not None and not media_sequence_written:
        insert_at = 1 if rewritten and rewritten[0].strip() == "#EXTM3U" else 0
        rewritten.insert(insert_at, f"#EXT-X-MEDIA-SEQUENCE:{live_sequence}")
    return "\n".join(rewritten) + ("\n" if text.endswith("\n") else "")


def _proxied_stream_url(stream: ResolvedStream) -> str:
    lower = (stream.url or "").lower()
    if stream.hls_playlist_text and ".m3u8" in lower:
        # Chaturbate LL-HLS master tokens can reject an immediate second fetch.
        # Reuse the resolver-fetched master and proxy its rewritten variants.
        rewritten = _rewrite_hls_playlist(
            stream.hls_playlist_text,
            stream.hls_playlist_base_url or stream.url,
            headers=stream.headers,
        )
        return _register_cached_hls_body(
            stream.url,
            rewritten.encode("utf-8"),
            stream.hls_playlist_content_type or "application/vnd.apple.mpegurl",
            suffix=".m3u8",
        )
    if stream.source_type == "livejasmin":
        return _register_hls_proxy_url(stream.url, headers=stream.headers, suffix=".m3u8")
    if ".m3u8" in lower or ".mpd" in lower:
        return _register_hls_proxy_url(stream.url, headers=stream.headers)
    return stream.url


def _watch_stream_payload(stream: ResolvedStream) -> dict:
    return {"streamUrl": _proxied_stream_url(stream)}


def _local_proxy_url_for_ffmpeg(url: str) -> str:
    if (url or "").startswith("/api/proxy/hls/"):
        port = os.getenv("PORT", "8080")
        return f"http://127.0.0.1:{port}{url}"
    return url


def _ffmpeg_stream_input(stream: ResolvedStream) -> tuple[str, Optional[dict[str, str]], str]:
    proxied_url = _proxied_stream_url(stream)
    if proxied_url != stream.url:
        return _local_proxy_url_for_ffmpeg(proxied_url), None, stream.url
    return stream.url, stream.headers, stream.url


def _start_browser_capture(
    source_type: str,
    target: str,
    person: str,
    display_name: Optional[str] = None,
    record: bool = True,
    filename_format: str = FILENAME_FORMAT_TIMESTAMP,
    records_dir_for_person: Optional[Path] = None,
    target_username: Optional[str] = None,
    session_key: Optional[str] = None,
):
    provider = _provider_for(source_type)
    normalized_source = (source_type or "").strip().lower()
    if target.startswith("http://") or target.startswith("https://"):
        page_url = target
    elif normalized_source == "livejasmin":
        page_url = f"https://www.livejasmin.com/en/chat/{quote(target.strip(), safe='')}"
    else:
        page_url = provider.canonical_url(target)
    capture_mode = "websocket_mp4" if normalized_source == "livejasmin" else "media_recorder"
    return browser_capture_manager.start_session(
        source_type=source_type,
        page_url=page_url,
        person=person,
        display_name=display_name or target,
        record=record,
        capture_mode=capture_mode,
        filename_format=filename_format,
        records_dir_for_person=records_dir_for_person,
        target=target_username or target,
        session_key=session_key,
    )


def _all_recording_statuses() -> list[dict]:
    return manager.list_status() + browser_capture_manager.list_status(recording_only=True)


async def _index_browser_capture_recording(session) -> None:
    if not getattr(session, "record", False):
        return
    record_path = Path(session.record_path)
    if not record_path.exists() or not record_path.is_file():
        return
    try:
        from app.tasks.monitor import generate_recording_thumbnail, get_media_created_at, get_video_duration

        await _remux_browser_recording(record_path)
        file_size = record_path.stat().st_size
        duration_seconds = await get_video_duration(record_path, FFMPEG_PATH)
        if not duration_seconds:
            duration_seconds = max(0, int(time.time() - getattr(session, "start_time", time.time())))
        if duration_seconds < MIN_RECORDING_SECONDS and file_size < MIN_RECORDING_BYTES:
            return
        fallback_created_at = int(getattr(session, "start_time", None) or record_path.stat().st_mtime)
        thumbnail_path = await generate_recording_thumbnail(
            record_path,
            OUTPUT_DIR,
            session.person,
            FFMPEG_PATH,
        )
        await db.add_or_update_recording(
            username=session.person,
            filename=record_path.name,
            file_path=str(record_path),
            file_size=file_size,
            recording_id=f"{session.person}_{record_path.stem}",
            duration_seconds=duration_seconds,
            thumbnail_path=thumbnail_path,
            created_at=await get_media_created_at(
                record_path,
                FFMPEG_PATH,
                fallback_timestamp=fallback_created_at,
            ),
        )
    except Exception as exc:
        logger.warning(
            "Indexation browser recording échouée",
            session_id=session.id,
            person=session.person,
            error=str(exc),
        )


async def _index_ffmpeg_recording(session) -> None:
    paths = []
    try:
        paths = [Path(path) for path in session._recording_paths_for_cleanup()]
    except Exception:
        record_path = getattr(session, "record_path", None)
        if record_path:
            paths = [Path(record_path)]

    if not paths:
        return

    try:
        from app.tasks.monitor import generate_recording_thumbnail, get_media_created_at, get_video_duration

        for record_path in paths:
            if not record_path.exists() or not record_path.is_file():
                continue
            file_size = record_path.stat().st_size
            duration_seconds = await get_video_duration(record_path, FFMPEG_PATH)
            if not duration_seconds:
                duration_seconds = max(0, int(time.time() - getattr(session, "start_time", time.time())))
            if duration_seconds < MIN_RECORDING_SECONDS and file_size < MIN_RECORDING_BYTES:
                logger.info(
                    "Recording trop court, indexation ignorée",
                    session_id=session.id,
                    person=session.person,
                    file=record_path.name,
                    duration_seconds=duration_seconds,
                    file_size=file_size,
                )
                continue
            fallback_created_at = int(getattr(session, "start_time", None) or record_path.stat().st_mtime)
            thumbnail_path = await generate_recording_thumbnail(
                record_path,
                OUTPUT_DIR,
                session.person,
                FFMPEG_PATH,
            )
            await db.add_or_update_recording(
                username=session.person,
                filename=record_path.name,
                file_path=str(record_path),
                file_size=file_size,
                recording_id=f"{session.person}_{record_path.stem}",
                duration_seconds=duration_seconds,
                thumbnail_path=thumbnail_path,
                is_converted=False,
                created_at=await get_media_created_at(
                    record_path,
                    FFMPEG_PATH,
                    fallback_timestamp=fallback_created_at,
                ),
            )
            logger.info(
                "Recording FFmpeg indexée",
                session_id=session.id,
                person=session.person,
                file=record_path.name,
                duration_seconds=duration_seconds,
                file_size=file_size,
            )
    except Exception as exc:
        logger.warning(
            "Indexation recording FFmpeg échouée",
            session_id=getattr(session, "id", None),
            person=getattr(session, "person", None),
            error=str(exc),
        )


async def _remux_browser_recording(record_path: Path) -> bool:
    if record_path.suffix.lower() not in {".webm", ".mp4"}:
        return False
    tmp_path = record_path.with_name(f"{record_path.stem}.remuxing{record_path.suffix}")
    try:
        if tmp_path.exists():
            tmp_path.unlink()
        proc = await asyncio.create_subprocess_exec(
            FFMPEG_PATH,
            "-y",
            "-v",
            "error",
            "-i",
            str(record_path),
            "-c",
            "copy",
            str(tmp_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            logger.warning(
                "Remux browser recording échoué",
                path=str(record_path),
                error=(stderr or stdout or b"").decode("utf-8", "replace")[:500],
            )
            return False
        if not tmp_path.exists() or tmp_path.stat().st_size <= 0:
            return False
        os.replace(tmp_path, record_path)
        return True
    except Exception as exc:
        logger.warning(
            "Remux browser recording impossible",
            path=str(record_path),
            error=str(exc),
        )
        return False
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass


async def _provider_status(source_type: str, username: str) -> ProviderStatus:
    provider = _provider_for(source_type)
    status = await provider.check_status(username)
    if not status.source_type:
        status.source_type = source_type
    return status


# Fichier de sauvegarde des modèles (côté serveur)
MODELS_FILE = OUTPUT_DIR / "models.json"

def load_models():
    """Charge la liste des modèles depuis le fichier JSON"""
    if MODELS_FILE.exists():
        try:
            with open(MODELS_FILE, 'r') as f:
                return json.load(f)
        except:
            return []
    return []

def save_models_to_file(models):
    """Sauvegarde la liste des modèles dans le fichier JSON"""
    try:
        with open(MODELS_FILE, 'w') as f:
            json.dump(models, f, indent=2)
        return True
    except Exception as e:
        logger.error("Erreur sauvegarde modèles", exc_info=True, error=str(e))
        return False


class StartBody(BaseModel):
    target: str  # Either an m3u8 URL or a username (if resolver enabled)
    source_type: Optional[str] = None  # "m3u8", provider source_type, or None/"auto"
    name: Optional[str] = None  # display name
    person: Optional[str] = None  # recording bucket (per person)
    auto_start: Optional[bool] = False  # True si démarrage automatique
    record_quality: Optional[str] = None  # best, 1080p, 720p, 480p, 360p
    recordQuality: Optional[str] = None  # camelCase frontend alias
    session_key: Optional[str] = None
    sessionKey: Optional[str] = None


class ProviderLoginBody(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None


class ProviderSessionBody(BaseModel):
    username: Optional[str] = None
    cookies: Optional[Any] = None
    cookieHeader: Optional[str] = None
    localStorage: Optional[Any] = None
    origins: Optional[Any] = None
    storageState: Optional[dict[str, Any]] = None
    userAgent: Optional[str] = None
    user_agent: Optional[str] = None
    xBc: Optional[str] = None
    xbc: Optional[str] = None


class ProviderEnabledBody(BaseModel):
    enabled: bool


class ModelVolumeBody(BaseModel):
    volume: float


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9_-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "session"


def _records_root() -> Path:
    return (OUTPUT_DIR / "records").resolve()


def _default_record_path(username: str) -> str:
    return f"{_clean_record_path_part(username)}/videos/record"


def _legacy_record_path(username: str) -> str:
    return _clean_record_path_part(username)


def _clean_record_path_part(part: object) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(part or "").strip())
    cleaned = cleaned.strip(".-")
    if not cleaned or cleaned in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid record path")
    return cleaned


def _normalize_record_path(value: object, username: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raw = _default_record_path(username)

    raw = raw.replace("\\", "/")
    root = _records_root()

    if raw.startswith("/"):
        try:
            absolute = Path(raw).resolve()
            relative = absolute.relative_to(root)
        except ValueError:
            raise HTTPException(status_code=400, detail="Record path must stay under /data/records")
        parts = relative.parts
    else:
        raw = raw.strip("/")
        if raw == "records":
            raw = ""
        elif raw.startswith("records/"):
            raw = raw[len("records/"):]
        parts = Path(raw).parts

    if any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(status_code=400, detail="Invalid record path")

    cleaned_parts = [_clean_record_path_part(part) for part in parts]
    if not cleaned_parts:
        raise HTTPException(status_code=400, detail="Invalid record path")
    model_folder = _clean_record_path_part(username)
    if cleaned_parts[0].lower() != model_folder.lower():
        raise HTTPException(status_code=400, detail="Record path must stay inside this model folder")
    cleaned_parts[0] = model_folder

    relative_path = Path(*cleaned_parts)
    absolute_path = (root / relative_path).resolve()
    if not absolute_path.is_relative_to(root):
        raise HTTPException(status_code=400, detail="Record path must stay under /data/records")
    return relative_path.as_posix()


def _record_path_from_model(model: Optional[dict], username: str) -> str:
    try:
        if model is not None and not (model.get("record_path") or "").strip():
            return _legacy_record_path(username)
        return _normalize_record_path((model or {}).get("record_path"), username)
    except HTTPException:
        logger.warning(
            "Chemin recording modèle invalide, fallback défaut",
            username=username,
            record_path=(model or {}).get("record_path"),
        )
        return _default_record_path(username)


def _record_dir_from_path(record_path: str) -> Path:
    return (_records_root() / record_path).resolve()


def _model_record_dir(model: Optional[dict], username: str) -> Path:
    return _record_dir_from_path(_record_path_from_model(model, username))


def _record_path_fields(username: str, model: Optional[dict] = None) -> dict:
    record_path = _record_path_from_model(model, username)
    return {
        "recordPath": record_path,
        "record_path": record_path,
        "recordPathDefault": _default_record_path(username),
        "recordPathLegacy": _legacy_record_path(username),
        "recordPathDisplay": str(_record_dir_from_path(record_path)),
    }


def _record_dirs_for_model(username: str, model: Optional[dict] = None) -> list[Path]:
    candidates = [
        _model_record_dir(model, username),
        _record_dir_from_path(_default_record_path(username)),
        _records_root() / username,
    ]
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve())
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


@app.get("/")
async def index():
    """Root now serves Discover page"""
    return FileResponse(str(STATIC_DIR / "discover.html"))


@app.get("/discover")
async def discover_page():
    """Discover page - browse live models"""
    return FileResponse(str(STATIC_DIR / "discover.html"))


@app.get("/following")
async def following_page():
    """Following page - tracked models from Chaturbate"""
    return FileResponse(str(STATIC_DIR / "following.html"))


@app.get("/recordings")
async def recordings_page():
    """Legacy recordings page; Media is now the library and profile surface."""
    return RedirectResponse(url="/media", status_code=307)


@app.get("/media")
async def media_page():
    """Media page - file library for recordings folders"""
    return FileResponse(str(STATIC_DIR / "media.html"))


@app.get("/settings")
async def settings_page():
    """Settings page"""
    return FileResponse(str(STATIC_DIR / "settings.html"))


@app.get("/wiki")
async def wiki_page():
    """Wiki page - internal documentation"""
    return FileResponse(str(STATIC_DIR / "wiki.html"))


@app.get("/watch/{username}")
async def watch_page(username: str):
    """Watch page - view live stream or recording for a model"""
    return FileResponse(str(STATIC_DIR / "watch.html"))


@app.get("/login")
async def login_page():
    """Page de connexion"""
    return FileResponse(str(STATIC_DIR / "login.html"))


class LoginBody(BaseModel):
    password: str


@app.post("/api/login")
async def api_login(body: LoginBody, response: Response):
    """Endpoint de connexion"""
    if not PASSWORD:
        raise HTTPException(status_code=400, detail="Authentification non configurée")
    
    if not verify_password(body.password):
        logger.warning("Tentative de connexion échouée")
        raise HTTPException(status_code=401, detail="Mot de passe incorrect")
    
    # Créer une session
    session_token = generate_session_token()
    active_sessions.add(session_token)
    
    # Définir le cookie de session
    response.set_cookie(
        key="session_token",
        value=session_token,
        httponly=True,
        max_age=86400 * 30,  # 30 jours
        samesite="lax"
    )
    
    logger.info("Connexion réussie")
    return {"success": True, "message": "Connecté"}


@app.post("/api/logout")
async def api_logout(response: Response, session_token: Optional[str] = Cookie(None)):
    """Endpoint de déconnexion"""
    if session_token and session_token in active_sessions:
        active_sessions.remove(session_token)
    
    response.delete_cookie(key="session_token")
    logger.info("Déconnexion")
    return {"success": True, "message": "Déconnecté"}


@app.get("/favicon.ico")
async def favicon():
    """Retourne un favicon SVG simple"""
    from fastapi.responses import Response
    svg_favicon = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
        <circle cx="50" cy="50" r="45" fill="#6366f1"/>
        <circle cx="50" cy="35" r="8" fill="white"/>
        <rect x="35" y="45" width="30" height="35" rx="5" fill="white"/>
        <rect x="42" y="52" width="16" height="20" fill="#6366f1"/>
    </svg>'''
    return Response(content=svg_favicon, media_type="image/svg+xml")


@app.get("/api/version")
async def get_version():
    """Retourne les informations de version et configuration"""
    version = os.environ.get("APP_VERSION", "dev")
    check_interval = await get_check_interval_seconds(db)
    return {
        "version": version,
        "output_dir": str(OUTPUT_DIR),
        "ffmpeg_path": FFMPEG_PATH,
        "check_interval": check_interval,
        "check_interval_seconds": check_interval,
    }


# ============================================
# Logs Endpoints
# ============================================

@app.get("/api/logs")
async def get_logs(level: Optional[str] = None, limit: int = 200, offset: int = 0):
    """Retourne les logs de l'application depuis la mémoire"""
    logs = logger.memory_handler.get_logs(level=level, limit=limit, offset=offset)
    total = logger.memory_handler.get_total(level=level)
    return {"logs": logs, "total": total}


# ============================================
# GitOps Endpoints
# ============================================

@app.get("/api/git/status")
async def git_status():
    """Vérifie s'il y a des mises à jour disponibles depuis Git"""
    try:
        # Vérifier si on est dans un repo Git
        is_git = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=BASE_DIR,
            capture_output=True,
            text=True
        ).returncode == 0
        
        if not is_git:
            return {
                "isGitRepo": False,
                "message": "Not a Git repository"
            }
        
        # Récupérer le commit actuel
        current_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=BASE_DIR,
            capture_output=True,
            text=True
        ).stdout.strip()
        
        # Récupérer la branche actuelle
        current_branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=BASE_DIR,
            capture_output=True,
            text=True
        ).stdout.strip()
        
        # Fetch pour vérifier les updates
        subprocess.run(
            ["git", "fetch"],
            cwd=BASE_DIR,
            capture_output=True
        )
        
        # Vérifier s'il y a des commits en avance sur origin
        remote_commit = subprocess.run(
            ["git", "rev-parse", f"origin/{current_branch}"],
            cwd=BASE_DIR,
            capture_output=True,
            text=True
        ).stdout.strip()
        
        has_updates = current_commit != remote_commit
        
        # Compter les commits en retard
        if has_updates:
            behind_count = subprocess.run(
                ["git", "rev-list", "--count", f"HEAD..origin/{current_branch}"],
                cwd=BASE_DIR,
                capture_output=True,
                text=True
            ).stdout.strip()
        else:
            behind_count = "0"
        
        return {
            "isGitRepo": True,
            "currentBranch": current_branch,
            "currentCommit": current_commit[:8],
            "remoteCommit": remote_commit[:8],
            "hasUpdates": has_updates,
            "behindBy": int(behind_count),
            "canUpdate": has_updates
        }
        
    except Exception as e:
        return {
            "isGitRepo": False,
            "error": str(e)
        }


@app.post("/api/git/update")
async def git_update():
    """Effectue un git pull et redémarre l'application"""
    try:
        # Vérifier si on est dans un repo Git
        is_git = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=BASE_DIR,
            capture_output=True,
            text=True
        ).returncode == 0
        
        if not is_git:
            raise HTTPException(status_code=400, detail="Not a Git repository")
        
        # Sauvegarder le commit actuel
        old_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=BASE_DIR,
            capture_output=True,
            text=True
        ).stdout.strip()
        
        # Git pull
        pull_result = subprocess.run(
            ["git", "pull"],
            cwd=BASE_DIR,
            capture_output=True,
            text=True
        )
        
        if pull_result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"Git pull failed: {pull_result.stderr}"
            )
        
        # Nouveau commit
        new_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=BASE_DIR,
            capture_output=True,
            text=True
        ).stdout.strip()
        
        updated = old_commit != new_commit
        
        # Si des changements ont été appliqués, redémarrer
        if updated:
            # Planifier le redémarrage dans 2 secondes
            asyncio.create_task(restart_application())
            
            return {
                "success": True,
                "updated": True,
                "oldCommit": old_commit[:8],
                "newCommit": new_commit[:8],
                "message": "Update applied. Application will restart in 2 seconds...",
                "output": pull_result.stdout
            }
        else:
            return {
                "success": True,
                "updated": False,
                "commit": new_commit[:8],
                "message": "Already up to date",
                "output": pull_result.stdout
            }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def restart_application():
    """Redémarre l'application après un délai"""
    await asyncio.sleep(2)
    logger.info("Redémarrage application", task="restart")

    # Dev: uvicorn --reload reagit au touch du module principal.
    if "--reload" in sys.argv:
        try:
            Path(__file__).touch()
            return
        except Exception:
            pass

    # Prod (Docker): on quitte proprement. Le container manager relance
    # l'app via sa restart policy (unless-stopped).
    os._exit(0)


@app.get("/model.html")
async def model_page():
    return FileResponse(str(STATIC_DIR / "model.html"))


@app.api_route("/vendor/{asset_name}", methods=["GET", "HEAD"])
async def amazon_ivs_player_asset(asset_name: str, request: Request):
    media_type = IVS_PLAYER_ASSETS.get(asset_name)
    if not media_type:
        raise HTTPException(status_code=404, detail="Unknown vendor asset")

    now = time.time()
    cached = _IVS_PLAYER_ASSET_CACHE.get(asset_name) or {}
    cached_body = cached.get("body")
    cached_at = float(cached.get("cached_at") or 0)
    cache_headers = {"Cache-Control": "public, max-age=86400"}
    if isinstance(cached_body, bytes) and now - cached_at < 86400:
        if request.method == "HEAD":
            return Response(status_code=200, media_type=media_type, headers=cache_headers)
        return Response(content=cached_body, media_type=media_type, headers=cache_headers)

    try:
        async with aiohttp_client_session(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.get(
                f"{IVS_PLAYER_ASSET_BASE_URL}/{asset_name}",
                headers={"User-Agent": "Mozilla/5.0"},
                allow_redirects=True,
                **aiohttp_request_kwargs(),
            ) as resp:
                if resp.status >= 400:
                    raise HTTPException(status_code=502, detail="IVS player asset unavailable")
                body = await resp.read()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Erreur chargement asset IVS", asset=asset_name, error=str(exc), exc_info=True)
        raise HTTPException(status_code=502, detail="IVS player asset unavailable")

    _IVS_PLAYER_ASSET_CACHE[asset_name] = {"body": body, "cached_at": now}
    if request.method == "HEAD":
        return Response(status_code=200, media_type=media_type, headers=cache_headers)
    return Response(content=body, media_type=media_type, headers=cache_headers)


@app.api_route("/api/proxy/hls/{token_path:path}", methods=["GET", "HEAD"])
async def hls_proxy(token_path: str, request: Request):
    token = (token_path or "").split("/", 1)[0].split(".", 1)[0]
    entry = _HLS_PROXY_CACHE.get(token)
    if not entry or entry.get("expires_at", 0) <= time.time():
        _HLS_PROXY_CACHE.pop(token, None)
        raise HTTPException(status_code=404, detail="Flux proxy expire")

    url = entry["url"]
    headers = dict(entry.get("headers") or {})
    entry["expires_at"] = time.time() + max(60, _HLS_PROXY_TTL_SECONDS)
    if "body" in entry:
        content_type = entry.get("content_type") or "application/octet-stream"
        if request.method == "HEAD":
            return Response(status_code=200, media_type=content_type)
        return Response(
            content=entry.get("body") or b"",
            media_type=content_type,
            headers={"Cache-Control": "no-store"},
        )
    try:
        async with aiohttp_client_session(timeout=aiohttp.ClientTimeout(total=30)) as session:
            last_status = None
            for attempt_headers in _hls_segment_header_variants(headers):
                async with session.get(
                    url,
                    headers=attempt_headers,
                    allow_redirects=True,
                    **aiohttp_request_kwargs(),
                ) as resp:
                    last_status = resp.status
                    if resp.status in {401, 403} and "Cookie" in attempt_headers:
                        logger.debug(
                            "Proxy HLS upstream rejected cookies, retrying without them",
                            url=url,
                            status=resp.status,
                        )
                        continue
                    if resp.status >= 400:
                        raise HTTPException(status_code=resp.status, detail=f"Provider HTTP {resp.status}")
                    content_type = resp.headers.get("Content-Type", "")
                    if request.method == "HEAD":
                        return Response(status_code=200, media_type=content_type or None)
                    body = await resp.read()
                    lower_url = str(resp.url).lower()
                    is_playlist = (
                        ".m3u8" in lower_url
                        or "mpegurl" in content_type.lower()
                        or body.startswith(b"#EXTM3U")
                    )
                    if is_playlist:
                        text = body.decode("utf-8", errors="replace")
                        if "flags=segment" in text and "#EXT-X-ENDLIST" in text:
                            rewritten = await _rewrite_prefetched_single_segment_playlist(
                                text,
                                str(resp.url),
                                headers=attempt_headers,
                                entry=entry,
                                session=session,
                            )
                        else:
                            rewritten = _rewrite_hls_playlist(text, str(resp.url), headers=attempt_headers)
                        return Response(
                            content=rewritten,
                            media_type="application/vnd.apple.mpegurl",
                            headers={"Cache-Control": "no-store"},
                        )
                    return Response(
                        content=body,
                        media_type=content_type or "application/octet-stream",
                        headers={"Cache-Control": "no-store"},
                    )
            raise HTTPException(status_code=last_status or 502, detail=f"Provider HTTP {last_status or 502}")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Erreur proxy HLS", url=url, error=str(exc), exc_info=True)
        raise HTTPException(status_code=502, detail="Erreur proxy HLS")


async def _provider_login_state(source_type: str) -> dict:
    try:
        provider = _provider_for(source_type)
    except Exception:
        return {"isLoggedIn": False, "username": None, "lastError": "Unknown provider"}

    if not getattr(getattr(provider, "capabilities", None), "can_login", False):
        return {
            "isLoggedIn": False,
            "username": None,
            "lastError": None,
            "lastLoginAt": None,
            "hasCookies": False,
            "hasLocalStorage": False,
            "hasSession": False,
            "hasSavedSessionData": False,
            "hasSavedCredentials": False,
            "accountDisabled": True,
        }

    auth = getattr(provider, "auth", None)
    if auth is not None:
        try:
            status = auth.get_status()
            row = await db.get_provider_session(source_type)
            has_saved_credentials = bool(row and row.get("credential_username") and row.get("credential_password"))
            has_local_storage = _stored_json_has_items(row.get("local_storage") if row else None)
            has_saved_session_data = bool(status.get("hasCookies") or has_local_storage)
            is_logged_in = bool(status.get("isLoggedIn"))
            status["hasSavedCredentials"] = has_saved_credentials
            status["credentialsUpdatedAt"] = row.get("credentials_updated_at") if has_saved_credentials else None
            status["hasLocalStorage"] = has_local_storage
            status["hasSavedSessionData"] = has_saved_session_data
            status["hasSession"] = bool(is_logged_in and (status.get("hasCookies") or has_local_storage or is_logged_in))
            if not is_logged_in:
                status["lastLoginAt"] = None
            if not status.get("username") and row:
                status["username"] = row.get("credential_username")
            return status
        except Exception:
            pass

    row = await db.get_provider_session(source_type)
    if not row:
        return {
            "isLoggedIn": False,
            "username": None,
            "lastError": None,
            "hasCookies": False,
            "hasLocalStorage": False,
            "hasSession": False,
            "hasSavedSessionData": False,
            "hasSavedCredentials": False,
        }
    has_saved_credentials = bool(row.get("credential_username") and row.get("credential_password"))
    has_cookies = _stored_json_has_items(row.get("session_cookies"))
    has_local_storage = _stored_json_has_items(row.get("local_storage"))
    has_saved_session_data = bool(has_cookies or has_local_storage)
    is_logged_in = bool(row.get("is_logged_in")) and bool(has_cookies or has_local_storage)
    return {
        "isLoggedIn": is_logged_in,
        "username": row.get("username") or row.get("credential_username"),
        "lastError": row.get("last_error"),
        "lastLoginAt": row.get("last_login_at") if is_logged_in else None,
        "hasCookies": has_cookies,
        "hasLocalStorage": has_local_storage,
        "hasSession": is_logged_in,
        "hasSavedSessionData": has_saved_session_data,
        "hasSavedCredentials": has_saved_credentials,
        "credentialsUpdatedAt": row.get("credentials_updated_at") if has_saved_credentials else None,
    }


def _stored_json_has_items(raw_value: object) -> bool:
    if not raw_value:
        return False
    try:
        parsed = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
    except Exception:
        return False
    if isinstance(parsed, list):
        return any(bool(item) for item in parsed)
    if isinstance(parsed, dict):
        return bool(parsed)
    return False

async def _saved_provider_credentials(source_type: str) -> Optional[tuple[str, str]]:
    row = await db.get_provider_session(source_type)
    if not row:
        return None
    username = (row.get("credential_username") or "").strip()
    password = row.get("credential_password") or ""
    if not username or not password:
        return None
    return username, password


async def _login_with_saved_provider_credentials(source_type: str) -> bool:
    credentials = await _saved_provider_credentials(source_type)
    if not credentials:
        return False
    username, password = credentials
    result = await _provider_for(source_type).login(username, password)
    return bool(result.get("success"))


async def _ensure_saved_provider_login(provider, source_type: str) -> None:
    try:
        logged_in = await _login_with_saved_provider_credentials(source_type)
    except ProviderInteractionRequired:
        raise HTTPException(
            status_code=409,
            detail=f"{provider.display_name}: session navigateur verifiee requise",
        )
    except ProviderAuthError:
        raise HTTPException(status_code=401, detail=f"{provider.display_name}: connexion requise")
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not logged_in:
        raise HTTPException(status_code=401, detail=f"{provider.display_name}: connexion requise")


async def _disabled_provider_sources() -> set[str]:
    try:
        return set(await db.get_disabled_providers())
    except Exception:
        return set()


@app.get("/api/providers")
async def list_providers():
    disabled_sources = await _disabled_provider_sources()
    providers = []
    for meta in provider_registry.metadata():
        source_type = meta["sourceType"]
        meta["enabled"] = source_type not in disabled_sources
        meta["status"] = await _provider_login_state(source_type)
        providers.append(meta)
    return {"providers": providers, "sourceTypes": sorted(_available_source_types())}


@app.put("/api/providers/{source_type}/enabled")
async def provider_set_enabled(source_type: str, body: ProviderEnabledBody):
    source_type = _normalize_source_type(source_type) or ""
    if source_type not in _available_source_types():
        raise HTTPException(status_code=404, detail=f"Source '{source_type}' non disponible")

    disabled_sources = await _disabled_provider_sources()
    if body.enabled:
        disabled_sources.discard(source_type)
    else:
        disabled_sources.add(source_type)
    await db.set_disabled_providers(sorted(disabled_sources))
    return {
        "sourceType": source_type,
        "enabled": source_type not in disabled_sources,
        "disabledProviders": sorted(disabled_sources),
    }


@app.get("/api/providers/{source_type}/status")
async def provider_status(source_type: str):
    source_type = _normalize_source_type(source_type) or ""
    if source_type not in _available_source_types():
        raise HTTPException(status_code=404, detail=f"Source '{source_type}' non disponible")
    provider = _provider_for(source_type)
    return {
        **provider.metadata(),
        **await _provider_login_state(source_type),
    }


@app.post("/api/providers/{source_type}/login")
async def provider_login(source_type: str, body: ProviderLoginBody):
    source_type = _normalize_source_type(source_type) or ""
    if source_type not in _available_source_types():
        raise HTTPException(status_code=404, detail=f"Source '{source_type}' non disponible")
    provider = _provider_for(source_type)
    if not getattr(provider.capabilities, "can_login", False):
        raise HTTPException(status_code=400, detail=f"{provider.display_name}: account connection unsupported")
    username = (body.username or "").strip()
    password = body.password or ""
    saved_credentials = False
    if password:
        if not username:
            raise HTTPException(status_code=400, detail="Username is required")
        await db.save_provider_credentials(source_type, username, password)
        saved_credentials = True
    else:
        credentials = await _saved_provider_credentials(source_type)
        if not credentials:
            raise HTTPException(status_code=400, detail="Username and password are required")
        username, password = credentials
        saved_credentials = True
    try:
        result = await provider.login(username, password)
    except ProviderInteractionRequired as exc:
        return JSONResponse(
            status_code=409,
            content={"success": False, "savedCredentials": saved_credentials, "detail": str(exc)},
        )
    except ProviderError as exc:
        return JSONResponse(
            status_code=400,
            content={"success": False, "savedCredentials": saved_credentials, "detail": str(exc)},
        )
    if not result.get("success"):
        return JSONResponse(
            status_code=401,
            content={
                "success": False,
                "savedCredentials": saved_credentials,
                "detail": result.get("error", "Login failed"),
            },
        )
    result["savedCredentials"] = saved_credentials
    return result


def _provider_session_payload(body: ProviderSessionBody) -> tuple[Optional[list[dict[str, Any]]], list[dict[str, Any]]]:
    storage_state = body.storageState if isinstance(body.storageState, dict) else {}
    cookies = body.cookies if body.cookies is not None else storage_state.get("cookies")
    local_storage = (
        body.localStorage
        if body.localStorage is not None
        else body.origins
        if body.origins is not None
        else storage_state.get("origins")
    )

    if cookies is not None and not isinstance(cookies, list):
        raise HTTPException(status_code=400, detail="Session cookies must be a JSON array or cookie header")
    normalized_cookies = [
        cookie
        for cookie in (cookies or [])
        if isinstance(cookie, dict) and cookie.get("name") and cookie.get("value") is not None
    ]
    if local_storage is None:
        local_storage = []
    if not isinstance(local_storage, list):
        raise HTTPException(status_code=400, detail="localStorage/origins must be a JSON array")
    normalized_storage = [entry for entry in local_storage if isinstance(entry, dict)]
    return normalized_cookies or None, normalized_storage


@app.post("/api/providers/{source_type}/session")
async def provider_import_session(source_type: str, body: ProviderSessionBody):
    source_type = _normalize_source_type(source_type) or ""
    if source_type not in _available_source_types():
        raise HTTPException(status_code=404, detail=f"Source '{source_type}' non disponible")
    provider = _provider_for(source_type)
    if not getattr(provider.capabilities, "can_login", False):
        raise HTTPException(status_code=400, detail=f"{provider.display_name}: session import unsupported")
    cookies, local_storage = _provider_session_payload(body)
    cookie_header = (body.cookieHeader or "").strip()
    if not cookies and not cookie_header and not local_storage:
        raise HTTPException(status_code=400, detail="No valid session data provided")
    username = (body.username or "").strip() or None
    try:
        result = await provider.import_session(
            username=username,
            cookie_header=cookie_header or None,
            cookies=cookies,
            local_storage=local_storage,
            user_agent=(body.userAgent or body.user_agent or None),
            x_bc=(body.xBc or body.xbc or None),
        )
    except ProviderError as exc:
        return JSONResponse(status_code=400, content={"success": False, "detail": str(exc)})
    if not result.get("success"):
        return JSONResponse(
            status_code=401,
            content={"success": False, "detail": result.get("error", "Session import failed")},
        )
    return result


@app.post("/api/providers/{source_type}/logout")
async def provider_logout(source_type: str):
    source_type = _normalize_source_type(source_type) or ""
    if source_type not in _available_source_types():
        raise HTTPException(status_code=404, detail=f"Source '{source_type}' non disponible")
    result = await _provider_for(source_type).logout()
    return result


@app.post("/api/providers/{source_type}/following/sync")
async def provider_sync_following(source_type: str):
    source_type = _normalize_source_type(source_type) or ""
    if source_type not in _available_source_types():
        raise HTTPException(status_code=404, detail=f"Source '{source_type}' non disponible")
    provider = _provider_for(source_type)
    if not getattr(provider.capabilities, "can_sync_following", False):
        raise HTTPException(
            status_code=400,
            detail=f"{provider.display_name}: remote following sync unsupported; local follows only",
        )
    try:
        try:
            items = await asyncio.wait_for(provider.sync_following(), timeout=60)
        except ProviderAuthError:
            await _ensure_saved_provider_login(provider, source_type)
            items = await asyncio.wait_for(provider.sync_following(), timeout=60)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail=f"{provider.display_name}: following sync timeout")
    except ProviderInteractionRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ProviderAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    stored = await store_provider_following(db, source_type, items)
    repaired_sources = await db.reconcile_model_sources_from_followed()
    return {
        "synced": stored["synced"],
        "sourceType": source_type,
        "trusted": stored["trusted"],
        "skippedReason": stored["skippedReason"],
        "repairedSources": repaired_sources,
        "message": (
            f"{provider.display_name}: {stored['synced']} follows synced"
            if stored["trusted"]
            else f"{provider.display_name}: following sync skipped"
        ),
    }


@app.get("/api/providers/{source_type}/is-following/{username}")
async def provider_is_following(source_type: str, username: str):
    source_type = _normalize_source_type(source_type) or ""
    if source_type not in _available_source_types():
        raise HTTPException(status_code=404, detail=f"Source '{source_type}' non disponible")
    provider = _provider_for(source_type)
    local = await db.get_followed_model(username, source_type=source_type)
    local_match = bool(local and (local.get("source_type") or "chaturbate") == source_type)
    remote_match = False
    remote_checked = False
    if getattr(provider.capabilities, "can_sync_following", False):
        try:
            remote_match = bool(await provider.is_following(username))
            remote_checked = True
        except ProviderAuthError:
            try:
                await _ensure_saved_provider_login(provider, source_type)
                remote_match = bool(await provider.is_following(username))
                remote_checked = True
            except HTTPException:
                remote_checked = False
        except ProviderError:
            remote_checked = False
    return {
        "isFollowing": bool(remote_match or local_match),
        "localOnly": not getattr(provider.capabilities, "can_sync_following", False),
        "localFollowing": local_match,
        "remoteFollowing": remote_match if remote_checked else None,
    }


@app.post("/api/providers/{source_type}/follow/{username}")
async def provider_follow(source_type: str, username: str):
    source_type = _normalize_source_type(source_type) or ""
    if source_type not in _available_source_types():
        raise HTTPException(status_code=404, detail=f"Source '{source_type}' non disponible")
    provider = _provider_for(source_type)
    if getattr(provider.capabilities, "can_sync_following", False):
        try:
            try:
                result = await provider.follow(username)
            except ProviderAuthError:
                await _ensure_saved_provider_login(provider, source_type)
                result = await provider.follow(username)
        except ProviderInteractionRequired as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ProviderAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        except ProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not result.get("success", True):
            return JSONResponse(status_code=400, content=result)
        result = {**result, "localOnly": False, "action": result.get("action") or "follow"}
    else:
        result = {"success": True, "localOnly": True, "action": "follow"}
    status = ProviderStatus(False, source_type=source_type)
    try:
        status = await _provider_status(source_type, username)
    except Exception:
        pass
    await db.upsert_followed_model(
        username=username,
        display_name=username,
        is_online=bool(status.is_online),
        viewers=int(status.viewers or 0),
        thumbnail_url=status.thumbnail,
        source_type=source_type,
        room_status=status.room_status,
    )
    await db.reconcile_model_sources_from_followed()
    return result


@app.post("/api/providers/{source_type}/unfollow/{username}")
async def provider_unfollow(source_type: str, username: str):
    source_type = _normalize_source_type(source_type) or ""
    if source_type not in _available_source_types():
        raise HTTPException(status_code=404, detail=f"Source '{source_type}' non disponible")
    provider = _provider_for(source_type)
    if getattr(provider.capabilities, "can_sync_following", False):
        try:
            try:
                result = await provider.unfollow(username)
            except ProviderAuthError:
                await _ensure_saved_provider_login(provider, source_type)
                result = await provider.unfollow(username)
        except ProviderInteractionRequired as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ProviderAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        except ProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not result.get("success", True):
            return JSONResponse(status_code=400, content=result)
        result = {**result, "localOnly": False, "action": result.get("action") or "unfollow"}
    else:
        result = {"success": True, "localOnly": True, "action": "unfollow"}
    await db.delete_followed_model(username, source_type=source_type)
    return result


@app.post("/api/start")
async def api_start(body: StartBody):
    start_time = time.time()
    logger.section("API /api/start - Démarrage Enregistrement")
    logger.debug("Requête reçue", 
                target=body.target, 
                source_type=body.source_type,
                person=body.person,
                name=body.name,
                auto_start=body.auto_start)
    
    target = (body.target or "").strip()
    if not target:
        logger.error("Champ 'target' vide dans la requête")
        raise HTTPException(status_code=400, detail="Champ 'target' requis")

    requested_source = _normalize_source_type(body.source_type)
    model_settings = None
    if not target.startswith("http://") and not target.startswith("https://"):
        model_lookup_usernames = []
        if (body.person or "").strip():
            model_lookup_usernames.append((body.person or "").strip())
        if target not in model_lookup_usernames:
            model_lookup_usernames.append(target)
        for model_lookup_username in model_lookup_usernames:
            try:
                model_settings = await db.get_model(model_lookup_username, source_type=requested_source)
                if model_settings:
                    break
            except Exception:
                model_settings = None
    
    # Si c'est un auto-start, vérifier que auto_record est activé dans la DB
    if body.auto_start:
        username = body.person or target
        model = model_settings or await db.get_model(username, source_type=requested_source)
        if model:
            auto_record = bool(model.get('auto_record', True))
            if not auto_record:
                logger.warning("Auto-record désactivé pour ce modèle", username=username)
                raise HTTPException(status_code=403, detail=f"Auto-record désactivé pour {username}")
        else:
            logger.warning("Modèle non trouvé en DB, auto-start refusé", username=username)
            raise HTTPException(status_code=404, detail=f"Modèle {username} non trouvé")

    logger.info("Paramètres validés", target=target, source_type=body.source_type)

    m3u8_url: Optional[str] = None
    stream_headers: Optional[dict[str, str]] = None
    source_url: Optional[str] = None
    ffmpeg_video_stream_index: Optional[int] = None
    person: Optional[str] = (body.person or "").strip() or None
    record_quality = body.record_quality or body.recordQuality
    session_key = body.session_key or body.sessionKey
    if not record_quality and model_settings:
        record_quality = model_settings.get("record_quality")
    max_height = await _get_recording_height_for_quality(record_quality)
    filename_format = await _get_recording_filename_format()

    # Determine source type
    stype = requested_source or await _infer_source_type(person or target, model_settings)
    if target.startswith("http://") or target.startswith("https://"):
        url_source = _source_type_from_url(target)
        if not requested_source and url_source:
            stype = url_source
    logger.debug("Détermination type source", source_type=stype or 'auto', target=target)

    direct_media_url = (
        target.startswith("http://")
        or target.startswith("https://")
    ) and (".m3u8" in target.lower() or ".mpd" in target.lower())

    if stype == "m3u8" or direct_media_url:
        logger.info("URL M3U8 directe détectée", url=target[:80])
        m3u8_url = target
        source_url = target
    else:
        effective_source = stype
        available_sources = _available_source_types()
        if effective_source not in available_sources:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"source_type '{effective_source}' inconnu. "
                    f"Sources disponibles: {', '.join(sorted(available_sources))}"
                ),
            )
        if _supports_browser_capture(effective_source):
            if not person:
                person = target
            person = slugify(person)
            logger.subsection(f"Démarrage capture navigateur '{effective_source}'")
            try:
                await _ensure_browser_capture_session(effective_source)
                sess = _start_browser_capture(
                    effective_source,
                    target,
                    person=person,
                    display_name=body.name or target,
                    record=True,
                    filename_format=filename_format,
                    records_dir_for_person=_model_record_dir(model_settings, person),
                    target_username=target,
                    session_key=session_key,
                )
                ready = await asyncio.to_thread(sess.wait_until_ready, 35)
                if not ready:
                    browser_capture_manager.stop_session(sess.id)
                    raise RuntimeError("Aucun flux video capturable dans le navigateur")
                duration_ms = (time.time() - start_time) * 1000
                logger.success(
                    "Session browser capture créée",
                    session_id=sess.id,
                    person=person,
                    duration_ms=f"{duration_ms:.2f}",
                )
            except RuntimeError as e:
                logger.error("Session browser capture impossible", person=person, error=str(e))
                raise HTTPException(status_code=409, detail=str(e))
            except ProviderError as e:
                logger.error("Session browser capture refusee", person=person, error=str(e))
                raise HTTPException(status_code=400, detail=_provider_error_detail(effective_source, target, e))
            except Exception as e:
                logger.critical("Erreur création browser capture", exc_info=True, person=person, error=str(e))
                raise HTTPException(status_code=500, detail=f"Erreur serveur: {str(e)}")

            return {
                "id": sess.id,
                "person": person,
                "name": sess.name,
                "playback_url": sess.playback_url,
                "record_path": sess.record_path_today(),
                "created_at": sess.created_at,
                "running": True,
                "capture_type": "browser",
                "source_type": effective_source,
                "target": target,
                "session_key": session_key or person,
            }
        logger.subsection(f"Résolution via source '{effective_source}'")
        try:
            resolved = await _resolve_stream(effective_source, target, max_height)
            ffmpeg_video_stream_index = resolved.ffmpeg_video_stream_index
            m3u8_url, stream_headers, source_url = _ffmpeg_stream_input(resolved)
            if not m3u8_url:
                raise HTTPException(
                    status_code=400,
                    detail=f"Impossible de trouver le flux pour {target}",
                )
            logger.success("M3U8 résolu", username=target, url=m3u8_url[:80])
            if not person:
                person = target
                logger.debug("Person défini depuis target", person=person)
        except HTTPException:
            raise
        except Exception as e:
            error_detail = f"Échec résolution {effective_source}: {_provider_error_detail(effective_source, target, e)}"
            logger.error(error_detail, exc_info=True, username=target)
            raise HTTPException(status_code=400, detail=error_detail)

    # If person still not set (direct m3u8), infer from URL
    if not person:
        try:
            pu = urlparse(m3u8_url)
            # try last non-empty path part without extension
            parts = [p for p in pu.path.split('/') if p]
            base = parts[-2] if len(parts) >= 2 else (parts[-1] if parts else pu.hostname or "session")
            base = base.split('.')[0]
            person = base or (pu.hostname or "session")
        except Exception:
            person = "session"

    person = slugify(person)
    logger.info("Identifiant slugifié", person=person, display_name=body.name)
    source_url = source_url or m3u8_url
    records_dir_for_person = _model_record_dir(model_settings, person)

    segment_duration_seconds, segment_size_bytes = await _get_recording_segment_limits()
    logger.subsection("Démarrage Session FFmpeg")
    try:
        sess = manager.start_session(
            m3u8_url,
            person=person,
            display_name=body.name,
            max_height=max_height,
            segment_duration_seconds=segment_duration_seconds,
            segment_size_bytes=segment_size_bytes,
            input_headers=stream_headers,
            source_url=source_url,
            ffmpeg_video_stream_index=ffmpeg_video_stream_index,
            filename_format=filename_format,
            records_dir_for_person=str(records_dir_for_person),
            source_type=stype,
            target=target,
            session_key=session_key,
        )
        duration_ms = (time.time() - start_time) * 1000
        logger.success("Session créée avec succès", 
                      session_id=sess.id,
                      person=person,
                      duration_ms=f"{duration_ms:.2f}")
    except RuntimeError as e:
        logger.error("Session déjà en cours", person=person, error=str(e))
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        logger.critical("Erreur création session", exc_info=True, person=person, error=str(e))
        raise HTTPException(status_code=500, detail=f"Erreur serveur: {str(e)}")

    return {
        "id": sess.id,
        "person": person,
        "name": sess.name,
        "playback_url": sess.playback_url,
        "record_path": sess.record_path_today(),
        "created_at": sess.created_at,
        "running": True,
        "source_type": stype,
        "target": target,
        "session_key": session_key or person,
    }


@app.get("/api/status")
async def api_status():
    return _all_recording_statuses()


@app.post("/api/stop/{session_id}")
async def api_stop(session_id: str):
    ffmpeg_session = manager.get_session(session_id)
    ok = manager.stop_session(session_id)
    if ok and ffmpeg_session:
        await _index_ffmpeg_recording(ffmpeg_session)
    if not ok:
        browser_session = browser_capture_manager.get(session_id)
        ok = browser_capture_manager.stop_session(session_id)
        if ok and browser_session:
            await _index_browser_capture_recording(browser_session)
    if not ok:
        raise HTTPException(status_code=404, detail="Session introuvable")
    return {"stopped": True, "id": session_id}


# ============================================
# FFmpeg process inspection endpoints
# ============================================

def _proc_state_letter(status: str) -> str:
    """psutil status string -> short Linux state letter."""
    mapping = {
        "running": "R",
        "sleeping": "S",
        "disk-sleep": "D",
        "stopped": "T",
        "tracing-stop": "t",
        "zombie": "Z",
        "dead": "X",
        "wake-kill": "K",
        "waking": "W",
        "idle": "I",
        "parked": "P",
    }
    return mapping.get(status, status[:1].upper() if status else "?")


def _safe_dir_size(path: str) -> Tuple[int, int]:
    """Return (segment_count, bytes) for files under `path`. Best effort."""
    try:
        total = 0
        count = 0
        for entry in os.scandir(path):
            if entry.is_file() and entry.name.endswith(".ts"):
                try:
                    total += entry.stat().st_size
                    count += 1
                except OSError:
                    continue
        return count, total
    except OSError:
        return 0, 0


async def _build_process_snapshot(sess_status: dict) -> dict:
    """Augment a manager.list_status() entry with /proc + psutil + disk stats."""
    import psutil

    out = {
        "session_id": sess_status.get("id"),
        "person": sess_status.get("person"),
        "name": sess_status.get("name"),
        "input_url": sess_status.get("input_url"),
        "record_path": sess_status.get("record_path"),
        "playback_url": sess_status.get("playback_url"),
        "running": bool(sess_status.get("running")),
        "started_at": sess_status.get("created_at"),
        "start_date": sess_status.get("start_date"),
        # filled below
        "pid": None,
        "uptime_seconds": None,
        "cpu_percent": None,
        "rss_bytes": None,
        "vsz_bytes": None,
        "num_threads": None,
        "num_fds": None,
        "status": None,
        "nice": None,
        "io_read_bytes": None,
        "io_write_bytes": None,
        "record_size_bytes": None,
        "segment_count": None,
        "quality": None,
        "bytes_written": sess_status.get("bytes_written"),
        "seconds_since_progress": sess_status.get("seconds_since_progress"),
    }

    # Resolve the underlying FFmpegSession to grab the live process
    sess = manager._sessions.get(sess_status.get("id")) if hasattr(manager, "_sessions") else None
    proc = getattr(sess, "process", None) if sess else None
    pid = proc.pid if proc and proc.poll() is None else None
    out["pid"] = pid

    if pid:
        try:
            p = psutil.Process(pid)
            with p.oneshot():
                out["cpu_percent"] = round(p.cpu_percent(interval=0.0), 1)
                mem = p.memory_info()
                out["rss_bytes"] = mem.rss
                out["vsz_bytes"] = mem.vms
                out["num_threads"] = p.num_threads()
                try:
                    out["num_fds"] = p.num_fds()
                except (psutil.AccessDenied, AttributeError):
                    out["num_fds"] = None
                out["status"] = _proc_state_letter(p.status())
                try:
                    out["nice"] = p.nice()
                except psutil.AccessDenied:
                    out["nice"] = None
                try:
                    io = p.io_counters()
                    out["io_read_bytes"] = io.read_bytes
                    out["io_write_bytes"] = io.write_bytes
                except (psutil.AccessDenied, AttributeError):
                    pass
                out["uptime_seconds"] = max(0, int(time.time() - p.create_time()))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if out["uptime_seconds"] is None and out["started_at"]:
        try:
            out["uptime_seconds"] = max(0, int(time.time() - float(out["started_at"])))
        except (TypeError, ValueError):
            pass

    # Recorded TS file size (output)
    rp = sess_status.get("record_path")
    if rp:
        try:
            out["record_size_bytes"] = os.path.getsize(rp)
        except OSError:
            out["record_size_bytes"] = None

    # HLS segment dir stats
    if sess and getattr(sess, "sessions_dir", None):
        seg_count, seg_size = _safe_dir_size(sess.sessions_dir)
        out["segment_count"] = seg_count
        out["segment_bytes"] = seg_size

    # Effective quality from the model's current setting (plus global cap)
    person = sess_status.get("person")
    if person:
        try:
            model = await db.get_model(person)
            if model:
                rq = model.get("record_quality") or "best"
                eff = await _get_recording_height_for_quality(rq)
                out["quality"] = f"{eff}p" if eff else "best"
                out["record_quality"] = rq
        except Exception:
            pass

    return out


@app.get("/api/processes")
async def api_processes():
    """List ffmpeg recording processes with detailed metrics."""
    import psutil
    statuses = manager.list_status()
    procs = []
    total_cpu = 0.0
    total_rss = 0
    for s in statuses:
        snap = await _build_process_snapshot(s)
        procs.append(snap)
        if snap.get("cpu_percent") is not None:
            total_cpu += snap["cpu_percent"]
        if snap.get("rss_bytes") is not None:
            total_rss += snap["rss_bytes"]
    active = sum(1 for p in procs if p.get("running"))
    try:
        cores = psutil.cpu_count(logical=True) or 1
    except Exception:
        cores = 1
    return {
        "processes": procs,
        "totals": {
            "active": active,
            "total": len(procs),
            "cpu_percent_sum": round(total_cpu, 1),
            "rss_bytes_sum": total_rss,
            "host_cores": cores,
        },
    }


@app.get("/api/processes/{session_id}/log")
async def api_process_log(session_id: str, lines: int = 30):
    """Return the last `lines` lines of the session's ffmpeg.log."""
    sess = manager._sessions.get(session_id) if hasattr(manager, "_sessions") else None
    if not sess:
        raise HTTPException(status_code=404, detail="Session introuvable")
    log_path = getattr(sess, "log_path", None)
    if not log_path or not os.path.exists(log_path):
        return {"session_id": session_id, "lines": [], "path": log_path}
    lines = max(1, min(int(lines or 30), 500))
    try:
        # Tail without loading the whole file
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 4096
            data = b""
            while size > 0 and data.count(b"\n") <= lines:
                read = min(block, size)
                size -= read
                f.seek(size)
                data = f.read(read) + data
        text = data.decode("utf-8", errors="replace")
        tail = text.splitlines()[-lines:]
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Lecture log: {e}")
    return {"session_id": session_id, "lines": tail, "path": log_path}


@app.post("/api/processes/{session_id}/stop")
async def api_process_stop(session_id: str):
    """Graceful stop (SIGTERM, then SIGKILL on timeout). Same as /api/stop."""
    ok = manager.stop_session(session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Session introuvable")
    return {"stopped": True, "session_id": session_id}


@app.post("/api/processes/{session_id}/kill")
async def api_process_kill(session_id: str):
    """Force SIGKILL on the ffmpeg process. Use when graceful stop hangs."""
    sess = manager._sessions.get(session_id) if hasattr(manager, "_sessions") else None
    if not sess:
        raise HTTPException(status_code=404, detail="Session introuvable")
    proc = getattr(sess, "process", None)
    if not proc or proc.poll() is not None:
        # Already gone — fall back to graceful stop to clean up bookkeeping
        manager.stop_session(session_id)
        return {"killed": False, "reason": "already-exited", "session_id": session_id}
    try:
        proc.kill()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur kill: {e}")
    # Run the manager's stop to release locks / threads
    manager.stop_session(session_id)
    return {"killed": True, "session_id": session_id}


@app.post("/api/processes/{session_id}/restart")
async def api_process_restart(session_id: str):
    """Stop the session — the auto-monitor will re-spawn it on its next tick.

    Restart is implemented as a clean stop on purpose: Chaturbate URLs are
    token-bearing and may have expired, so respawning with the cached URL
    can fail. The monitor task re-resolves a fresh URL when it picks the
    model back up (typically within a few seconds).
    """
    sess = manager._sessions.get(session_id) if hasattr(manager, "_sessions") else None
    if not sess:
        raise HTTPException(status_code=404, detail="Session introuvable")
    person = sess.person
    ok = manager.stop_session(session_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Stop échoué")
    return {
        "restarted": True,
        "session_id": session_id,
        "person": person,
        "note": "monitor will re-spawn within a few seconds",
    }


@app.get("/api/model/{username}/status")
async def get_model_status(username: str, source: Optional[str] = None):
    """Récupère le statut d'un modèle via le registre provider."""
    source_type = _normalize_source_type(source)
    model = await db.get_model(username, source_type=source_type)
    if not model or not model.get('source_type'):
        followed = await db.get_followed_model(username, source_type=source_type)
        if followed and followed.get('source_type'):
            if not model:
                model = followed
            else:
                model = {**model, 'source_type': followed['source_type']}

    if not source_type:
        source_type = await _infer_source_type(username, model)

    if source_type not in _available_source_types():
        return {
            "username": username,
            "isOnline": False,
            "thumbnail": f"/api/thumbnail/{username}",
            "viewers": 0,
            "tags": [],
            "roomStatus": "unsupported",
            "sourceType": source_type,
        }

    if model and model.get('is_online'):
        return {
            "username": username,
            "isOnline": True,
            "thumbnail": f"/api/thumbnail/{username}",
            "viewers": model.get('viewers', 0),
            "tags": list(model.get('tags') or []),
            "roomStatus": model.get('room_status'),
            "sourceType": source_type,
        }

    try:
        status = await _provider_status(source_type, username)
        return {
            "username": username,
            "isOnline": bool(status.is_online),
            "thumbnail": status.thumbnail or f"/api/thumbnail/{username}",
            "viewers": int(status.viewers or 0),
            "tags": list(status.tags or []),
            "roomStatus": status.room_status,
            "sourceType": source_type,
        }
    except Exception as e:
        logger.debug(
            "Provider check_status échoué",
            username=username,
            source_type=source_type,
            error=str(e),
        )

    return {
        "username": username,
        "isOnline": model.get('is_online', False) if model else False,
        "thumbnail": f"/api/thumbnail/{username}",
        "viewers": model.get('viewers', 0) if model else 0,
        "tags": list(model.get('tags') or []) if model else [],
        "roomStatus": model.get('room_status') if model else None,
        "sourceType": source_type,
    }


@app.get("/api/model/{username}/stream")
async def get_model_stream(username: str, source: Optional[str] = None):
    """Récupère l'URL du stream live pour un modèle.

    Le source_type est déterminé par ordre de priorité: query param `source`
    (depuis le discover multi-plugin), puis cache SQLite, puis défaut
    Chaturbate.
    """
    try:
        model = None
        try:
            requested_source = _normalize_source_type(source)
            model = await db.get_model(username, source_type=requested_source)
        except Exception:
            pass
        source_type = _normalize_source_type(source)
        if not source_type:
            source_type = await _infer_source_type(username, model)

        if source_type not in _available_source_types():
            raise HTTPException(
                status_code=404,
                detail=f"Source '{source_type}' non disponible",
            )
        if _supports_browser_capture(source_type):
            status = None
            person = slugify(username)
            try:
                await _ensure_browser_capture_session(source_type)
                sess = _start_browser_capture(
                    source_type,
                    username,
                    person=person,
                    display_name=username,
                    record=False,
                )
                ready = await asyncio.to_thread(sess.wait_until_ready, 35)
                if not ready:
                    browser_capture_manager.stop_session(sess.id)
                    raise RuntimeError("Aucun flux video capturable dans le navigateur")
            except Exception as e:
                raise HTTPException(status_code=404, detail=_provider_error_detail(source_type, username, e))

            return {
                "username": username,
                "streamUrl": sess.playback_url,
                "streamType": getattr(sess, "file_extension", "webm"),
                "isOnline": True,
                "sourceType": source_type,
                "roomStatus": "public",
                "viewers": int(status.viewers or 0) if status else 0,
                "tags": list(status.tags or []) if status else [],
                "thumbnail": status.thumbnail if status else None,
            }
        try:
            max_height = await _get_max_recording_height()
            resolved = await _resolve_stream(source_type, username, max_height)
        except Exception as e:
            raise HTTPException(status_code=404, detail=_provider_error_detail(source_type, username, e))

        if not resolved.url:
            raise HTTPException(status_code=404, detail=f"Impossible de trouver le flux pour {username}")

        return {
            "username": username,
            **_watch_stream_payload(resolved),
            "isOnline": True,
            "sourceType": source_type,
            "roomStatus": resolved.room_status,
            "viewers": int(resolved.viewers or 0),
            "tags": list(resolved.tags or []),
            "thumbnail": resolved.thumbnail,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Erreur récupération stream", username=username, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/thumbnail/{username}")
async def get_thumbnail(username: str):
    """Sert la miniature depuis le cache (générée par la tâche de monitoring)"""
    from fastapi.responses import FileResponse, Response
    
    # Récupérer le chemin de la miniature depuis SQLite
    model = await db.get_model(username)
    
    if model and model.get('thumbnail_path'):
        thumb_path = Path(model['thumbnail_path'])
        
        if thumb_path.exists():
            return FileResponse(
                path=str(thumb_path),
                media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=60"}
            )
    
    # Chercher manuellement dans les dossiers si pas en cache
    # Ordre de préférence: live > chaturbate > offline
    for subdir in ["live", "chaturbate", "offline"]:
        thumb_path = OUTPUT_DIR / "thumbnails" / subdir / f"{username}.jpg"
        if thumb_path.exists():
            return FileResponse(
                path=str(thumb_path),
                media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=60"}
            )
    
    # SVG placeholder si aucune miniature trouvée
    svg_placeholder = f'''<svg xmlns="http://www.w3.org/2000/svg" width="280" height="200">
        <defs>
            <linearGradient id="grad" x1="0%" y1="0%" x2="100%" y2="100%">
                <stop offset="0%" style="stop-color:#6366f1;stop-opacity:1" />
                <stop offset="100%" style="stop-color:#a855f7;stop-opacity:1" />
            </linearGradient>
        </defs>
        <rect fill="url(#grad)" width="280" height="200"/>
        <text x="50%" y="50%" dominant-baseline="middle" text-anchor="middle" fill="white" font-family="system-ui" font-size="18" font-weight="600">{username}</text>
        <text x="50%" y="70%" dominant-baseline="middle" text-anchor="middle" fill="white" font-family="system-ui" font-size="12" opacity="0.8">📷 Loading...</text>
    </svg>'''
    
    return Response(
        content=svg_placeholder,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=10"}
    )


def _format_duration_label(duration_seconds: int) -> str:
    hours = duration_seconds // 3600
    minutes = (duration_seconds % 3600) // 60
    seconds = duration_seconds % 60
    if hours > 0:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{seconds:02d}s"


def _format_size_display(file_size: int) -> str:
    if file_size >= 1000 * 1024 * 1024:
        return f"{file_size / 1024 / 1024 / 1024:.2f} GB"
    return f"{file_size / 1024 / 1024:.0f} MB"


def _format_import_recording(rec: dict, username: str) -> Optional[dict]:
    from .core.utils import format_bytes

    source_path = Path(rec.get("file_path") or "")
    if not source_path.exists():
        return None

    recording_id = rec.get("recording_id") or source_path.stem
    playable_raw = rec.get("playable_path")
    playable_path = Path(playable_raw) if playable_raw else None
    playable = bool(playable_path and playable_path.exists())
    file_size = (
        rec.get("playable_size")
        if playable and rec.get("playable_size")
        else rec.get("file_size") or source_path.stat().st_size
    )
    created_at = rec.get("created_at") or int(source_path.stat().st_mtime)
    duration_seconds = int(rec.get("duration_seconds") or 0)
    thumb_path = Path(rec.get("thumbnail_path") or "")
    thumb_url = (
        f"/api/recording-thumbnail/{username}/{thumb_path.name}"
        if thumb_path.exists()
        else None
    )

    return {
        "recordingId": recording_id,
        "filename": source_path.name,
        "title": rec.get("title") or source_path.stem,
        "date": source_path.stem,
        "size": file_size,
        "size_formatted": format_bytes(file_size),
        "size_mb": round(file_size / 1024 / 1024, 2),
        "size_display": _format_size_display(file_size),
        "modified": datetime.fromtimestamp(source_path.stat().st_mtime).isoformat(),
        "url": f"/streams/media/{recording_id}" if playable else None,
        "downloadUrl": f"/streams/media/{recording_id}?download=1",
        "thumbnail": thumb_url,
        "duration": duration_seconds,
        "duration_str": _format_duration_label(duration_seconds),
        "isConverted": playable,
        "isImported": True,
        "mediaKind": "import",
        "importStatus": rec.get("import_status") or ("ready" if playable else "failed"),
        "importError": rec.get("import_error"),
        "playable": playable,
        "createdAt": created_at,
        "mp4": None,
    }


@app.get("/api/recordings/{username}")
async def list_recordings(username: str, show_ts: bool = False):
    """Liste les enregistrements (MP4 convertis ou TS bruts)"""
    from datetime import datetime
    from .core.utils import format_bytes

    # Récupérer depuis SQLite
    recordings_db = await db.get_recordings(username)

    recordings = []
    thumbnails_dir = OUTPUT_DIR / "thumbnails" / username

    for rec in recordings_db:
        if rec.get("media_kind") == "import":
            formatted_import = _format_import_recording(rec, username)
            if formatted_import:
                recordings.append(formatted_import)
            continue

        # Determine the playable file: prefer MP4, fall back to TS
        is_converted = bool(rec.get('is_converted'))
        mp4_raw = rec.get('mp4_path')
        ts_raw = rec.get('file_path')

        if is_converted and mp4_raw and Path(mp4_raw).exists():
            serve_path = Path(mp4_raw)
            file_size = rec.get('mp4_size') or serve_path.stat().st_size
        elif ts_raw and Path(ts_raw).exists():
            serve_path = Path(ts_raw)
            # Skip raw TS files unless show_ts is enabled. Browser captures are
            # written as WebM and are directly playable, so keep them visible.
            if serve_path.suffix.lower() == ".ts" and not show_ts:
                continue
            file_size = rec.get('file_size') or serve_path.stat().st_size
        else:
            continue

        stat = serve_path.stat()

        # Miniature
        thumb_path = thumbnails_dir / f"{serve_path.stem}.jpg"
        thumb_url = f"/api/recording-thumbnail/{username}/{serve_path.stem}.jpg"

        # Formater la durée
        duration_seconds = rec.get('duration_seconds', 0)
        if (
            (duration_seconds and duration_seconds < MIN_RECORDING_SECONDS)
            or (not duration_seconds and file_size < MIN_RECORDING_BYTES)
        ):
            continue

        duration_str = _format_duration_label(duration_seconds)
        size_display = _format_size_display(file_size)

        # Use created_at from DB, fallback to file mtime
        created_at = rec.get('created_at')
        if not created_at:
            created_at = int(stat.st_mtime)

        recording_id = rec.get('recording_id', serve_path.stem)
        recording_url = f"/streams/recordings/{quote(recording_id, safe='')}"

        recordings.append({
            "recordingId": recording_id,
            "filename": serve_path.name,
            "date": serve_path.stem,
            "size": file_size,
            "size_formatted": format_bytes(file_size),
            "size_mb": round(file_size / 1024 / 1024, 2),
            "size_display": size_display,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "url": recording_url,
            "thumbnail": thumb_url if thumb_path.exists() else None,
            "duration": duration_seconds,
            "duration_str": duration_str,
            "isConverted": is_converted,
            "isImported": False,
            "mediaKind": "recording",
            "importStatus": None,
            "importError": None,
            "playable": True,
            "downloadUrl": recording_url,
            "conversionAttempts": rec.get('conversion_attempts') or 0,
            "conversionError": rec.get('conversion_error'),
            "createdAt": created_at,
            "mp4": {
                "filename": Path(mp4_raw).name,
                "size": rec.get('mp4_size', 0),
                "size_formatted": format_bytes(rec.get('mp4_size', 0)),
                "url": recording_url
            } if is_converted and mp4_raw else None
        })

    return {"recordings": recordings}


@app.post("/api/recordings/{recording_id}/retry-conversion")
async def retry_conversion(recording_id: str):
    """Réinitialise le compteur d'échecs pour forcer une nouvelle tentative de conversion."""
    rec = await db.get_recording_by_id(recording_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Enregistrement introuvable")
    if rec.get('is_converted'):
        return {"success": True, "message": "Déjà converti", "alreadyConverted": True}
    ts_path = Path(rec.get('file_path', ''))
    if not ts_path.exists():
        raise HTTPException(status_code=404, detail="Fichier TS source introuvable")
    reset = await db.reset_conversion_failure(recording_id)
    return {
        "success": reset,
        "message": "Conversion programmée au prochain scan (~30s)",
        "recordingId": recording_id
    }


def _get_media_import_manager() -> MediaImportManager:
    global media_import_manager
    if media_import_manager is None:
        media_import_manager = MediaImportManager(db, OUTPUT_DIR, FFMPEG_PATH)
    return media_import_manager


@app.get("/api/media-imports/status")
async def get_media_imports_status():
    """Return local media import feature status."""
    if not MEDIA_IMPORTS_ENABLED:
        return {
            "enabled": False,
            "running": False,
            "lastScanAt": None,
            "lastResult": None,
        }

    manager_ref = _get_media_import_manager()
    return {
        "enabled": True,
        "running": manager_ref.running,
        "lastScanAt": manager_ref.last_scan_at,
        "lastResult": manager_ref.last_result,
    }


@app.post("/api/media-imports/rescan")
async def rescan_media_imports():
    """Trigger an immediate local media import scan."""
    if not MEDIA_IMPORTS_ENABLED:
        return {
            "success": False,
            "enabled": False,
            "message": "Local media imports are disabled",
        }

    result = await _get_media_import_manager().scan()
    return {
        "enabled": True,
        **result,
    }


@app.get("/api/media-library")
async def get_media_library(
    username: Optional[str] = None,
    kind: str = "all",
    search: str = "",
    sort: str = "newest",
    watched: str = "all",
    metadata: str = "full",
    limit: int = 1000,
    offset: int = 0,
):
    """Liste les médias présents dans les dossiers records."""
    from .core.utils import format_bytes

    metadata_mode = (metadata or "full").strip().lower()
    refresh_metadata = metadata_mode not in {"lazy", "fast", "0", "false", "no"}
    scan_username = username if refresh_metadata and username else None
    all_items = await _scan_media_library_items(
        profile_username=scan_username,
        refresh_metadata=refresh_metadata,
    )
    await _repair_truncated_media_profile_usernames()
    folder_profiles = _list_media_profile_folders()
    media_profiles = await db.get_all_media_profiles()
    all_models = await db.get_all_models()
    all_profile_sources = await db.get_all_media_profile_sources()
    media_profiles_by_username = {
        profile["username"]: profile
        for profile in media_profiles
        if profile.get("username")
    }
    profile_sources_by_username: dict[str, list[dict]] = {}
    for source in all_profile_sources:
        profile_username = source.get("profile_username")
        if not profile_username:
            continue
        profile_sources_by_username.setdefault(profile_username, []).append(source)

    profile_stats: dict[str, dict] = {}
    for item in all_items:
        profile = item["username"]
        if profile not in profile_stats:
            profile_stats[profile] = {
                "username": profile,
                "total": 0,
                "videos": 0,
                "images": 0,
                "audio": 0,
                "totalSize": 0,
            }
        stats = profile_stats[profile]
        stats["total"] += 1
        stats["totalSize"] += int(item.get("size") or 0)
        if item.get("type") == "video":
            stats["videos"] += 1
        elif item.get("type") == "image":
            stats["images"] += 1
        elif item.get("type") == "audio":
            stats["audio"] += 1

        if int(item.get("createdAt") or 0) >= int(stats.get("latestAt") or 0):
            stats["latestAt"] = int(item.get("createdAt") or 0)
            stats["latestTitle"] = item.get("title") or item.get("filename") or ""
            stats["latestType"] = item.get("type") or ""
            stats["previewUrl"] = item.get("url")

    profile_names = set(profile_stats)
    profile_names.update(folder_profiles)
    profile_names.update(media_profiles_by_username)
    profile_names.update(model["username"] for model in all_models if model.get("username"))

    profiles = []
    for profile_name in profile_names:
        profile = profile_stats.setdefault(profile_name, {
            "username": profile_name,
            "total": 0,
            "videos": 0,
            "images": 0,
            "audio": 0,
            "totalSize": 0,
            "latestAt": None,
            "latestTitle": "",
            "latestType": "",
            "previewUrl": None,
        })
        metadata = media_profiles_by_username.get(profile_name)
        model = _model_for_media_profile(profile_name, all_models)
        formatted_metadata = _media_profile_formatted(metadata)
        raw_sources = profile_sources_by_username.get(profile_name) or []
        stream_sources = [_profile_source_response(source) for source in raw_sources]
        if not stream_sources and model:
            stream_sources = await _media_profile_stream_sources(profile_name)
        primary_source = stream_sources[0] if stream_sources else None
        source_type = (primary_source or {}).get("sourceType") or (await _infer_source_type(profile_name, model) if model else "chaturbate")
        profiles.append({
            **profile,
            **formatted_metadata,
            "displayName": formatted_metadata["displayName"] or (model or {}).get("display_name") or profile_name,
            "folderExists": profile_name in folder_profiles,
            "empty": int(profile.get("total") or 0) == 0,
            "autoRecord": bool((primary_source or {}).get("autoRecord", (model or {}).get("auto_record", False))),
            "recordQuality": (primary_source or {}).get("recordQuality") or (model or {}).get("record_quality", "best"),
            "retentionDays": (primary_source or {}).get("retentionDays", (model or {}).get("retention_days", 30)),
            "sourceType": source_type,
            "source_type": source_type,
            "streamSources": stream_sources,
            "stream_sources": stream_sources,
            "deleteUrl": f"/api/media-profiles/{quote(profile_name, safe='')}",
            "totalSizeFormatted": format_bytes(profile["totalSize"]),
        })
    profiles.sort(key=lambda item: (
        -int(item["total"] > 0),
        -int(item.get("latestAt") or 0),
        item["displayName"].lower(),
        item["username"].lower(),
    ))

    normalized_kind = (kind or "all").strip().lower()
    kind_aliases = {
        "photos": "image",
        "photo": "image",
        "images": "image",
        "videos": "video",
        "audio": "audio",
        "all": "all",
        "tous": "all",
    }
    normalized_kind = kind_aliases.get(normalized_kind, normalized_kind)
    if normalized_kind not in {"all", "video", "image", "audio"}:
        normalized_kind = "all"

    filtered = all_items
    if username:
        filtered = [item for item in filtered if item["username"] == username]
    if normalized_kind != "all":
        filtered = [item for item in filtered if item["type"] == normalized_kind]
    if search:
        query = search.strip().lower()
        if query:
            filtered = [
                item for item in filtered
                if query in item["filename"].lower()
                or query in item["title"].lower()
                or query in item["username"].lower()
                or query in item["relativePath"].lower()
            ]

    watched_threshold = await _get_watched_threshold()
    playback_positions = await db.get_all_playback_positions(username)
    playback_by_recording_id = {
        row.get("recording_id"): row
        for row in playback_positions
        if row.get("recording_id")
    }
    for item in filtered:
        _attach_media_playback_state(
            item,
            playback_by_recording_id.get(item.get("recordingId")),
            watched_threshold,
        )

    watched_filter = (watched or "all").strip().lower()
    if watched_filter in {"unwatched", "not_watched", "unseen", "non_vue", "non_vues", "non-vue", "non-vues"}:
        filtered = [
            item for item in filtered
            if item.get("type") == "video" and not item.get("isWatched")
        ]
    elif watched_filter in {"watched", "seen", "viewed", "vue", "vues"}:
        filtered = [
            item for item in filtered
            if item.get("type") == "video" and item.get("isWatched")
        ]

    sort_key = (sort or "newest").strip().lower()
    reverse = True
    if sort_key == "oldest":
        key_func = lambda item: item["createdAt"]
        reverse = False
    elif sort_key == "largest":
        key_func = lambda item: item["size"]
    elif sort_key == "smallest":
        key_func = lambda item: item["size"]
        reverse = False
    elif sort_key == "name":
        key_func = lambda item: (item["title"].lower(), item["filename"].lower())
        reverse = False
    else:
        key_func = lambda item: item["createdAt"]

    filtered = sorted(filtered, key=key_func, reverse=reverse)
    total = len(filtered)
    limit = max(1, min(int(limit or 1000), 5000))
    offset = max(0, int(offset or 0))
    page_items = filtered[offset:offset + limit]

    library_stats = _media_library_stats(all_items)
    filtered_stats = _media_library_stats(filtered)
    return {
        "items": page_items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "hasMore": offset + limit < total,
        "profiles": profiles,
        "stats": {
            **filtered_stats,
            "totalSizeFormatted": format_bytes(filtered_stats["totalSize"]),
        },
        "libraryStats": {
            **library_stats,
            "totalSizeFormatted": format_bytes(library_stats["totalSize"]),
        },
    }


@app.get("/api/media-profiles/{username}")
async def get_media_profile(username: str):
    """Retourne la fiche enrichie et les réglages stream d'un profil média."""
    username = _validate_media_profile_username(username)
    profile_dir = _media_profile_dir(username)
    profile = await db.get_media_profile(username)
    model = await db.get_model(username)
    recordings = await db.get_recordings(username)
    if not profile_dir.exists() and not profile and not model and not recordings:
        raise HTTPException(status_code=404, detail="Profile not found")
    return await _media_profile_payload(username)


@app.get("/api/media-profiles/{username}/profile-image")
async def get_media_profile_image(username: str):
    """Retourne l'image verticale dédiée d'un profil média."""
    username = _validate_media_profile_username(username)
    profile = await db.get_media_profile(username)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile image not found")

    image_path = Path(profile.get("profile_image_path") or "")
    if (
        not image_path
        or not image_path.exists()
        or not image_path.is_file()
        or not _path_is_inside_output(image_path)
    ):
        raise HTTPException(status_code=404, detail="Profile image not found")

    media_type = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
    return FileResponse(str(image_path), media_type=media_type)


@app.post("/api/media-profiles/{username}/profile-image/resolve")
async def resolve_media_profile_image(username: str, body: dict):
    """Récupère et cache une image verticale de profil depuis Babepedia ou une URL d'image."""
    username = _validate_media_profile_username(username)
    existing_profile = await db.get_media_profile(username) or {"username": username}
    resolved = await _resolve_profile_image_from_babepedia(username, existing_profile, body or {})
    image_url = _normalize_profile_image_url(resolved["imageUrl"])
    source_url = _normalize_profile_source_url(resolved.get("sourceUrl") or image_url)
    downloaded = await _download_profile_image(username, image_url)

    old_path = existing_profile.get("profile_image_path")
    if old_path and old_path != downloaded["path"]:
        try:
            path = Path(old_path)
            if path.exists() and path.is_file() and _path_is_inside_output(path):
                path.unlink()
        except OSError as e:
            logger.debug("Ancienne image profil non supprimée", username=username, error=str(e))

    await db.upsert_media_profile(username, {
        **existing_profile,
        "profile_image_url": image_url,
        "profile_image_source_url": source_url,
        "profile_image_path": downloaded["path"],
    })
    return {
        "success": True,
        "resolved": {
            "imageUrl": image_url,
            "sourceUrl": source_url,
            "size": downloaded["size"],
            "contentType": downloaded["contentType"],
        },
        "profile": await _media_profile_payload(username),
    }


@app.put("/api/media-profiles/{username}")
async def update_media_profile(username: str, body: dict):
    """Met à jour les informations locales et les réglages stream d'un profil."""
    body = body or {}
    username = _validate_media_profile_username(username)
    existing_profile = await db.get_media_profile(username) or {"username": username}
    raw_stream_sources = body.get("streamSources") if "streamSources" in body else body.get("stream_sources")
    has_stream_sources = isinstance(raw_stream_sources, list)
    requested_source = _normalize_source_type(
        body.get("sourceType") or body.get("source_type")
    ) or "chaturbate"
    if requested_source not in _available_source_types():
        raise HTTPException(
            status_code=400,
            detail=f"Source '{requested_source}' is not available",
        )

    profile_data = {
        "display_name": body.get("displayName") or body.get("display_name") or "",
        "first_name": body.get("firstName") or body.get("first_name") or "",
        "last_name": body.get("lastName") or body.get("last_name") or "",
        "age": body.get("age"),
        "birth_date": _normalize_birth_date(body.get("birthDate") or body.get("birth_date")),
        "address": body.get("address") or "",
        "city": body.get("city") or "",
        "region": body.get("region") or "",
        "postal_code": body.get("postalCode") or body.get("postal_code") or "",
        "country": body.get("country") or "",
        "aliases": body.get("aliases") or "",
        "tags": body.get("tags") or "",
        "notes": body.get("notes") or "",
        "social_urls": body.get("socialUrls") or body.get("social_urls") or [],
        "stream_urls": body.get("streamUrls") or body.get("stream_urls") or [],
        "profile_urls": body.get("profileUrls") or body.get("profile_urls") or [],
        **_profile_image_update_from_body(body, existing_profile),
    }
    await db.upsert_media_profile(username, profile_data)

    if has_stream_sources:
        normalized_sources = []
        seen_sources: set[tuple[str, str]] = set()
        for raw_source in raw_stream_sources:
            if not isinstance(raw_source, dict):
                continue
            normalized = await _normalize_profile_source_payload(username, raw_source, default_auto_record=False)
            key = (normalized["source_type"], normalized["channel_username"].lower())
            if key in seen_sources:
                continue
            seen_sources.add(key)
            normalized_sources.append(normalized)

        saved_sources = await db.replace_media_profile_sources(username, normalized_sources)
        for source in saved_sources:
            await db.add_or_update_model(
                username=source["channel_username"],
                display_name=profile_data["display_name"] or source["channel_username"],
                auto_record=bool(source.get("auto_record", False)),
                record_quality=source.get("record_quality") or await _get_default_record_quality(),
                retention_days=int(source.get("retention_days") if source.get("retention_days") is not None else await _get_default_retention_days()),
                source_type=source.get("source_type") or "chaturbate",
            )

        return {
            "success": True,
            "profile": await _media_profile_payload(username),
        }

    existing = await db.get_model(username, source_type=requested_source)
    retention_days = _normalize_retention_days(
        body.get("retentionDays"),
        (existing or {}).get("retention_days", await _get_default_retention_days()),
    )
    record_quality = (
        body.get("recordQuality")
        or (existing or {}).get("record_quality")
        or await _get_default_record_quality()
    )
    auto_record = bool(body.get("autoRecord", (existing or {}).get("auto_record", False)))
    if "recordPath" in body or "record_path" in body:
        record_path = _normalize_record_path(
            body.get("recordPath") or body.get("record_path"),
            username,
        )
    else:
        record_path = (existing or {}).get("record_path")

    await db.add_or_update_model(
        username=username,
        display_name=profile_data["display_name"] or username,
        auto_record=auto_record,
        record_quality=record_quality,
        retention_days=retention_days,
        record_path=record_path,
        source_type=requested_source,
    )

    normalized_source = await _normalize_profile_source_payload(
        username,
        {
            "sourceType": requested_source,
            "channelUsername": username,
            "autoRecord": auto_record,
            "recordQuality": record_quality,
            "retentionDays": retention_days,
            "recordPath": record_path or _default_record_path(username),
        },
        default_auto_record=auto_record,
    )
    await db.upsert_media_profile_source(**normalized_source)

    return {
        "success": True,
        "profile": await _media_profile_payload(username),
    }


@app.post("/api/media-profiles/link-live")
async def link_live_to_media_profile(body: dict):
    """Lie le live courant à un profil Media existant ou nouvellement créé."""
    body = body or {}
    live_username = _normalize_live_channel_username(
        body.get("liveUsername")
        or body.get("live_username")
        or body.get("channelUsername")
        or body.get("channel_username")
        or body.get("target"),
        body.get("channelUrl") or body.get("channel_url") or body.get("url"),
    )
    source_type = _normalize_source_type(
        body.get("sourceType")
        or body.get("source_type")
        or _source_type_from_url(str(body.get("channelUrl") or body.get("channel_url") or ""))
        or "chaturbate"
    ) or "chaturbate"
    if source_type not in _available_source_types():
        raise HTTPException(status_code=400, detail=f"Source '{source_type}' is not available")

    create_profile = bool(body.get("createProfile") or body.get("create_profile"))
    profile_username = _validate_media_profile_username(
        body.get("profileUsername")
        or body.get("profile_username")
        or (live_username if create_profile else "")
    )
    existing_profile = await db.get_media_profile(profile_username)
    profile_dir = _media_profile_dir(profile_username)
    if not create_profile and not existing_profile and not profile_dir.exists():
        raise HTTPException(status_code=404, detail="Profile not found")

    display_name = (
        body.get("displayName")
        or body.get("display_name")
        or (existing_profile or {}).get("display_name")
        or profile_username
    )
    await db.upsert_media_profile(profile_username, {
        **(existing_profile or {}),
        "display_name": display_name,
        "first_name": (existing_profile or {}).get("first_name"),
        "last_name": (existing_profile or {}).get("last_name"),
        "age": (existing_profile or {}).get("age"),
        "birth_date": (existing_profile or {}).get("birth_date"),
        "address": (existing_profile or {}).get("address"),
        "city": (existing_profile or {}).get("city"),
        "region": (existing_profile or {}).get("region"),
        "postal_code": (existing_profile or {}).get("postal_code"),
        "country": (existing_profile or {}).get("country"),
        "aliases": (existing_profile or {}).get("aliases"),
        "tags": (existing_profile or {}).get("tags"),
        "notes": (existing_profile or {}).get("notes"),
        "social_urls": (existing_profile or {}).get("social_urls") or [],
        "stream_urls": list(dict.fromkeys([
            *((existing_profile or {}).get("stream_urls") or []),
            _canonical_stream_url(source_type, live_username, body.get("channelUrl") or body.get("channel_url")),
        ])),
        "profile_urls": (existing_profile or {}).get("profile_urls") or [],
        "profile_image_url": (existing_profile or {}).get("profile_image_url"),
        "profile_image_source_url": (existing_profile or {}).get("profile_image_source_url"),
        "profile_image_path": (existing_profile or {}).get("profile_image_path"),
    })

    source_payload = await _normalize_profile_source_payload(
        profile_username,
        {
            "sourceType": source_type,
            "channelUsername": live_username,
            "channelUrl": body.get("channelUrl") or body.get("channel_url") or _canonical_stream_url(source_type, live_username),
            "autoRecord": body.get("autoRecord", body.get("auto_record", True)),
            "recordQuality": body.get("recordQuality") or body.get("record_quality") or await _get_default_record_quality(),
            "retentionDays": body.get("retentionDays") if "retentionDays" in body else body.get("retention_days", await _get_default_retention_days()),
            "recordPath": body.get("recordPath") or body.get("record_path") or _default_record_path(profile_username),
        },
        default_auto_record=True,
    )
    saved_source = await db.upsert_media_profile_source(**source_payload)
    await db.add_or_update_model(
        username=source_payload["channel_username"],
        display_name=display_name,
        auto_record=source_payload["auto_record"],
        record_quality=source_payload["record_quality"],
        retention_days=source_payload["retention_days"],
        source_type=source_payload["source_type"],
    )

    return {
        "success": True,
        "source": _profile_source_response(saved_source),
        "profile": await _media_profile_payload(profile_username),
    }


def _assert_media_profile_not_active(username: str):
    for session in _all_recording_statuses():
        if session.get("person") == username and session.get("running"):
            raise HTTPException(
                status_code=403,
                detail="Cannot delete a profile while it is recording.",
            )


@app.delete("/api/media-profiles/{username}")
async def delete_media_profile(username: str):
    """Supprime la fiche, les enregistrements DB et le dossier records du profil."""
    username = _validate_media_profile_username(username)
    profile_dir = _media_profile_dir(username)
    _assert_media_profile_not_active(username)

    profile = await db.get_media_profile(username)
    model = await db.get_model(username)
    profile_sources = await db.get_media_profile_sources(username)
    recordings = await db.get_recordings(username)
    folder_exists = profile_dir.exists()
    if not folder_exists and not profile and not model and not recordings and not profile_sources:
        raise HTTPException(status_code=404, detail="Profile not found")
    if folder_exists and not profile_dir.is_dir():
        raise HTTPException(status_code=400, detail="The profile records path is not a folder")

    for rec in recordings:
        recording_id = rec.get("recording_id")
        if recording_id:
            await db.delete_playback_position(recording_id)
    removed_recordings = await db.delete_recordings_for_username(username)
    await db.delete_model(username)
    all_sources = await db.get_all_media_profile_sources()
    sources_used_elsewhere = {
        (source.get("source_type"), source.get("channel_username"))
        for source in all_sources
        if source.get("profile_username") != username
    }
    for source in profile_sources:
        key = (source.get("source_type"), source.get("channel_username"))
        if key not in sources_used_elsewhere and source.get("channel_username"):
            await db.delete_model(
                source["channel_username"],
                source_type=source.get("source_type") or "chaturbate",
            )
    await db.delete_media_profile(username)

    folder_deleted = False
    if folder_exists:
        shutil.rmtree(profile_dir)
        folder_deleted = True

    thumb_dir = OUTPUT_DIR / "thumbnails" / username
    thumbs_deleted = False
    try:
        if thumb_dir.exists() and thumb_dir.is_dir() and _path_is_inside_output(thumb_dir):
            shutil.rmtree(thumb_dir)
            thumbs_deleted = True
    except OSError as e:
        logger.warning("Suppression miniatures profil impossible", username=username, error=str(e))

    profile_image_deleted = False
    image_path = Path((profile or {}).get("profile_image_path") or "")
    try:
        if image_path.exists() and image_path.is_file() and _path_is_inside_output(image_path):
            image_path.unlink()
            profile_image_deleted = True
    except OSError as e:
        logger.warning("Suppression image profil impossible", username=username, error=str(e))

    logger.info(
        "Profil média supprimé",
        username=username,
        folder_deleted=folder_deleted,
        removed_recordings=removed_recordings,
    )
    return {
        "success": True,
        "username": username,
        "folderDeleted": folder_deleted,
        "thumbnailsDeleted": thumbs_deleted,
        "profileImageDeleted": profile_image_deleted,
        "removedRecordings": removed_recordings,
    }


def _resolved_path_key(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return str(Path(value).resolve())
    except Exception:
        return None


def _path_is_inside_output(path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(OUTPUT_DIR.resolve())
    except Exception:
        return False


async def _find_recording_for_media_path(username: str, media_path: Path, relative_path: str) -> Optional[dict]:
    target_key = _resolved_path_key(str(media_path))
    recordings = await db.get_recordings(username)
    for rec in recordings:
        for key in ("file_path", "mp4_path", "playable_path"):
            if target_key and _resolved_path_key(rec.get(key)) == target_key:
                return rec
        if len(Path(relative_path).parts) == 1 and rec.get("filename") == media_path.name:
            return rec
    return None


def _assert_media_not_active(username: str, media_path: Path):
    target_key = _resolved_path_key(str(media_path))
    for session in _all_recording_statuses():
        if not (session.get("person") == username and session.get("running")):
            continue

        record_path = session.get("record_path") or ""
        active_key = _resolved_path_key(record_path)
        if active_key and target_key and active_key == target_key:
            raise HTTPException(
                status_code=403,
                detail="Cannot delete media while it is recording.",
            )
        if record_path and Path(record_path).stem == media_path.stem:
            raise HTTPException(
                status_code=403,
                detail="Cannot delete media while it is recording.",
            )


async def _delete_media_library_record(username: str, media_path: Path, relative_path: str) -> dict:
    rec = await _find_recording_for_media_path(username, media_path, relative_path)
    if rec and rec.get("media_kind") == "import":
        deleted = await remove_import_record(
            db,
            rec,
            reason="media_library_delete",
            delete_original=True,
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="Media not found")
        return {
            "success": True,
            "message": "Media deleted",
            "deletedFiles": [media_path.name],
            "removedRecord": True,
        }

    paths_to_delete: list[Path] = [media_path]
    removed_record = False
    recording_id = None

    if rec:
        removed_record = True
        recording_id = rec.get("recording_id")
        for key in ("file_path", "mp4_path", "playable_path", "thumbnail_path"):
            value = rec.get(key)
            if value:
                paths_to_delete.append(Path(value))

    generated_thumb = OUTPUT_DIR / "thumbnails" / username / f"{media_path.stem}.jpg"
    paths_to_delete.append(generated_thumb)

    deleted_files = []
    seen = set()
    for path in paths_to_delete:
        path_key = _resolved_path_key(str(path))
        if not path_key or path_key in seen:
            continue
        seen.add(path_key)
        if not _path_is_inside_output(path):
            continue
        try:
            if path.exists() and path.is_file():
                path.unlink()
                deleted_files.append(path.name)
        except OSError as e:
            logger.warning("Suppression média impossible", path=str(path), error=str(e))

    if rec:
        if recording_id:
            await db.delete_recording_by_id(recording_id)
            await db.delete_playback_position(recording_id)
        else:
            await db.delete_recording(username, rec.get("filename") or media_path.name)

    if not deleted_files and not removed_record:
        raise HTTPException(status_code=404, detail="Media not found")

    return {
        "success": True,
        "message": "Media deleted",
        "deletedFiles": deleted_files,
        "removedRecord": removed_record,
    }


@app.delete("/api/media-library/{username}/{file_path:path}")
async def delete_media_library_item(username: str, file_path: str):
    """Supprime un média depuis la bibliothèque locale records."""
    media_path = _resolve_library_media_path(username, file_path)
    if media_path.suffix.lower() == ".ts":
        raise HTTPException(status_code=400, detail="TS files are not supported in Media")
    if not media_path.exists() or not media_path.is_file():
        raise HTTPException(status_code=404, detail="Media not found")

    _assert_media_not_active(username, media_path)
    result = await _delete_media_library_record(username, media_path, file_path)
    logger.info(
        "Média bibliothèque supprimé",
        username=username,
        file=file_path,
        deleted_files=result.get("deletedFiles", []),
        removed_record=result.get("removedRecord"),
    )
    return result


@app.get("/api/all-recordings")
async def get_all_recordings(
    page: int = 1,
    limit: int = 20,
    username: str = None,
    show_ts: bool = False
):
    """Get all recordings across all models with pagination"""
    from .core.utils import format_bytes

    result = await db.get_all_recordings_paginated(
        page=page,
        limit=limit,
        username_filter=username,
        show_ts=show_ts
    )

    recordings = []
    for rec in result["recordings"]:
        rec_username = rec.get("username", "")
        if rec.get("media_kind") == "import":
            formatted_import = _format_import_recording(rec, rec_username)
            if formatted_import:
                formatted_import["username"] = rec_username
                recordings.append(formatted_import)
            continue

        is_converted = bool(rec.get("is_converted"))
        mp4_raw = rec.get("mp4_path")
        ts_raw = rec.get("file_path")

        # Determine the playable file: prefer MP4, fall back to TS
        if is_converted and mp4_raw and Path(mp4_raw).exists():
            serve_file = Path(mp4_raw)
            file_size = rec.get("mp4_size") or serve_file.stat().st_size
        elif ts_raw and Path(ts_raw).exists():
            # Skip TS files unless show_ts is enabled
            if not show_ts:
                continue
            serve_file = Path(ts_raw)
            file_size = rec.get("file_size") or serve_file.stat().st_size
        else:
            continue

        file_stem = serve_file.stem

        # Format duration
        duration_seconds = rec.get("duration_seconds", 0)
        if (
            (duration_seconds and duration_seconds < MIN_RECORDING_SECONDS)
            or (not duration_seconds and file_size < MIN_RECORDING_BYTES)
        ):
            continue

        duration_str = _format_duration_label(duration_seconds)

        # Thumbnail
        thumb_path = OUTPUT_DIR / "thumbnails" / rec_username / f"{file_stem}.jpg"

        recording_id = rec.get("recording_id", file_stem)
        recording_url = f"/streams/recordings/{quote(recording_id, safe='')}"

        recordings.append({
            "recordingId": recording_id,
            "username": rec_username,
            "filename": serve_file.name,
            "date": file_stem,
            "size": file_size,
            "size_formatted": format_bytes(file_size),
            "duration": duration_seconds,
            "duration_str": duration_str,
            "url": recording_url,
            "downloadUrl": recording_url,
            "thumbnail": f"/api/recording-thumbnail/{rec_username}/{file_stem}.jpg" if thumb_path.exists() else None,
            "createdAt": rec.get("created_at"),
            "isImported": False,
            "mediaKind": "recording",
            "importStatus": None,
            "playable": True,
        })

    # Get distinct usernames for filter dropdown
    usernames = await db.get_distinct_recording_usernames()

    return {
        "recordings": recordings,
        "total": result["total"],
        "totalSize": result["total_size"],
        "totalSizeFormatted": format_bytes(result["total_size"]),
        "page": result["page"],
        "limit": result["limit"],
        "totalPages": result["total_pages"],
        "usernames": usernames,
    }


@app.get("/api/recording-thumbnail/{username}/{filename}")
async def get_recording_thumbnail(username: str, filename: str):
    """Récupère la miniature d'un enregistrement"""
    from fastapi.responses import FileResponse, Response
    
    # Sécurité
    if ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="Nom invalide")
    
    thumb_path = OUTPUT_DIR / "thumbnails" / username / filename
    
    if thumb_path.exists():
        return FileResponse(
            path=str(thumb_path),
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"}
        )
    
    # Placeholder SVG si pas de miniature
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180">
        <rect fill="#1a1f3a" width="320" height="180"/>
        <text x="50%" y="50%" dominant-baseline="middle" text-anchor="middle" fill="#a0aec0" font-size="16">📹 Génération...</text>
    </svg>'''
    
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/api/models")
async def get_models():
    """Récupère la liste des modèles depuis SQLite.

    Jamais 500 : en cas d'erreur transitoire (ex : verrou SQLite pendant une
    écriture par un background task), on renvoie une liste vide pour éviter de
    casser l'affichage côté front. Le prochain fetch récupérera l'état correct.
    """
    try:
        models = await db.get_all_models()
        formatted_models = []
        for model in models:
            source_type = await _infer_source_type(model.get("username"), model)
            formatted_models.append({
                "username": model['username'],
                "autoRecord": bool(model.get('auto_record', True)),
                "recordQuality": model.get('record_quality', 'best'),
                "retentionDays": model.get('retention_days', 30),
                "sourceType": source_type,
                "source_type": source_type,
                **_record_path_fields(model['username'], model),
            })
        return {"models": formatted_models}
    except Exception as e:
        logger.error("Erreur /api/models", error=str(e), exc_info=True)
        return {"models": [], "error": "transient"}


@app.get("/api/models/{username}/volume")
async def get_model_volume(username: str):
    """Return the saved playback volume for one profile."""
    username = (username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username requis")

    return {
        "username": username,
        "volume": await db.get_model_volume(username),
    }


@app.put("/api/models/{username}/volume")
async def update_model_volume(username: str, body: ModelVolumeBody):
    """Persist the playback volume for one profile."""
    username = (username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username requis")

    volume = float(body.volume)
    if not 0 <= volume <= 1:
        raise HTTPException(status_code=400, detail="Volume must be between 0 and 1")

    await db.set_model_volume(username, volume)
    return {
        "success": True,
        "username": username,
        "volume": volume,
    }


@app.post("/api/models")
async def add_model(model: dict):
    """Ajoute un modèle dans SQLite"""
    raw_username = str(model.get('username') or "").strip()
    source_from_url = _source_type_from_url(raw_username)
    username = (
        _normalize_live_channel_username(raw_username, raw_username)
        if raw_username.startswith(("http://", "https://"))
        else raw_username
    )
    if not username:
        raise HTTPException(status_code=400, detail="Username requis")

    requested_source = _normalize_source_type(
        model.get("sourceType") or model.get("source_type") or source_from_url
    )
    source_type = requested_source or await _infer_source_type(username)
    if source_type not in _available_source_types():
        raise HTTPException(
            status_code=400,
            detail=f"Source '{source_type}' non disponible",
        )

    # Vérifier si le modèle existe déjà
    existing = await db.get_model(username, source_type=source_type)
    if existing:
        raise HTTPException(status_code=409, detail="Modèle déjà existant")

    auto_record = bool(model.get('autoRecord', True))
    # When auto-record is enabled and the caller did not pin a per-model
    # resolution, fall back to the global default. Otherwise keep "best".
    if 'recordQuality' in model and model.get('recordQuality') is not None:
        record_quality = model['recordQuality']
    elif auto_record:
        record_quality = await _get_default_record_quality()
    else:
        record_quality = 'best'
    if "retentionDays" in model and model.get("retentionDays") is not None:
        retention_days = _normalize_retention_days(model.get("retentionDays"))
    else:
        retention_days = await _get_default_retention_days()
    record_path = _normalize_record_path(
        model.get("recordPath") or model.get("record_path"),
        username,
    )

    # Ajouter dans SQLite
    await db.add_or_update_model(
        username=username,
        auto_record=auto_record,
        record_quality=record_quality,
        retention_days=retention_days,
        record_path=record_path,
        source_type=source_type,
    )
    
    # Récupérer tous les modèles pour retourner
    all_models = await db.get_all_models()
    formatted = [{
        "username": m['username'],
        "autoRecord": bool(m.get('auto_record', True)),
        "recordQuality": m.get('record_quality', 'best'),
        "retentionDays": m.get('retention_days', 30),
        "sourceType": m.get('source_type') or 'chaturbate',
        "source_type": m.get('source_type') or 'chaturbate',
        **_record_path_fields(m['username'], m),
    } for m in all_models]
    
    return {"success": True, "models": formatted}


@app.put("/api/models/{username}")
async def update_model(username: str, model_data: dict):
    """Met à jour les paramètres d'un modèle dans SQLite"""
    requested_source = _normalize_source_type(
        model_data.get('sourceType') or model_data.get('source_type')
    )
    if requested_source and requested_source not in _available_source_types():
        raise HTTPException(
            status_code=400,
            detail=f"Source '{requested_source}' non disponible",
        )

    # Vérifier si le modèle existe
    existing = await db.get_model(username, source_type=requested_source)
    if not existing:
        raise HTTPException(status_code=404, detail="Modèle introuvable")
    
    if "retentionDays" in model_data and model_data.get("retentionDays") is not None:
        retention_days = _normalize_retention_days(
            model_data.get("retentionDays"),
            existing.get("retention_days", 30),
        )
    else:
        retention_days = existing.get("retention_days", 30)

    source_type = requested_source or existing.get("source_type") or "chaturbate"
    if "recordPath" in model_data or "record_path" in model_data:
        record_path = _normalize_record_path(
            model_data.get("recordPath") or model_data.get("record_path"),
            username,
        )
    else:
        record_path = existing.get("record_path")

    # Mettre à jour dans SQLite
    await db.add_or_update_model(
        username=username,
        auto_record=model_data.get('autoRecord', existing.get('auto_record', True)),
        record_quality=model_data.get('recordQuality', existing.get('record_quality', 'best')),
        retention_days=retention_days,
        record_path=record_path,
        source_type=source_type,
    )
    
    # Récupérer le modèle mis à jour
    updated = await db.get_model(username, source_type=source_type)
    
    return {
        "success": True,
        "model": {
            "username": updated['username'],
            "autoRecord": bool(updated.get('auto_record', True)),
            "recordQuality": updated.get('record_quality', 'best'),
            "retentionDays": updated.get('retention_days', 30),
            "sourceType": updated.get('source_type') or 'chaturbate',
            "source_type": updated.get('source_type') or 'chaturbate',
            **_record_path_fields(updated['username'], updated),
        }
    }


@app.delete("/api/models/{username}")
async def delete_model(username: str, source: Optional[str] = None):
    """Supprime un modèle de SQLite"""
    source_type = _normalize_source_type(source)
    # Vérifier si le modèle existe
    existing = await db.get_model(username, source_type=source_type)
    if not existing:
        raise HTTPException(status_code=404, detail="Modèle introuvable")
    
    # Supprimer de SQLite
    await db.delete_model(username, source_type=source_type)
    
    # Récupérer la liste mise à jour
    all_models = await db.get_all_models()
    formatted = [{
        "username": m['username'],
        "autoRecord": bool(m.get('auto_record', True)),
        "recordQuality": m.get('record_quality', 'best'),
        "retentionDays": m.get('retention_days', 30),
        "sourceType": m.get('source_type') or 'chaturbate',
        "source_type": m.get('source_type') or 'chaturbate',
        **_record_path_fields(m['username'], m),
    } for m in all_models]
    
    return {"success": True, "models": formatted}


@app.delete("/api/recordings/{username}/{filename}")
async def delete_recording(username: str, filename: str):
    """Supprime un enregistrement (TS + MP4 + miniature + DB)"""
    from fastapi.responses import Response
    from datetime import datetime
    
    # Sécurité
    if ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="Nom invalide")

    existing_recs = await db.get_recordings(username)
    matching_rec = next((
        r for r in existing_recs
        if r.get("filename") == filename
        or Path(r.get("file_path") or "").name == filename
        or Path(r.get("mp4_path") or "").name == filename
        or Path(r.get("playable_path") or "").name == filename
    ), None)
    if matching_rec and matching_rec.get("media_kind") == "import":
        deleted = await remove_import_record(
            db,
            matching_rec,
            reason="user_delete",
            delete_original=True,
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="Média importé introuvable")
        return {
            "success": True,
            "message": "Média importé supprimé",
            "deleted_files": ["Import"],
        }

    allowed_extensions = {".ts", ".mp4"} | SUPPORTED_VIDEO_EXTENSIONS
    if Path(filename).suffix.lower() not in allowed_extensions:
        raise HTTPException(status_code=400, detail="Format invalide")
    
    # Vérifier que ce n'est pas l'enregistrement en cours
    file_stem = Path(filename).stem
    
    paths_to_delete: list[tuple[Path, str]] = []
    if matching_rec:
        for key, label in (
            ("file_path", "TS"),
            ("mp4_path", "MP4"),
            ("playable_path", "Fichier"),
            ("thumbnail_path", "Miniature"),
        ):
            value = matching_rec.get(key)
            if value:
                paths_to_delete.append((Path(value), label))
        selected = _select_recording_path(matching_rec, filename)
        if selected:
            _assert_recording_not_active(username, selected)
            paths_to_delete.append((selected.with_suffix(".ts"), "TS"))
            paths_to_delete.append((selected.with_suffix(".mp4"), "MP4"))
            paths_to_delete.append((OUTPUT_DIR / "thumbnails" / username / f"{selected.stem}.jpg", "Miniature"))
    else:
        records_dir = OUTPUT_DIR / "records" / username
        paths_to_delete.extend([
            (records_dir / f"{file_stem}.ts", "TS"),
            (records_dir / f"{file_stem}.mp4", "MP4"),
            (records_dir / filename, Path(filename).suffix.upper().lstrip(".") or "Fichier"),
            (OUTPUT_DIR / "thumbnails" / username / f"{file_stem}.jpg", "Miniature"),
        ])
        _assert_recording_not_active(username, records_dir / filename)

    # Si les fichiers ont déjà disparu (cleanup externe, volume remonté, etc.)
    # on doit quand même pouvoir nettoyer la row DB orpheline — sinon elle
    # reste affichée dans /recordings sans jamais pouvoir être retirée.
    has_db_row = any(Path(r['filename']).stem == file_stem for r in existing_recs)

    if not any(path.exists() for path, _ in paths_to_delete) and not has_db_row:
        raise HTTPException(status_code=404, detail="Enregistrement introuvable")

    # Supprimer tous les fichiers associés
    try:
        files_deleted = []

        seen_paths: set[str] = set()
        for path, label in paths_to_delete:
            key = _resolved_path_key(str(path))
            if not key or key in seen_paths:
                continue
            seen_paths.add(key)
            if not _path_is_inside_output(path):
                logger.warning("Suppression recording hors volume ignorée", username=username, path=str(path))
                continue
            if path.exists() and path.is_file():
                path.unlink()
                files_deleted.append(label)
                logger.info("Fichier recording supprimé", username=username, file=path.name, label=label)

        # Supprimer de la base de données
        await db.delete_recording(username, filename)
        if matching_rec and matching_rec.get("filename") != filename:
            await db.delete_recording(username, matching_rec.get("filename"))
        if filename != f"{file_stem}.ts":
            await db.delete_recording(username, f"{file_stem}.ts")
        logger.info("Enregistrement supprimé de la DB", username=username, filename=filename)

        if not files_deleted and has_db_row:
            files_deleted.append("DB (row orpheline)")

        return {
            "success": True,
            "message": f"Supprimé: {', '.join(files_deleted)}",
            "deleted_files": files_deleted
        }
    except Exception as e:
        logger.error("Erreur suppression enregistrement", 
                    username=username, 
                    filename=filename,
                    error=str(e),
                    exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# System Statistics Endpoint
# ============================================

@app.get("/api/system/stats")
async def get_system_stats():
    """Get comprehensive system statistics"""
    import psutil
    import shutil

    # --- Disk Usage ---
    output_path = str(OUTPUT_DIR)
    disk = shutil.disk_usage(output_path)
    disk_info = {
        "total": disk.total,
        "used": disk.used,
        "free": disk.free,
        "percent": round((disk.used / disk.total) * 100, 1),
    }

    # --- CPU ---
    cpu_info = {
        "cores_physical": psutil.cpu_count(logical=False) or 0,
        "cores_logical": psutil.cpu_count(logical=True) or 0,
        "usage_percent": psutil.cpu_percent(interval=0.5),
        "per_core": psutil.cpu_percent(interval=0, percpu=True),
        "frequency": None,
    }
    freq = psutil.cpu_freq()
    if freq:
        cpu_info["frequency"] = {
            "current": round(freq.current, 0),
            "max": round(freq.max, 0) if freq.max else None,
        }

    # --- RAM ---
    mem = psutil.virtual_memory()
    ram_info = {
        "total": mem.total,
        "used": mem.used,
        "available": mem.available,
        "percent": mem.percent,
    }

    # --- Current Process ---
    process = psutil.Process()
    proc_mem = process.memory_info()
    process_info = {
        "pid": process.pid,
        "cpu_percent": process.cpu_percent(interval=0.1),
        "memory_rss": proc_mem.rss,
        "memory_vms": proc_mem.vms,
        "threads": process.num_threads(),
        "open_files": len(process.open_files()),
        "connections": len(process.connections()) if hasattr(process, 'connections') else len(process.net_connections()),
        "uptime_seconds": time.time() - process.create_time(),
    }

    # --- Child Processes (ffmpeg, etc.) ---
    children = []
    for child in process.children(recursive=True):
        try:
            child_mem = child.memory_info()
            children.append({
                "pid": child.pid,
                "name": child.name(),
                "cmdline": " ".join(child.cmdline()[:3]) if child.cmdline() else child.name(),
                "cpu_percent": child.cpu_percent(interval=0),
                "memory_rss": child_mem.rss,
                "status": child.status(),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # --- Recording Storage Breakdown ---
    records_dir = OUTPUT_DIR / "records"
    storage_breakdown = {
        "ts_files": {"count": 0, "size": 0},
        "mp4_files": {"count": 0, "size": 0},
        "other_files": {"count": 0, "size": 0},
        "thumbnails": {"count": 0, "size": 0},
        "total_recordings_size": 0,
        "by_model": [],
    }

    if records_dir.exists():
        model_stats = {}
        for model_dir in records_dir.iterdir():
            if not model_dir.is_dir():
                continue
            username = model_dir.name
            model_stat = {"username": username, "ts_size": 0, "mp4_size": 0, "other_size": 0, "ts_count": 0, "mp4_count": 0}
            for f in model_dir.iterdir():
                if not f.is_file():
                    continue
                fsize = f.stat().st_size
                ext = f.suffix.lower()
                if ext == ".ts":
                    storage_breakdown["ts_files"]["count"] += 1
                    storage_breakdown["ts_files"]["size"] += fsize
                    model_stat["ts_size"] += fsize
                    model_stat["ts_count"] += 1
                elif ext == ".mp4":
                    storage_breakdown["mp4_files"]["count"] += 1
                    storage_breakdown["mp4_files"]["size"] += fsize
                    model_stat["mp4_size"] += fsize
                    model_stat["mp4_count"] += 1
                else:
                    storage_breakdown["other_files"]["count"] += 1
                    storage_breakdown["other_files"]["size"] += fsize
                    model_stat["other_size"] += fsize
            model_stat["total_size"] = model_stat["ts_size"] + model_stat["mp4_size"] + model_stat["other_size"]
            model_stats[username] = model_stat

        # Sort models by total size descending
        storage_breakdown["by_model"] = sorted(model_stats.values(), key=lambda x: x["total_size"], reverse=True)[:20]
        storage_breakdown["total_recordings_size"] = (
            storage_breakdown["ts_files"]["size"]
            + storage_breakdown["mp4_files"]["size"]
            + storage_breakdown["other_files"]["size"]
        )

    # Thumbnails
    thumbs_dir = OUTPUT_DIR / "thumbnails"
    if thumbs_dir.exists():
        for f in thumbs_dir.rglob("*"):
            if f.is_file():
                storage_breakdown["thumbnails"]["count"] += 1
                storage_breakdown["thumbnails"]["size"] += f.stat().st_size

    # --- Active Sessions ---
    active_sessions = manager.list_status()
    sessions_info = {
        "active_count": sum(1 for s in active_sessions if s.get("running")),
        "total_count": len(active_sessions),
        "sessions": [],
    }
    for s in active_sessions:
        if s.get("running"):
            sessions_info["sessions"].append({
                "person": s.get("person", "unknown"),
                "duration_seconds": s.get("duration", 0),
                "file_size": s.get("file_size", 0),
            })

    # --- Network I/O ---
    net = psutil.net_io_counters()
    network_info = {
        "bytes_sent": net.bytes_sent,
        "bytes_recv": net.bytes_recv,
        "packets_sent": net.packets_sent,
        "packets_recv": net.packets_recv,
    }

    # --- Disk I/O ---
    try:
        disk_io = psutil.disk_io_counters()
        disk_io_info = {
            "read_bytes": disk_io.read_bytes if disk_io else 0,
            "write_bytes": disk_io.write_bytes if disk_io else 0,
            "read_count": disk_io.read_count if disk_io else 0,
            "write_count": disk_io.write_count if disk_io else 0,
        }
    except Exception:
        disk_io_info = {"read_bytes": 0, "write_bytes": 0, "read_count": 0, "write_count": 0}

    return {
        "disk": disk_info,
        "cpu": cpu_info,
        "ram": ram_info,
        "process": process_info,
        "children": children,
        "storage": storage_breakdown,
        "sessions": sessions_info,
        "network": network_info,
        "disk_io": disk_io_info,
    }


# ============================================
# Update System Endpoints
# ============================================

@app.get("/api/system/check-update")
async def check_for_update():
    """Check GitHub for the latest release and compare with current version."""
    current_version = os.getenv("APP_VERSION", "dev")
    docker_available = os.path.exists(DOCKER_SOCKET)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.github.com/repos/raccommode/P-StreamRec/releases/latest",
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"Accept": "application/vnd.github.v3+json"},
            ) as resp:
                status_code = resp.status
                release = await resp.json(content_type=None) if status_code == 200 else None
        if status_code == 200 and release:
            latest_version = release.get("tag_name", "").lstrip("v")
            update_available = _is_update_available(current_version, latest_version)
            return {
                "current_version": current_version,
                "latest_version": latest_version,
                "update_available": update_available,
                "release_url": release.get("html_url", ""),
                "release_notes": release.get("body", ""),
                "published_at": release.get("published_at", ""),
                "docker_available": docker_available,
            }
        return {
            "current_version": current_version,
            "latest_version": None,
            "update_available": False,
            "error": f"GitHub API returned {status_code}",
            "docker_available": docker_available,
        }
    except Exception as e:
        return {
            "current_version": current_version,
            "latest_version": None,
            "update_available": False,
            "error": str(e),
            "docker_available": docker_available,
        }


@app.post("/api/system/update")
async def perform_system_update():
    """Pull latest Docker image and recreate the container via docker compose."""
    if not os.path.exists(DOCKER_SOCKET):
        return {
            "success": False,
            "error": "docker_socket_unavailable",
            "message": "Docker socket not available. Add this volume to your docker-compose.yml to enable automatic updates: /var/run/docker.sock:/var/run/docker.sock",
            "manual_commands": "docker compose pull && docker compose up -d",
        }

    container_id = _get_container_id()
    if not container_id:
        return {
            "success": False,
            "error": "container_id_unknown",
            "message": "Cannot determine container ID.",
            "manual_commands": "docker compose pull && docker compose up -d",
        }

    try:
        # 1. Inspect current container to get compose project info
        status, inspect_data = _docker_api('GET', f'/containers/{container_id}/json')
        if status != 200:
            return {"success": False, "error": "inspect_failed", "message": "Cannot inspect current container"}

        container_config = json.loads(inspect_data)
        labels = container_config.get("Config", {}).get("Labels", {})

        compose_working_dir = labels.get("com.docker.compose.project.working_dir", "")
        compose_service = labels.get("com.docker.compose.service", "")

        if not compose_working_dir or not compose_service:
            return {
                "success": False,
                "error": "not_compose",
                "message": "Container was not started via Docker Compose.",
                "manual_commands": "docker compose pull && docker compose up -d",
            }

        # 2. Pull docker:cli image for the updater container
        logger.info("Update: pulling docker:cli for updater")
        _docker_api('POST', '/images/create?fromImage=docker&tag=cli', timeout=120)

        # 3. Build updater script using docker compose (preserves the stack)
        updater_script = (
            f"sleep 2\n"
            f"echo '[P-StreamRec Updater] Pulling latest image via compose...'\n"
            f"docker compose -f /compose-project/docker-compose.yml pull {compose_service}\n"
            f"echo '[P-StreamRec Updater] Recreating container via compose...'\n"
            f"docker compose -f /compose-project/docker-compose.yml up -d --no-deps {compose_service}\n"
            f"echo '[P-StreamRec Updater] Update complete!'\n"
        )

        # 4. Create the updater container
        _docker_api('DELETE', '/containers/p-streamrec-updater?force=true')
        updater_body = {
            "Image": "docker:cli",
            "Cmd": ["sh", "-c", updater_script],
            "HostConfig": {
                "Binds": [
                    "/var/run/docker.sock:/var/run/docker.sock",
                    f"{compose_working_dir}:/compose-project",
                ],
                "AutoRemove": True,
            },
        }
        status, create_data = _docker_api('POST', '/containers/create?name=p-streamrec-updater', body=updater_body)
        if status not in (200, 201):
            return {
                "success": False,
                "error": "updater_create_failed",
                "message": f"Failed to create updater container (HTTP {status})",
                "manual_commands": "docker compose pull && docker compose up -d",
            }

        updater_id = json.loads(create_data).get("Id", "")

        # 5. Start the updater — it will pull + recreate via compose in ~5 seconds
        status, _ = _docker_api('POST', f'/containers/{updater_id}/start')
        if status not in (200, 204):
            return {
                "success": False,
                "error": "updater_start_failed",
                "message": f"Failed to start updater (HTTP {status})",
                "manual_commands": "docker compose pull && docker compose up -d",
            }

        logger.info("Update: updater started, compose will recreate container in ~5 seconds")
        return {
            "success": True,
            "message": "Update in progress. The application will restart in a few seconds.",
        }

    except Exception as e:
        logger.error("Update failed", error=str(e), exc_info=True)
        return {
            "success": False,
            "error": "exception",
            "message": str(e),
            "manual_commands": "docker compose pull && docker compose up -d",
        }


# ============================================
# Settings / Blacklisted Tags Endpoints
# ============================================

FLARE_SERVICE_URL_SETTING_KEY = "flaresolverr_url"


def _normalize_flaresolverr_url(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="FlareSolverr URL is required")

    parsed = urlparse(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="FlareSolverr URL must be an HTTP URL")
    if parsed.query or parsed.fragment:
        raise HTTPException(status_code=400, detail="FlareSolverr URL must not include query or fragment")

    path = parsed.path.rstrip("/")
    return urlunparse((
        parsed.scheme.lower(),
        parsed.netloc,
        path,
        "",
        "",
        "",
    )).rstrip("/")


async def _get_flaresolverr_url() -> str:
    raw = await db.get_setting(FLARE_SERVICE_URL_SETTING_KEY)
    if raw:
        try:
            return _normalize_flaresolverr_url(raw)
        except HTTPException:
            pass
    return _normalize_flaresolverr_url(DEFAULT_FLARE_SERVICE_URL)


def _apply_flaresolverr_url(url: str, client: Optional[FlareSolverrClient] = None) -> FlareSolverrClient:
    global flaresolverr_client
    if client is not None:
        client.set_base_url(url)
        flaresolverr_client = client
    elif flaresolverr_client is None:
        flaresolverr_client = FlareSolverrClient(url)
    else:
        flaresolverr_client.set_base_url(url)

    auth_router.set_flaresolverr(flaresolverr_client)
    if chaturbate_api:
        chaturbate_api.flaresolverr = flaresolverr_client
    return flaresolverr_client


@app.get("/api/settings/flaresolverr")
async def get_flaresolverr_settings():
    url = await _get_flaresolverr_url()
    return {"url": url, "flaresolverrUrl": url}


@app.put("/api/settings/flaresolverr")
async def update_flaresolverr_settings(body: dict):
    url = _normalize_flaresolverr_url(
        body.get("url", body.get("flaresolverrUrl"))
    )
    await db.set_setting(FLARE_SERVICE_URL_SETTING_KEY, url)
    _apply_flaresolverr_url(url)
    return {"success": True, "url": url, "flaresolverrUrl": url}


@app.get("/api/settings/blacklisted-tags")
async def get_blacklisted_tags():
    """Get the list of blacklisted tags"""
    tags = await db.get_blacklisted_tags()
    return {"tags": tags}


@app.post("/api/settings/blacklisted-tags")
async def set_blacklisted_tags(body: dict):
    """Set the list of blacklisted tags"""
    tags = body.get("tags", [])
    if not isinstance(tags, list):
        raise HTTPException(status_code=400, detail="tags must be a list")
    # Normalize: lowercase, strip, deduplicate
    tags = list(set(t.strip().lower() for t in tags if t.strip()))
    await db.set_blacklisted_tags(tags)
    return {"tags": tags}


# ============================================
# Recording Settings Endpoints
# ============================================

@app.get("/api/settings/recording")
async def get_recording_settings():
    """Get recording settings."""
    from .core.config import AUTO_CONVERT, KEEP_TS

    auto_convert_val = await db.get_setting("auto_convert")
    keep_ts_val = await db.get_setting("keep_ts")
    show_ts_val = await db.get_setting("show_ts_files")
    auto_delete_val = await db.get_setting("auto_delete_watched")
    auto_delete_threshold_val = await db.get_setting("auto_delete_threshold")

    # Fall back to env var defaults if not set in DB
    if auto_convert_val is not None:
        auto_convert = auto_convert_val.lower() in {"1", "true", "yes"}
    else:
        auto_convert = AUTO_CONVERT

    if keep_ts_val is not None:
        keep_ts = keep_ts_val.lower() in {"1", "true", "yes"}
    else:
        keep_ts = KEEP_TS

    show_ts_files = show_ts_val is not None and show_ts_val.lower() in {"1", "true", "yes"}
    auto_delete_watched = auto_delete_val is not None and auto_delete_val.lower() in {"1", "true", "yes"}

    auto_delete_threshold = _normalize_watched_threshold(
        auto_delete_threshold_val if auto_delete_threshold_val is not None else 90
    )

    # Max recording resolution (0 = best available)
    max_res_val = await db.get_setting("max_resolution")
    try:
        max_resolution = int(max_res_val) if max_res_val is not None else 0
    except (ValueError, TypeError):
        max_resolution = 0

    # Default recording resolution applied to a model when auto-record is
    # turned on (0 = best available).
    default_res_val = await db.get_setting("default_resolution")
    try:
        default_resolution = int(default_res_val) if default_res_val is not None else 0
    except (ValueError, TypeError):
        default_resolution = 0

    default_retention_days = await _get_default_retention_days()
    segment_duration_minutes = await _get_segment_duration_minutes()
    segment_size_mb = await _get_segment_size_mb()
    filename_format = normalize_filename_format(await db.get_setting("filename_format"))
    check_interval_seconds = await get_check_interval_seconds(db)

    return {
        "auto_convert": auto_convert,
        "keep_ts": keep_ts,
        "show_ts_files": show_ts_files,
        "auto_delete_watched": auto_delete_watched,
        "auto_delete_threshold": auto_delete_threshold,
        "max_resolution": max_resolution,
        "default_resolution": default_resolution,
        "default_retention_days": default_retention_days,
        "segment_duration_minutes": segment_duration_minutes,
        "segment_size_mb": segment_size_mb,
        "filename_format": filename_format,
        "check_interval": check_interval_seconds,
        "check_interval_seconds": check_interval_seconds,
        "records_root": str(_records_root()),
    }


# Allowed HLS heights. 0 means "best available".
_ALLOWED_MAX_RESOLUTIONS = {0, 360, 480, 720, 1080, 1440, 2160}
_ALLOWED_SEGMENT_DURATIONS = {0, 30, 60, 90}
_DEFAULT_RETENTION_DAYS = 30
_MAX_RETENTION_DAYS = 365


def _normalize_retention_days(value, default: int = _DEFAULT_RETENTION_DAYS) -> int:
    """Return a valid retention window. 0 means keep forever."""
    try:
        retention_days = int(value)
    except (ValueError, TypeError):
        retention_days = default

    if retention_days < 0:
        raise HTTPException(status_code=400, detail="retentionDays must be 0 or greater")
    if retention_days > _MAX_RETENTION_DAYS:
        raise HTTPException(status_code=400, detail=f"retentionDays must be <= {_MAX_RETENTION_DAYS}")
    return retention_days


async def _get_default_retention_days() -> int:
    raw = await db.get_setting("default_retention_days")
    try:
        return _normalize_retention_days(raw if raw is not None else _DEFAULT_RETENTION_DAYS)
    except HTTPException:
        return _DEFAULT_RETENTION_DAYS


def _normalize_segment_duration_minutes(value, default: int = 0) -> int:
    try:
        duration_minutes = int(value)
    except (ValueError, TypeError):
        duration_minutes = default

    if duration_minutes not in _ALLOWED_SEGMENT_DURATIONS:
        raise HTTPException(
            status_code=400,
            detail=f"segment_duration_minutes must be one of {sorted(_ALLOWED_SEGMENT_DURATIONS)}",
        )
    return duration_minutes


def _normalize_segment_size_mb(value, default: int = 0) -> int:
    try:
        size_mb = int(value)
    except (ValueError, TypeError):
        size_mb = default

    if size_mb < 0:
        raise HTTPException(status_code=400, detail="segment_size_mb must be 0 or greater")
    return size_mb


def _normalize_filename_format_or_400(value) -> str:
    filename_format = str(value or "").strip().lower()
    if filename_format not in ALLOWED_FILENAME_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"filename_format must be one of {sorted(ALLOWED_FILENAME_FORMATS)}",
        )
    return filename_format


async def _get_segment_duration_minutes() -> int:
    from .core.config import RECORD_SEGMENT_DURATION_MINUTES

    raw = await db.get_setting("segment_duration_minutes")
    try:
        return _normalize_segment_duration_minutes(
            raw if raw is not None else RECORD_SEGMENT_DURATION_MINUTES
        )
    except HTTPException:
        return 0


async def _get_segment_size_mb() -> int:
    from .core.config import RECORD_SEGMENT_SIZE_MB

    raw = await db.get_setting("segment_size_mb")
    try:
        return _normalize_segment_size_mb(
            raw if raw is not None else RECORD_SEGMENT_SIZE_MB
        )
    except HTTPException:
        return 0


async def _get_recording_segment_limits() -> tuple[int, int]:
    duration_minutes = await _get_segment_duration_minutes()
    size_mb = await _get_segment_size_mb()
    return duration_minutes * 60, size_mb * 1024 * 1024


async def _get_recording_filename_format() -> str:
    return normalize_filename_format(await db.get_setting("filename_format"))


async def _get_max_recording_height() -> Optional[int]:
    """Return the configured max_resolution as an int, or None for 'best'."""
    try:
        raw = await db.get_setting("max_resolution")
        if raw is None:
            return None
        val = int(raw)
        return val if val > 0 else None
    except (ValueError, TypeError):
        return None


async def _get_default_record_quality() -> str:
    """Return the global default record quality as a string ("best"/"720p"/...).

    Used to populate `record_quality` when a model is enrolled or has
    auto-record turned on without an explicit per-model value.
    """
    try:
        raw = await db.get_setting("default_resolution")
        if raw is None:
            return "best"
        val = int(raw)
        if val <= 0:
            return "best"
        return f"{val}p"
    except (ValueError, TypeError):
        return "best"


def _record_quality_to_height(record_quality: Optional[str]) -> Optional[int]:
    """Map model-level quality settings to an HLS height cap."""
    if not record_quality:
        return None

    value = str(record_quality).strip().lower()
    if value in {"best", "auto", "highest"}:
        return None

    match = re.fullmatch(r"(\d+)\s*p?", value)
    if not match:
        return None

    height = int(match.group(1))
    return height if height in _ALLOWED_MAX_RESOLUTIONS and height > 0 else None


async def _get_recording_height_for_quality(
    record_quality: Optional[str],
) -> Optional[int]:
    """Combine per-model quality with the global max-resolution cap."""
    global_height = await _get_max_recording_height()
    quality_height = _record_quality_to_height(record_quality)

    if global_height and quality_height:
        return min(global_height, quality_height)
    return quality_height or global_height


@app.put("/api/settings/recording")
async def update_recording_settings(body: dict):
    """Update recording settings."""
    applied_retention_models = None
    default_retention_days = None

    if "auto_convert" in body:
        await db.set_setting("auto_convert", str(body["auto_convert"]).lower())
    if "keep_ts" in body:
        await db.set_setting("keep_ts", str(body["keep_ts"]).lower())
    if "show_ts_files" in body:
        await db.set_setting("show_ts_files", str(body["show_ts_files"]).lower())
    if "auto_delete_watched" in body:
        await db.set_setting("auto_delete_watched", str(body["auto_delete_watched"]).lower())
    if "auto_delete_threshold" in body:
        threshold = _normalize_watched_threshold(body["auto_delete_threshold"])
        await db.set_setting("auto_delete_threshold", str(threshold))
    if "max_resolution" in body:
        try:
            max_res = int(body["max_resolution"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="max_resolution must be an integer")
        if max_res not in _ALLOWED_MAX_RESOLUTIONS:
            raise HTTPException(
                status_code=400,
                detail=f"max_resolution must be one of {sorted(_ALLOWED_MAX_RESOLUTIONS)}"
            )
        await db.set_setting("max_resolution", str(max_res))
    if "default_resolution" in body:
        try:
            default_res = int(body["default_resolution"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="default_resolution must be an integer")
        if default_res not in _ALLOWED_MAX_RESOLUTIONS:
            raise HTTPException(
                status_code=400,
                detail=f"default_resolution must be one of {sorted(_ALLOWED_MAX_RESOLUTIONS)}"
            )
        await db.set_setting("default_resolution", str(default_res))
    if "default_retention_days" in body:
        default_retention_days = _normalize_retention_days(body["default_retention_days"])
        await db.set_setting("default_retention_days", str(default_retention_days))
    if "segment_duration_minutes" in body:
        segment_duration_minutes = _normalize_segment_duration_minutes(
            body["segment_duration_minutes"]
        )
        await db.set_setting("segment_duration_minutes", str(segment_duration_minutes))
    if "segment_size_mb" in body:
        segment_size_mb = _normalize_segment_size_mb(body["segment_size_mb"])
        await db.set_setting("segment_size_mb", str(segment_size_mb))
    if "filename_format" in body:
        await db.set_setting(
            "filename_format",
            _normalize_filename_format_or_400(body["filename_format"]),
        )
    if "check_interval_seconds" in body or "check_interval" in body:
        try:
            check_interval_seconds = normalize_check_interval_seconds(
                body.get("check_interval_seconds", body.get("check_interval"))
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        await db.set_setting(CHECK_INTERVAL_SETTING_KEY, str(check_interval_seconds))
    if body.get("apply_default_retention_to_models"):
        if default_retention_days is None:
            default_retention_days = await _get_default_retention_days()
        applied_retention_models = await db.update_all_models_retention_days(default_retention_days)

    # Return current state
    settings = await get_recording_settings()
    if applied_retention_models is not None:
        settings["applied_retention_models"] = applied_retention_models
    return settings


# ============================================
# Follow/Unfollow on Chaturbate
# ============================================

@app.post("/api/chaturbate/follow/{username}")
async def follow_model_on_chaturbate(username: str):
    """Follow a model on Chaturbate"""
    return await provider_follow("chaturbate", username)


@app.post("/api/chaturbate/unfollow/{username}")
async def unfollow_model_on_chaturbate(username: str):
    """Unfollow a model on Chaturbate"""
    return await provider_unfollow("chaturbate", username)


@app.get("/api/chaturbate/is-following/{username}")
async def is_following_model(username: str):
    """Check if following a model on Chaturbate"""
    return await provider_is_following("chaturbate", username)


# ============================================
# Auto-record Toggle
# ============================================

@app.patch("/api/models/{username}/auto-record")
async def toggle_auto_record(username: str, body: dict):
    """Toggle auto-record for a model"""
    requested_source = _normalize_source_type(
        body.get("sourceType") or body.get("source_type")
    )
    if requested_source and requested_source not in _available_source_types():
        raise HTTPException(
            status_code=400,
            detail=f"Source '{requested_source}' non disponible",
        )
    existing = await db.get_model(username, source_type=requested_source)
    if not existing:
        raise HTTPException(status_code=404, detail="Model not found")

    auto_record = body.get("autoRecord")
    if auto_record is None:
        raise HTTPException(status_code=400, detail="autoRecord field required")

    source_type = requested_source or existing.get("source_type") or "chaturbate"
    new_auto = bool(auto_record)
    was_auto = bool(existing.get("auto_record", False))
    record_quality = existing.get("record_quality", "best")
    # On the off -> on transition, apply the global default resolution so the
    # model immediately starts recording at the configured default.
    if new_auto and not was_auto:
        record_quality = await _get_default_record_quality()

    await db.add_or_update_model(
        username=username,
        auto_record=new_auto,
        record_quality=record_quality,
        retention_days=existing.get("retention_days", 30),
        source_type=source_type,
    )
    return {
        "success": True,
        "autoRecord": new_auto,
        "recordQuality": record_quality,
        "sourceType": source_type,
    }


# ============================================
# Playback Position Endpoints
# ============================================

@app.get("/api/playback-position/{recording_id}")
async def get_playback_position(recording_id: str):
    """Get saved playback position for a recording"""
    pos = await db.get_playback_position(recording_id)
    if pos:
        watched_threshold = await _get_watched_threshold()
        is_watched = bool(pos.get("watched_at")) or _is_playback_watched(
            pos["position_seconds"],
            pos["duration_seconds"],
            watched_threshold,
        )
        return {
            "recordingId": recording_id,
            "position": pos["position_seconds"],
            "duration": pos["duration_seconds"],
            "progress": _playback_progress(pos["position_seconds"], pos["duration_seconds"]),
            "watchedThreshold": watched_threshold,
            "isWatched": is_watched,
            "watchedAt": pos.get("watched_at") or (pos.get("updated_at") if is_watched else None),
        }
    return {
        "recordingId": recording_id,
        "position": 0,
        "duration": 0,
        "progress": 0,
        "watchedThreshold": await _get_watched_threshold(),
        "isWatched": False,
        "watchedAt": None,
    }


@app.post("/api/playback-position/{recording_id}")
async def save_playback_position(recording_id: str, body: dict):
    """Save playback position for a recording. Auto-delete if threshold reached."""
    try:
        position = float(body.get("position", 0) or 0)
    except (ValueError, TypeError):
        position = 0
    try:
        duration = float(body.get("duration", 0) or 0)
    except (ValueError, TypeError):
        duration = 0
    username = body.get("username", "")
    watched_threshold = await _get_watched_threshold()
    playback_progress = _playback_progress(position, duration)
    position_reached_watched_threshold = _is_playback_watched(position, duration, watched_threshold)
    await db.save_playback_position(
        recording_id,
        username,
        position,
        duration,
        mark_watched=position_reached_watched_threshold,
    )
    saved_position = await db.get_playback_position(recording_id)
    is_watched = bool((saved_position or {}).get("watched_at")) or position_reached_watched_threshold

    # Check auto-delete
    should_delete = False
    if duration > 0 and position > 0:
        rec = await db.get_recording_by_id(recording_id)
        is_protected_import = bool(
            rec
            and rec.get("media_kind") == "import"
            and rec.get("protected_from_retention")
        )
        auto_delete_val = await db.get_setting("auto_delete_watched")
        if (
            not is_protected_import
            and auto_delete_val
            and auto_delete_val.lower() in {"1", "true", "yes"}
        ):
            if position_reached_watched_threshold:
                should_delete = True

    return {
        "success": True,
        "autoDelete": should_delete,
        "isWatched": is_watched,
        "progress": playback_progress,
        "watchedThreshold": watched_threshold,
        "watchedAt": (saved_position or {}).get("watched_at"),
    }


# ============================================
# Recordings grouped by model
# ============================================

@app.get("/api/recordings-by-model")
async def get_recordings_by_model(show_ts: bool = False):
    """Get recordings grouped by model with stats, including models with 0 recordings"""
    groups = await db.get_recordings_grouped_by_model(show_ts=show_ts)

    # Build a lookup of auto_record status
    all_models = await db.get_all_models()
    auto_record_map = {m["username"]: bool(m.get("auto_record")) for m in all_models}
    source_type_map = {
        m["username"]: await _infer_source_type(m.get("username"), m)
        for m in all_models
    }

    # Build a set of usernames that have recordings
    usernames_with_recordings = set()

    # Any model with recordings is shown here, regardless of auto_record status.
    # auto_record only controls whether NEW recordings are triggered; past
    # recordings should never disappear from the list (GH #13).
    result = []
    for group in groups:
        username = group["username"]
        usernames_with_recordings.add(username)
        thumb_url = f"/api/thumbnail/{username}"
        result.append({
            "username": username,
            "recordingCount": group["recording_count"],
            "totalSize": group["total_size"],
            "lastRecordingAt": group["last_recording_at"],
            "totalDuration": group["total_duration"],
            "thumbnail": thumb_url,
            "autoRecord": auto_record_map.get(username, True),
            "sourceType": source_type_map.get(username, "chaturbate"),
            "source_type": source_type_map.get(username, "chaturbate"),
        })

    # Also include tracked models (auto_record=1) that have 0 recordings
    for model in all_models:
        username = model["username"]
        if username not in usernames_with_recordings and model.get("auto_record"):
            result.append({
                "username": username,
                "recordingCount": 0,
                "totalSize": 0,
                "lastRecordingAt": None,
                "totalDuration": 0,
                "thumbnail": f"/api/thumbnail/{username}",
                "autoRecord": True,
                "sourceType": source_type_map.get(username, "chaturbate"),
                "source_type": source_type_map.get(username, "chaturbate"),
            })

    return {"models": result}


@app.post("/api/recordings/recalculate-durations")
async def recalculate_all_durations():
    """Recalcule les durées de tous les enregistrements"""
    logger.info("API: Demande de recalcul des durées", endpoint="/api/recordings/recalculate-durations")
    
    try:
        # Créer une tâche en arrière-plan
        asyncio.create_task(_recalculate_durations_task())
        
        return {
            "success": True,
            "message": "Recalcul des durées démarré en arrière-plan"
        }
    except Exception as e:
        logger.error("Erreur lancement recalcul durées", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def _recalculate_durations_task():
    """Tâche de recalcul des durées en arrière-plan"""
    from app.tasks.monitor import generate_recording_thumbnail, get_media_created_at, get_video_duration
    
    logger.background_task("recalculate-durations", "Démarrage du recalcul")
    
    try:
        # Récupérer tous les modèles
        models = await db.get_all_models()
        
        total_processed = 0
        total_updated = 0
        
        for model in models:
            username = model['username']
            records_dirs = [path for path in _record_dirs_for_model(username, model) if path.exists()]

            if not records_dirs:
                continue
            
            logger.info("Recalcul durées", username=username, task="recalculate-durations")

            ts_files = []
            for records_dir in records_dirs:
                ts_files.extend(records_dir.glob("*.ts"))
            
            for ts_file in ts_files:
                try:
                    total_processed += 1
                    
                    # Récupérer l'enregistrement depuis la DB
                    recordings = await db.get_recordings(username)
                    existing_rec = next((r for r in recordings if r['filename'] == ts_file.name), None)
                    
                    current_duration = 0
                    if existing_rec:
                        current_duration = existing_rec.get('duration_seconds', 0)
                    
                    # Calculer la durée si elle est à 0
                    if current_duration == 0:
                        duration = await get_video_duration(ts_file, FFMPEG_PATH)
                        
                        if duration > 0:
                            # Générer aussi la miniature
                            thumbnail_path = await generate_recording_thumbnail(
                                ts_file, OUTPUT_DIR, username, FFMPEG_PATH
                            )
                            
                            # Mettre à jour dans la DB
                            await db.add_or_update_recording(
                                username=username,
                                filename=ts_file.name,
                                file_path=str(ts_file),
                                file_size=ts_file.stat().st_size,
                                duration_seconds=duration,
                                thumbnail_path=thumbnail_path,
                                created_at=await get_media_created_at(
                                    ts_file,
                                    FFMPEG_PATH,
                                    fallback_timestamp=int(ts_file.stat().st_mtime),
                                ),
                            )
                            
                            total_updated += 1
                            
                            logger.success("Durée calculée", 
                                         username=username,
                                         filename=ts_file.name,
                                         duration=duration)
                        
                except Exception as e:
                    logger.error("Erreur recalcul fichier", 
                               username=username,
                               filename=ts_file.name,
                               error=str(e))
                    continue
        
        logger.success("Recalcul terminé",
                      task="recalculate-durations",
                      updated=total_updated,
                      total=total_processed)
        
    except Exception as e:
        logger.error("Erreur tâche recalcul durées", 
                    task="recalculate-durations", 
                    error=str(e), 
                    exc_info=True)


# ============================================
# Background Task - Auto-enregistrement
# ============================================

async def ffmpeg_watchdog_task():
    """Stop FFmpeg sessions that stay alive without writing any TS data."""
    timeout = getattr(manager, "stall_timeout_seconds", 180)
    if timeout <= 0:
        logger.info("FFmpeg watchdog désactivé", task="ffmpeg-watchdog")
        return

    while True:
        try:
            stopped = manager.stop_stalled_sessions(timeout)
            if stopped:
                logger.warning(
                    "Sessions FFmpeg bloquées arrêtées",
                    task="ffmpeg-watchdog",
                    count=len(stopped),
                    sessions=stopped,
                )
            await asyncio.sleep(30)
        except Exception as e:
            logger.error(
                "Erreur watchdog FFmpeg",
                task="ffmpeg-watchdog",
                error=str(e),
                exc_info=True,
            )
            await asyncio.sleep(60)


async def auto_record_task():
    """Vérifie automatiquement les modèles et lance les enregistrements (utilise SQLite)"""
    failure_cooldowns: dict[str, float] = {}
    while True:
        try:
            check_interval = await get_check_interval_seconds(db)
            failure_cooldown_seconds = max(60, min(check_interval, 300))
            await asyncio.sleep(check_interval)

            media_sources = await db.get_media_profile_sources_for_auto_record()
            models = await db.get_models_for_auto_record()

            jobs: list[dict] = []
            media_source_keys = {
                ((source.get("source_type") or "chaturbate"), source.get("channel_username"))
                for source in media_sources
                if source.get("channel_username")
            }

            for source in media_sources:
                profile_username = source.get("profile_username")
                channel_username = source.get("channel_username")
                source_type = source.get("source_type") or "chaturbate"
                if not profile_username or not channel_username:
                    continue
                jobs.append({
                    "profile_username": profile_username,
                    "target_username": channel_username,
                    "source_type": source_type,
                    "display_name": profile_username if profile_username == channel_username else f"{profile_username} ({channel_username})",
                    "record_quality": source.get("record_quality"),
                    "record_path": source.get("record_path") or _default_record_path(profile_username),
                    "session_key": _recording_source_session_key(profile_username, source_type, channel_username),
                })

            for model in models:
                username = model.get("username")
                source_type = model.get("source_type") or "chaturbate"
                if not username or (source_type, username) in media_source_keys:
                    continue
                jobs.append({
                    "profile_username": username,
                    "target_username": username,
                    "source_type": source_type,
                    "display_name": username,
                    "record_quality": model.get("record_quality"),
                    "record_path": _record_path_from_model(model, username),
                    "session_key": _recording_source_session_key(username, source_type, username),
                })

            if not jobs:
                continue
            
            # Récupérer les sessions actives
            active_sessions = _all_recording_statuses()
            
            for job in jobs:
                profile_username = job["profile_username"]
                target_username = job["target_username"]
                source_hint = job["source_type"]
                session_key = job["session_key"]
                cooldown_until = failure_cooldowns.get(session_key, 0)
                if cooldown_until > time.time():
                    continue

                # Vérifier si déjà en enregistrement
                is_recording = any(
                    s.get("running")
                    and (
                        s.get("session_key") == session_key
                        or (
                            not s.get("session_key")
                            and s.get("person") == profile_username
                            and (s.get("target") in {None, "", target_username})
                        )
                    )
                    for s in active_sessions
                )

                if is_recording:
                    continue  # Déjà en cours

                # Vérifier le statut depuis le cache SQLite (mis à jour par monitor)
                cached_status = await db.get_model(target_username, source_type=source_hint)
                
                if cached_status and cached_status.get('is_online'):
                    # Modèle en ligne: résoudre le flux HLS
                    try:
                        hls_source = None
                        hls_source_url = None
                        ffmpeg_video_stream_index = None
                        stream_headers = None
                        filename_format = await _get_recording_filename_format()
                        max_height = await _get_recording_height_for_quality(
                            job.get("record_quality") or cached_status.get("record_quality")
                        )
                        source_type = await _infer_source_type(target_username, cached_status)
                        if source_type not in _available_source_types():
                            logger.warning(
                                "Source inconnue pour auto-record",
                                task="auto-record",
                                username=target_username,
                                source_type=source_type,
                            )
                            continue
                        record_dir = _record_dir_from_path(
                            _normalize_record_path(job.get("record_path"), profile_username)
                        )
                        if _supports_browser_capture(source_type):
                            logger.background_task("auto-record", "Modèle WebRTC en ligne détecté", username=target_username)
                            try:
                                await _ensure_browser_capture_session(source_type)
                                sess = _start_browser_capture(
                                    source_type,
                                    target_username,
                                    person=profile_username,
                                    display_name=job["display_name"],
                                    record=True,
                                    filename_format=filename_format,
                                    records_dir_for_person=record_dir,
                                    target_username=target_username,
                                    session_key=session_key,
                                )
                                ready = await asyncio.to_thread(sess.wait_until_ready, 35)
                                if not ready:
                                    browser_capture_manager.stop_session(sess.id)
                                    raise RuntimeError("Aucun flux video capturable dans le navigateur")
                                logger.success("Auto-enregistrement browser démarré",
                                             task="auto-record",
                                             username=target_username,
                                             profile=profile_username,
                                             session_id=sess.id)
                                active_sessions = _all_recording_statuses()
                            except RuntimeError as e:
                                logger.warning("Impossible démarrer browser capture",
                                             task="auto-record",
                                             username=target_username,
                                             error=str(e))
                                failure_cooldowns[session_key] = time.time() + failure_cooldown_seconds
                            except ProviderError as e:
                                logger.warning("Browser capture refusee",
                                             task="auto-record",
                                             username=target_username,
                                             error=str(e))
                                failure_cooldowns[session_key] = time.time() + failure_cooldown_seconds
                        else:
                            try:
                                resolved = await _resolve_stream(
                                    source_type, target_username, max_height
                                )
                                ffmpeg_video_stream_index = resolved.ffmpeg_video_stream_index
                                hls_source, stream_headers, hls_source_url = _ffmpeg_stream_input(resolved)
                            except Exception as e:
                                logger.debug(
                                    "Auto-record resolve échec",
                                    task="auto-record",
                                    username=target_username,
                                    error=str(e),
                                )
                                failure_cooldowns[session_key] = time.time() + failure_cooldown_seconds

                        if hls_source:
                            # Lancer l'enregistrement
                            logger.background_task("auto-record", "Modèle en ligne détecté", username=target_username, profile=profile_username)

                            try:
                                segment_duration_seconds, segment_size_bytes = await _get_recording_segment_limits()
                                sess = manager.start_session(
                                    input_url=hls_source,
                                    display_name=job["display_name"],
                                    person=profile_username,
                                    max_height=max_height,
                                    segment_duration_seconds=segment_duration_seconds,
                                    segment_size_bytes=segment_size_bytes,
                                    input_headers=stream_headers,
                                    source_url=hls_source_url,
                                    ffmpeg_video_stream_index=ffmpeg_video_stream_index,
                                    filename_format=filename_format,
                                    records_dir_for_person=str(record_dir),
                                    source_type=source_type,
                                    target=target_username,
                                    session_key=session_key,
                                )

                                if sess:
                                    logger.success("Auto-enregistrement démarré",
                                                   task="auto-record",
                                                   username=target_username,
                                                   profile=profile_username,
                                                   session_id=sess.id)
                                    failure_cooldowns.pop(session_key, None)
                                    active_sessions = _all_recording_statuses()
                            except RuntimeError as e:
                                logger.warning("Impossible démarrer enregistrement",
                                             task="auto-record",
                                             username=target_username,
                                             error=str(e))
                                failure_cooldowns[session_key] = time.time() + failure_cooldown_seconds
                                continue

                    except Exception as e:
                        logger.error("Erreur vérification modèle",
                                   task="auto-record",
                                   username=target_username,
                                   error=str(e))
                        failure_cooldowns[session_key] = time.time() + failure_cooldown_seconds
                        continue
                
        except Exception as e:
            logger.error("Erreur auto-record task", task="auto-record", exc_info=True, error=str(e))
            await asyncio.sleep(60)


async def cleanup_old_recordings_task():
    """Nettoie automatiquement les anciennes rediffusions selon la rétention configurée"""
    from datetime import datetime, timedelta
    
    while True:
        try:
            await asyncio.sleep(3600)  # Vérifier toutes les heures
            
            logger.background_task("cleanup", "Début nettoyage anciennes rediffusions")
            
            # Charger les modèles et les sources Media avec leurs paramètres de rétention
            media_sources = await db.get_all_media_profile_sources()
            models = await db.get_all_models()
            cleanup_jobs: list[dict] = []
            media_source_keys = {
                ((source.get("source_type") or "chaturbate"), source.get("channel_username"))
                for source in media_sources
                if source.get("channel_username")
            }
            for source in media_sources:
                profile_username = source.get("profile_username")
                if not profile_username:
                    continue
                try:
                    records_dir = _record_dir_from_path(
                        _normalize_record_path(
                            source.get("record_path") or _default_record_path(profile_username),
                            profile_username,
                        )
                    )
                except HTTPException:
                    continue
                cleanup_jobs.append({
                    "username": profile_username,
                    "retention_days": source.get("retention_days", 30),
                    "records_dirs": [records_dir],
                })
            for model in models:
                username = model.get("username")
                source_type = model.get("source_type") or "chaturbate"
                if not username or (source_type, username) in media_source_keys:
                    continue
                cleanup_jobs.append({
                    "username": username,
                    "retention_days": model.get("retention_days", 30),
                    "records_dirs": _record_dirs_for_model(username, model),
                })

            for job in cleanup_jobs:
                username = job.get('username')
                retention_days = job.get('retention_days', 30)  # Défaut 30 jours

                if not username:
                    continue

                # retention_days == 0 means keep forever
                if retention_days == 0:
                    logger.debug("Rétention infinie, skip",
                               task="cleanup",
                               username=username)
                    continue

                thumbnails_dir = OUTPUT_DIR / "thumbnails" / username
                records_dirs = [path for path in job.get("records_dirs", []) if path.exists()]

                if not records_dirs:
                    continue

                # Date limite (aujourd'hui - rétention)
                cutoff_date = datetime.now() - timedelta(days=retention_days)
                
                # Parcourir les fichiers .ts
                for records_dir in records_dirs:
                    for ts_file in records_dir.glob("*.ts"):
                        try:
                            # Le nom du fichier est au format YYYY-MM-DD.ts
                            date_str = ts_file.stem  # Enlève .ts
                            file_date = datetime.strptime(date_str, "%Y-%m-%d")

                            # Si le fichier est plus vieux que la limite
                            if file_date < cutoff_date:
                                # Supprimer le fichier TS
                                file_size = ts_file.stat().st_size
                                ts_file.unlink()
                                logger.info("Fichier supprimé (rétention)",
                                          task="cleanup",
                                          username=username,
                                          filename=ts_file.name,
                                          retention_days=retention_days,
                                          size_mb=f"{file_size / 1024 / 1024:.1f}")

                                # Supprimer la miniature associée
                                thumb_file = thumbnails_dir / f"{ts_file.stem}.jpg"
                                if thumb_file.exists():
                                    thumb_file.unlink()

                                # Supprimer l'entrée du cache
                                cache_file = records_dir / ".metadata_cache.json"
                                if cache_file.exists():
                                    try:
                                        with open(cache_file, 'r') as f:
                                            cache = json.load(f)
                                        if ts_file.name in cache:
                                            del cache[ts_file.name]
                                            with open(cache_file, 'w') as f:
                                                json.dump(cache, f)
                                    except:
                                        pass

                        except Exception as e:
                            logger.error("Erreur nettoyage fichier",
                                       task="cleanup",
                                       filename=ts_file.name,
                                       error=str(e))
                            continue
                        
        except Exception as e:
            logger.error("Erreur cleanup task", task="cleanup", exc_info=True, error=str(e))
            await asyncio.sleep(3600)


async def sync_following_task(source_type: str = "chaturbate"):
    """Background task: sync provider follows through the provider abstraction."""
    source_type = _normalize_source_type(source_type) or "chaturbate"
    while True:
        try:
            await asyncio.sleep(300)  # 5 minutes

            if source_type not in _available_source_types():
                continue
            provider = _provider_for(source_type)
            if not getattr(provider.capabilities, "can_sync_following", False):
                continue

            models = await provider.sync_following()
            stored = await store_provider_following(db, source_type, models)
            if not stored["trusted"]:
                logger.warning(
                    "Following sync skipped",
                    task="following-sync",
                    source_type=source_type,
                    reason=stored["skippedReason"],
                )
                continue
            if stored["synced"]:
                repaired = await db.reconcile_model_sources_from_followed()
                logger.debug(
                    "Following synced",
                    count=stored["synced"],
                    repaired_sources=repaired,
                    source_type=source_type,
                    task="following-sync",
                )
        except Exception as e:
            logger.error("Following sync error", task="following-sync", error=str(e))
            await asyncio.sleep(60)


async def sync_cam4_following_task(cam4_auth):
    """Background task: sync les favoris CAM4 toutes les 5 minutes."""
    from .services import cam4_source
    while True:
        try:
            await asyncio.sleep(300)

            if cam4_auth is None:
                continue
            status = cam4_auth.get_status()
            if not status.get("isLoggedIn"):
                continue

            items = await cam4_source.list_followed(cam4_auth.get_cookies())
            synced = set()
            for item in items:
                await db.upsert_followed_model(
                    username=item["username"],
                    display_name=item.get("display_name") or item["username"],
                    is_online=bool(item.get("is_online", False)),
                    viewers=int(item.get("viewers") or 0),
                    thumbnail_url=item.get("thumbnail"),
                    source_type="cam4",
                    room_status=item.get("room_status"),
                )
                synced.add(item["username"])
            await db.remove_unfollowed(synced, source_type="cam4")
            if items:
                repaired = await db.reconcile_model_sources_from_followed()
                logger.debug("CAM4 following synced", count=len(items), task="cam4-sync")
                if repaired:
                    logger.info("Sources modèles réparées depuis CAM4", count=repaired, task="cam4-sync")
        except Exception as e:
            logger.error("CAM4 sync error", task="cam4-sync", error=str(e))
            await asyncio.sleep(60)


@app.on_event("startup")
async def startup_event():
    """Démarre les background tasks au démarrage de l'application"""
    # Initialiser la base de données
    await db.initialize()

    # Migrer les données depuis le JSON si nécessaire
    await db.migrate_from_json(MODELS_FILE)
    repaired_sources = await db.reconcile_model_sources_from_followed()
    if repaired_sources:
        logger.info("Sources modèles réparées depuis les favoris", count=repaired_sources)
    await _import_auto_record_users_from_env()

    # Initialize FlareSolverr client.
    # The docker-compose healthcheck normally guarantees FlareSolverr is
    # ready before we start, but we keep a short retry loop as a safety net
    # for bare-metal / non-compose deployments.
    flare_url = await _get_flaresolverr_url()
    flaresolverr = FlareSolverrClient(flare_url)
    _apply_flaresolverr_url(flare_url, flaresolverr)
    fs_status = None
    for attempt in range(6):  # ~15s max (6 tries × 2.5s sleep between)
        fs_status = await flaresolverr.check_status()
        if fs_status["available"]:
            break
        if attempt < 5:
            await asyncio.sleep(2.5)

    if fs_status and fs_status["available"]:
        logger.info(
            "FlareSolverr connecté",
            url=flare_url,
            version=fs_status.get("version"),
        )
    else:
        # Log the actual reason so users can diagnose DNS / network / timing
        # issues without having to dig through DEBUG logs.
        reason = (fs_status or {}).get("message") or "unknown"
        logger.warning(
            "FlareSolverr non disponible (optionnel)",
            url=flare_url,
            reason=reason,
        )

    # Initialize Chaturbate auth service
    cb_auth = ChaturbateAuthService(db, flaresolverr)
    await cb_auth.initialize()
    if CHATURBATE_USERNAME and CHATURBATE_PASSWORD:
        try:
            result = await cb_auth.login(CHATURBATE_USERNAME, CHATURBATE_PASSWORD)
            if result.get("success"):
                logger.info("Chaturbate auto-login succeeded", username=CHATURBATE_USERNAME)
            else:
                logger.warning(
                    "Chaturbate auto-login failed",
                    username=CHATURBATE_USERNAME,
                    error=result.get("error"),
                )
        except Exception as exc:
            logger.warning("Chaturbate auto-login error", username=CHATURBATE_USERNAME, error=str(exc))

    # Initialize CAM4 auth service (cookie-based session)
    global cam4_auth_service
    cam4_auth_service = CAM4AuthService(db)
    await cam4_auth_service.initialize()

    # Initialize Chaturbate API client
    global chaturbate_api
    cb_api = ChaturbateAPI(cb_auth, flaresolverr)
    chaturbate_api = cb_api

    global provider_registry, SOURCE_TYPES
    provider_registry = create_provider_registry(
        db,
        chaturbate_api=cb_api,
        chaturbate_auth=cb_auth,
        cam4_auth=cam4_auth_service,
        output_dir=OUTPUT_DIR,
    )
    SOURCE_TYPES = provider_registry.source_types()

    # Wire up API routers
    auth_router.init(cb_auth, flaresolverr)
    discover_router.init(cb_api, db, provider_registry)
    following_router.init(cb_api, cb_auth, db, provider_registry)

    # Set authenticated resolver for chaturbate
    from .resolvers.chaturbate import set_chaturbate_api
    set_chaturbate_api(cb_api)

    # Wire CAM4 router avec auth service
    cam4_router.init(cam4_auth_service, db)

    # Démarrer les tâches de fond
    asyncio.create_task(monitor_models_task(
        db,
        manager,
        FFMPEG_PATH,
        chaturbate_auth=cb_auth,
        cam4_auth=cam4_auth_service,
        provider_registry=provider_registry,
    ))
    asyncio.create_task(ffmpeg_watchdog_task())
    asyncio.create_task(auto_record_task())
    asyncio.create_task(sync_following_task("chaturbate"))
    asyncio.create_task(sync_cam4_following_task(cam4_auth_service))
    asyncio.create_task(cleanup_old_recordings_task())
    asyncio.create_task(auto_convert_recordings_task(db, OUTPUT_DIR, manager, FFMPEG_PATH))
    if MEDIA_IMPORTS_ENABLED:
        global media_import_manager
        media_import_manager = MediaImportManager(db, OUTPUT_DIR, FFMPEG_PATH)
        asyncio.create_task(media_imports_task(media_import_manager))
    logger.info("Background tasks démarrés",
                tasks=["monitor", "ffmpeg-watchdog", "auto-record", "following-sync", "cam4-sync", "cleanup", "convert"])
