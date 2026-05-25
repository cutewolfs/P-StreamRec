import re
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import quote, urljoin, urlparse
import os
import asyncio
import aiohttp
import json
import queue
import subprocess
import sys
import time
from datetime import datetime
import secrets
import hashlib
import http.client
import socket as raw_socket

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
from .core.http_client import aiohttp_client_session, aiohttp_request_kwargs
from .tasks.monitor import monitor_models_task
from .tasks.convert import auto_convert_recordings_task
from .tasks.media_imports import (
    MediaImportManager,
    media_imports_task,
    remove_import_record,
    SUPPORTED_VIDEO_EXTENSIONS,
)
from .services.flaresolverr import FlareSolverrClient
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
FLARESOLVERR_URL = os.getenv("FLARESOLVERR_URL", "http://flaresolverr:8191")
RECORDING_RANGE_CHUNK_SIZE = int(os.getenv("RECORDING_RANGE_CHUNK_SIZE", str(8 * 1024 * 1024)))
MEDIA_IMPORTS_ENABLED = os.getenv("PSTREAMREC_MEDIA_IMPORTS", "false").lower() in {"1", "true", "yes"}

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
    public_prefixes = ["/static/", "/api/chaturbate/status"]

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

# Configuration CORS permissive pour Umbrel
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
    no_cache_paths = {"/", "/discover", "/following", "/recordings", "/settings", "/wiki"}
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


def _parse_byte_range(range_header: str, file_size: int) -> Optional[tuple[int, int]]:
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
                end = start + max(RECORDING_RANGE_CHUNK_SIZE, 1) - 1
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
        raise HTTPException(status_code=404, detail="Média introuvable")

    file_size = file_path.stat().st_size
    logger.file_operation("Lecture", str(file_path), size=file_size)

    media_type = _recording_media_type(filename)
    base_headers = _recording_headers(filename, file_size)
    range_header = request.headers.get("range")

    if range_header:
        byte_range = _parse_byte_range(range_header, file_size)
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


# Route protégée pour les enregistrements
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
        raise HTTPException(status_code=400, detail="Nom de fichier invalide")

    active_sessions = _all_recording_statuses()
    for session in active_sessions:
        if not (session.get("person") == username and session.get("running")):
            continue
        record_path = session.get("record_path") or ""
        if record_path and Path(record_path).name == filename:
            logger.warning("Accès bloqué à enregistrement en cours", username=username, filename=filename)
            raise HTTPException(
                status_code=403,
                detail="Cet enregistrement est en cours. Regardez le live à la place.",
            )

    # Servir le fichier
    file_path = OUTPUT_DIR / "records" / username / filename
    return await _serve_video_file_with_ranges(request, file_path, filename)


@app.api_route("/streams/media/{recording_id}", methods=["GET", "HEAD"])
async def serve_imported_media(request: Request, recording_id: str, download: bool = False):
    """Sert un média importé via son ID stable, sans exposer de chemin disque."""
    if ".." in recording_id or "/" in recording_id:
        raise HTTPException(status_code=400, detail="ID média invalide")

    rec = await db.get_recording_by_id(recording_id)
    if not rec or rec.get("media_kind") != "import":
        raise HTTPException(status_code=404, detail="Média introuvable")

    path_value = rec.get("file_path") if download else (rec.get("playable_path") or rec.get("file_path"))
    if not path_value:
        raise HTTPException(status_code=404, detail="Fichier média introuvable")

    file_path = Path(path_value)
    output_root = OUTPUT_DIR.resolve()
    try:
        resolved = file_path.resolve()
        if not resolved.is_relative_to(output_root):
            raise HTTPException(status_code=403, detail="Chemin média non autorisé")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Chemin média invalide")

    return await _serve_video_file_with_ranges(request, file_path, file_path.name)

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
media_import_manager: Optional[MediaImportManager] = None

# Chaturbate API (initialized at startup)
chaturbate_api: Optional[ChaturbateAPI] = None

# Plugin manager (initialized at startup)
# Set at startup via setup_services
cam4_auth_service: Optional[CAM4AuthService] = None

