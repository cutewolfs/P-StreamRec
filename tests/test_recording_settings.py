import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from app import main as app_main
from app.core.database import Database


class RecordingSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_db = app_main.db
        self.tmpdir = tempfile.TemporaryDirectory()
        app_main.db = Database(Path(self.tmpdir.name) / "streamrec.db")
        await app_main.db.initialize()

    async def asyncTearDown(self):
        app_main.db = self.original_db
        self.tmpdir.cleanup()

    async def test_filename_format_defaults_to_timestamp(self):
        settings = await app_main.get_recording_settings()

        self.assertEqual("timestamp", settings["filename_format"])

    async def test_filename_format_can_be_saved(self):
        settings = await app_main.update_recording_settings(
            {"filename_format": "username_timestamp"}
        )

        self.assertEqual("username_timestamp", settings["filename_format"])
        self.assertEqual(
            "username_timestamp",
            await app_main.db.get_setting("filename_format"),
        )

    async def test_invalid_filename_format_is_rejected(self):
        with self.assertRaises(HTTPException):
            await app_main.update_recording_settings({"filename_format": "template"})


if __name__ == "__main__":
    unittest.main()
