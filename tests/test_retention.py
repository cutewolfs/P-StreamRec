import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app import main as app_main
from app.core.database import Database
from app.tasks.retention import cleanup_retention_job


FIXED_NOW = datetime(2026, 7, 12, 12, 0, 0).timestamp()


class RetentionJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.temp_dir.name)
        self.records_dir = self.output_dir / "records" / "alice" / "videos" / "record"
        self.records_dir.mkdir(parents=True)
        self.db = Database(self.output_dir / "streamrec.db")
        await self.db.initialize()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_cleans_actual_timestamp_username_segmented_and_browser_names(self):
        expired_paths = [
            self.records_dir / "20240102_030405_abcdef.ts",
            self.records_dir / "alice_20240102-030405_part001.mp4",
            self.records_dir / "alice_20240102-030405.webm",
        ]
        for path in expired_paths:
            path.write_bytes(b"recording")
        recent = self.records_dir / "alice_20260711-120000.mp4"
        recent.write_bytes(b"recent")
        unrecognized_import = self.records_dir / "vacation-video.mp4"
        unrecognized_import.write_bytes(b"user media")

        result = await cleanup_retention_job(
            self.db,
            self.output_dir,
            "alice",
            30,
            [self.records_dir],
            now_timestamp=FIXED_NOW,
        )

        self.assertTrue(all(not path.exists() for path in expired_paths))
        self.assertTrue(recent.exists())
        self.assertTrue(unrecognized_import.exists())
        self.assertEqual(3, result["deleted_files"])

    async def test_cleans_companions_thumbnail_cache_db_and_playback(self):
        stem = "alice_20240102-030405_part001"
        ts_path = self.records_dir / f"{stem}.ts"
        mp4_path = self.records_dir / f"{stem}.mp4"
        ts_path.write_bytes(b"transport stream")
        mp4_path.write_bytes(b"converted mp4")
        thumbnail = self.output_dir / "thumbnails" / "alice" / f"{stem}.jpg"
        thumbnail.parent.mkdir(parents=True)
        thumbnail.write_bytes(b"thumbnail")
        cache_file = self.records_dir / ".metadata_cache.json"
        cache_file.write_text(
            json.dumps({ts_path.name: {"duration": 20}, "keep.ts": {"duration": 10}}),
            encoding="utf-8",
        )

        await self.db.add_or_update_recording(
            username="alice",
            filename=ts_path.name,
            file_path=str(ts_path),
            file_size=ts_path.stat().st_size,
            recording_id="expired_recording",
            duration_seconds=20,
            thumbnail_path=str(thumbnail),
            mp4_path=str(mp4_path),
            mp4_size=mp4_path.stat().st_size,
            is_converted=True,
            created_at=int(datetime(2024, 1, 2).timestamp()),
        )
        await self.db.save_playback_position("expired_recording", "alice", 5, 20)

        result = await cleanup_retention_job(
            self.db,
            self.output_dir,
            "alice",
            30,
            [self.records_dir],
            now_timestamp=FIXED_NOW,
        )

        self.assertFalse(ts_path.exists())
        self.assertFalse(mp4_path.exists())
        self.assertFalse(thumbnail.exists())
        self.assertIsNone(await self.db.get_recording_by_id("expired_recording"))
        self.assertIsNone(await self.db.get_playback_position("expired_recording"))
        self.assertNotIn(ts_path.name, json.loads(cache_file.read_text(encoding="utf-8")))
        self.assertEqual(1, result["deleted_recordings"])
        self.assertEqual(1, result["deleted_playback"])

    async def test_protected_import_and_its_state_are_never_touched(self):
        imported = self.records_dir / "alice_20240102-030405.mp4"
        imported.write_bytes(b"protected import")
        thumbnail = self.output_dir / "thumbnails" / "alice" / f"{imported.stem}.jpg"
        thumbnail.parent.mkdir(parents=True)
        thumbnail.write_bytes(b"thumbnail")
        await self.db.add_or_update_recording(
            username="alice",
            filename=imported.name,
            file_path=str(imported),
            file_size=imported.stat().st_size,
            recording_id="protected_import",
            thumbnail_path=str(thumbnail),
            media_kind="import",
            protected_from_retention=True,
            created_at=int(datetime(2024, 1, 2).timestamp()),
        )
        await self.db.save_playback_position("protected_import", "alice", 5, 20)

        result = await cleanup_retention_job(
            self.db,
            self.output_dir,
            "alice",
            30,
            [self.records_dir],
            now_timestamp=FIXED_NOW,
        )

        self.assertTrue(imported.exists())
        self.assertTrue(thumbnail.exists())
        self.assertIsNotNone(await self.db.get_recording_by_id("protected_import"))
        self.assertIsNotNone(await self.db.get_playback_position("protected_import"))
        self.assertEqual(1, result["skipped_protected"])
        self.assertEqual(0, result["deleted_files"])

    async def test_active_and_out_of_profile_files_are_never_deleted(self):
        active = self.records_dir / "alice_20240102-030405.webm"
        active.write_bytes(b"active")
        outside = self.output_dir / "records" / "bob" / "20240102_030405_other.mp4"
        outside.parent.mkdir(parents=True)
        outside.write_bytes(b"outside")

        result = await cleanup_retention_job(
            self.db,
            self.output_dir,
            "alice",
            30,
            [self.records_dir, outside.parent],
            now_timestamp=FIXED_NOW,
            active_paths=[active],
        )

        self.assertTrue(active.exists())
        self.assertTrue(outside.exists())
        self.assertEqual(1, result["skipped_active"])
        self.assertEqual(0, result["deleted_files"])

    async def test_active_companion_prevents_partial_recording_deletion(self):
        stem = "alice_20240102-030405"
        ts_path = self.records_dir / f"{stem}.ts"
        mp4_path = self.records_dir / f"{stem}.mp4"
        ts_path.write_bytes(b"source")
        mp4_path.write_bytes(b"active conversion")
        await self.db.add_or_update_recording(
            username="alice",
            filename=ts_path.name,
            file_path=str(ts_path),
            file_size=ts_path.stat().st_size,
            recording_id="active_companion",
            mp4_path=str(mp4_path),
            mp4_size=mp4_path.stat().st_size,
            is_converted=True,
            created_at=int(datetime(2024, 1, 2).timestamp()),
        )

        result = await cleanup_retention_job(
            self.db,
            self.output_dir,
            "alice",
            30,
            [self.records_dir],
            now_timestamp=FIXED_NOW,
            active_paths=[mp4_path],
        )

        self.assertTrue(ts_path.exists())
        self.assertTrue(mp4_path.exists())
        self.assertIsNotNone(await self.db.get_recording_by_id("active_companion"))
        self.assertEqual(1, result["skipped_active"])


class RetentionPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_db = app_main.db
        self.original_output_dir = app_main.OUTPUT_DIR
        self.temp_dir = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.temp_dir.name)
        app_main.OUTPUT_DIR = self.output_dir
        app_main.db = Database(self.output_dir / "streamrec.db")
        await app_main.db.initialize()

    async def asyncTearDown(self):
        app_main.db = self.original_db
        app_main.OUTPUT_DIR = self.original_output_dir
        self.temp_dir.cleanup()

    async def test_each_models_retention_setting_is_applied(self):
        await app_main.db.add_or_update_model(
            "alice",
            retention_days=7,
            record_path="alice",
            source_type="chaturbate",
        )
        await app_main.db.add_or_update_model(
            "bob",
            retention_days=0,
            record_path="bob",
            source_type="chaturbate",
        )
        alice_file = self.output_dir / "records" / "alice" / "20240102_030405_aaaaaa.ts"
        bob_file = self.output_dir / "records" / "bob" / "20240102_030405_bbbbbb.webm"
        alice_file.parent.mkdir(parents=True)
        bob_file.parent.mkdir(parents=True)
        alice_file.write_bytes(b"expired")
        bob_file.write_bytes(b"keep forever")

        with patch("app.main._all_recording_statuses", return_value=[]):
            result = await app_main.cleanup_old_recordings_once(now_timestamp=FIXED_NOW)

        self.assertFalse(alice_file.exists())
        self.assertTrue(bob_file.exists())
        self.assertEqual(1, result["deleted_files"])

    async def test_nested_record_path_uses_its_more_specific_policy(self):
        await app_main.db.upsert_media_profile_source(
            "shared",
            "chaturbate",
            "alice",
            retention_days=7,
            record_path="shared",
        )
        await app_main.db.upsert_media_profile_source(
            "shared",
            "cam4",
            "bob",
            retention_days=30,
            record_path="shared/cam4",
        )
        parent_file = self.output_dir / "records" / "shared" / "20260701_120000_parent.ts"
        nested_file = self.output_dir / "records" / "shared" / "cam4" / "20260701_120000_nested.webm"
        very_old_nested = self.output_dir / "records" / "shared" / "cam4" / "20240102_030405_old.mp4"
        nested_file.parent.mkdir(parents=True)
        parent_file.write_bytes(b"expired after seven days")
        nested_file.write_bytes(b"kept for thirty days")
        very_old_nested.write_bytes(b"expired after thirty days")

        with patch("app.main._all_recording_statuses", return_value=[]):
            result = await app_main.cleanup_old_recordings_once(now_timestamp=FIXED_NOW)

        self.assertFalse(parent_file.exists())
        self.assertTrue(nested_file.exists())
        self.assertFalse(very_old_nested.exists())
        self.assertEqual(2, result["deleted_files"])

    async def test_normalized_profile_folder_is_accepted_safely(self):
        await app_main.db.add_or_update_model(
            "Alice Smith",
            retention_days=7,
            record_path="Alice-Smith",
            source_type="chaturbate",
        )
        expired = (
            self.output_dir
            / "records"
            / "Alice-Smith"
            / "Alice-Smith_20240102-030405.webm"
        )
        expired.parent.mkdir(parents=True)
        expired.write_bytes(b"expired")

        with patch("app.main._all_recording_statuses", return_value=[]):
            result = await app_main.cleanup_old_recordings_once(now_timestamp=FIXED_NOW)

        self.assertFalse(expired.exists())
        self.assertEqual(1, result["deleted_files"])


if __name__ == "__main__":
    unittest.main()
