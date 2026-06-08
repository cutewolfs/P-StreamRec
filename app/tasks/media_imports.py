"""
Local media import scanner.

This is intentionally opt-in from app startup. It indexes user-provided videos
that are dropped into /data/records/<profile>/ without changing normal recorder
behavior when PSTREAMREC_MEDIA_IMPORTS is disabled.
"""
import asyncio
import hashlib
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from ..logger import logger
from .monitor import get_media_created_at, get_video_duration


SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi"}
DIRECT_PLAYABLE_EXTENSIONS = {".mp4", ".m4v", ".webm"}
TEMP_FILE_SUFFIXES = (
    ".tmp",
    ".part",
    ".partial",
    ".download",
    ".crdownload",
)
DEFAULT_MIN_AGE_SECONDS = 30


def is_supported_import_file(path: Path) -> bool:
    name = path.name
    if name.startswith(".") or any(name.endswith(suffix) for suffix in TEMP_FILE_SUFFIXES):
        return False
    return path.is_file() and path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS


def stable_import_recording_id(username: str, filename: str) -> str:
    digest = hashlib.sha256(f"{username}\0{filename}".encode("utf-8")).hexdigest()[:16]
    return f"import_{digest}"


def title_from_filename(filename: str) -> str:
    stem = Path(filename).stem.strip()
    title = re.sub(r"[_-]+", " ", stem)
    title = re.sub(r"\s+", " ", title).strip()
    return title or stem or filename


def _path_key(path_value: Optional[str]) -> Optional[str]:
    if not path_value:
        return None
    try:
        return str(Path(path_value).resolve())
    except Exception:
        return str(path_value)


def _file_exists(path_value: Optional[str]) -> bool:
    if not path_value:
        return False
    try:
        return Path(path_value).exists()
    except Exception:
        return False


def _import_metadata_ready(rec: dict) -> bool:
    return int(rec.get("duration_seconds") or 0) > 0 and _file_exists(rec.get("thumbnail_path"))