SOURCE_TYPES = {"chaturbate", "cam4"}
provider_registry = create_provider_registry(db, output_dir=OUTPUT_DIR)
SOURCE_TYPES = provider_registry.source_types()
_HLS_PROXY_CACHE: dict[str, dict] = {}
_HLS_PROXY_TTL_SECONDS = int(os.getenv("PSTREAMREC_HLS_PROXY_TTL_SECONDS", "900"))


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


def _normalize_source_type(source_type: Optional[str]) -> Optional[str]:
    value = (source_type or "").strip().lower()
    if not value or value == "auto":
        return None
    return value


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
        _HLS_PROXY_CACHE.pop(token, None)


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


def _register_hls_proxy_url(
    url: str,
    headers: Optional[dict[str, str]] = None,
    suffix: Optional[str] = None,
) -> str:
    _prune_hls_proxy_cache()
    token = secrets.token_urlsafe(24)
    now = time.time()
    _HLS_PROXY_CACHE[token] = {
        "url": url,
        "headers": dict(headers or {}),
        "created_at": now,
        "expires_at": now + max(60, _HLS_PROXY_TTL_SECONDS),
    }
    return f"/api/proxy/hls/{token}{suffix if suffix is not None else _hls_proxy_path_suffix(url)}"


def _register_cached_hls_body(url: str, body: bytes, content_type: str = "") -> str:
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
    return f"/api/proxy/hls/{token}.mp4"


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

    media_sequence_written = False
    rewritten = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            rewritten.append(line)
            continue
        if stripped.startswith("#"):
            if live_sequence is not None:
                upper = stripped.upper()
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
        rewritten.append(proxy_url(stripped))
    if live_sequence is not None and not media_sequence_written:
        insert_at = 1 if rewritten and rewritten[0].strip() == "#EXTM3U" else 0
        rewritten.insert(insert_at, f"#EXT-X-MEDIA-SEQUENCE:{live_sequence}")
    return "\n".join(rewritten) + ("\n" if text.endswith("\n") else "")


def _proxied_stream_url(stream: ResolvedStream) -> str:
    lower = (stream.url or "").lower()
    if stream.source_type == "livejasmin":
        return _register_hls_proxy_url(stream.url, headers=stream.headers, suffix=".m3u8")
    if ".m3u8" in lower or ".mpd" in lower:
        return _register_hls_proxy_url(stream.url, headers=stream.headers)
    return stream.url


def _local_proxy_url_for_ffmpeg(url: str) -> str:
    if (url or "").startswith("/api/proxy/hls/"):
        port = os.getenv("PORT", "8080")
        return f"http://127.0.0.1:{port}{url}"
    return url


def _ffmpeg_stream_input(stream: ResolvedStream) -> tuple[str, Optional[dict[str, str]]]:
    proxied_url = _proxied_stream_url(stream)
    if proxied_url != stream.url:
        return _local_proxy_url_for_ffmpeg(proxied_url), None
    return stream.url, stream.headers


