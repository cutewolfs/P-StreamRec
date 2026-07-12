import unittest

from app.api import following
from app.providers.base import ProviderCapabilities
from app.services.chaturbate_api import FollowedSyncResult


class _Provider:
    def __init__(self, source_type, items, can_login=False, can_sync_following=False):
        self.source_type = source_type
        self.display_name = source_type.upper()
        self.capabilities = ProviderCapabilities(
            can_login=can_login,
            can_follow=True,
            can_sync_following=can_sync_following,
        )
        self.items = items

    async def sync_following(self):
        return self.items


class _Registry:
    def __init__(self, providers):
        self.providers = providers

    def all(self):
        return self.providers


class _DB:
    def __init__(self):
        self.upserts = []
        self.removed = []
        self.reconciled = False

    async def upsert_followed_model(self, **kwargs):
        self.upserts.append(kwargs)

    async def remove_unfollowed(self, current_usernames, source_type="chaturbate"):
        self.removed.append((set(current_usernames), source_type))

    async def reconcile_model_sources_from_followed(self):
        self.reconciled = True
        return 0


class _FollowingDB(_DB):
    def __init__(self):
        super().__init__()
        self.followed = [
            {
                "username": "alice",
                "display_name": "Alice",
                "is_online": True,
                "viewers": 12,
                "source_type": "chaturbate",
                "room_status": "public",
            },
            {
                "username": "bella",
                "display_name": "Bella",
                "is_online": False,
                "viewers": 0,
                "source_type": "cam4",
                "room_status": "offline",
            },
        ]
        self.models = [{"username": "alice", "is_recording": True, "source_type": "chaturbate"}]
        self.sessions = {
            "cam4": {
                "username": "tester",
                "is_logged_in": 1,
                "credential_username": None,
                "credential_password": None,
                "credentials_updated_at": None,
                "session_cookies": '[{"name":"sid","value":"abc"}]',
                "local_storage": None,
                "last_error": None,
            }
        }

    async def get_all_followed(self):
        return [dict(item) for item in self.followed]

    async def get_all_models(self):
        return [dict(item) for item in self.models]

    async def get_provider_session(self, source_type):
        row = self.sessions.get(source_type)
        return dict(row) if row else None


class FollowingSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        following.init(None, None, None, None)

    async def test_legacy_sync_calls_remote_for_sync_capable_providers(self):
        db = _DB()
        registry = _Registry([
            _Provider("chaturbate", [
                {"username": "alice", "display_name": "Alice", "is_online": True, "viewers": 12},
            ], can_login=True, can_sync_following=True),
            _Provider("cam4", [
                {"username": "bella", "display_name": "Bella", "is_online": False, "viewers": 0},
            ], can_login=True, can_sync_following=True),
            _Provider("stripchat", [
                {"username": "local_only"},
            ], can_sync_following=False),
        ])
        following.init(None, None, db, registry)

        result = await following.sync_following()

        self.assertEqual(2, result["synced"])
        self.assertFalse(result["localOnly"])
        self.assertEqual(["alice", "bella"], [item["username"] for item in db.upserts])
        self.assertEqual([
            ({"alice"}, "chaturbate"),
            ({"bella"}, "cam4"),
        ], db.removed)
        self.assertTrue(all(item["authoritative"] for item in result["results"]))
        self.assertTrue(db.reconciled)

    async def test_provider_without_remote_sync_is_left_local_only(self):
        db = _DB()
        registry = _Registry([_Provider("stripchat", [], can_sync_following=False)])
        following.init(None, None, db, registry)

        result = await following.sync_following()

        self.assertEqual(0, result["synced"])
        self.assertTrue(result["localOnly"])
        self.assertEqual([], db.upserts)
        self.assertEqual([], db.removed)

    async def test_untrusted_remote_sync_does_not_mutate_followed_cache(self):
        db = _DB()
        registry = _Registry([
            _Provider(
                "chaturbate",
                FollowedSyncResult([], trusted=False, skipped_reason="login page"),
                can_login=True,
                can_sync_following=True,
            ),
        ])
        following.init(None, None, db, registry)

        result = await following.sync_following()

        self.assertEqual(0, result["synced"])
        self.assertEqual([], db.upserts)
        self.assertEqual([], db.removed)
        self.assertFalse(result["results"][0]["trusted"])
        self.assertFalse(result["results"][0]["authoritative"])
        self.assertEqual("login page", result["results"][0]["skippedReason"])

    async def test_non_authoritative_remote_sync_upserts_without_removing_cache(self):
        db = _DB()
        registry = _Registry([
            _Provider(
                "chaturbate",
                FollowedSyncResult(
                    [{"username": "alice", "display_name": "Alice", "is_online": True}],
                    authoritative=False,
                ),
                can_login=True,
                can_sync_following=True,
            ),
        ])
        following.init(None, None, db, registry)

        result = await following.sync_following()

        self.assertEqual(["alice"], [item["username"] for item in db.upserts])
        self.assertEqual([], db.removed)
        self.assertTrue(result["results"][0]["trusted"])
        self.assertFalse(result["results"][0]["authoritative"])

    async def test_get_following_includes_provider_summaries(self):
        db = _FollowingDB()
        registry = _Registry([
            _Provider("chaturbate", [], can_login=True, can_sync_following=True),
            _Provider("cam4", [], can_login=True, can_sync_following=True),
            _Provider("stripchat", []),
        ])
        following.init(None, None, db, registry)

        result = await following.get_following()

        self.assertEqual(2, len(result["models"]))
        self.assertEqual({"chaturbate": False, "cam4": True, "stripchat": False}, result["perSource"])
        self.assertEqual({"chaturbate", "cam4"}, set(result["byProvider"].keys()))
        summaries = {item["sourceType"]: item for item in result["providers"]}
        self.assertEqual({"chaturbate", "cam4", "stripchat"}, set(summaries))
        self.assertEqual(1, summaries["chaturbate"]["totalCount"])
        self.assertEqual(1, summaries["cam4"]["totalCount"])
        self.assertEqual(0, summaries["stripchat"]["totalCount"])
        self.assertFalse(summaries["stripchat"]["capabilities"]["can_sync_following"])
        self.assertFalse(summaries["cam4"]["status"].get("accountDisabled", False))
        self.assertTrue(summaries["cam4"]["status"]["isLoggedIn"])

    async def test_get_following_sorts_models_by_viewers_across_providers(self):
        db = _FollowingDB()
        db.followed = [
            {
                "username": "low_cb",
                "is_online": True,
                "viewers": 12,
                "source_type": "chaturbate",
                "room_status": "public",
            },
            {
                "username": "top_cam4",
                "is_online": True,
                "viewers": 320,
                "source_type": "cam4",
                "room_status": "public",
            },
            {
                "username": "mid_strip",
                "is_online": True,
                "viewers": 88,
                "source_type": "stripchat",
                "room_status": "public",
            },
            {
                "username": "offline_zero",
                "is_online": False,
                "viewers": 999,
                "source_type": "chaturbate",
                "room_status": "offline",
            },
        ]
        registry = _Registry([
            _Provider("chaturbate", []),
            _Provider("cam4", []),
            _Provider("stripchat", [], can_sync_following=False),
        ])
        following.init(None, None, db, registry)

        result = await following.get_following()

        self.assertEqual(
            ["top_cam4", "mid_strip", "low_cb", "offline_zero"],
            [item["username"] for item in result["models"]],
        )

    async def test_get_following_hides_sources_missing_from_registry(self):
        db = _FollowingDB()
        db.followed.append({
            "username": "removed_source_model",
            "is_online": True,
            "viewers": 500,
            "source_type": "removedsource",
            "room_status": "public",
        })
        registry = _Registry([
            _Provider("chaturbate", []),
            _Provider("cam4", []),
        ])
        following.init(None, None, db, registry)

        result = await following.get_following()

        self.assertNotIn("removed_source_model", [item["username"] for item in result["models"]])
        self.assertNotIn("removedsource", result["byProvider"])
        self.assertNotIn("removedsource", {item["sourceType"] for item in result["providers"]})
