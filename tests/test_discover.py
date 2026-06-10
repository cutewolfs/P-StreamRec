import unittest

from app.api import discover
from app.providers.base import BaseProvider, ProviderCapabilities


class _Provider(BaseProvider):
    def __init__(
        self,
        source_type,
        can_follow=False,
        can_stream=True,
        provider_status=None,
        provider_detail="",
        models=None,
    ):
        super().__init__()
        self.source_type = source_type
        self.display_name = source_type
        self.capabilities = ProviderCapabilities(
            can_discover=True,
            can_follow=can_follow,
            can_stream=can_stream,
            can_record=can_stream,
        )
        self.provider_status = provider_status
        self.provider_detail = provider_detail
        self.models = models

    async def list_live_models(self, **kwargs):
        if self.provider_status:
            return {
                "models": [],
                "total": 0,
                "page": kwargs.get("page", 1),
                "limit": kwargs.get("limit", 24),
                "total_pages": 1,
                "provider_status": self.provider_status,
                "provider_detail": self.provider_detail,
            }
        if self.models is not None:
            models = [dict(model, source_type=model.get("source_type") or self.source_type) for model in self.models]
            limit = kwargs.get("limit", 24)
            return {
                "models": models,
                "total": len(models),
                "page": kwargs.get("page", 1),
                "limit": limit,
                "total_pages": max(1, (len(models) + limit - 1) // limit),
            }
        return {
            "models": [
                {
                    "username": f"{self.source_type}_model",
                    "is_online": True,
                    "room_status": "public",
                    "viewers": 10,
                    "tags": [],
                }
            ],
            "total": 1,
            "page": kwargs.get("page", 1),
            "limit": kwargs.get("limit", 24),
            "total_pages": 1,
        }


class _Registry:
    def __init__(self):
        self.providers = {
            "chaturbate": _Provider("chaturbate", can_follow=True),
            "stripchat": _Provider("stripchat"),
            "camsoda": _Provider("camsoda"),
        }

    def all(self):
        return list(self.providers.values())

    def has(self, source_type):
        return source_type in self.providers

    def get(self, source_type):
        return self.providers[source_type]


class _SettingsDB:
    def __init__(self, disabled=None):
        self.disabled = list(disabled or [])

    async def get_disabled_providers(self):
        return list(self.disabled)

    async def get_blacklisted_tags(self):
        return []


class _UnstableTotalProvider(_Provider):
    async def list_live_models(self, **kwargs):
        page = int(kwargs.get("page", 1) or 1)
        limit = int(kwargs.get("limit", 24) or 24)
        total_by_page = {1: 240, 2: 96, 3: 48}
        total = total_by_page.get(page, 24)
        return {
            "models": [
                {
                    "username": f"{self.source_type}_{page}_{idx}",
                    "is_online": True,
                    "room_status": "public",
                    "viewers": max(1, 100 - idx),
                    "tags": ["public"],
                    "source_type": self.source_type,
                }
                for idx in range(limit)
            ],
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": max(1, (total + limit - 1) // limit),
        }


class DiscoverProviderRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        discover.init(None, None, _Registry())

    async def asyncTearDown(self):
        discover.init(None, None, None)

    async def test_discover_aggregates_registered_sources(self):
        result = await discover.discover_models(
            page=1, limit=6, source=None, gender=None, search=None, tags=None, sort="viewers"
        )

        sources = {item["source_type"] for item in result["models"]}
        self.assertEqual({"chaturbate", "stripchat", "camsoda"}, sources)
        follow_flags = {
            item["source_type"]: item["can_follow"]
            for item in result["models"]
        }
        self.assertTrue(follow_flags["chaturbate"])
        self.assertTrue(follow_flags["stripchat"])

    async def test_discover_source_filter_limits_provider(self):
        result = await discover.discover_models(
            page=1, limit=6, source="stripchat", gender=None, search=None, tags=None, sort="viewers"
        )

        self.assertEqual(["stripchat"], [item["source_type"] for item in result["models"]])

    async def test_discover_filters_gender_after_provider_results(self):
        registry = _Registry()
        registry.providers = {
            "camsoda": _Provider("camsoda", models=[
                {
                    "username": "male_model",
                    "is_online": True,
                    "room_status": "public",
                    "viewers": 50,
                    "gender": "male",
                    "tags": ["men", "public"],
                },
                {
                    "username": "trans_model",
                    "is_online": True,
                    "room_status": "public",
                    "viewers": 500,
                    "gender": "trans",
                    "tags": ["trans", "public"],
                },
            ]),
        }
        discover.init(None, None, registry)

        result = await discover.discover_models(
            page=1, limit=6, source=None, gender="male", search=None, tags=None, sort="viewers"
        )

        self.assertEqual(["male_model"], [item["username"] for item in result["models"]])

    async def test_discover_excludes_disabled_providers_from_all_sources(self):
        registry = _Registry()
        discover.init(None, _SettingsDB(disabled=["camsoda"]), registry)

        result = await discover.discover_models(
            page=1, limit=6, source=None, gender=None, search=None, tags=None, sort="viewers"
        )

        self.assertEqual(
            {"chaturbate", "stripchat"},
            {item["source_type"] for item in result["models"]},
        )

    async def test_discover_returns_provider_status_for_empty_source(self):
        registry = _Registry()
        registry.providers["lockedsite"] = _Provider(
            "lockedsite",
            provider_status="auth_required",
            provider_detail="Provider key required",
        )
        discover.init(None, None, registry)

        result = await discover.discover_models(
            page=1, limit=6, source="lockedsite", gender=None, search=None, tags=None, sort="viewers"
        )

        self.assertEqual([], result["models"])
        self.assertEqual("auth_required", result["provider_statuses"][0]["status"])
        self.assertEqual("Provider key required", result["provider_statuses"][0]["detail"])

    async def test_discover_sorts_globally_by_viewers(self):
        registry = _Registry()
        registry.providers = {
            "chaturbate": _Provider("chaturbate", models=[
                {"username": "cb_big", "is_online": True, "room_status": "public", "viewers": 300, "tags": ["public"]},
                {"username": "cb_mid", "is_online": True, "room_status": "public", "viewers": 120, "tags": ["public"]},
            ]),
            "cam4": _Provider("cam4", models=[
                {"username": "cam4_top", "is_online": True, "room_status": "public", "viewers": 250, "tags": ["public"]},
            ]),
            "stripchat": _Provider("stripchat", models=[
                {"username": "strip_zero", "is_online": True, "room_status": "public", "viewers": 0, "tags": ["public"]},
            ]),
        }
        discover.init(None, None, registry)

        result = await discover.discover_models(
            page=1, limit=3, source=None, gender=None, search=None, tags=None, sort="viewers"
        )

        self.assertEqual(
            ["cb_big", "cam4_top", "cb_mid"],
            [item["username"] for item in result["models"]],
        )
        self.assertNotIn("strip_zero", [item["username"] for item in result["models"]])

    async def test_discover_total_pages_is_stable_between_aggregate_pages(self):
        registry = _Registry()
        registry.providers = {
            "chaturbate": _Provider("chaturbate", models=[
                {
                    "username": f"cb_{idx}",
                    "is_online": True,
                    "room_status": "public",
                    "viewers": 100 - idx,
                    "tags": ["public"],
                }
                for idx in range(72)
            ]),
        }
        discover.init(None, None, registry)

        page_one = await discover.discover_models(
            page=1, limit=24, source=None, gender=None, search=None, tags=None, sort="viewers"
        )
        page_two = await discover.discover_models(
            page=2, limit=24, source=None, gender=None, search=None, tags=None, sort="viewers"
        )

        self.assertEqual(3, page_one["total_pages"])
        self.assertEqual(page_one["total_pages"], page_two["total_pages"])
        self.assertEqual(24, len(page_two["models"]))

    async def test_discover_source_total_pages_is_stable_between_pages(self):
        registry = _Registry()
        registry.providers = {
            "bongacams": _UnstableTotalProvider("bongacams"),
        }
        discover.init(None, None, registry)

        page_one = await discover.discover_models(
            page=1, limit=24, source="bongacams", gender=None, search=None, tags=None, sort="viewers"
        )
        page_two = await discover.discover_models(
            page=2, limit=24, source="bongacams", gender=None, search=None, tags=None, sort="viewers"
        )
        page_three = await discover.discover_models(
            page=3, limit=24, source="bongacams", gender=None, search=None, tags=None, sort="viewers"
        )

        self.assertEqual(10, page_one["total_pages"])
        self.assertEqual(page_one["total_pages"], page_two["total_pages"])
        self.assertEqual(page_one["total_pages"], page_three["total_pages"])
        self.assertEqual(page_one["total"], page_two["total"])
        self.assertEqual(page_one["total"], page_three["total"])

    async def test_discover_global_excludes_non_streamable_sources(self):
        registry = _Registry()
        registry.providers = {
            "chaturbate": _Provider("chaturbate", models=[
                {"username": "cb_mid", "is_online": True, "room_status": "public", "viewers": 120, "tags": ["public"]},
            ]),
            "livejasmin": _Provider("livejasmin", can_stream=False, models=[
                {"username": "lj_top", "is_online": True, "room_status": "public", "viewers": 500, "tags": ["public"]},
            ]),
        }
        discover.init(None, None, registry)

        result = await discover.discover_models(
            page=1, limit=24, source=None, gender=None, search=None, tags=None, sort="viewers"
        )

        self.assertEqual(["cb_mid"], [item["username"] for item in result["models"]])
        lj_status = [item for item in result["provider_statuses"] if item["source_type"] == "livejasmin"][0]
        self.assertEqual("discover_only", lj_status["status"])

    async def test_discover_source_filter_marks_non_streamable_cards(self):
        registry = _Registry()
        registry.providers = {
            "livejasmin": _Provider("livejasmin", can_stream=False, models=[
                {"username": "lj_model", "is_online": True, "room_status": "public", "viewers": 0, "tags": ["public"]},
            ]),
        }
        discover.init(None, None, registry)

        result = await discover.discover_models(
            page=1, limit=24, source="livejasmin", gender=None, search=None, tags=None, sort="viewers"
        )

        self.assertEqual(["lj_model"], [item["username"] for item in result["models"]])
        self.assertFalse(result["models"][0]["stream_available"])
        self.assertEqual("discover_only", result["provider_statuses"][0]["status"])


if __name__ == "__main__":
    unittest.main()
