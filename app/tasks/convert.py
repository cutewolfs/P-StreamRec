"""
Tâche de conversion automatique des enregistrements TS -> MP4
"""
import asyncio
import json
import time
from pathlib import Path
from typing import Optional
from ..logger import logger
from ..core.config import AUTO_CONVERT, KEEP_TS, MIN_RECORDING_BYTES, MIN_RECORDING_SECONDS
from .monitor import get_media_created_at, get_video_duration

# Nombre maximal de tentatives de conversion avant skip automatique
MAX_CONVERSION_ATTEMPTS = 3
SHORT_RECORDING_PROBE_BYTES = max(MIN_RECORDING_BYTES, 64 * 1024 * 1024)


def _select_best_video_stream_map(probe_data: dict) -> str:
    """Return an FFmpeg -map value for the highest-quality video stream."""
    candidates = []
    for stream in probe_data.get("streams") or []:
        try:
            index = int(stream.get("index"))
        except (TypeError, ValueError):
            continue
        try:
            height = int(stream.get("height") or stream.get("coded_height") or 0)
        except (TypeError, ValueError):
            height = 0
        try:
            width = int(stream.get("width") or stream.get("coded_width") or 0)
        except (TypeError, ValueError):
            width = 0
        try:
            bitrate = int(stream.get("bit_rate") or 0)
        except (TypeError, ValueError):
            bitrate = 0
        candidates.append((height, width, bitrate, -index, index))

    if not candidates:
        return "0:v:0"
    return f"0:{sorted(candidates, reverse=True)[0][-1]}"


