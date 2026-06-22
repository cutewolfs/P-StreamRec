import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from app import main as app_main
from app.core.database import Database
from app.recording_names import safe_filename_part
from app.tasks.monitor import get_check_interval_seconds


class RecordingSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._original_db = app_main.db
        self._original_flaresolverr_client = app_main.flaresolverr_client
        self._tmpdir = tempfile.TemporaryDirectory()
        app_main.db = Database(Path(self._tmpdir.name) / "streamrec.db")
        await app_main.db.initialize()

    async def asyncTearDown(self):
        app_main.db = self._original_db
        app_main.flaresolverr_client = self._original_flaresolverr_client
        app_main.auth_router.set_flaresolverr(self._original_flaresolverr_client)
        if app_main.chaturbate_api:
            app_main.chaturbate_api.flaresolverr = self._original_flaresolverr_client
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

    async def test_flaresolverr_url_defaults_to_internal_compose_service(self):
        settings = await app_main.get_flaresolverr_settings()

        self.assertEqual("http://flaresolverr:8191", settings["flaresolverrUrl"])

    async def test_flaresolverr_url_can_be_saved_and_applied(self):
        settings = await app_main.update_flaresolverr_settings(
            {"url": "http://flaresolve:8191/"}
        )

        self.assertTrue(settings["success"])
        self.assertEqual("http://flaresolve:8191", settings["flaresolverrUrl"])
        self.assertEqual(
            "http://flaresolve:8191",
            await app_main.db.get_setting(app_main.FLARE_SERVICE_URL_SETTING_KEY),
        )
        self.assertEqual("http://flaresolve:8191", app_main.flaresolverr_client.base_url)

    async def test_invalid_flaresolverr_url_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            await app_main.update_flaresolverr_settings({"url": "ftp://flaresolverr"})

        self.assertEqual(400, ctx.exception.status_code)

    async def test_provider_enabled_setting_can_be_toggled(self):
        disabled = await app_main.provider_set_enabled(
            "camsoda",
            app_main.ProviderEnabledBody(enabled=False),
        )

        self.assertFalse(disabled["enabled"])
        self.assertIn("camsoda", await app_main.db.get_disabled_providers())

        providers = await app_main.list_providers()
        camsoda = next(
            provider for provider in providers["providers"]
            if provider["sourceType"] == "camsoda"
        )
        self.assertFalse(camsoda["enabled"])

        enabled = await app_main.provider_set_enabled(
            "camsoda",
            app_main.ProviderEnabledBody(enabled=True),
        )

        self.assertTrue(enabled["enabled"])
        self.assertNotIn("camsoda", await app_main.db.get_disabled_providers())

    async def test_existing_model_without_record_path_keeps_legacy_folder(self):
        await app_main.db.add_or_update_model(username="model")

        models = await app_main.get_models()
        model = models["models"][0]

        self.assertEqual("model", model["recordPath"])
        self.assertEqual("model/videos/record", model["recordPathDefault"])

    async def test_new_model_defaults_to_videos_record_folder(self):
        await app_main.add_model({"username": "model"})

        saved = await app_main.db.get_model("model")

        self.assertEqual("model/videos/record", saved["record_path"])

    async def test_edge_underscores_are_preserved_in_record_names(self):
        self.assertEqual("_username_", safe_filename_part("_username_"))
        self.assertEqual("_username_/videos/record", app_main._default_record_path("_username_"))
        self.assertEqual("_username_/videos/record", Database._default_record_path("_username_"))

    async def test_model_record_path_can_be_saved_with_zero_retention(self):
        await app_main.db.add_or_update_model(username="model")

        result = await app_main.update_model(
            "model",
            {
                "recordPath": "model/videos/record",
                "retentionDays": 0,
                "autoRecord": True,
            },
        )
        saved = await app_main.db.get_model("model")

        self.assertEqual("model/videos/record", result["model"]["recordPath"])
        self.assertEqual("model/videos/record", saved["record_path"])
        self.assertEqual(0, saved["retention_days"])

    async def test_model_record_path_must_stay_inside_model_folder(self):
        await app_main.db.add_or_update_model(username="model")

        with self.assertRaises(HTTPException) as ctx:
            await app_main.update_model("model", {"recordPath": "other/videos/record"})

        self.assertEqual(400, ctx.exception.status_code)


if __name__ == "__main__":
    unittest.main()
