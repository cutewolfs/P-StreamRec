"""
Tâche background: Monitoring continu des modèles
Vérifie l'état en ligne, génère les miniatures et met à jour SQLite
"""
import asyncio
import aiohttp
import subprocess
import os
from pathlib import Path
from typing import TYPE_CHECKING
from datetime import datetime

if TYPE_CHECKING:
    from ..ffmpeg_runner import FFmpegManager
    from ..core.database import Database

from ..logger import logger
from ..core.config import MIN_RECORDING_BYTES, MIN_RECORDING_SECONDS, OUTPUT_DIR
from ..core.http_client import aiohttp_client_session, aiohttp_request_kwargs

# Intervalle de vérification (en secondes)
MONITOR_INTERVAL = 60  # Vérifie toutes les 60 secondes
THUMBNAIL_UPDATE_INTERVAL = 300  # Miniature offline: toutes les 5 minutes
THUMBNAIL_UPDATE_INTERVAL_LIVE = 60  # Miniature live: toutes les 60s pour refléter l'activité
SHORT_RECORDING_PROBE_BYTES = max(MIN_RECORDING_BYTES, 64 * 1024 * 1024)

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
        }
    except Exception as e:
        logger.debug("Erreur fallback CDN", username=username, error=str(e))

    return {
        "is_online": False,
        "viewers": 0,
        "hls_source": None,
        "room_status": None,
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
        ts_files = sorted(records_dir.glob("*.ts"), key=lambda p: p.stat().st_mtime, reverse=True)
        
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


async def update_recordings_cache(db: 'Database', username: str, output_dir: Path, ffmpeg_path: str = "ffmpeg"):
    """Met à jour le cache des enregistrements dans SQLite"""
    try:
        import time
        records_root = output_dir / "records"
        records_dir = records_root / username

        # Purge des rows DB orphelines : un fichier TS et MP4 qui n'existent
        # plus sur disque doivent être retirés de la DB, sinon le recording
        # reste affiché sur /recordings sans moyen de le supprimer depuis
        # l'UI (ex: suppression manuelle du fichier, volume réinitialisé).
        # Safety: on ne purge QUE si le dossier parent /records existe (sinon
        # volume démonté, on ne veut rien effacer).
        if records_root.exists():
            existing_recs = await db.get_recordings(username)
            for rec in existing_recs:
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

        if not records_dir.exists():
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

        for ts_file in records_dir.glob("*.ts"):
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
            
            await db.add_or_update_recording(
                username=username,
                filename=ts_file.name,
                file_path=str(ts_file),
                file_size=stat.st_size,
                recording_id=recording_id,
                duration_seconds=duration_seconds,
                thumbnail_path=thumbnail_path
            )
    
    except Exception as e:
        logger.debug("Erreur mise à jour cache enregistrements", username=username, error=str(e))

async def monitor_models_task(
    db: 'Database',
    manager: 'FFmpegManager',
    ffmpeg_path: str = "ffmpeg",
    chaturbate_auth=None,
    cam4_auth=None,
):
    """
    Tâche de monitoring en arrière-plan.

    Pour chaque modèle trackée, vérifie son statut via la source appropriée
    (Chaturbate ou CAM4) en switchant sur le source_type stocké.

    ``chaturbate_auth`` fournit les cookies authentifiés pour check_model_status
    Chaturbate (évite le redirect login, GH #11). ``cam4_auth`` est reservé
    pour d'éventuels besoins futurs côté CAM4.
    """
    logger.background_task("monitor", "Démarrage du monitoring continu")

    csrftoken = os.getenv("CHATURBATE_CSRFTOKEN")
    if csrftoken:
        logger.info("CSRF token détecté", has_token=True)

    await db.initialize()

    async with aiohttp_client_session() as session:
        while True:
            try:
                models = await db.get_all_models()

                if not models:
                    await asyncio.sleep(MONITOR_INTERVAL)
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
                        if source_type == "cam4":
                            try:
                                from ..services import cam4_source
                                status = await cam4_source.check_status(username)
                            except Exception as e:
                                logger.debug(
                                    "CAM4 check_status error",
                                    username=username,
                                    error=str(e),
                                )
                                status = {'is_online': False, 'viewers': 0, 'hls_source': None, 'room_status': None}
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
                await asyncio.sleep(MONITOR_INTERVAL)
            
            except Exception as e:
                logger.error("Erreur dans monitor task",
                           error=str(e),
                           exc_info=True)
                await asyncio.sleep(60)