async def _best_video_stream_map(ts_path: Path, ffmpeg_path: str = "ffmpeg") -> str:
    ffprobe_path = ffmpeg_path.replace("ffmpeg", "ffprobe")
    cmd = [
        ffprobe_path,
        "-v", "error",
        "-select_streams", "v",
        "-show_entries", "stream=index,width,height,coded_width,coded_height,bit_rate",
        "-of", "json",
        str(ts_path),
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode == 0 and stdout:
            return _select_best_video_stream_map(json.loads(stdout.decode("utf-8")))
        error_msg = stderr.decode("utf-8", errors="replace") if stderr else ""
        logger.debug("ffprobe video stream selection failed", ts_file=str(ts_path), error=error_msg[:300])
    except Exception as e:
        logger.debug("ffprobe video stream selection unavailable", ts_file=str(ts_path), error=str(e))
    return "0:v:0"


async def convert_ts_to_mp4(
    ts_path: Path, 
    mp4_path: Optional[Path] = None,
    ffmpeg_path: str = "ffmpeg"
) -> tuple[bool, Optional[Path], Optional[int]]:
    """
    Convertit un fichier TS en MP4 avec compression optimisée
    
    Returns:
        (success, mp4_path, mp4_size)
    """
    if not ts_path.exists():
        logger.error("Fichier TS introuvable", ts_path=str(ts_path))
        return False, None, None
    
    # Générer le nom du fichier MP4 si non fourni
    if mp4_path is None:
        mp4_path = ts_path.with_suffix('.mp4')
    
    logger.info("Remux TS->MP4 démarré",
               ts_file=ts_path.name,
               mp4_file=mp4_path.name)

    video_map = await _best_video_stream_map(ts_path, ffmpeg_path)

    # Remux only: the HLS source is already H.264+AAC, so copying codecs
    # produces a valid MP4 almost instantly with near-zero CPU usage. Keep a
    # conventional video-then-audio stream order for broad player compatibility.
    # -bsf:a aac_adtstoasc : reformat ADTS AAC (TS) into MP4-friendly ASC
    cmd = [
        ffmpeg_path,
        "-nostdin", "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts+igndts",
        "-i", str(ts_path),
        "-map", video_map,
        "-map", "0:a:0?",
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        "-y",
        str(mp4_path)
    ]
    
    try:
        # Lancer la conversion
        logger.debug("Commande FFmpeg", command=" ".join(cmd[:8]) + "...")
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode == 0:
            # Conversion réussie
            mp4_size = mp4_path.stat().st_size
            ts_size = ts_path.stat().st_size
            reduction = ((ts_size - mp4_size) / ts_size) * 100
            
            logger.success("Conversion réussie",
                         ts_file=ts_path.name,
                         mp4_file=mp4_path.name,
                         ts_size_mb=f"{ts_size / 1024 / 1024:.1f}",
                         mp4_size_mb=f"{mp4_size / 1024 / 1024:.1f}",
                         reduction_percent=f"{reduction:.1f}%")
            
            return True, mp4_path, mp4_size
        else:
            # Erreur de conversion
            error_msg = stderr.decode('utf-8') if stderr else "Unknown error"
            logger.error("Erreur conversion FFmpeg",
                        ts_file=ts_path.name,
                        error=error_msg[:500])
            return False, None, None
            
    except Exception as e:
        logger.error("Exception conversion",
                    ts_file=ts_path.name,
                    error=str(e),
                    exc_info=True)
        return False, None, None


async def _get_recording_settings(db) -> tuple[bool, bool]:
    """Read auto_convert and keep_ts settings from DB, falling back to env var defaults."""
    auto_convert_val = await db.get_setting("auto_convert")
    keep_ts_val = await db.get_setting("keep_ts")

    if auto_convert_val is not None:
        auto_convert = auto_convert_val.lower() in {"1", "true", "yes"}
    else:
        auto_convert = AUTO_CONVERT

    if keep_ts_val is not None:
        keep_ts = keep_ts_val.lower() in {"1", "true", "yes"}
    else:
        keep_ts = KEEP_TS

    return auto_convert, keep_ts


async def auto_convert_recordings_task(db, output_dir: Path, ffmpeg_manager, ffmpeg_path: str = "ffmpeg"):
    """
    Tâche qui scanne tous les fichiers .ts et les convertit s'ils ne sont pas en cours d'enregistrement.
    Respects auto_convert and keep_ts settings from DB.
    """
    logger.info("Tâche de conversion automatique démarrée", task="auto-convert")

    # SCAN INITIAL : Scanner tous les fichiers TS existants au démarrage
    logger.info("Scan initial des fichiers TS existants", task="auto-convert")
    try:
        records_root = output_dir / "records"
        if records_root.exists():
            for user_dir in records_root.iterdir():
                if user_dir.is_dir():
                    username = user_dir.name
                    for ts_file in user_dir.glob("*.ts"):
                        # Vérifier si déjà dans la DB
                        recordings = await db.get_recordings(username)
                        existing = next((r for r in recordings if r['filename'] == ts_file.name), None)

                        if not existing:
                            # Ajouter à la DB
                            logger.info("Indexation fichier existant", username=username, file=ts_file.name)
                            recording_id = f"{username}_{ts_file.stem}"
                            await db.add_or_update_recording(
                                username=username,
                                filename=ts_file.name,
                                file_path=str(ts_file),
                                file_size=ts_file.stat().st_size,
                                recording_id=recording_id,
                                duration_seconds=0,
                                is_converted=False,
                                created_at=await get_media_created_at(
                                    ts_file,
                                    ffmpeg_path,
                                    fallback_timestamp=int(ts_file.stat().st_mtime),
                                ),
                            )
        logger.success("Scan initial terminé", task="auto-convert")
    except Exception as e:
        logger.error("Erreur scan initial", error=str(e), exc_info=True)

    while True:
        try:
            # Scan every 2 minutes (remux is cheap but glob/stat over large dirs is not free)
            await asyncio.sleep(120)

            # Read settings from DB each iteration (runtime changeable)
            auto_convert, keep_ts = await _get_recording_settings(db)

            # Scanner TOUS les dossiers users dans /records pour trouver les fichiers .ts
            records_root = output_dir / "records"
            if not records_root.exists():
                continue

            # Récupérer les sessions actives pour savoir quels fichiers sont en cours d'enregistrement
            active_sessions = ffmpeg_manager.list_status()
            active_recordings = {}  # {username: recording_filename}
            for session in active_sessions:
                if session.get('running'):
                    username = session.get('person')
                    record_path = session.get('record_path', '')
                    if username and record_path:
                        filename = Path(record_path).name
                        active_recordings[username] = filename

            logger.debug("Sessions actives", active_count=len(active_recordings), active_users=list(active_recordings.keys()))

            for user_dir in records_root.iterdir():
                if not user_dir.is_dir():
                    continue

                username = user_dir.name

                # Charger les recordings UNE seule fois par user (évite N+1 DB calls)
                user_recordings = await db.get_recordings(username)
                recordings_by_filename = {r['filename']: r for r in user_recordings}

                # Scanner TOUS les fichiers .ts dans le dossier de l'utilisateur
                for ts_file in user_dir.glob("*.ts"):
                    ts_path = Path(ts_file)

                    # Vérifier si ce fichier est en cours d'enregistrement
                    if username in active_recordings and active_recordings[username] == ts_file.name:
                        logger.debug("Fichier en cours d'enregistrement, skip",
                                   username=username,
                                   file=ts_file.name)
                        continue

                    # Vérifier si le MP4 existe déjà
                    mp4_path = ts_path.with_suffix('.mp4')
                    if mp4_path.exists():
                        logger.debug("MP4 existe déjà, skip conversion",
                                   username=username,
                                   file=ts_file.name)

                        existing = recordings_by_filename.get(ts_file.name)

                        if existing and not existing.get('is_converted'):
                            # Mettre à jour la DB
                            created_at = await get_media_created_at(
                                ts_path,
                                ffmpeg_path,
                                fallback_timestamp=int(ts_path.stat().st_mtime),
                            )
                            await db.add_or_update_recording(
                                username=username,
                                filename=ts_file.name,
                                file_path=str(ts_path),
                                file_size=ts_path.stat().st_size if ts_path.exists() else existing['file_size'],
                                recording_id=existing.get('recording_id'),
                                duration_seconds=existing.get('duration_seconds', 0),
                                thumbnail_path=existing.get('thumbnail_path'),
                                mp4_path=str(mp4_path),
                                mp4_size=mp4_path.stat().st_size,
                                is_converted=True,
                                created_at=created_at,
                            )
                            logger.info("DB mise à jour pour MP4 existant",
                                      username=username,
                                      file=ts_file.name)

                        # Only delete TS if keep_ts is disabled
                        if not keep_ts and ts_path.exists():
                            try:
                                ts_path.unlink()
                                logger.success("Fichier TS supprimé (MP4 existe déjà)",
                                             username=username,
                                             ts_file=ts_file.name,
                                             mp4_file=mp4_path.name)
                            except Exception as e:
                                logger.error("Erreur suppression TS",
                                           ts_file=ts_file.name,
                                           error=str(e))
                        continue

                    # Vérifier si le fichier TS est stable (pas modifié depuis 180s)
                    last_modified = ts_path.stat().st_mtime
                    if time.time() - last_modified < 180:
                        # Fichier encore en cours d'écriture
                        logger.debug("Fichier modifié récemment, attente stabilité",
                                   file=ts_path.name,
                                   last_modified_ago=f"{time.time() - last_modified:.0f}s")
                        continue

                    existing = recordings_by_filename.get(ts_file.name)
                    ts_size = ts_path.stat().st_size
                    candidate_duration = int((existing or {}).get('duration_seconds') or 0)
                    if ts_size <= SHORT_RECORDING_PROBE_BYTES and candidate_duration == 0 and ts_size > 0:
                        candidate_duration = await get_video_duration(ts_path, ffmpeg_path)

                    is_short_fragment = (
                        ts_size == 0
                        or (candidate_duration == 0 and ts_size < MIN_RECORDING_BYTES)
                        or (0 < candidate_duration < MIN_RECORDING_SECONDS)
                    )
                    if is_short_fragment:
                        try:
                            if ts_path.exists():
                                ts_path.unlink()
                            await db.delete_recording(username, ts_file.name)
                            logger.warning(
                                "Fragment ignoré avant conversion",
                                username=username,
                                filename=ts_file.name,
                                duration_seconds=candidate_duration,
                                file_size=ts_size,
                                min_seconds=MIN_RECORDING_SECONDS,
                                min_bytes=MIN_RECORDING_BYTES,
                            )
                        except Exception as e:
                            logger.error(
                                "Erreur suppression fragment avant conversion",
                                username=username,
                                filename=ts_file.name,
                                error=str(e),
                            )
                        continue

                    # If auto_convert is disabled, just index the TS file in DB
                    if not auto_convert:
                        if not existing:
                            recording_id = f"{username}_{ts_file.stem}"
                            # Calculate duration from TS file
                            duration = candidate_duration
                            if duration == 0:
                                duration = await get_video_duration(ts_path, ffmpeg_path)
                            created_at = await get_media_created_at(
                                ts_path,
                                ffmpeg_path,
                                fallback_timestamp=int(ts_path.stat().st_mtime),
                            )
                            await db.add_or_update_recording(
                                username=username,
                                filename=ts_file.name,
                                file_path=str(ts_path),
                                file_size=ts_path.stat().st_size,
                                recording_id=recording_id,
                                duration_seconds=duration if duration > 0 else 0,
                                is_converted=False,
                                created_at=created_at,
                            )
                            logger.info("TS indexé (auto-convert désactivé)",
                                      username=username,
                                      filename=ts_file.name)
                        continue

                    # Skip si trop de tentatives ratées pour ce fichier
                    attempts = (existing or {}).get('conversion_attempts') or 0
                    if attempts >= MAX_CONVERSION_ATTEMPTS:
                        logger.debug("Conversion skippée (trop d'échecs)",
                                   username=username,
                                   filename=ts_file.name,
                                   attempts=attempts,
                                   task="auto-convert")
                        continue

                    # Le fichier n'est pas en cours d'enregistrement, on peut le convertir
                    logger.info("Début conversion automatique",
                              username=username,
                              filename=ts_file.name,
                              attempt=attempts + 1,
                              task="auto-convert")

                    success, mp4_path_result, mp4_size = await convert_ts_to_mp4(
                        ts_path,
                        mp4_path,
                        ffmpeg_path
                    )

                    if success and mp4_path_result:
                        # existing vient déjà du cache recordings_by_filename
                        recording_id = existing.get('recording_id') if existing else f"{username}_{ts_file.stem}"

                        # Reset compteur d'échec car la conversion a réussi
                        if (existing or {}).get('conversion_attempts'):
                            await db.reset_conversion_failure(recording_id)

                        # Recalculer la durée sur le fichier MP4 maintenant qu'il est stable
                        final_duration = await get_video_duration(mp4_path_result, ffmpeg_path)

                        # Utiliser la durée recalculée ou celle existante si le calcul échoue
                        if final_duration > 0:
                            duration_to_use = final_duration
                            logger.info("Durée recalculée après conversion",
                                      username=username,
                                      filename=ts_file.name,
                                      duration=final_duration)
                        else:
                            duration_to_use = existing.get('duration_seconds', 0) if existing else 0

                        created_at = await get_media_created_at(
                            ts_path,
                            ffmpeg_path,
                            fallback_timestamp=int(ts_path.stat().st_mtime) if ts_path.exists() else existing.get('created_at') if existing else None,
                        )
                        await db.add_or_update_recording(
                            username=username,
                            filename=ts_file.name,
                            file_path=str(ts_path),
                            file_size=ts_path.stat().st_size if ts_path.exists() else 0,
                            recording_id=recording_id,
                            duration_seconds=duration_to_use,
                            thumbnail_path=existing.get('thumbnail_path') if existing else None,
                            mp4_path=str(mp4_path_result),
                            mp4_size=mp4_size,
                            is_converted=True,
                            created_at=created_at,
                        )

                        # Only delete TS if keep_ts is disabled
                        if not keep_ts:
                            try:
                                if ts_path.exists():
                                    ts_path.unlink()
                                    logger.success("Fichier TS supprimé après conversion",
                                                 username=username,
                                                 ts_file=ts_file.name,
                                                 mp4_file=mp4_path_result.name)
                            except Exception as e:
                                logger.error("Erreur suppression TS",
                                           ts_file=ts_file.name,
                                           error=str(e))
                        else:
                            logger.info("Fichier TS conservé (keep_ts activé)",
                                      username=username,
                                      ts_file=ts_file.name)

                        logger.success("Enregistrement converti et indexé",
                                     username=username,
                                     filename=ts_file.name,
                                     mp4_file=mp4_path_result.name)
                    else:
                        # Tracker l'échec en DB pour éviter les retries infinis et informer l'UI
                        # S'assurer que l'enregistrement existe d'abord
                        if not existing:
                            duration = await get_video_duration(ts_path, ffmpeg_path)
                            recording_id = f"{username}_{ts_file.stem}"
                            created_at = await get_media_created_at(
                                ts_path,
                                ffmpeg_path,
                                fallback_timestamp=int(ts_path.stat().st_mtime),
                            )
                            await db.add_or_update_recording(
                                username=username,
                                filename=ts_file.name,
                                file_path=str(ts_path),
                                file_size=ts_path.stat().st_size,
                                recording_id=recording_id,
                                duration_seconds=duration if duration > 0 else 0,
                                is_converted=False,
                                created_at=created_at,
                            )
                        error_msg = f"FFmpeg conversion failed (attempt {attempts + 1}/{MAX_CONVERSION_ATTEMPTS})"
                        await db.mark_conversion_failed(username, ts_file.name, error_msg)
                        logger.error("Échec conversion",
                                   username=username,
                                   filename=ts_file.name,
                                   attempt=attempts + 1,
                                   max_attempts=MAX_CONVERSION_ATTEMPTS)

                    # Petit yield entre conversions; le remux est quasi-instantané
                    await asyncio.sleep(1)

        except Exception as e:
            logger.error("Erreur dans tâche de conversion",
                        error=str(e),
                        exc_info=True)
            await asyncio.sleep(60)