def _start_browser_capture(
    source_type: str,
    target: str,
    person: str,
    display_name: Optional[str] = None,
    record: bool = True,
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
        from app.tasks.monitor import get_video_duration, generate_recording_thumbnail

        await _remux_browser_recording(record_path)
        file_size = record_path.stat().st_size
        duration_seconds = await get_video_duration(record_path, FFMPEG_PATH)
        if not duration_seconds:
            duration_seconds = max(0, int(time.time() - getattr(session, "start_time", time.time())))
        if duration_seconds < MIN_RECORDING_SECONDS and file_size < MIN_RECORDING_BYTES:
            return
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
        )
    except Exception as exc:
        logger.warning(
            "Indexation browser recording échouée",
            session_id=session.id,
            person=session.person,
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


class ProviderLoginBody(BaseModel):
    username: str
    password: str


class ModelVolumeBody(BaseModel):
    volume: float


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9_-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "session"


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
    """Recordings page - all recordings across models"""
    return FileResponse(str(STATIC_DIR / "recordings.html"))


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


@app.get("/dashboard")
async def dashboard_page():
    """Legacy dashboard (old index.html)"""
    return FileResponse(str(STATIC_DIR / "index.html"))


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
    from app.core.config import AUTO_RECORD_INTERVAL
    return {
        "version": version,
        "output_dir": str(OUTPUT_DIR),
        "ffmpeg_path": FFMPEG_PATH,
        "check_interval": AUTO_RECORD_INTERVAL,
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

    # Prod (Docker/Umbrel): on quitte proprement. Le container manager relance
    # l'app via sa restart policy (unless-stopped).
    os._exit(0)


@app.get("/model.html")
async def model_page():
    return FileResponse(str(STATIC_DIR / "model.html"))


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
            async with session.get(
                url,
                headers=headers,
                allow_redirects=True,
                **aiohttp_request_kwargs(),
            ) as resp:
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
                            headers=headers,
                            entry=entry,
                            session=session,
                        )
                    else:
                        rewritten = _rewrite_hls_playlist(text, str(resp.url), headers=headers)
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

    auth = getattr(provider, "auth", None)
    if auth is not None:
        try:
            return auth.get_status()
        except Exception:
            pass

    row = await db.get_provider_session(source_type)
    if not row:
        return {"isLoggedIn": False, "username": None, "lastError": None, "hasCookies": False}
    return {
        "isLoggedIn": bool(row.get("is_logged_in")),
        "username": row.get("username"),
        "lastError": row.get("last_error"),
        "lastLoginAt": row.get("last_login_at"),
        "hasCookies": bool(row.get("session_cookies")),
    }


@app.get("/api/providers")
async def list_providers():
    providers = []
    for meta in provider_registry.metadata():
        source_type = meta["sourceType"]
        meta["status"] = await _provider_login_state(source_type)
        providers.append(meta)
    return {"providers": providers, "sourceTypes": sorted(_available_source_types())}


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
    try:
        result = await provider.login(body.username, body.password)
    except ProviderInteractionRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not result.get("success"):
        raise HTTPException(status_code=401, detail=result.get("error", "Login failed"))
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
    try:
        timeout = float(os.getenv("PSTREAMREC_FOLLOW_SYNC_TIMEOUT", "45") or "45")
        items = await asyncio.wait_for(provider.sync_following(), timeout=timeout)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail=f"{provider.display_name}: sync timeout")
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    synced = set()
    for item in items or []:
        username = item.get("username")
        if not username:
            continue
        await db.upsert_followed_model(
            username=username,
            display_name=item.get("display_name") or username,
            is_online=bool(item.get("is_online", item.get("isOnline", False))),
            viewers=int(item.get("viewers") or 0),
            thumbnail_url=item.get("thumbnail") or item.get("thumbnail_url"),
            source_type=source_type,
            room_status=item.get("room_status") or item.get("roomStatus"),
        )
        synced.add(username)

    if source_type == "chaturbate":
        await db.remove_unfollowed(synced, source_type=source_type)
    await db.reconcile_model_sources_from_followed()
    return {"synced": len(synced), "message": f"{provider.display_name}: {len(synced)} follows synchronises"}


@app.get("/api/providers/{source_type}/is-following/{username}")
async def provider_is_following(source_type: str, username: str):
    source_type = _normalize_source_type(source_type) or ""
    if source_type not in _available_source_types():
        raise HTTPException(status_code=404, detail=f"Source '{source_type}' non disponible")
    provider = _provider_for(source_type)
    remote = False
    try:
        if provider.capabilities.can_follow:
            remote = bool(await provider.is_following(username))
    except Exception:
        remote = False
    local = await db.get_followed_model(username)
    local_match = bool(local and (local.get("source_type") or "chaturbate") == source_type)
    return {"isFollowing": remote or local_match}


