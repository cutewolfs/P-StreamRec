import unittest

from app.api import following
from app.providers.base import ProviderCapabilities


class _Provider:
    def __init__(self, source_type, items):
        self.source_type = source_type
        self.display_name = source_type.upper()
        self.capabilities = ProviderCapabilities(can_sync_following=True)
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


class FollowingSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        following.init(None, None, None, None)

    async def test_legacy_sync_uses_provider_registry_and_preserves_source(self):
        db = _DB()
        registry = _Registry([
            _Provider("chaturbate", [
                {"username": "alice", "display_name": "Alice", "is_online": True, "viewers": 12},
            ]),
            _Provider("cam4", [
                {"username": "bella", "display_name": "Bella", "is_online": False, "viewers": 0},
            ]),
        ])
        following.init(None, None, db, registry)

        result = await following.sync_following()

        self.assertEqual(2, result["synced"])
        self.assertEqual(["chaturbate", "cam4"], [item["source_type"] for item in db.upserts])
        self.assertEqual([({"alice"}, "chaturbate")], db.removed)
        self.assertTrue(db.reconciled)
