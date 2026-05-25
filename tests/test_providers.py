import unittest
from unittest.mock import AsyncMock, patch

from app.providers.browser import BrowserCaptureProvider
from app.providers.browser import extract_media_urls
from app.providers.browser import extract_hls_url_fields
from app.providers.browser import _page_metadata
from app.providers.browser import streamate_hls_manifest
from app.providers.builtin import CAM4Provider, ChaturbateProvider
from app.providers.registry import create_provider_registry
from app.services.cam4_source import _parse_broadcasts


class _DummyDB:
    pass


class ProviderRegistryTests(unittest.TestCase):
    def test_builtin_registry_includes_supported_sources(self):
        registry = create_provider_registry(_DummyDB())

        self.assertEqual(
            {
                "chaturbate",
                "cam4",
                "stripchat",
                "bongacams",
                "myfreecams",
                "livejasmin",
                "camsoda",
                "streamate",
                "flirt4free",
                "cams",
                "xcams",
            },
            registry.source_types(),
        )
        self.assertEqual(
            {
                "chaturbate",
                "cam4",
                "stripchat",
                "bongacams",
                "myfreecams",
                "livejasmin",
                "camsoda",
                "streamate",
                "flirt4free",
                "cams",
                "xcams",
            },
            {
                provider.source_type
                for provider in registry.all()
                if provider.capabilities.can_discover
            },
        )
        self.assertTrue(registry.get("xcams").capabilities.can_stream)
        self.assertTrue(registry.get("xcams").capabilities.can_record)
        self.assertTrue(registry.get("livejasmin").capabilities.can_stream)
        self.assertTrue(registry.get("livejasmin").capabilities.can_record)

    def test_browser_html_media_url_extraction_decodes_escaped_slashes(self):
        urls = extract_media_urls(
            'window.config={"hls":"https:\\/\\/cdn.example.test\\/live\\/room\\/playlist.m3u8?token=abc"}'
        )

        self.assertEqual(
            ["https://cdn.example.test/live/room/playlist.m3u8?token=abc"],
            urls,
        )

    def test_browser_websocket_hls_url_field_extraction(self):
        urls = extract_hls_url_fields(
            '42["setVideoData",{"protocol":{"h5live":{'
            '"hlsUrl":"https:\\/\\/edge.example.test\\/live\\/jwt-token?expires=123"}}}]'
        )

        self.assertEqual(
            ["https://edge.example.test/live/jwt-token?expires=123"],
            urls,
        )

    def test_streamate_manifest_payload_extracts_hls_master(self):
        manifest = streamate_hls_manifest({
            "formats": {
                "mp4-hls": {
                    "manifest": "https://manifest-server.naiadsystems.com/live/room.m3u8?token=abc"
                }
            }
        })

        self.assertEqual(
            "https://manifest-server.naiadsystems.com/live/room.m3u8?token=abc",
            manifest,
        )

    def test_browser_discover_parser_extracts_provider_models(self):
        provider = BrowserCaptureProvider(
            source_type="stripchat",
            display_name="Stripchat",
            url_templates=("https://stripchat.com/{username}",),
            domains=("stripchat.com",),
            discover_templates=("https://stripchat.com/",),
        )

        items = provider._parse_discover_models(
            '<a href="/alice" data-tags="cosplay, french">'
            '<img src="/alice.jpg" alt="Alice"> Alice <span>1.2k viewers</span> #latex</a>'
            '<a href="/login">Login</a>',
            "https://stripchat.com/",
        )

        self.assertEqual(1, len(items))
        self.assertEqual("alice", items[0]["username"])
        self.assertEqual("https://stripchat.com/alice.jpg", items[0]["thumbnail"])
        self.assertEqual(1200, items[0]["viewers"])
        self.assertEqual(["cosplay", "french", "latex"], items[0]["tags"])
        self.assertEqual("stripchat", items[0]["source_type"])

    def test_browser_page_metadata_extracts_tags_and_viewers(self):
        meta = _page_metadata(
            '<html><head><title>Alice live</title></head><body>'
            '<script>{"viewerCount":"2.5k","tags":["Cosplay",{"name":"French"}]}</script>'
            '<a href="/tags/gamer">Gamer</a> #latex'
            '<img src="/thumb.jpg">'
            '</body></html>',
            "https://stripchat.com/alice",
        )

        self.assertEqual(2500, meta["viewers"])
        self.assertEqual(["gamer", "latex", "cosplay", "french"], meta["tags"])
        self.assertEqual("https://stripchat.com/thumb.jpg", meta["thumbnail"])

    def test_flirt4free_discover_skips_non_model_links(self):
        provider = BrowserCaptureProvider(
            source_type="flirt4free",
            display_name="Flirt4Free",
            url_templates=("https://www.flirt4free.com/models/{username}.html",),
            domains=("flirt4free.com",),
            discover_templates=("https://www.flirt4free.com/search?q={query}",),
        )

        items = provider._parse_discover_models(
            '<a href="/schedule.php"><img src="/schedule.png">Schedules</a>'
            '<a href="/models/alice.html"><img src="/alice.jpg">Alice</a>',
            "https://www.flirt4free.com/",
        )

        self.assertEqual(["alice"], [item["username"] for item in items])

    def test_flirt4free_homepage_data_adds_public_hls_models(self):
        provider = BrowserCaptureProvider(
            source_type="flirt4free",
            display_name="Flirt4Free",
            url_templates=("https://www.flirt4free.com/models/{username}.html",),
            domains=("flirt4free.com",),
            discover_templates=("https://www.flirt4free.com/live/girls/",),
        )

        items = provider._parse_discover_models(
            "<script>window.__homePageData__ = {'models': ["
            '{"is_hls":"1","video_blocked":"0","room_status_char":"O","room_status":"In Open",'
            '"category_name":"Blonde","category_name_2":"College Girls","login_group_title":"Standard",'
            '"is_high_quality":"1","age":"24","display":"Esmi Bennet","model_name":"ESMI_BENNET",'
            '"model_seo_name":"esmi-bennet","model_id":"1456565","video_host":"video.test"}'
            "]};</script>",
            "https://www.flirt4free.com/live/girls/",
        )

        self.assertEqual(["esmi-bennet"], [item["username"] for item in items])
        self.assertEqual(24, items[0]["age"])
        self.assertIn("blonde", items[0]["tags"])
        self.assertIn("college girls", items[0]["tags"])
        self.assertIn("hd", items[0]["tags"])

    def test_cam4_rendered_cards_add_viewers_and_tags(self):
        items = _parse_broadcasts(
            '<div data-position="1" data-profile="NickiFrenchy">'
            '<a href="/nickifrenchy"><img src="https://thumb.test/n.jpg"></a>'
            '<div data-count="168" data-name="Viewers Count">168</div>'
            '<a href="/tags/female/c2c">#C2C</a>'
            '<a href="/tags/female/femdom">#femdom</a>'
            '</div>'
        )

        self.assertEqual(1, len(items))
        self.assertEqual("NickiFrenchy", items[0]["username"])
        self.assertEqual(168, items[0]["viewers"])
        self.assertIn("c2c", items[0]["tags"])
        self.assertIn("femdom", items[0]["tags"])

    def test_bongacams_cards_add_viewers_and_badge_tags(self):
        provider = BrowserCaptureProvider(
            source_type="bongacams",
            display_name="BongaCams",
            url_templates=("https://bongacams.com/{username}",),
            domains=("bongacams.com",),
            discover_templates=("https://bongacams.com/",),
        )

        items = provider._parse_discover_models(
            '<div class="lst_wrp __hd_plus __vibratoy" data-name="Dana4kabest" data-gender="female">'
            '<a href="/danabest"><img src="/d.jpg"></a>'
            '<div class="lsti_box lst_viewers">188</div>'
            '</div>',
            "https://bongacams.com/",
        )

        self.assertEqual(["danabest"], [item["username"] for item in items])
        self.assertEqual(188, items[0]["viewers"])
        self.assertIn("female", items[0]["tags"])
        self.assertIn("hd plus", items[0]["tags"])
        self.assertIn("vibratoy", items[0]["tags"])

    def test_stripchat_json_payload_adds_viewers_and_tags(self):
        provider = BrowserCaptureProvider(
            source_type="stripchat",
            display_name="Stripchat",
            url_templates=("https://stripchat.com/{username}",),
            domains=("stripchat.com",),
            discover_templates=("https://stripchat.com/girls",),
        )

        items = provider._parse_discover_models(
            '<script type="application/json" data-pstreamrec-url="https://stripchat.com/api/front/v2/models">'
            '{"blocks":[{"models":[{"username":"AliceHD","status":"public","gender":"female",'
            '"broadcastGender":"female","country":"fr","viewersCount":123,"id":42,'
            '"snapshotTimestamp":"1700000000","isOnline":true,"isHd":true,"isLovense":true}]}]}'
            '</script>',
            "https://stripchat.com/girls",
        )

        self.assertEqual(["AliceHD"], [item["username"] for item in items])
        self.assertEqual(123, items[0]["viewers"])
        self.assertEqual("https://img.doppiocdn.net/snapshot/42/1700000000", items[0]["thumbnail"])
        self.assertIn("female", items[0]["tags"])
        self.assertIn("fr", items[0]["tags"])
        self.assertIn("hd", items[0]["tags"])
        self.assertIn("lovense", items[0]["tags"])

    def test_livejasmin_embedded_performer_tags_are_parsed(self):
        provider = BrowserCaptureProvider(
            source_type="livejasmin",
            display_name="LiveJasmin",
            url_templates=("https://www.livejasmin.com/en/girls/{username}",),
            domains=("livejasmin.com",),
            discover_templates=("https://www.livejasmin.com/en/girls",),
        )

        items = provider._parse_discover_models(
            '<script>window._JSMConfig={}; listPagePerformers = ['
            '{"display_name":"LessieLean","status":1,"profilePictureUrl":"https://img.test/l.jpg",'
            '"willingnesses":{"cosplay":"Cosplay","live_orgasm":"Live orgasm"},'
            '"language":"lng_en","region":"EU","main_category":"girl"}];</script>',
            "https://www.livejasmin.com/en/girls",
        )

        self.assertEqual("LessieLean", items[0]["username"])
        self.assertEqual(["cosplay", "live orgasm", "en", "eu"], items[0]["tags"])

    def test_myfreecams_model_boxes_are_parsed_with_global_tags(self):
        provider = BrowserCaptureProvider(
            source_type="myfreecams",
            display_name="MyFreeCams",
            url_templates=("https://www.myfreecams.com/#{username}",),
            domains=("myfreecams.com",),
            discover_templates=("https://www.myfreecams.com/",),
        )

        items = provider._parse_discover_models(
            '<a href="/?tag=ebony">ebony</a><a href="#">\u00d7</a>'
            '<div class="model_online homepage_online_pattern2 modelbox_3765510" id="model_box_3765510" data-list-pos="0">'
            '<a title="Enter Chat Room of BeaClips">Chat</a><img src="https://img.test/b.jpg"></div>',
            "https://www.myfreecams.com/",
        )

        self.assertEqual(["BeaClips"], [item["username"] for item in items])
        self.assertIn("ebony", items[0]["tags"])

class BuiltinProviderDiscoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_chaturbate_discover_filters_generic_kwargs(self):
        class FakeAPI:
            def __init__(self):
                self.kwargs = None

            async def get_live_models(self, **kwargs):
                self.kwargs = kwargs
                return {"models": [{"username": "alice"}]}

        api = FakeAPI()
        provider = ChaturbateProvider(api=api)

        result = await provider.list_live_models(
            page=2,
            limit=7,
            gender="female",
            search="ali",
            tags=["french"],
            allow_browser=True,
        )

        self.assertEqual({"page": 2, "limit": 7, "gender": "female", "search": "ali", "tag": "french"}, api.kwargs)
        self.assertEqual("chaturbate", result["models"][0]["source_type"])

    async def test_chaturbate_status_supplements_tags_and_viewers_from_discover(self):
        class FakeAPI:
            async def check_status(self, username):
                return {
                    "is_online": True,
                    "viewers": 0,
                    "room_status": "public",
                    "hls_source": "https://cdn.example/live.m3u8",
                    "tags": [],
                }

            async def get_live_models(self, **kwargs):
                return {
                    "models": [
                        {
                            "username": kwargs["search"],
                            "viewers": 321,
                            "tags": ["French", "Cosplay"],
                            "thumbnail": "https://example.test/thumb.jpg",
                            "room_status": "public",
                        }
                    ]
                }

        provider = ChaturbateProvider(api=FakeAPI())

        status = await provider.check_status("alice")

        self.assertTrue(status.is_online)
        self.assertEqual(321, status.viewers)
        self.assertEqual(["French", "Cosplay"], status.tags)
        self.assertEqual("https://example.test/thumb.jpg", status.thumbnail)

    async def test_cam4_discover_filters_generic_kwargs(self):
        provider = CAM4Provider()
        mocked = AsyncMock(return_value={"models": [{"username": "bob"}]})

        with patch("app.services.cam4_source.list_live_models", mocked):
            result = await provider.list_live_models(page=3, limit=5, tags=["tag"], allow_browser=True)

        mocked.assert_awaited_once_with(page=3, limit=5, gender=None, search=None, tags=["tag"])
        self.assertEqual("cam4", result["models"][0]["source_type"])


if __name__ == "__main__":
    unittest.main()
