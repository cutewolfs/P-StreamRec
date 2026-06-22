import asyncio
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import main

ROOT = Path(__file__).resolve().parents[1]


class HlsProxyTests(unittest.TestCase):
    def setUp(self):
        main._HLS_PROXY_CACHE.clear()
        main._HLS_PROXY_REVERSE.clear()
        main._IVS_PLAYER_ASSET_CACHE.clear()

    def tearDown(self):
        main._HLS_PROXY_CACHE.clear()
        main._HLS_PROXY_REVERSE.clear()
        main._IVS_PLAYER_ASSET_CACHE.clear()

    def test_rewrite_playlist_proxies_segments_and_keys(self):
        rewritten = main._rewrite_hls_playlist(
            '#EXTM3U\n'
            '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
            '#EXTINF:4.0,\n'
            'seg-1.ts\n',
            'https://cdn.example.test/live/master.m3u8',
            headers={"Cookie": "session=secret"},
        )

        self.assertIn("#EXTM3U", rewritten)
        self.assertEqual(2, rewritten.count("/api/proxy/hls/"))
        self.assertIn(".bin", rewritten)
        self.assertIn(".ts", rewritten)
        self.assertEqual(2, len(main._HLS_PROXY_CACHE))
        urls = {entry["url"] for entry in main._HLS_PROXY_CACHE.values()}
        self.assertIn("https://cdn.example.test/live/key.bin", urls)
        self.assertIn("https://cdn.example.test/live/seg-1.ts", urls)
        for entry in main._HLS_PROXY_CACHE.values():
            self.assertEqual({"Cookie": "session=secret"}, entry["headers"])

    def test_rewrite_playlist_strips_low_latency_hints(self):
        rewritten = main._rewrite_hls_playlist(
            "#EXTM3U\n"
            "#EXT-X-VERSION:6\n"
            "#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,PART-HOLD-BACK=2.430000\n"
            "#EXT-X-PART-INF:PART-TARGET=0.800000\n"
            "#EXT-X-MAP:URI=\"init.m4s\"\n"
            "#EXT-X-PART:DURATION=0.8,URI=\"part-1.m4s\",INDEPENDENT=YES\n"
            "#EXT-X-PROGRAM-DATE-TIME:2026-05-25T14:34:51.627+00:00\n"
            "#EXTINF:1.6,\n"
            "segment-1.m4s\n"
            "#EXT-X-PROGRAM-DATE-TIME:2026-05-25T14:34:53.227+00:00\n"
            "#EXT-X-PART:DURATION=0.8,URI=\"part-orphan.m4s\",INDEPENDENT=YES\n"
            "#EXT-X-PRELOAD-HINT:TYPE=PART,URI=\"part-2.m4s\"\n"
            "#EXT-X-RENDITION-REPORT:URI=\"variant.m3u8\",LAST-MSN=1,LAST-PART=0\n",
            "https://cdn.example.test/live/playlist.m3u8",
        )

        self.assertNotIn("#EXT-X-SERVER-CONTROL", rewritten)
        self.assertNotIn("#EXT-X-PART-INF", rewritten)
        self.assertNotIn("#EXT-X-PART:", rewritten)
        self.assertNotIn("#EXT-X-PRELOAD-HINT", rewritten)
        self.assertNotIn("#EXT-X-RENDITION-REPORT", rewritten)
        self.assertIn("#EXT-X-MAP", rewritten)
        self.assertIn("2026-05-25T14:34:51.627+00:00", rewritten)
        self.assertNotIn("2026-05-25T14:34:53.227+00:00", rewritten)
        self.assertEqual(2, rewritten.count("/api/proxy/hls/"))
        urls = {entry["url"] for entry in main._HLS_PROXY_CACHE.values()}
        self.assertIn("https://cdn.example.test/live/init.m4s", urls)
        self.assertIn("https://cdn.example.test/live/segment-1.m4s", urls)
        self.assertNotIn("https://cdn.example.test/live/part-1.m4s", urls)
        self.assertNotIn("https://cdn.example.test/live/part-orphan.m4s", urls)
        self.assertNotIn("https://cdn.example.test/live/part-2.m4s", urls)

    def test_proxy_suffix_maps_pts_segments_to_ts_for_ffmpeg(self):
        proxied = main._register_hls_proxy_url("https://cdn.example.test/live/chunk.pts")

        self.assertTrue(proxied.endswith(".ts"))

    def test_proxy_suffix_maps_segment_flag_to_mp4_for_ffmpeg(self):
        proxied = main._register_hls_proxy_url("https://cdn.example.test/live/token?flags=segment")

        self.assertTrue(proxied.endswith(".mp4"))

    def test_proxy_reuses_token_for_same_upstream_asset(self):
        first = main._register_hls_proxy_url(
            "https://cdn.example.test/live/segment-1.m4s",
            headers={"Referer": "https://example.test"},
        )
        second = main._register_hls_proxy_url(
            "https://cdn.example.test/live/segment-1.m4s",
            headers={"Referer": "https://example.test"},
        )
        different_headers = main._register_hls_proxy_url(
            "https://cdn.example.test/live/segment-1.m4s",
            headers={"Referer": "https://other.example.test"},
        )

        self.assertEqual(first, second)
        self.assertNotEqual(first, different_headers)
        self.assertEqual(2, len(main._HLS_PROXY_CACHE))

    def test_hls_segment_header_variants_retry_without_cookie(self):
        variants = main._hls_segment_header_variants({
            "User-Agent": "UA",
            "Referer": "https://example.test",
            "Cookie": "session=secret",
        })

        self.assertEqual("session=secret", variants[0]["Cookie"])
        self.assertNotIn("Cookie", variants[1])
        self.assertEqual("UA", variants[1]["User-Agent"])

    def test_rewrite_livejasmin_short_playlist_as_live_refresh(self):
        rewritten = main._rewrite_hls_playlist(
            "#EXTM3U\n"
            "#EXT-X-PLAYLIST-TYPE:VOD\n"
            "#EXT-X-TARGETDURATION:1\n"
            "#EXT-X-MEDIA-SEQUENCE:0\n"
            "#EXTINF:1.0,\n"
            "token?flags=segment\n"
            "#EXT-X-ENDLIST\n",
            "https://cdn.example.test/live/token",
            live_sequence=7,
        )

        self.assertIn("#EXT-X-MEDIA-SEQUENCE:7", rewritten)
        self.assertNotIn("#EXT-X-PLAYLIST-TYPE:VOD", rewritten)
        self.assertNotIn("#EXT-X-ENDLIST", rewritten)
        self.assertIn(".mp4", rewritten)

    def test_rewrite_stripchat_master_appends_mouflon_keys_to_variants(self):
        rewritten = main._rewrite_hls_playlist(
            "#EXTM3U\n"
            "#EXT-X-MOUFLON:PSCH:v2:key-one\n"
            "#EXT-X-MOUFLON:PSCH:v2:key-two\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=2000,NAME=\"source\"\n"
            "https://media-hls.doppiocdn.net/live/model.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=1000,NAME=\"480p\"\n"
            "https://media-hls.doppiocdn.net/live/model_480p.m3u8?minHeight=240\n",
            "https://edge-hls.doppiocdn.net/hls/model/master.m3u8",
        )

        self.assertEqual(2, rewritten.count("/api/proxy/hls/"))
        urls = {entry["url"] for entry in main._HLS_PROXY_CACHE.values()}
        self.assertIn("https://media-hls.doppiocdn.net/live/model.m3u8?playlistType=lowLatency&psch=v2&pkey=key-one", urls)
        self.assertIn(
            "https://media-hls.doppiocdn.net/live/model_480p.m3u8?minHeight=240&playlistType=lowLatency&psch=v2&pkey=key-two",
            urls,
        )

    def test_rewrite_stripchat_master_preserves_existing_playback_key(self):
        main._rewrite_hls_playlist(
            "#EXTM3U\n"
            "#EXT-X-MOUFLON:PSCH:v2:key-one\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=2000,NAME=\"source\"\n"
            "https://media-hls.doppiocdn.net/live/model.m3u8?minHeight=240&playlistType=standard&pkey=site-key\n",
            "https://edge-hls.doppiocdn.net/hls/model/master.m3u8",
        )

        urls = {entry["url"] for entry in main._HLS_PROXY_CACHE.values()}
        self.assertIn(
            "https://media-hls.doppiocdn.net/live/model.m3u8?minHeight=240&playlistType=standard&pkey=site-key",
            urls,
        )

    def test_rewrite_stripchat_media_playlist_uses_mouflon_segment_urls(self):
        rewritten = main._rewrite_hls_playlist(
            "#EXTM3U\n"
            "#EXT-X-VERSION:6\n"
            "#EXT-X-MAP:URI=\"init.mp4\"\n"
            "#EXT-X-MOUFLON:URI:https://media-hls.doppiocdn.net/live/model_part0.mp4\n"
            "#EXT-X-PART:DURATION=0.5,URI=\"https://media-hls.doppiocdn.net/live/media.mp4\"\n"
            "#EXTINF:2.0,\n"
            "#EXT-X-MOUFLON:URI:https://media-hls.doppiocdn.net/live/model_123.mp4\n"
            "https://media-hls.doppiocdn.net/live/media.mp4\n",
            "https://media-hls.doppiocdn.net/live/model.m3u8",
        )

        self.assertNotIn("#EXT-X-MOUFLON:URI", rewritten)
        self.assertNotIn("#EXT-X-PART", rewritten)
        self.assertIn("#EXTINF:0.5,", rewritten)
        self.assertEqual(2, rewritten.count("/api/proxy/hls/"))
        urls = {entry["url"] for entry in main._HLS_PROXY_CACHE.values()}
        self.assertIn("https://media-hls.doppiocdn.net/live/init.mp4", urls)
        self.assertIn("https://media-hls.doppiocdn.net/live/model_part0.mp4", urls)
        self.assertNotIn("https://media-hls.doppiocdn.net/live/model_123.mp4", urls)
        self.assertNotIn("https://media-hls.doppiocdn.net/live/media.mp4", urls)

    def test_ffmpeg_input_uses_local_proxy_for_livejasmin(self):
        stream = main.ResolvedStream(
            url="https://cdn.example.test/live/token",
            headers={"Cookie": "session=secret"},
            source_type="livejasmin",
        )

        url, headers, source_url = main._ffmpeg_stream_input(stream)

        self.assertTrue(url.startswith("http://127.0.0.1:"))
        self.assertIn("/api/proxy/hls/", url)
        self.assertTrue(url.endswith(".m3u8"))
        self.assertIsNone(headers)
        self.assertEqual(stream.url, source_url)

    def test_ffmpeg_input_uses_local_proxy_for_regular_hls(self):
        stream = main.ResolvedStream(
            url="https://cdn.example.test/live/master.m3u8",
            headers={"Referer": "https://example.test/model"},
            source_type="cams",
        )

        url, headers, source_url = main._ffmpeg_stream_input(stream)

        self.assertTrue(url.startswith("http://127.0.0.1:"))
        self.assertIn("/api/proxy/hls/", url)
        self.assertIsNone(headers)
        self.assertEqual(stream.url, source_url)

    def test_ffmpeg_input_uses_local_proxy_for_chaturbate_hls(self):
        stream = main.ResolvedStream(
            url="https://edge.mmcdn.com/live/llhls.m3u8",
            headers={"Referer": "https://chaturbate.com/"},
            source_type="chaturbate",
        )

        url, headers, source_url = main._ffmpeg_stream_input(stream)

        self.assertTrue(url.startswith("http://127.0.0.1:"))
        self.assertIn("/api/proxy/hls/", url)
        self.assertIsNone(headers)
        self.assertEqual(stream.url, source_url)

    def test_ffmpeg_input_uses_preloaded_chaturbate_master_playlist(self):
        stream = main.ResolvedStream(
            url="https://edge.mmcdn.com/live/llhls.m3u8?token=one",
            headers={"Referer": "https://chaturbate.com/"},
            source_type="chaturbate",
            hls_playlist_text=(
                "#EXTM3U\n"
                "#EXT-X-STREAM-INF:BANDWIDTH=3096000,RESOLUTION=1280x720\n"
                "chunklist_3_video_llhls.m3u8?session=abc\n"
            ),
            hls_playlist_base_url="https://edge.mmcdn.com/live/llhls.m3u8?token=one",
            hls_playlist_content_type="application/vnd.apple.mpegurl",
        )

        url, headers, source_url = main._ffmpeg_stream_input(stream)

        self.assertTrue(url.startswith("http://127.0.0.1:"))
        self.assertTrue(url.endswith(".m3u8"))
        self.assertIsNone(headers)
        self.assertEqual(stream.url, source_url)
        master_token = url.split("/api/proxy/hls/", 1)[1].split(".", 1)[0]
        master_entry = main._HLS_PROXY_CACHE[master_token]
        self.assertIn("body", master_entry)
        body = master_entry["body"].decode("utf-8")
        self.assertIn("/api/proxy/hls/", body)
        variant_urls = {
            entry["url"] for token, entry in main._HLS_PROXY_CACHE.items()
            if token != master_token
        }
        self.assertIn(
            "https://edge.mmcdn.com/live/chunklist_3_video_llhls.m3u8?session=abc",
            variant_urls,
        )

    def test_watch_stream_payload_proxies_hls_by_default(self):
        stream = main.ResolvedStream(
            url="https://edge.example.test/live/channel.m3u8",
            headers={"Referer": "https://example.test/live/alice"},
            source_type="xcams",
        )

        payload = main._watch_stream_payload(stream)

        self.assertNotIn("streamType", payload)
        self.assertTrue(payload["streamUrl"].startswith("/api/proxy/hls/"))
        self.assertEqual(1, len(main._HLS_PROXY_CACHE))

    def test_hls_proxy_is_public_when_password_is_enabled(self):
        original_password = main.PASSWORD
        main.PASSWORD = "secret"
        try:
            url = main._register_cached_hls_body(
                "https://edge.example.test/live/master.m3u8",
                b"#EXTM3U\n",
                "application/vnd.apple.mpegurl",
                suffix=".m3u8",
            )
            response = TestClient(main.app).get(url)
        finally:
            main.PASSWORD = original_password

        self.assertEqual(200, response.status_code)
        self.assertEqual("#EXTM3U\n", response.text)

    def test_hls_proxy_retries_upstream_without_cookie_on_403(self):
        class FakeResp:
            def __init__(self, status, body=b"segment", url="https://edge.example.test/seg.ts"):
                self.status = status
                self._body = body
                self.url = url
                self.headers = {"Content-Type": "video/mp2t"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def read(self):
                return self._body

        class FakeSession:
            def __init__(self):
                self.calls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def get(self, url, headers=None, **kwargs):
                self.calls.append(dict(headers or {}))
                if "Cookie" in (headers or {}):
                    return FakeResp(403)
                return FakeResp(200)

        fake_session = FakeSession()
        original_session_factory = main.aiohttp_client_session
        main.aiohttp_client_session = lambda *args, **kwargs: fake_session
        try:
            token_url = main._register_hls_proxy_url(
                "https://edge.example.test/seg.ts",
                headers={"Cookie": "session=bad", "Referer": "https://chaturbate.com/"},
            )
            token_path = token_url.split("/api/proxy/hls/", 1)[1]
            request = type("Request", (), {"method": "GET"})()
            response = asyncio.run(main.hls_proxy(token_path, request))
        finally:
            main.aiohttp_client_session = original_session_factory

        self.assertEqual(200, response.status_code)
        self.assertEqual([True, False], ["Cookie" in headers for headers in fake_session.calls])

    def test_watch_player_loads_ivs_script_from_local_vendor_route(self):
        js = (ROOT / "static" / "watch.js").read_text()
        html = (ROOT / "static" / "watch.html").read_text()

        self.assertIn("/vendor/amazon-ivs-player.min.js", js)
        self.assertNotIn("https://player.live-video.net/1.4.1/amazon-ivs-player.min.js", js)
        self.assertIn("/static/watch.js?v=14", html)
        self.assertIn("amazon-ivs-wasmworker.min.js", main.IVS_PLAYER_ASSETS)
        self.assertIn("amazon-ivs-worker.min.js", main.IVS_PLAYER_ASSETS)
        self.assertIn("amazon-ivs-wasmworker.min.wasm", main.IVS_PLAYER_ASSETS)

    def test_stripchat_uses_hls_proxy_path(self):
        self.assertFalse(main._supports_browser_capture("stripchat"))
        self.assertFalse(main._supports_browser_capture("onlyfans"))

    def test_resolve_stream_rejects_discover_only_provider(self):
        class _Caps:
            can_stream = False

        class _Provider:
            display_name = "LiveJasmin"
            capabilities = _Caps()

            async def resolve_stream(self, *args, **kwargs):
                raise AssertionError("resolve_stream should not be called")

        class _Registry:
            def has(self, source_type):
                return source_type == "livejasmin"

            def get(self, source_type):
                return _Provider()

        original_registry = main.provider_registry
        main.provider_registry = _Registry()
        try:
            with self.assertRaises(main.ProviderError):
                asyncio.run(main._resolve_stream("livejasmin", "model", None))
        finally:
            main.provider_registry = original_registry


if __name__ == "__main__":
    unittest.main()
