"""Safe retention cleanup for locally recorded media."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Iterable, Optional

from ..logger import logger
from .monitor import recording_timestamp_from_filename


RETENTION_MEDIA_EXTENSIONS = frozenset({".ts", ".mp4", ".webm"})


def _path_key(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path.absolute())


def _is_inside(path: Path, roots: Iterable[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any(resolved.is_relative_to(root) for root in roots)


def _media_paths(recording: dict) -> list[Path]:
    paths: list[Path] = []
    for key in ("file_path", "mp4_path", "playable_path"):
        value = recording.get(key)
        if not value:
            continue
        path = Path(str(value))
        if path.suffix.lower() in RETENTION_MEDIA_EXTENSIONS:
            paths.append(path)
    return paths


def _recording_timestamp(recording: dict) -> Optional[int]:
    references = [recording.get("filename")]
    references.extend(path.name for path in _media_paths(recording))
    for reference in references:
        parsed = recording_timestamp_from_filename(str(reference or ""))
        if parsed is not None:
            return parsed

    try:
        created_at = int(recording.get("created_at") or 0)
    except (TypeError, ValueError):
        created_at = 0
    return created_at or None


def _stem_key(path: Path) -> tuple[str, str]:
    return (_path_key(path.parent), path.stem)


def _recording_belongs_to_roots(
    recording: dict,
    roots: list[Path],
    excluded_roots: list[Path],
) -> bool:
    paths = _media_paths(recording)
    if any(
        _is_inside(path, roots) and not _is_inside(path, excluded_roots)
        for path in paths
    ):
        return True
    if paths:
        return False

    filename = str(recording.get("filename") or "").strip()
    return bool(
        filename
        and any(
            _is_inside(root / filename, roots)
            and not _is_inside(root / filename, excluded_roots)
            for root in roots
        )
    )


def _profile_folder_name(username: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(username or "").strip())
    return cleaned.strip(".-")


def _safe_record_roots(output_dir: Path, username: str, record_dirs: Iterable[Path]) -> list[Path]:
    records_root = (output_dir / "records").resolve()
    profile_folder = _profile_folder_name(username)
    if not profile_folder:
        return []
    roots: list[Path] = []
    seen: set[str] = set()
    for value in record_dirs:
        try:
            root = Path(value).resolve()
            relative = root.relative_to(records_root)
        except (OSError, ValueError):
            logger.warning(
                "Chemin de rétention hors records ignoré",
                username=username,
                path=str(value),
            )
            continue
        if not relative.parts or relative.parts[0].lower() != profile_folder.lower():
            logger.warning(
                "Chemin de rétention hors profil ignoré",
                username=username,
                path=str(value),
            )
            continue
        key = str(root)
        if key not in seen:
            seen.add(key)
            roots.append(root)
    return roots


def _update_metadata_caches(entries: dict[Path, set[str]]) -> None:
    for directory, filenames in entries.items():
        cache_file = directory / ".metadata_cache.json"
        if not cache_file.is_file():
            continue
        temp_file = directory / ".metadata_cache.retention.tmp"
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
            if not isinstance(cache, dict):
                continue
            changed = False
            for filename in filenames:
                if filename in cache:
                    del cache[filename]
                    changed = True
            if changed:
                temp_file.write_text(json.dumps(cache), encoding="utf-8")
                temp_file.replace(cache_file)
        except (OSError, ValueError, TypeError) as exc:
            logger.warning(
                "Cache metadata de rétention non mis à jour",
                path=str(cache_file),
                error=str(exc),
            )
        finally:
            try:
                temp_file.unlink(missing_ok=True)
            except OSError:
                pass


async def cleanup_retention_job(
    db,
    output_dir: Path,
    username: str,
    retention_days: int,
    record_dirs: Iterable[Path],
    *,
    now_timestamp: Optional[float] = None,
    active_paths: Iterable[str | Path] = (),
    excluded_dirs: Iterable[Path] = (),
) -> dict[str, int]:
    """Delete expired recorder outputs and their associated state for one profile."""
    result = {
        "deleted_files": 0,
        "deleted_recordings": 0,
        "deleted_playback": 0,
        "skipped_protected": 0,
        "skipped_active": 0,
        "errors": 0,
    }
    try:
        retention_days = int(retention_days)
    except (TypeError, ValueError):
        retention_days = 30
    if retention_days <= 0:
        return result

    roots = _safe_record_roots(output_dir, username, record_dirs)
    if not roots:
        return result
    excluded_roots = [
        path
        for path in _safe_record_roots(output_dir, username, excluded_dirs)
        if any(path != root and path.is_relative_to(root) for root in roots)
    ]

    cutoff_timestamp = float(now_timestamp if now_timestamp is not None else time.time()) - (
        retention_days * 86400
    )
    active_keys = {_path_key(Path(path)) for path in active_paths if str(path or "").strip()}
    thumbnail_root = (output_dir / "thumbnails" / username).resolve()
    cache_removals: dict[Path, set[str]] = {}
    handled_keys: set[str] = set()
    reserved_stems: set[tuple[str, str]] = set()

    def in_record_scope(path: Path) -> bool:
        return _is_inside(path, roots) and not _is_inside(path, excluded_roots)

    def remember_cache_removal(path: Path) -> None:
        if in_record_scope(path):
            cache_removals.setdefault(path.parent, set()).add(path.name)

    def unlink_allowed(
        path: Path,
        allowed_roots: Iterable[Path],
        *,
        exclude_nested_policies: bool = False,
    ) -> bool:
        if not _is_inside(path, allowed_roots) or (
            exclude_nested_policies and _is_inside(path, excluded_roots)
        ):
            return False
        try:
            if path.is_file():
                path.unlink()
                result["deleted_files"] += 1
                remember_cache_removal(path)
            return True
        except OSError as exc:
            result["errors"] += 1
            logger.warning(
                "Fichier de rétention non supprimé",
                username=username,
                path=str(path),
                error=str(exc),
            )
            return False

    recordings = await db.get_recordings(username)
    for recording in recordings:
        if not _recording_belongs_to_roots(recording, roots, excluded_roots):
            continue

        media_paths = _media_paths(recording)
        row_stems = {
            _stem_key(path)
            for path in media_paths
            if in_record_scope(path)
        }
        if recording.get("media_kind") == "import" or recording.get("protected_from_retention"):
            reserved_stems.update(row_stems)
            result["skipped_protected"] += 1
            continue

        timestamp = _recording_timestamp(recording)
        if timestamp is None or timestamp >= cutoff_timestamp:
            reserved_stems.update(row_stems)
            continue

        if any(_path_key(path) in active_keys for path in media_paths):
            reserved_stems.update(row_stems)
            result["skipped_active"] += 1
            continue

        paths_to_remove: dict[str, Path] = {}
        unsafe_existing_media = False
        for path in media_paths:
            if not in_record_scope(path):
                if path.exists():
                    unsafe_existing_media = True
                continue
            paths_to_remove[_path_key(path)] = path
            for suffix in RETENTION_MEDIA_EXTENSIONS:
                companion = path.with_suffix(suffix)
                paths_to_remove[_path_key(companion)] = companion

        active_companions = [key for key in paths_to_remove if key in active_keys]
        if unsafe_existing_media or active_companions:
            reserved_stems.update(row_stems)
            if active_companions:
                result["skipped_active"] += 1
            continue

        deletion_ok = True
        for key, path in paths_to_remove.items():
            if not unlink_allowed(path, roots, exclude_nested_policies=True):
                deletion_ok = False
            handled_keys.add(key)

        if any(path.exists() for path in paths_to_remove.values()):
            deletion_ok = False
        if not deletion_ok:
            reserved_stems.update(row_stems)
            continue

        thumbnail_paths: dict[str, Path] = {}
        thumbnail_value = recording.get("thumbnail_path")
        if thumbnail_value:
            thumbnail = Path(str(thumbnail_value))
            thumbnail_paths[_path_key(thumbnail)] = thumbnail
        for path in paths_to_remove.values():
            generated = thumbnail_root / f"{path.stem}.jpg"
            thumbnail_paths[_path_key(generated)] = generated
        for thumbnail in thumbnail_paths.values():
            unlink_allowed(thumbnail, (thumbnail_root,))

        recording_id = str(recording.get("recording_id") or "").strip()
        try:
            if recording_id:
                await db.delete_recording_by_id(recording_id)
                await db.delete_playback_position(recording_id)
                result["deleted_playback"] += 1
            else:
                await db.delete_recording(username, recording.get("filename") or "")
            result["deleted_recordings"] += 1
        except Exception as exc:
            result["errors"] += 1
            logger.warning(
                "Etat DB de rétention non supprimé",
                username=username,
                recording_id=recording_id,
                error=str(exc),
            )

    candidates: dict[str, Path] = {}
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for path in root.rglob("*"):
                if (
                    path.is_file()
                    and path.suffix.lower() in RETENTION_MEDIA_EXTENSIONS
                    and in_record_scope(path)
                ):
                    candidates[_path_key(path)] = path
        except OSError as exc:
            result["errors"] += 1
            logger.warning("Scan rétention impossible", path=str(root), error=str(exc))

    for key, path in candidates.items():
        if key in handled_keys or _stem_key(path) in reserved_stems:
            continue
        if key in active_keys:
            result["skipped_active"] += 1
            continue
        timestamp = recording_timestamp_from_filename(path.name)
        if timestamp is None or timestamp >= cutoff_timestamp:
            continue
        if unlink_allowed(path, roots, exclude_nested_policies=True):
            generated_thumbnail = thumbnail_root / f"{path.stem}.jpg"
            unlink_allowed(generated_thumbnail, (thumbnail_root,))

    _update_metadata_caches(cache_removals)
    return result