async def _run_ffmpeg(cmd: list[str], timeout: int = 3600) -> tuple[bool, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except Exception as e:
        return False, str(e)

    output = (stderr or stdout or b"").decode("utf-8", errors="replace").strip()
    return process.returncode == 0, output[-1000:]


async def generate_import_thumbnail(
    source_path: Path,
    output_dir: Path,
    username: str,
    recording_id: str,
    ffmpeg_path: str = "ffmpeg",
) -> Optional[str]:
    thumbs_dir = output_dir / "thumbnails" / username
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    thumb_path = thumbs_dir / f"{recording_id}.jpg"

    if thumb_path.exists():
        return str(thumb_path)

    for seek in ("00:00:30", "00:00:03", "00:00:00"):
        cmd = [
            ffmpeg_path,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            seek,
            "-i",
            str(source_path),
            "-vframes",
            "1",
            "-vf",
            "scale=320:-1",
            "-y",
            str(thumb_path),
        ]
        ok, error = await _run_ffmpeg(cmd, timeout=30)
        if ok and thumb_path.exists():
            return str(thumb_path)
        logger.debug(
            "Miniature import non générée",
            username=username,
            filename=source_path.name,
            seek=seek,
            error=error,
        )

    return None


async def create_playable_mp4_copy(
    source_path: Path,
    output_dir: Path,
    username: str,
    recording_id: str,
    ffmpeg_path: str = "ffmpeg",
) -> tuple[bool, Optional[Path], Optional[str]]:
    cache_dir = output_dir / "media_imports" / username
    cache_dir.mkdir(parents=True, exist_ok=True)
    final_path = cache_dir / f"{recording_id}.mp4"
    tmp_path = cache_dir / f"{recording_id}.tmp.mp4"

    for path in (tmp_path,):
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass

    remux_cmd = [
        ffmpeg_path,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-y",
        str(tmp_path),
    ]
    ok, error = await _run_ffmpeg(remux_cmd)

    if not ok:
        transcode_cmd = [
            ffmpeg_path,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            "-y",
            str(tmp_path),
        ]
        ok, error = await _run_ffmpeg(transcode_cmd)

    if ok and tmp_path.exists() and tmp_path.stat().st_size > 0:
        tmp_path.replace(final_path)
        return True, final_path, None

    if tmp_path.exists():
        try:
            tmp_path.unlink()
        except OSError:
            pass
    return False, None, error or "FFmpeg conversion failed"


async def ensure_import_profile(db, username: str):
    existing = await db.get_model(username)
    if existing:
        return

    await db.add_or_update_model(
        username=username,
        display_name=username,
        auto_record=False,
        record_quality="best",
        retention_days=0,
        source_type="chaturbate",
    )


async def remove_import_record(
    db,
    rec: dict,
    reason: str = "missing_source",
    delete_original: bool = False,
) -> bool:
    recording_id = rec.get("recording_id")
    if not recording_id:
        return False

    paths_to_remove = []
    for key in ("playable_path", "thumbnail_path"):
        value = rec.get(key)
        if value:
            paths_to_remove.append(Path(value))

    original = Path(rec.get("file_path") or "")
    if delete_original and rec.get("file_path"):
        paths_to_remove.append(original)
    for path in paths_to_remove:
        try:
            if path.exists() and (delete_original or path.resolve() != original.resolve()):
                path.unlink()
        except Exception as e:
            logger.warning(
                "Impossible supprimer fichier import associé",
                recording_id=recording_id,
                path=str(path),
                reason=reason,
                error=str(e),
            )

    await db.delete_recording_by_id(recording_id)
    await db.delete_playback_position(recording_id)
    logger.info("Import média retiré", recording_id=recording_id, reason=reason)
    return True


async def scan_media_imports(
    db,
    output_dir: Path,
    ffmpeg_path: str = "ffmpeg",
    min_age_seconds: int = DEFAULT_MIN_AGE_SECONDS,
) -> Dict[str, Any]:
    started_at = time.time()
    records_root = output_dir / "records"
    result: Dict[str, Any] = {
        "success": True,
        "profilesScanned": 0,
        "filesSeen": 0,
        "imported": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "removed": 0,
    }

    if not records_root.exists():
        result["durationSeconds"] = round(time.time() - started_at, 3)
        return result

    existing_imports = await db.get_import_recordings()
    for rec in existing_imports:
        source = Path(rec.get("file_path") or "")
        if source and not source.exists():
            if await remove_import_record(db, rec):
                result["removed"] += 1

    for profile_dir in records_root.iterdir():
        if not profile_dir.is_dir() or profile_dir.name.startswith("."):
            continue

        username = profile_dir.name
        result["profilesScanned"] += 1
        user_recordings = await db.get_recordings(username)
        by_filename = {rec.get("filename"): rec for rec in user_recordings}
        known_paths = {
            key
            for rec in user_recordings
            for key in (
                _path_key(rec.get("file_path")),
                _path_key(rec.get("mp4_path")),
                _path_key(rec.get("playable_path")),
            )
            if key
        }
        non_import_stems = {
            Path(rec.get("filename") or "").stem
            for rec in user_recordings
            if (rec.get("media_kind") or "recording") != "import"
        }

        for source_path in profile_dir.iterdir():
            if not is_supported_import_file(source_path):
                continue

            result["filesSeen"] += 1
            stat = source_path.stat()
            if time.time() - stat.st_mtime < min_age_seconds:
                result["skipped"] += 1
                continue

            source_key = _path_key(str(source_path))
            existing = by_filename.get(source_path.name)
            is_existing_import = existing and (existing.get("media_kind") == "import")
            if not is_existing_import and source_key in known_paths:
                result["skipped"] += 1
                continue
            if not is_existing_import and source_path.stem in non_import_stems:
                result["skipped"] += 1
                continue

            source_mtime = int(stat.st_mtime)
            existing_source_mtime = int((existing or {}).get("source_mtime") or 0)
            existing_size = int((existing or {}).get("file_size") or 0)
            existing_created_at = int((existing or {}).get("created_at") or 0)
            should_probe_created_at = (
                not is_existing_import
                or existing_source_mtime != source_mtime
                or existing_size != stat.st_size
                or existing_created_at in {0, source_mtime}
                or not _import_metadata_ready(existing or {})
            )
            title = title_from_filename(source_path.name)
            created_at = existing_created_at or source_mtime
            if should_probe_created_at:
                created_at = await get_media_created_at(
                    source_path,
                    ffmpeg_path,
                    fallback_timestamp=source_mtime,
                    reference_texts=[title],
                )
            if (
                is_existing_import
                and existing_source_mtime == source_mtime
                and existing_size == stat.st_size
                and existing_created_at == created_at
                and (existing.get("playable_path") or existing.get("import_status") == "failed")
                and _import_metadata_ready(existing)
            ):
                result["skipped"] += 1
                continue

            await ensure_import_profile(db, username)

            recording_id = (existing or {}).get("recording_id") or stable_import_recording_id(
                username,
                source_path.name,
            )
            duration_seconds = await get_video_duration(source_path, ffmpeg_path)
            thumbnail_path = await generate_import_thumbnail(
                source_path,
                output_dir,
                username,
                recording_id,
                ffmpeg_path,
            )

            playable_path: Optional[Path] = None
            playable_size: Optional[int] = None
            import_status = "ready"
            import_error = None

            if source_path.suffix.lower() in DIRECT_PLAYABLE_EXTENSIONS:
                playable_path = source_path
                playable_size = stat.st_size
            else:
                cached_path = output_dir / "media_imports" / username / f"{recording_id}.mp4"
                if cached_path.exists() and existing_source_mtime == source_mtime:
                    playable_path = cached_path
                    playable_size = cached_path.stat().st_size
                else:
                    ok, converted_path, error = await create_playable_mp4_copy(
                        source_path,
                        output_dir,
                        username,
                        recording_id,
                        ffmpeg_path,
                    )
                    if ok and converted_path:
                        playable_path = converted_path
                        playable_size = converted_path.stat().st_size
                    else:
                        import_status = "failed"
                        import_error = error or "Conversion failed"
                        result["failed"] += 1

            await db.add_or_update_recording(
                username=username,
                filename=source_path.name,
                file_path=str(source_path),
                file_size=stat.st_size,
                recording_id=recording_id,
                duration_seconds=duration_seconds,
                thumbnail_path=thumbnail_path,
                mp4_path=str(playable_path) if playable_path and playable_path != source_path else None,
                mp4_size=playable_size if playable_path and playable_path != source_path else None,
                is_converted=bool(playable_path),
                media_kind="import",
                title=title,
                import_status=import_status,
                import_error=import_error,
                source_mtime=source_mtime,
                playable_path=str(playable_path) if playable_path else None,
                playable_size=playable_size,
                protected_from_retention=True,
                created_at=created_at,
            )

            if is_existing_import:
                result["updated"] += 1
            else:
                result["imported"] += 1

    result["durationSeconds"] = round(time.time() - started_at, 3)
    logger.info("Scan imports médias terminé", **result)
    return result


class MediaImportManager:
    def __init__(self, db, output_dir: Path, ffmpeg_path: str = "ffmpeg"):
        self.db = db
        self.output_dir = output_dir
        self.ffmpeg_path = ffmpeg_path
        self._lock = asyncio.Lock()
        self.last_result: Optional[Dict[str, Any]] = None
        self.last_scan_at: Optional[int] = None

    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def scan(self) -> Dict[str, Any]:
        if self._lock.locked():
            return {
                "success": False,
                "running": True,
                "message": "Media import scan already running",
                "lastResult": self.last_result,
                "lastScanAt": self.last_scan_at,
            }

        async with self._lock:
            self.last_scan_at = int(time.time())
            self.last_result = await scan_media_imports(
                self.db,
                self.output_dir,
                self.ffmpeg_path,
            )
            return {
                **self.last_result,
                "running": False,
                "lastScanAt": self.last_scan_at,
            }


async def media_imports_task(manager: MediaImportManager, interval_seconds: int = 60):
    logger.info("Tâche imports médias démarrée", task="media-imports")
    await manager.scan()

    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await manager.scan()
        except Exception as e:
            logger.error(
                "Erreur tâche imports médias",
                task="media-imports",
                error=str(e),
                exc_info=True,
            )
            await asyncio.sleep(interval_seconds)
