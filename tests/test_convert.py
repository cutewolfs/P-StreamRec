import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.tasks.convert import (
    _communicate_with_timeout,
    _video_stream_map_from_probe,
    convert_ts_to_mp4,
)


class ConvertStreamSelectionTests(unittest.TestCase):
    def test_video_stream_map_picks_highest_resolution_video(self):
        probe = {
            "streams": [
                {"width": 640, "height": 360, "bit_rate": "800000"},
                {"width": 1280, "height": 720, "bit_rate": "2500000"},
                {"width": 1920, "height": 1080, "bit_rate": "5000000"},
            ]
        }

        self.assertEqual("0:v:2", _video_stream_map_from_probe(probe))

    def test_video_stream_map_falls_back_to_first_video(self):
        self.assertEqual("0:v:0", _video_stream_map_from_probe({}))


class ConvertOutputSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_ts_is_rejected_before_starting_ffmpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts_path = Path(tmp) / "empty.ts"
            ts_path.write_bytes(b"")
            with patch(
                "app.tasks.convert.asyncio.create_subprocess_exec",
                new=AsyncMock(),
            ) as exec_mock:
                result = await convert_ts_to_mp4(ts_path)

        self.assertEqual((False, None, None), result)
        exec_mock.assert_not_awaited()

    async def test_subprocess_timeout_terminates_and_reaps_child(self):
        class Process:
            returncode = None
            terminated = False

            async def communicate(self):
                await asyncio.sleep(60)

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def kill(self):
                self.returncode = -9

            async def wait(self):
                return self.returncode

        process = Process()
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            await _communicate_with_timeout(process, 0.001)
        self.assertTrue(process.terminated)

    async def _run_conversion(self, process):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        ts_path = root / "recording.ts"
        mp4_path = root / "recording.mp4"
        ts_path.write_bytes(b"transport-stream")
        output_paths = []

        async def fake_exec(*cmd, **_kwargs):
            output_paths.append(Path(cmd[-1]))
            process.output_path = Path(cmd[-1])
            return process

        with (
            patch(
                "app.tasks.convert._best_video_stream_map",
                new=AsyncMock(return_value="0:v:0"),
            ),
            patch("app.tasks.convert.asyncio.create_subprocess_exec", new=fake_exec),
        ):
            result = await convert_ts_to_mp4(ts_path, mp4_path)

        return root, ts_path, mp4_path, output_paths, result

    async def test_success_publishes_nonempty_temp_output_atomically(self):
        class Process:
            returncode = 0
            output_path = None

            async def communicate(self):
                self.output_path.write_bytes(b"complete-mp4")
                return b"", b""

        root, ts_path, mp4_path, output_paths, result = await self._run_conversion(Process())

        self.assertEqual((True, mp4_path, len(b"complete-mp4")), result)
        self.assertEqual(b"complete-mp4", mp4_path.read_bytes())
        self.assertTrue(ts_path.exists())
        self.assertEqual(1, len(output_paths))
        self.assertNotEqual(mp4_path, output_paths[0])
        self.assertEqual(mp4_path.parent, output_paths[0].parent)
        self.assertTrue(output_paths[0].name.endswith(".tmp.mp4"))
        self.assertEqual([], list(root.glob(".*.tmp.mp4")))

    async def test_ffmpeg_failure_preserves_existing_final_and_cleans_partial_temp(self):
        class Process:
            returncode = 1
            output_path = None

            async def communicate(self):
                self.output_path.write_bytes(b"partial-new-output")
                return b"", b"conversion failed"

        process = Process()
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        ts_path = root / "recording.ts"
        mp4_path = root / "recording.mp4"
        ts_path.write_bytes(b"transport-stream")
        mp4_path.write_bytes(b"previous-final")
        output_paths = []

        async def fake_exec(*cmd, **_kwargs):
            output_paths.append(Path(cmd[-1]))
            process.output_path = Path(cmd[-1])
            return process

        with (
            patch(
                "app.tasks.convert._best_video_stream_map",
                new=AsyncMock(return_value="0:v:0"),
            ),
            patch("app.tasks.convert.asyncio.create_subprocess_exec", new=fake_exec),
        ):
            result = await convert_ts_to_mp4(ts_path, mp4_path)

        self.assertEqual((False, None, None), result)
        self.assertEqual(b"previous-final", mp4_path.read_bytes())
        self.assertFalse(output_paths[0].exists())
        self.assertTrue(ts_path.exists())

    async def test_zero_exit_without_nonempty_output_is_failure(self):
        class Process:
            returncode = 0
            output_path = None

            async def communicate(self):
                self.output_path.write_bytes(b"")
                return b"", b""

        _root, ts_path, mp4_path, output_paths, result = await self._run_conversion(Process())

        self.assertEqual((False, None, None), result)
        self.assertFalse(mp4_path.exists())
        self.assertFalse(output_paths[0].exists())
        self.assertTrue(ts_path.exists())

    async def test_communicate_exception_cleans_temp_and_preserves_final(self):
        class Process:
            returncode = 0
            output_path = None

            async def communicate(self):
                self.output_path.write_bytes(b"interrupted-output")
                raise RuntimeError("ffmpeg communication interrupted")

        process = Process()
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        ts_path = root / "recording.ts"
        mp4_path = root / "recording.mp4"
        ts_path.write_bytes(b"transport-stream")
        mp4_path.write_bytes(b"previous-final")
        output_paths = []

        async def fake_exec(*cmd, **_kwargs):
            output_paths.append(Path(cmd[-1]))
            process.output_path = Path(cmd[-1])
            return process

        with (
            patch(
                "app.tasks.convert._best_video_stream_map",
                new=AsyncMock(return_value="0:v:0"),
            ),
            patch("app.tasks.convert.asyncio.create_subprocess_exec", new=fake_exec),
        ):
            result = await convert_ts_to_mp4(ts_path, mp4_path)

        self.assertEqual((False, None, None), result)
        self.assertEqual(b"previous-final", mp4_path.read_bytes())
        self.assertFalse(output_paths[0].exists())
        self.assertTrue(ts_path.exists())


if __name__ == "__main__":
    unittest.main()
