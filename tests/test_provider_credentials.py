import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from app.core.database import Database
from app.providers.base import ProviderInteractionRequired
from app.providers.sessions import ProviderSessionStore
import app.main as app_main


class ProviderCredentialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmpdir.name) / "streamrec.db")
        await self.db.initialize()

    async def asyncTearDown(self):
        self._tmpdir.cleanup()

    async def test_credentials_are_saved_without_login_session(self):
        await self.db.save_provider_credentials("stripchat", "alice", "secret")

        row = await self.db.get_provider_session("stripchat")

        self.assertFalse(row["is_logged_in"])
        self.assertEqual("alice", row["username"])
        self.assertEqual("alice", row["credential_username"])
        self.assertEqual("secret", row["credential_password"])
        self.assertIsNotNone(row["credentials_updated_at"])

    async def test_session_updates_preserve_saved_credentials(self):
        await self.db.save_provider_credentials("stripchat", "alice", "secret")
        await self.db.save_provider_session(
            "stripchat",
            username="alice",
            is_logged_in=False,
            last_error="interaction_required",
        )

        row = await self.db.get_provider_session("stripchat")

        self.assertEqual("alice", row["credential_username"])
        self.assertEqual("secret", row["credential_password"])
        self.assertEqual("interaction_required", row["last_error"])

    async def test_provider_session_store_saves_manual_browser_session(self):
        store = ProviderSessionStore(self.db)
        await store.save(
            "xcams",
            username="alice",
            is_logged_in=True,
            cookies=[{"name": "session", "value": "abc", "domain": ".xcams.com", "path": "/"}],
            local_storage=[{"origin": "https://www.xcams.com", "localStorage": []}],
        )

        state = await store.get("xcams")

        self.assertTrue(state["is_logged_in"])
        self.assertEqual("alice", state["username"])
        self.assertEqual("session=abc", await store.cookie_header("xcams"))
        self.assertEqual("https://www.xcams.com", state["localStorage"][0]["origin"])

    async def test_failed_provider_session_clears_last_login_at(self):
        await self.db.save_provider_session(
            "xcams",
            username="alice",
            is_logged_in=True,
            session_cookies="[]",
            local_storage='[{"origin":"https://www.xcams.com","localStorage":[]}]',
        )
        connected = await self.db.get_provider_session("xcams")
        self.assertIsNotNone(connected["last_login_at"])

        await self.db.save_provider_session(
            "xcams",
            username="alice",
            is_logged_in=False,
            last_error="interaction_required",
        )
        disconnected = await self.db.get_provider_session("xcams")

        self.assertFalse(disconnected["is_logged_in"])
        self.assertIsNone(disconnected["last_login_at"])
        self.assertEqual("interaction_required", disconnected["last_error"])

    async def test_saved_provider_login_challenge_maps_to_session_required(self):
        class Provider:
            display_name = "Stripchat"

        original = app_main._login_with_saved_provider_credentials

        async def blocked(source_type):
            raise ProviderInteractionRequired("challenge")

        app_main._login_with_saved_provider_credentials = blocked
        try:
            with self.assertRaises(HTTPException) as raised:
                await app_main._ensure_saved_provider_login(Provider(), "stripchat")
        finally:
            app_main._login_with_saved_provider_credentials = original

        self.assertEqual(409, raised.exception.status_code)
        self.assertIn("session navigateur", raised.exception.detail)

    async def test_saved_provider_login_missing_credentials_maps_to_auth_required(self):
        class Provider:
            display_name = "Stripchat"

        original = app_main._login_with_saved_provider_credentials

        async def unavailable(source_type):
            return False

        app_main._login_with_saved_provider_credentials = unavailable
        try:
            with self.assertRaises(HTTPException) as raised:
                await app_main._ensure_saved_provider_login(Provider(), "stripchat")
        finally:
            app_main._login_with_saved_provider_credentials = original

        self.assertEqual(401, raised.exception.status_code)
        self.assertIn("connexion requise", raised.exception.detail)

    async def test_models_and_follows_are_provider_aware(self):
        await self.db.add_or_update_model("alice", source_type="chaturbate")
        await self.db.add_or_update_model("alice", source_type="stripchat")
        await self.db.upsert_followed_model("alice", source_type="chaturbate")
        await self.db.upsert_followed_model("alice", source_type="stripchat")

        models = await self.db.get_all_models()
        follows = await self.db.get_all_followed()

        self.assertEqual(
            {("alice", "chaturbate"), ("alice", "stripchat")},
            {(row["username"], row["source_type"]) for row in models},
        )
        self.assertEqual(
            {("alice", "chaturbate"), ("alice", "stripchat")},
            {(row["username"], row["source_type"]) for row in follows},
        )
        self.assertEqual("stripchat", (await self.db.get_model("alice", "stripchat"))["source_type"])
        await self.db.delete_followed_model("alice", source_type="stripchat")
        self.assertIsNone(await self.db.get_followed_model("alice", "stripchat"))
        self.assertIsNotNone(await self.db.get_followed_model("alice", "chaturbate"))


if __name__ == "__main__":
    unittest.main()
