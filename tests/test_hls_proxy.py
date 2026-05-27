import asyncio
import unittest

from app import main


class HlsProxyTests(unittest.TestCase):
    def setUp(self):
        main._HLS_PROXY_CACHE.clear()
        main._HLS_PROXY_REVERSE.clear()

    def tearDown(self):
        main._HLS_PROXY_CACHE.clear()
        main._HLS_PROXY_REVERSE.clear()

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
            source_type="streamate",
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
