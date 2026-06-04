import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import main as app_main
from app.core.database import Database
from app.tasks.monitor import (
    _parse_video_recorded_at,
    recording_timestamp_from_filename,
)


class RecordingDateHelperTests(unittest.TestCase):
    def test_parses_ffprobe_creation_time_metadata(self):
        expected = int(datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc).timestamp())

        self.assertEqual(
            _parse_video_recorded_at({
                "format": {"tags": {"creation_time": "2024-01-02T03:04:05.000000Z"}},
                "streams": [],
            }),
            expected,
        )

    def test_parses_quicktime_creation_date_with_offset(self):
        expected = int(datetime(2024, 1, 2, 8, 4, 5, tzinfo=timezone.utc).timestamp())

        self.assertEqual(
            _parse_video_recorded_at({
                "format": {"tags": {"com.apple.quicktime.creationdate": "2024-01-02T03:04:05-0500"}},
                "streams": [],
            }),
            expected,
        )

    def test_extracts_recording_timestamp_from_live_filename(self):
        expected = int(datetime(2024, 1, 2, 3, 4, 5).timestamp())

        self.assertEqual(
            recording_timestamp_from_filename("20240102_030405_abcdef.ts"),
            expected,
        )


class RecordingDateIndexingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_db = app_main.db
        self.original_output_dir = app_main.OUTPUT_DIR

        self.tmpdir = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.tmpdir.name)
        app_main.OUTPUT_DIR = self.output_dir
        app_main.db = Database(self.output_dir / "streamrec.db")
        await app_main.db.initialize()

    async def asyncTearDown(self):
        app_main.db = self.original_db
        app_main.OUTPUT_DIR = self.original_output_dir
        self.tmpdir.cleanup()

    async def test_ffmpeg_live_index_uses_media_metadata_date(self):
        records_dir = self.output_dir / "records" / "model"
        records_dir.mkdir(parents=True)
        record_path = records_dir / "20240102_030405_abcdef.ts"
        record_path.write_bytes(b"recording bytes")
        start_time = int(time.time()) - 3600
        metadata_created_at = 1704164645

        session = SimpleNamespace(
            id="abcdef",
            person="model",
            start_time=start_time,
            _recording_paths_for_cleanup=lambda: [str(record_path)],
        )

        with (
            patch("app.tasks.monitor.get_video_duration", new=AsyncMock(return_value=300)),
            patch("app.tasks.monitor.generate_recording_thumbnail", new=AsyncMock(return_value=None)),
            patch("app.tasks.monitor.get_media_created_at", new=AsyncMock(return_value=metadata_created_at)),
        ):
            await app_main._index_ffmpeg_recording(session)

        recordings = await app_main.db.get_recordings("model")
        self.assertEqual(len(recordings), 1)
        self.assertEqual(recordings[0]["created_at"], metadata_created_at)


if __name__ == "__main__":
    unittest.main()
