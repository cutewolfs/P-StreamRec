"""
Tâche de conversion automatique des enregistrements TS -> MP4
"""
import asyncio
import time
import subprocess
from pathlib import Path
from typing import Optional
from ..logger import logger
from ..core.config import AUTO_CONVERT, KEEP_TS


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
    
    logger.info("Conversion TS->MP4 démarrée", 
               ts_file=ts_path.name, 
               mp4_file=mp4_path.name)
    
    # Commande FFmpeg optimisée pour compression
    # -c:v libx264 : codec H.264 (meilleure compression)
    # -crf 23 : qualité (18-28, 23 = bon équilibre qualité/taille)
    # -preset medium : vitesse de compression (fast, medium, slow)
    # -c:a aac : codec audio AAC
    # -b:a 128k : bitrate audio
    cmd = [
        ffmpeg_path,
        "-i", str(ts_path),
        "-c:v", "libx264",
        "-crf", "23",
        "-preset", "medium",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",  # Optimisation streaming
        "-y",  # Overwrite
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
                                is_converted=False
                            )
        logger.success("Scan initial terminé", task="auto-convert")
    except Exception as e:
        logger.error("Erreur scan initial", error=str(e), exc_info=True)

    while True:
        try:
            await asyncio.sleep(30)  # Vérifier toutes les 30 secondes

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

                        # Vérifier si dans la DB et mettre à jour si nécessaire
                        recordings = await db.get_recordings(username)
                        existing = next((r for r in recordings if r['filename'] == ts_file.name), None)

                        if existing and not existing.get('is_converted'):
                            # Mettre à jour la DB
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
                                is_converted=True
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

                    # Vérifier si le fichier TS est stable (pas modifié depuis 60s)
                    last_modified = ts_path.stat().st_mtime
                    if time.time() - last_modified < 60:
                        # Fichier encore en cours d'écriture
                        logger.debug("Fichier modifié récemment, attente stabilité",
                                   file=ts_path.name,
                                   last_modified_ago=f"{time.time() - last_modified:.0f}s")
                        continue

                    # If auto_convert is disabled, just index the TS file in DB
                    if not auto_convert:
                        recordings = await db.get_recordings(username)
                        existing = next((r for r in recordings if r['filename'] == ts_file.name), None)
                        if not existing:
                            recording_id = f"{username}_{ts_file.stem}"
                            # Calculate duration from TS file
                            from .monitor import get_video_duration
                            duration = await get_video_duration(ts_path, ffmpeg_path)
                            await db.add_or_update_recording(
                                username=username,
                                filename=ts_file.name,
                                file_path=str(ts_path),
                                file_size=ts_path.stat().st_size,
                                recording_id=recording_id,
                                duration_seconds=duration if duration > 0 else 0,
                                is_converted=False
                            )
                            logger.info("TS indexé (auto-convert désactivé)",
                                      username=username,
                                      filename=ts_file.name)
                        continue

                    # Le fichier n'est pas en cours d'enregistrement, on peut le convertir
                    logger.info("Début conversion automatique",
                              username=username,
                              filename=ts_file.name,
                              task="auto-convert")

                    success, mp4_path_result, mp4_size = await convert_ts_to_mp4(
                        ts_path,
                        mp4_path,
                        ffmpeg_path
                    )

                    if success and mp4_path_result:
                        # Mettre à jour ou créer l'enregistrement dans la DB
                        recordings = await db.get_recordings(username)
                        existing = next((r for r in recordings if r['filename'] == ts_file.name), None)

                        recording_id = existing.get('recording_id') if existing else f"{username}_{ts_file.stem}"

                        # Recalculer la durée sur le fichier MP4 maintenant qu'il est stable
                        from .monitor import get_video_duration
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
                            is_converted=True
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
                        logger.error("Échec conversion",
                                   username=username,
                                   filename=ts_file.name)

                    # Attendre un peu entre chaque conversion pour éviter surcharge
                    await asyncio.sleep(5)

        except Exception as e:
            logger.error("Erreur dans tâche de conversion",
                        error=str(e),
                        exc_info=True)
            await asyncio.sleep(60)