@app.post("/api/providers/{source_type}/follow/{username}")
async def provider_follow(source_type: str, username: str):
    source_type = _normalize_source_type(source_type) or ""
    if source_type not in _available_source_types():
        raise HTTPException(status_code=404, detail=f"Source '{source_type}' non disponible")
    provider = _provider_for(source_type)
    if provider.capabilities.can_follow:
        result = await provider.follow(username)
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "Follow failed"))
    else:
        result = {"success": True, "localOnly": True}

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
    if provider.capabilities.can_follow:
        result = await provider.unfollow(username)
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "Unfollow failed"))
    else:
        result = {"success": True, "localOnly": True}
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

    model_settings = None
    if not target.startswith("http://") and not target.startswith("https://"):
        model_lookup_username = (body.person or target).strip()
        if model_lookup_username:
            try:
                model_settings = await db.get_model(model_lookup_username)
            except Exception:
                model_settings = None
    
    # Si c'est un auto-start, vérifier que auto_record est activé dans la DB
    if body.auto_start:
        username = body.person or target
        model = model_settings or await db.get_model(username)
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
    person: Optional[str] = (body.person or "").strip() or None
    record_quality = body.record_quality or body.recordQuality
    if not record_quality and model_settings:
        record_quality = model_settings.get("record_quality")
    max_height = await _get_recording_height_for_quality(record_quality)

    # Determine source type
    requested_source = _normalize_source_type(body.source_type)
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
                sess = _start_browser_capture(
                    effective_source,
                    target,
                    person=person,
                    display_name=body.name or target,
                    record=True,
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
            }
        logger.subsection(f"Résolution via source '{effective_source}'")
        try:
            resolved = await _resolve_stream(effective_source, target, max_height)
            m3u8_url, stream_headers = _ffmpeg_stream_input(resolved)
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
    }


@app.get("/api/status")
async def api_status():
    return _all_recording_statuses()


@app.post("/api/stop/{session_id}")
async def api_stop(session_id: str):
    ok = manager.stop_session(session_id)
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
    model = await db.get_model(username)
    if not model or not model.get('source_type'):
        followed = await db.get_followed_model(username)
        if followed and followed.get('source_type'):
            if not model:
                model = followed
            else:
                model = {**model, 'source_type': followed['source_type']}

    source_type = _normalize_source_type(source)
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
            model = await db.get_model(username)
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
            person = slugify(username)
            try:
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
                "viewers": 0,
                "tags": [],
                "thumbnail": None,
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
            "streamUrl": _proxied_stream_url(resolved),
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


