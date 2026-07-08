import tempfile
import unittest
from pathlib import Path

from app import main as app_main
from app.core.database import Database
from app.providers.base import ProviderStatus


class _LiveProvider:
    def __init__(self):
        self.calls = []

    async def check_status(self, username):
        self.calls.append(username)
        return ProviderStatus(
            is_online=True,
            viewers=42,
            room_status="public",
            hls_source="https://example.test/live.m3u8",
            source_type="chaturbate",
            tags=["live"],
        )


class _Registry:
    def __init__(self, provider):
        self.provider = provider

    def source_types(self):
        return {"chaturbate"}

    def has(self, source_type):
        return source_type == "chaturbate"

    def get(self, source_type):
        if not self.has(source_type):
            raise KeyError(source_type)
        return self.provider


class AutoRecordTaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._original_db = app_main.db
        self._original_registry = app_main.provider_registry
        self._original_source_types = app_main.SOURCE_TYPES
        self._tmpdir = tempfile.TemporaryDirectory()
        app_main.db = Database(Path(self._tmpdir.name) / "streamrec.db")
        await app_main.db.initialize()
        self.provider = _LiveProvider()
        app_main.provider_registry = _Registry(self.provider)
        app_main.SOURCE_TYPES = {"chaturbate"}

    async def asyncTearDown(self):
        app_main.db = self._original_db
        app_main.provider_registry = self._original_registry
        app_main.SOURCE_TYPES = self._original_source_types
        self._tmpdir.cleanup()

    async def test_auto_record_refreshes_stale_offline_cache(self):
        await app_main.db.add_or_update_model(
            username="alice",
            display_name="Alice",
            auto_record=True,
            source_type="chaturbate",
        )
        cached = await app_main.db.get_model("alice", source_type="chaturbate")
        self.assertFalse(bool(cached["is_online"]))

        status = await app_main._auto_record_status_for_job(
            "chaturbate",
            "alice",
            cached,
        )

        self.assertTrue(status["is_online"])
        self.assertEqual(42, status["viewers"])
        self.assertEqual(["alice"], self.provider.calls)

        updated = await app_main.db.get_model("alice", source_type="chaturbate")
        self.assertTrue(bool(updated["is_online"]))
        self.assertEqual(42, updated["viewers"])
        self.assertEqual("public", updated["room_status"])

    async def test_auto_record_can_use_live_status_without_cached_model(self):
        status = await app_main._auto_record_status_for_job(
            "chaturbate",
            "alice",
            None,
        )

        self.assertTrue(status["is_online"])
        self.assertEqual("public", status["room_status"])
        self.assertEqual("https://example.test/live.m3u8", status["hls_source"])
        self.assertEqual(["alice"], self.provider.calls)


if __name__ == "__main__":
    unittest.main()
