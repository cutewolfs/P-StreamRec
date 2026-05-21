import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.database import Database
from app import main as app_main


class AutoRecordUsersImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._original_db = app_main.db
        self._tmpdir = tempfile.TemporaryDirectory()
        app_main.db = Database(Path(self._tmpdir.name) / "streamrec.db")
        await app_main.db.initialize()

    async def asyncTearDown(self):
        app_main.db = self._original_db
        self._tmpdir.cleanup()

    async def test_empty_env_does_nothing(self):
        with patch.dict(os.environ, {}, clear=True):
            result = await app_main._import_auto_record_users_from_env()

        self.assertEqual(
            result,
            {"imported": 0, "created": 0, "updated": 0, "skipped": 0},
        )
        self.assertEqual(await app_main.db.get_all_models(), [])

    async def test_imports_comma_separated_users_with_defaults(self):
        await app_main.db.set_setting("default_resolution", "720")
        await app_main.db.set_setting("default_retention_days", "0")

        with patch.dict(
            os.environ,
            {"AUTO_RECORD_USERS": " alice, bob , alice,,BOB, carol "},
            clear=True,
        ):
            result = await app_main._import_auto_record_users_from_env()

        self.assertEqual(
            result,
            {"imported": 3, "created": 3, "updated": 0, "skipped": 3},
        )

        models = {
            model["username"]: model
            for model in await app_main.db.get_models_for_auto_record()
        }
        self.assertEqual(list(models), ["alice", "bob", "carol"])
        for username, model in models.items():
            self.assertEqual(model["display_name"], username)
            self.assertEqual(model["record_quality"], "720p")
            self.assertEqual(model["retention_days"], 0)
            self.assertEqual(model["source_type"], "chaturbate")
            self.assertTrue(model["auto_record"])

    async def test_existing_models_keep_settings_when_reenabled(self):
        await app_main.db.add_or_update_model(
            username="cammodel",
            display_name="Cam Model",
            auto_record=False,
            record_quality="480p",
            retention_days=7,
            source_type="cam4",
        )

        with patch.dict(os.environ, {"AUTO_RECORD_USERS": "cammodel"}, clear=True):
            result = await app_main._import_auto_record_users_from_env()

        self.assertEqual(
            result,
            {"imported": 1, "created": 0, "updated": 1, "skipped": 0},
        )

        model = await app_main.db.get_model("cammodel")
        self.assertEqual(model["display_name"], "Cam Model")
        self.assertTrue(model["auto_record"])
        self.assertEqual(model["record_quality"], "480p")
        self.assertEqual(model["retention_days"], 7)
        self.assertEqual(model["source_type"], "cam4")


if __name__ == "__main__":
    unittest.main()