@app.get("/api/dashboard")
async def get_dashboard():
    """
    Endpoint optimisé qui retourne TOUTES les données depuis le cache SQLite
    Ultra-rapide car tout est pré-calculé par la tâche de monitoring
    """
    try:
        # Récupérer tous les modèles depuis SQLite (déjà avec statut à jour)
        models = await db.get_all_models()
        
        # Récupérer les sessions actives
        active_sessions = manager.list_status()
        
        # Formater les données pour le frontend
        models_info = []
        
        for model in models:
            username = model['username']
            source_type = await _infer_source_type(username, model)
            
            # Récupérer le nombre d'enregistrements depuis SQLite
            recordings_count = await db.get_recordings_count(username)
            
            model_info = {
                "username": username,
                "isOnline": bool(model.get('is_online', False)),
                "isRecording": bool(model.get('is_recording', False)),
                "viewers": model.get('viewers', 0),
                "thumbnail": f"/api/thumbnail/{username}",
                "recordingsCount": recordings_count,
                "recordQuality": model.get('record_quality', 'best'),
                "retentionDays": model.get('retention_days', 30),
                "autoRecord": bool(model.get('auto_record', True)),
                "roomStatus": model.get('room_status'),
                "sourceType": source_type,
                "source_type": source_type,
            }
            
            models_info.append(model_info)
        
        # Retourner tout d'un coup
        return {
            "models": models_info,
            "sessions": active_sessions,
            "timestamp": int(time.time() * 1000)
        }
    
    except Exception as e:
        logger.error("Erreur dashboard", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


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

        recordings.append({
            "recordingId": rec.get('recording_id', serve_path.stem),
            "filename": serve_path.name,
            "date": serve_path.stem,
            "size": file_size,
            "size_formatted": format_bytes(file_size),
            "size_mb": round(file_size / 1024 / 1024, 2),
            "size_display": size_display,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "url": f"/streams/records/{username}/{serve_path.name}",
            "thumbnail": thumb_url if thumb_path.exists() else None,
            "duration": duration_seconds,
            "duration_str": duration_str,
            "isConverted": is_converted,
            "isImported": False,
            "mediaKind": "recording",
            "importStatus": None,
            "importError": None,
            "playable": True,
            "downloadUrl": f"/streams/records/{username}/{serve_path.name}",
            "conversionAttempts": rec.get('conversion_attempts') or 0,
            "conversionError": rec.get('conversion_error'),
            "createdAt": created_at,
            "mp4": {
                "filename": Path(mp4_raw).name,
                "size": rec.get('mp4_size', 0),
                "size_formatted": format_bytes(rec.get('mp4_size', 0)),
                "url": f"/streams/records/{username}/{Path(mp4_raw).name}"
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

        recordings.append({
            "recordingId": rec.get("recording_id", file_stem),
            "username": rec_username,
            "filename": serve_file.name,
            "date": file_stem,
            "size": file_size,
            "size_formatted": format_bytes(file_size),
            "duration": duration_seconds,
            "duration_str": duration_str,
            "url": f"/streams/records/{rec_username}/{serve_file.name}",
            "downloadUrl": f"/streams/records/{rec_username}/{serve_file.name}",
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
    username = model.get('username')
    if not username:
        raise HTTPException(status_code=400, detail="Username requis")

    # Vérifier si le modèle existe déjà
    existing = await db.get_model(username)
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
    requested_source = _normalize_source_type(
        model.get("sourceType") or model.get("source_type")
    )
    source_type = requested_source or await _infer_source_type(username)
    if source_type not in _available_source_types():
        raise HTTPException(
            status_code=400,
            detail=f"Source '{source_type}' non disponible",
        )

    if "retentionDays" in model and model.get("retentionDays") is not None:
        retention_days = _normalize_retention_days(model.get("retentionDays"))
    else:
        retention_days = await _get_default_retention_days()

    # Ajouter dans SQLite
    await db.add_or_update_model(
        username=username,
        auto_record=auto_record,
        record_quality=record_quality,
        retention_days=retention_days,
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
    } for m in all_models]
    
    return {"success": True, "models": formatted}


@app.put("/api/models/{username}")
async def update_model(username: str, model_data: dict):
    """Met à jour les paramètres d'un modèle dans SQLite"""
    # Vérifier si le modèle existe
    existing = await db.get_model(username)
    if not existing:
        raise HTTPException(status_code=404, detail="Modèle introuvable")
    
    if "retentionDays" in model_data and model_data.get("retentionDays") is not None:
        retention_days = _normalize_retention_days(
            model_data.get("retentionDays"),
            existing.get("retention_days", 30),
        )
    else:
        retention_days = existing.get("retention_days", 30)

    requested_source = _normalize_source_type(
        model_data.get('sourceType') or model_data.get('source_type')
    )
    if requested_source and requested_source not in _available_source_types():
        raise HTTPException(
            status_code=400,
            detail=f"Source '{requested_source}' non disponible",
        )

    # Mettre à jour dans SQLite
    await db.add_or_update_model(
        username=username,
        auto_record=model_data.get('autoRecord', existing.get('auto_record', True)),
        record_quality=model_data.get('recordQuality', existing.get('record_quality', 'best')),
        retention_days=retention_days,
        source_type=requested_source,
    )
    
    # Récupérer le modèle mis à jour
    updated = await db.get_model(username)
    
    return {
        "success": True,
        "model": {
            "username": updated['username'],
            "autoRecord": bool(updated.get('auto_record', True)),
            "recordQuality": updated.get('record_quality', 'best'),
            "retentionDays": updated.get('retention_days', 30),
            "sourceType": updated.get('source_type') or 'chaturbate',
            "source_type": updated.get('source_type') or 'chaturbate',
        }
    }


@app.delete("/api/models/{username}")
async def delete_model(username: str):
    """Supprime un modèle de SQLite"""
    # Vérifier si le modèle existe
    existing = await db.get_model(username)
    if not existing:
        raise HTTPException(status_code=404, detail="Modèle introuvable")
    
    # Supprimer de SQLite
    await db.delete_model(username)
    
    # Récupérer la liste mise à jour
    all_models = await db.get_all_models()
    formatted = [{
        "username": m['username'],
        "autoRecord": bool(m.get('auto_record', True)),
        "recordQuality": m.get('record_quality', 'best'),
        "retentionDays": m.get('retention_days', 30),
        "sourceType": m.get('source_type') or 'chaturbate',
        "source_type": m.get('source_type') or 'chaturbate',
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
    matching_rec = next((r for r in existing_recs if r.get("filename") == filename), None)
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
    
    # Vérifier si CE FICHIER SPÉCIFIQUE est en cours d'enregistrement
    active_sessions = _all_recording_statuses()
    for session in active_sessions:
        if session.get('person') == username and session.get('running'):
            # Récupérer le chemin du fichier en cours d'enregistrement
            record_path = session.get('record_path', '')
            if record_path and file_stem in record_path:
                raise HTTPException(
                    status_code=403, 
                    detail="Impossible de supprimer l'enregistrement en cours."
                )
    
    # Chemins des fichiers
    records_dir = OUTPUT_DIR / "records" / username
    ts_path = records_dir / f"{file_stem}.ts"
    mp4_path = records_dir / f"{file_stem}.mp4"
    original_path = records_dir / filename
    thumb_path = OUTPUT_DIR / "thumbnails" / username / f"{file_stem}.jpg"

    # Si les fichiers ont déjà disparu (cleanup externe, volume remonté, etc.)
    # on doit quand même pouvoir nettoyer la row DB orpheline — sinon elle
    # reste affichée dans /recordings sans jamais pouvoir être retirée.
    has_db_row = any(Path(r['filename']).stem == file_stem for r in existing_recs)

    if not ts_path.exists() and not mp4_path.exists() and not original_path.exists() and not has_db_row:
        raise HTTPException(status_code=404, detail="Enregistrement introuvable")

    # Supprimer tous les fichiers associés
    try:
        files_deleted = []

        # Supprimer TS
        if ts_path.exists():
            ts_path.unlink()
            files_deleted.append("TS")
            logger.info("Fichier TS supprimé", username=username, file=ts_path.name)

        # Supprimer MP4
        if mp4_path.exists():
            mp4_path.unlink()
            files_deleted.append("MP4")
            logger.info("Fichier MP4 supprimé", username=username, file=mp4_path.name)

        if original_path.exists() and original_path not in {ts_path, mp4_path}:
            original_path.unlink()
            files_deleted.append(original_path.suffix.upper().lstrip(".") or "Fichier")
            logger.info("Fichier recording supprimé", username=username, file=original_path.name)

        # Supprimer miniature
        if thumb_path.exists():
            thumb_path.unlink()
            files_deleted.append("Miniature")

        # Supprimer de la base de données
        await db.delete_recording(username, filename)
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

    try:
        auto_delete_threshold = int(auto_delete_threshold_val) if auto_delete_threshold_val is not None else 90
    except (ValueError, TypeError):
        auto_delete_threshold = 90

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
        threshold = max(0, min(100, int(body["auto_delete_threshold"])))
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
    existing = await db.get_model(username)
    if not existing:
        raise HTTPException(status_code=404, detail="Model not found")

    auto_record = body.get("autoRecord")
    if auto_record is None:
        raise HTTPException(status_code=400, detail="autoRecord field required")

    new_auto = bool(auto_record)
    requested_source = _normalize_source_type(
        body.get("sourceType") or body.get("source_type")
    )
    if requested_source and requested_source not in _available_source_types():
        raise HTTPException(
            status_code=400,
            detail=f"Source '{requested_source}' non disponible",
        )
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
        source_type=requested_source,
    )
    return {
        "success": True,
        "autoRecord": new_auto,
        "recordQuality": record_quality,
        "sourceType": requested_source or existing.get("source_type") or "chaturbate",
    }


# ============================================
# Playback Position Endpoints
# ============================================

@app.get("/api/playback-position/{recording_id}")
async def get_playback_position(recording_id: str):
    """Get saved playback position for a recording"""
    pos = await db.get_playback_position(recording_id)
    if pos:
        return {
            "recordingId": recording_id,
            "position": pos["position_seconds"],
            "duration": pos["duration_seconds"],
        }
    return {"recordingId": recording_id, "position": 0, "duration": 0}


@app.post("/api/playback-position/{recording_id}")
async def save_playback_position(recording_id: str, body: dict):
    """Save playback position for a recording. Auto-delete if threshold reached."""
    position = body.get("position", 0)
    duration = body.get("duration", 0)
    username = body.get("username", "")
    await db.save_playback_position(recording_id, username, position, duration)

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
            threshold_val = await db.get_setting("auto_delete_threshold")
            try:
                threshold = int(threshold_val) if threshold_val else 90
            except (ValueError, TypeError):
                threshold = 90
            completion_pct = (position / duration) * 100
            if completion_pct >= threshold:
                should_delete = True

    return {"success": True, "autoDelete": should_delete}


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
    from app.tasks.monitor import get_video_duration, generate_recording_thumbnail
    
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
    from app.tasks.monitor import get_video_duration, generate_recording_thumbnail
    
    logger.background_task("recalculate-durations", "Démarrage du recalcul")
    
    try:
        # Récupérer tous les modèles
        models = await db.get_all_models()
        
        total_processed = 0
        total_updated = 0
        
        for model in models:
            username = model['username']
            records_dir = OUTPUT_DIR / "records" / username
            
            if not records_dir.exists():
                continue
            
            logger.info("Recalcul durées", username=username, task="recalculate-durations")
            
            ts_files = list(records_dir.glob("*.ts"))
            
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
                                thumbnail_path=thumbnail_path
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
    while True:
        try:
            await asyncio.sleep(180)  # Vérifier toutes les 3 minutes
            
            # Charger les modèles depuis SQLite avec auto_record activé
            models = await db.get_models_for_auto_record()
            if not models:
                continue
            
            # Récupérer les sessions actives
            active_sessions = _all_recording_statuses()
            
            for model in models:
                username = model.get('username')
                
                if not username:
                    continue
                
                # Vérifier si déjà en enregistrement
                is_recording = any(
                    s.get('person') == username and s.get('running')
                    for s in active_sessions
                )
                
                if is_recording:
                    continue  # Déjà en cours
                
                # Vérifier le statut depuis le cache SQLite (mis à jour par monitor)
                cached_status = await db.get_model(username)
                
                if cached_status and cached_status.get('is_online'):
                    # Modèle en ligne: résoudre le flux HLS
                    try:
                        hls_source = None
                        stream_headers = None
                        max_height = await _get_recording_height_for_quality(
                            cached_status.get("record_quality")
                        )
                        source_type = await _infer_source_type(username, cached_status)
                        if source_type not in _available_source_types():
                            logger.warning(
                                "Source inconnue pour auto-record",
                                task="auto-record",
                                username=username,
                                source_type=source_type,
                            )
                            continue
                        if _supports_browser_capture(source_type):
                            logger.background_task("auto-record", "Modèle WebRTC en ligne détecté", username=username)
                            try:
                                sess = _start_browser_capture(
                                    source_type,
                                    username,
                                    person=username,
                                    display_name=username,
                                    record=True,
                                )
                                ready = await asyncio.to_thread(sess.wait_until_ready, 35)
                                if not ready:
                                    browser_capture_manager.stop_session(sess.id)
                                    raise RuntimeError("Aucun flux video capturable dans le navigateur")
                                logger.success("Auto-enregistrement browser démarré",
                                             task="auto-record",
                                             username=username,
                                             session_id=sess.id)
                            except RuntimeError as e:
                                logger.warning("Impossible démarrer browser capture",
                                             task="auto-record",
                                             username=username,
                                             error=str(e))
                        else:
                            try:
                                resolved = await _resolve_stream(
                                    source_type, username, max_height
                                )
                                hls_source, stream_headers = _ffmpeg_stream_input(resolved)
                            except Exception as e:
                                logger.debug(
                                    "Auto-record resolve échec",
                                    task="auto-record",
                                    username=username,
                                    error=str(e),
                                )

                        if hls_source:
                            # Lancer l'enregistrement
                            logger.background_task("auto-record", "Modèle en ligne détecté", username=username)

                            try:
                                segment_duration_seconds, segment_size_bytes = await _get_recording_segment_limits()
                                sess = manager.start_session(
                                    input_url=hls_source,
                                    display_name=username,
                                    person=username,
                                    max_height=max_height,
                                    segment_duration_seconds=segment_duration_seconds,
                                    segment_size_bytes=segment_size_bytes,
                                    input_headers=stream_headers,
                                )

                                if sess:
                                    logger.success("Auto-enregistrement démarré",
                                                 task="auto-record",
                                                 username=username,
                                                 session_id=sess.id)
                            except RuntimeError as e:
                                logger.warning("Impossible démarrer enregistrement",
                                             task="auto-record",
                                             username=username,
                                             error=str(e))
                                continue

                    except Exception as e:
                        logger.error("Erreur vérification modèle",
                                   task="auto-record",
                                   username=username,
                                   error=str(e))
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
            
            # Charger les modèles depuis SQLite avec leurs paramètres de rétention
            models = await db.get_all_models()
            
            for model in models:
                username = model.get('username')
                retention_days = model.get('retention_days', 30)  # Défaut 30 jours

                if not username:
                    continue

                # retention_days == 0 means keep forever
                if retention_days == 0:
                    logger.debug("Rétention infinie, skip",
                               task="cleanup",
                               username=username)
                    continue

                records_dir = OUTPUT_DIR / "records" / username
                thumbnails_dir = OUTPUT_DIR / "thumbnails" / username

                if not records_dir.exists():
                    continue

                # Date limite (aujourd'hui - rétention)
                cutoff_date = datetime.now() - timedelta(days=retention_days)
                
                # Parcourir les fichiers .ts
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


async def sync_following_task(chaturbate_api, auth_service):
    """Background task: sync followed models every 5 minutes"""
    while True:
        try:
            await asyncio.sleep(300)  # 5 minutes

            status = auth_service.get_status()
            if not status.get("isLoggedIn"):
                continue

            models = await chaturbate_api.get_followed_models()
            if models:
                for model in models:
                    await db.upsert_followed_model(
                        username=model["username"],
                        display_name=model.get("display_name"),
                        is_online=model.get("is_online", False),
                        viewers=model.get("viewers", 0),
                        thumbnail_url=model.get("thumbnail_url"),
                        source_type="chaturbate",
                        room_status=model.get("room_status"),
                    )
                logger.debug("Following synced", count=len(models), task="following-sync")
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
    flaresolverr = FlareSolverrClient(FLARESOLVERR_URL)
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
            url=FLARESOLVERR_URL,
            version=fs_status.get("version"),
        )
    else:
        # Log the actual reason so users can diagnose DNS / network / timing
        # issues without having to dig through DEBUG logs.
        reason = (fs_status or {}).get("message") or "unknown"
        logger.warning(
            "FlareSolverr non disponible (optionnel)",
            url=FLARESOLVERR_URL,
            reason=reason,
        )

    # Initialize Chaturbate auth service
    cb_auth = ChaturbateAuthService(db, flaresolverr)
    await cb_auth.initialize()

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

    # Auto-login if env vars are set
    if CHATURBATE_USERNAME and CHATURBATE_PASSWORD:
        logger.info("Auto-login Chaturbate", username=CHATURBATE_USERNAME)
        result = await cb_auth.login(CHATURBATE_USERNAME, CHATURBATE_PASSWORD)
        if result.get("success"):
            logger.success("Chaturbate auto-login successful")
        else:
            logger.warning("Chaturbate auto-login failed", error=result.get("error"))

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
    asyncio.create_task(cleanup_old_recordings_task())
    asyncio.create_task(auto_convert_recordings_task(db, OUTPUT_DIR, manager, FFMPEG_PATH))
    if MEDIA_IMPORTS_ENABLED:
        global media_import_manager
        media_import_manager = MediaImportManager(db, OUTPUT_DIR, FFMPEG_PATH)
        asyncio.create_task(media_imports_task(media_import_manager))
    asyncio.create_task(sync_following_task(cb_api, cb_auth))
    asyncio.create_task(sync_cam4_following_task(cam4_auth_service))
    logger.info("Background tasks démarrés",
                tasks=["monitor", "ffmpeg-watchdog", "auto-record", "cleanup", "convert", "following-sync", "cam4-sync"])
