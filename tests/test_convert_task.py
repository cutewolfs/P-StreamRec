import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.tasks.convert import _select_best_video_stream_map, auto_convert_recordings_task


class ConvertTaskTests(unittest.TestCase):
    def test_selects_highest_resolution_video_stream(self):
        probe_data = {
            "streams": [
                {"index": 0, "width": 640, "height": 360, "bit_rate": "530000"},
                {"index": 1, "width": 1280, "height": 720, "bit_rate": "1800000"},
                {"index": 3, "width": 1920, "height": 1080, "bit_rate": "3500000"},
            ]
        }

        self.assertEqual("0:3", _select_best_video_stream_map(probe_data))

    def test_breaks_resolution_ties_by_bitrate(self):
        probe_data = {
            "streams": [
                {"index": 0, "width": 1280, "height": 720, "bit_rate": "1200000"},
                {"index": 2, "width": 1280, "height": 720, "bit_rate": "2500000"},
            ]
        }

        self.assertEqual("0:2", _select_best_video_stream_map(probe_data))

    def test_falls_back_to_first_video_when_probe_has_no_video_streams(self):
        self.assertEqual("0:v:0", _select_best_video_stream_map({"streams": []}))


class ExistingConversionSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_existing_mp4_never_causes_ts_deletion(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            records_dir = output_dir / "records" / "model"
            records_dir.mkdir(parents=True)
            ts_path = records_dir / "recording.ts"
            mp4_path = records_dir / "recording.mp4"
            ts_path.write_bytes(b"transport-stream")
            mp4_path.write_bytes(b"interrupted-mp4")
            old_timestamp = ts_path.stat().st_mtime - 600
            os.utime(ts_path, (old_timestamp, old_timestamp))

            class Db:
                conversion_failure = None

                async def get_setting(self, name):
                    return "true" if name == "auto_convert" else "false"

                async def get_recordings(self, _username):
                    return [
                        {
                            "filename": ts_path.name,
                            "recording_id": "model_recording",
                            "duration_seconds": 20,
                            "conversion_attempts": 0,
                            "is_converted": False,
                        }
                    ]

                async def mark_conversion_failed(self, username, filename, error):
                    self.conversion_failure = (username, filename, error)

            class FfmpegManager:
                @staticmethod
                def list_status():
                    return []

            sleep_calls = 0

            async def stop_after_one_scan(_seconds):
                nonlocal sleep_calls
                sleep_calls += 1
                if sleep_calls > 1:
                    raise asyncio.CancelledError

            convert_mock = AsyncMock(return_value=(False, None, None))
            with (
                patch("app.tasks.convert.asyncio.sleep", new=stop_after_one_scan),
                patch(
                    "app.tasks.convert.get_video_duration",
                    new=AsyncMock(return_value=0),
                ),
                patch("app.tasks.convert.convert_ts_to_mp4", new=convert_mock),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await auto_convert_recordings_task(
                        Db(), output_dir, FfmpegManager(), "ffmpeg"
                    )

            convert_mock.assert_awaited_once_with(ts_path, mp4_path, "ffmpeg")
            self.assertTrue(ts_path.exists())
            self.assertEqual(b"interrupted-mp4", mp4_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
