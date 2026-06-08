import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import main as app_main
from app.core.database import Database
from app.tasks import media_imports
from app.tasks.media_imports import (
    MediaImportManager,
    mp4_needs_faststart_repair,
    scan_media_imports,
)


def mp4_box(box_type: bytes, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + box_type + payload


class MediaImportScannerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.tmpdir.name)
        self.db = Database(self.output_dir / "streamrec.db")
        await self.db.initialize()

    async def asyncTearDown(self):
        self.tmpdir.cleanup()

    def write_old_file(self, relative_path: str, data: bytes = b"video") -> Path:
        path = self.output_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        old = time.time() - 120
        os.utime(path, (old, old))
        return path

    def test_mp4_faststart_detector_flags_media_before_metadata(self):
        source = self.write_old_file(
            "records/model/slow_start.mp4",
            mp4_box(b"ftyp", b"isom0000")
            + mp4_box(b"mdat", b"1" * 16)
            + mp4_box(b"moov", b"0" * 16),
        )

        self.assertTrue(mp4_needs_faststart_repair(source))

    def test_mp4_faststart_detector_accepts_metadata_before_media(self):
        source = self.write_old_file(
            "records/model/fast_start.mp4",
            mp4_box(b"ftyp", b"isom0000")
            + mp4_box(b"moov", b"0" * 16)
            + mp4_box(b"mdat", b"1" * 16),
        )

        self.assertFalse(mp4_needs_faststart_repair(source))

    async def test_scan_imports_direct_mp4_and_creates_profile(self):
        source = self.write_old_file("records/new_profile/paid_clip.mp4")

        with (
            patch.object(media_imports, "get_video_duration", new=AsyncMock(return_value=123)),
            patch.object(media_imports, "get_media_created_at", new=AsyncMock(return_value=1704164645)),
            patch.object(media_imports, "generate_import_thumbnail", new=AsyncMock(return_value=None)),
        ):
            result = await scan_media_imports(self.db, self.output_dir, min_age_seconds=30)

        self.assertEqual(result["imported"], 1)
        model = await self.db.get_model("new_profile")
        self.assertIsNotNone(model)
        self.assertFalse(bool(model["auto_record"]))

        recs = await self.db.get_recordings("new_profile")
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec["media_kind"], "import")
        self.assertEqual(rec["title"], "paid clip")
        self.assertEqual(rec["file_path"], str(source))
        self.assertEqual(rec["playable_path"], str(source))
        self.assertTrue(bool(rec["protected_from_retention"]))
        self.assertEqual(rec["duration_seconds"], 123)
        self.assertEqual(rec["created_at"], 1704164645)

    async def test_scan_imports_non_faststart_mp4_uses_playable_copy(self):
        source = self.write_old_file(
            "records/model/slow_start.mp4",
            mp4_box(b"ftyp", b"isom0000")
            + mp4_box(b"mdat", b"1" * 16)
            + mp4_box(b"moov", b"0" * 16),
        )
        converted = self.output_dir / "media_imports/model/import_test.mp4"
        converted.parent.mkdir(parents=True, exist_ok=True)
        converted.write_bytes(b"mp4")

        with (
            patch.object(media_imports, "get_video_duration", new=AsyncMock(return_value=90)),
            patch.object(media_imports, "get_media_created_at", new=AsyncMock(return_value=1704164645)),
            patch.object(media_imports, "generate_import_thumbnail", new=AsyncMock(return_value=None)),
            patch.object(
                media_imports,
                "stable_import_recording_id",
                return_value="import_test",
            ),
            patch.object(
                media_imports,
                "create_playable_mp4_copy",
                new=AsyncMock(return_value=(True, converted, None)),
            ) as convert_mock,
        ):
            result = await scan_media_imports(self.db, self.output_dir, min_age_seconds=30)

        self.assertEqual(result["imported"], 1)
        convert_mock.assert_awaited_once()
        rec = (await self.db.get_recordings("model"))[0]
        self.assertEqual(rec["file_path"], str(source))
        self.assertEqual(rec["playable_path"], str(converted))
        self.assertEqual(rec["mp4_path"], str(converted))
        self.assertEqual(rec["import_status"], "ready")

    async def test_playable_mp4_copy_normalizes_timestamps(self):
        source = self.write_old_file("records/model/clip.mkv")
        commands = []

        async def fake_run_ffmpeg(cmd, timeout=3600):
            commands.append(cmd)
            tmp_path = self.output_dir / "media_imports/model/import_test.tmp.mp4"
            tmp_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_bytes(b"mp4")
            return True, ""

        with patch.object(media_imports, "_run_ffmpeg", new=fake_run_ffmpeg):
            ok, converted_path, error = await media_imports.create_playable_mp4_copy(
                source,
                self.output_dir,
                "model",
                "import_test",
                "ffmpeg",
            )

        self.assertTrue(ok)
        self.assertIsNone(error)
        self.assertTrue(converted_path.exists())
        remux_cmd = commands[0]
        self.assertIn("-fflags", remux_cmd)
        self.assertIn("+genpts+igndts", remux_cmd)
        self.assertIn("-avoid_negative_ts", remux_cmd)
        self.assertIn("make_zero", remux_cmd)

    async def test_scan_imports_uses_filename_title_as_date_reference(self):
        self.write_old_file("records/model/premium_show_2024-05-06_21h30.mp4")
        created_at_probe = AsyncMock(return_value=1704164645)

        with (
            patch.object(media_imports, "get_video_duration", new=AsyncMock(return_value=123)),
            patch.object(media_imports, "get_media_created_at", new=created_at_probe),
            patch.object(media_imports, "generate_import_thumbnail", new=AsyncMock(return_value=None)),
        ):
            result = await scan_media_imports(self.db, self.output_dir, min_age_seconds=30)

        self.assertEqual(result["imported"], 1)
        self.assertEqual(
            created_at_probe.await_args.kwargs["reference_texts"],
            ["premium show 2024 05 06 21h30"],
        )

    async def test_rescan_is_idempotent_and_removes_missing_sources(self):
        source = self.write_old_file("records/model/clip.mp4")
        thumb = self.output_dir / "thumbnails" / "model" / "clip.jpg"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"thumb")

        with (
            patch.object(media_imports, "get_video_duration", new=AsyncMock(return_value=10)),
            patch.object(media_imports, "generate_import_thumbnail", new=AsyncMock(return_value=str(thumb))),
        ):
            first = await scan_media_imports(self.db, self.output_dir, min_age_seconds=30)
            second = await scan_media_imports(self.db, self.output_dir, min_age_seconds=30)

        self.assertEqual(first["imported"], 1)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(len(await self.db.get_recordings("model")), 1)

        source.unlink()
        cleanup = await scan_media_imports(self.db, self.output_dir, min_age_seconds=30)
        self.assertEqual(cleanup["removed"], 1)
        self.assertEqual(await self.db.get_recordings("model"), [])

    async def test_rescan_repairs_existing_import_duration_and_thumbnail(self):
        source = self.write_old_file("records/model/clip.mp4")
        thumb = self.output_dir / "thumbnails" / "model" / "import_test.jpg"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"thumb")
        stat = source.stat()
        await self.db.add_or_update_recording(
            username="model",
            filename="clip.mp4",
            file_path=str(source),
            file_size=stat.st_size,
            recording_id="import_test",
            duration_seconds=0,
            playable_path=str(source),
            playable_size=stat.st_size,
            media_kind="import",
            import_status="ready",
            source_mtime=int(stat.st_mtime),
            protected_from_retention=True,
            created_at=int(stat.st_mtime),
        )

        with (
            patch.object(media_imports, "get_video_duration", new=AsyncMock(return_value=44)),
            patch.object(media_imports, "generate_import_thumbnail", new=AsyncMock(return_value=str(thumb))),
        ):
            result = await scan_media_imports(self.db, self.output_dir, min_age_seconds=30)

        self.assertEqual(result["updated"], 1)
        rec = (await self.db.get_recordings("model"))[0]
        self.assertEqual(rec["duration_seconds"], 44)
        self.assertEqual(rec["thumbnail_path"], str(thumb))

    async def test_rescan_repairs_existing_import_created_at_from_metadata(self):
        source = self.write_old_file("records/model/clip.mp4")
        thumb = self.output_dir / "thumbnails" / "model" / "import_test.jpg"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"thumb")
        stat = source.stat()
        old_created_at = int(stat.st_mtime)
        metadata_created_at = 1704164645
        await self.db.add_or_update_recording(
            username="model",
            filename="clip.mp4",
            file_path=str(source),
            file_size=stat.st_size,
            recording_id="import_test",
            duration_seconds=44,
            thumbnail_path=str(thumb),
            playable_path=str(source),
            playable_size=stat.st_size,
            media_kind="import",
            import_status="ready",
            source_mtime=int(stat.st_mtime),
            protected_from_retention=True,
            created_at=old_created_at,
        )

        with (
            patch.object(media_imports, "get_video_duration", new=AsyncMock(return_value=44)),
            patch.object(media_imports, "get_media_created_at", new=AsyncMock(return_value=metadata_created_at)),
            patch.object(media_imports, "generate_import_thumbnail", new=AsyncMock(return_value=str(thumb))),
        ):
            result = await scan_media_imports(self.db, self.output_dir, min_age_seconds=30)

        self.assertEqual(result["updated"], 1)
        rec = (await self.db.get_recordings("model"))[0]
        self.assertEqual(rec["created_at"], metadata_created_at)

    async def test_scan_skips_recent_and_temp_files(self):
        recent = self.output_dir / "records/model/recent.mp4"
        recent.parent.mkdir(parents=True, exist_ok=True)
        recent.write_bytes(b"video")
        self.write_old_file("records/model/file.mp4.tmp")

        result = await scan_media_imports(self.db, self.output_dir, min_age_seconds=30)

        self.assertEqual(result["filesSeen"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(await self.db.get_recordings("model"), [])

    async def test_mkv_import_uses_playable_mp4_copy(self):
        source = self.write_old_file("records/model/bonus_clip.mkv")
        converted = self.output_dir / "media_imports/model/import_test.mp4"
        converted.parent.mkdir(parents=True, exist_ok=True)
        converted.write_bytes(b"mp4")

        with (
            patch.object(media_imports, "get_video_duration", new=AsyncMock(return_value=90)),
            patch.object(media_imports, "generate_import_thumbnail", new=AsyncMock(return_value=None)),
            patch.object(
                media_imports,
                "create_playable_mp4_copy",
                new=AsyncMock(return_value=(True, converted, None)),
            ),
        ):
            result = await scan_media_imports(self.db, self.output_dir, min_age_seconds=30)

        self.assertEqual(result["imported"], 1)
        rec = (await self.db.get_recordings("model"))[0]
        self.assertEqual(rec["file_path"], str(source))
        self.assertEqual(rec["playable_path"], str(converted))
        self.assertEqual(rec["mp4_path"], str(converted))
        self.assertEqual(rec["import_status"], "ready")


class MediaImportApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_db = app_main.db
        self.original_output_dir = app_main.OUTPUT_DIR
        self.original_enabled = app_main.MEDIA_IMPORTS_ENABLED
        self.original_manager = app_main.media_import_manager

        self.tmpdir = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.tmpdir.name)
        app_main.OUTPUT_DIR = self.output_dir
        app_main.db = Database(self.output_dir / "streamrec.db")
        app_main.MEDIA_IMPORTS_ENABLED = True
        app_main.media_import_manager = MediaImportManager(app_main.db, self.output_dir, "ffmpeg")
        await app_main.db.initialize()

        self.source = self.output_dir / "records/model/imported.mp4"
        self.source.parent.mkdir(parents=True, exist_ok=True)
        self.source.write_bytes(b"0123456789")
        old = time.time() - 120
        os.utime(self.source, (old, old))

        await app_main.db.add_or_update_model(
            username="model",
            display_name="model",
            auto_record=False,
            retention_days=0,
        )
        await app_main.db.add_or_update_recording(
            username="model",
            filename="imported.mp4",
            file_path=str(self.source),
            file_size=self.source.stat().st_size,
            recording_id="import_test",
            duration_seconds=10,
            playable_path=str(self.source),
            playable_size=self.source.stat().st_size,
            is_converted=True,
            media_kind="import",
            title="Imported",
            import_status="ready",
            protected_from_retention=True,
            created_at=int(old),
        )
        self.client = TestClient(app_main.app)

    async def asyncTearDown(self):
        app_main.db = self.original_db
        app_main.OUTPUT_DIR = self.original_output_dir
        app_main.MEDIA_IMPORTS_ENABLED = self.original_enabled
        app_main.media_import_manager = self.original_manager
        self.tmpdir.cleanup()

    async def test_status_rescan_listing_stream_and_delete(self):
        status = self.client.get("/api/media-imports/status")
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json()["enabled"])

        app_main.media_import_manager.scan = AsyncMock(return_value={"success": True, "imported": 0})
        rescan = self.client.post("/api/media-imports/rescan")
        self.assertEqual(rescan.status_code, 200)
        self.assertTrue(rescan.json()["success"])

        listing = self.client.get("/api/recordings/model")
        self.assertEqual(listing.status_code, 200)
        item = listing.json()["recordings"][0]
        self.assertTrue(item["isImported"])
        self.assertEqual(item["url"], "/streams/media/import_test")
        self.assertEqual(item["downloadUrl"], "/streams/media/import_test?download=1")

        ranged = self.client.get(
            "/streams/media/import_test",
            headers={"Range": "bytes=0-3"},
        )
        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(ranged.content, b"0123")
        self.assertEqual(ranged.headers["content-range"], "bytes 0-3/10")

        await app_main.db.set_setting("auto_delete_watched", "true")
        position = self.client.post(
            "/api/playback-position/import_test",
            json={"username": "model", "position": 10, "duration": 10},
        )
        self.assertEqual(position.status_code, 200)
        self.assertFalse(position.json()["autoDelete"])

        deleted = self.client.delete("/api/recordings/model/imported.mp4")
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse(self.source.exists())
        self.assertEqual(await app_main.db.get_recordings("model"), [])

    async def test_status_disabled_when_feature_flag_is_off(self):
        app_main.MEDIA_IMPORTS_ENABLED = False
        status = self.client.get("/api/media-imports/status")
        self.assertEqual(status.status_code, 200)
        self.assertFalse(status.json()["enabled"])

        rescan = self.client.post("/api/media-imports/rescan")
        self.assertEqual(rescan.status_code, 200)
        self.assertFalse(rescan.json()["success"])


if __name__ == "__main__":
    unittest.main()
