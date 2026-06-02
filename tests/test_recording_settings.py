import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from app.core.database import Database
from app import main as app_main
from app.tasks.monitor import get_check_interval_seconds


class RecordingSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._original_db = app_main.db
        self._tmpdir = tempfile.TemporaryDirectory()
        app_main.db = Database(Path(self._tmpdir.name) / "streamrec.db")
        await app_main.db.initialize()

    async def asyncTearDown(self):
        app_main.db = self._original_db
        self._tmpdir.cleanup()

    async def test_check_interval_defaults_to_config_value(self):
        settings = await app_main.get_recording_settings()

        self.assertEqual(settings["check_interval_seconds"], 120)
        self.assertEqual(settings["check_interval"], 120)
        self.assertEqual(await get_check_interval_seconds(app_main.db), 120)

    async def test_updates_check_interval_setting(self):
        settings = await app_main.update_recording_settings(
            {"check_interval_seconds": "600"}
        )

        self.assertEqual(settings["check_interval_seconds"], 600)
        self.assertEqual(await app_main.db.get_setting("check_interval_seconds"), "600")
        self.assertEqual(await get_check_interval_seconds(app_main.db), 600)

    async def test_rejects_out_of_range_check_interval(self):
        with self.assertRaises(HTTPException) as ctx:
            await app_main.update_recording_settings({"check_interval_seconds": 10})

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("at least 30 seconds", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
