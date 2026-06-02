import unittest
from unittest.mock import patch

from app.ffmpeg_runner import _build_ffmpeg_command


class FFmpegCommandTests(unittest.TestCase):
    def _build(self, input_url, **kwargs):
        with (
            patch("app.ffmpeg_runner.ffmpeg_http_proxy_url", return_value=None),
            patch("app.ffmpeg_runner.get_outbound_proxy_url", return_value=None),
            patch("app.ffmpeg_runner.is_socks_proxy", return_value=False),
        ):
            return _build_ffmpeg_command("ffmpeg", input_url, "tee-output", **kwargs)

    def _maps(self, cmd):
        return [cmd[i + 1] for i, part in enumerate(cmd) if part == "-map"]

    def test_chaturbate_cdn_uses_persistent_http_before_input(self):
        cmd = self._build("https://edge30-ash.live.mmcdn.com/live/test/llhls.m3u8")
        input_index = cmd.index("-i")

        self.assertLess(cmd.index("-http_persistent"), input_index)
        self.assertEqual(cmd[cmd.index("-http_persistent") + 1], "1")
        self.assertLess(cmd.index("-http_multiple"), input_index)
        self.assertEqual(cmd[cmd.index("-http_multiple") + 1], "0")
        self.assertLess(cmd.index("-multiple_requests"), input_index)
        self.assertEqual(cmd[cmd.index("-multiple_requests") + 1], "1")

        headers = cmd[cmd.index("-headers") + 1]
        self.assertIn("Referer: https://chaturbate.com/", headers)
        self.assertIn("Origin: https://chaturbate.com", headers)
        self.assertIn("Connection: keep-alive", headers)

    def test_non_chaturbate_hls_keeps_default_http_behavior(self):
        cmd = self._build("https://example.com/live/test/playlist.m3u8")

        self.assertNotIn("-http_persistent", cmd)
        self.assertNotIn("-http_multiple", cmd)
        self.assertNotIn("-multiple_requests", cmd)
        self.assertNotIn("-headers", cmd)
        self.assertIn("-reconnect_at_eof", cmd)
        self.assertLess(cmd.index("-allowed_extensions"), cmd.index("-i"))
        self.assertEqual(cmd[cmd.index("-allowed_extensions") + 1], "ALL")
        self.assertLess(cmd.index("-allowed_segment_extensions"), cmd.index("-i"))
        self.assertEqual(cmd[cmd.index("-allowed_segment_extensions") + 1], "ALL")
        self.assertLess(cmd.index("-extension_picky"), cmd.index("-i"))
        self.assertEqual(cmd[cmd.index("-extension_picky") + 1], "0")
        self.assertIn("-i", cmd)

    def test_local_hls_proxy_skips_eof_reconnect(self):
        cmd = self._build("http://127.0.0.1:8080/api/proxy/hls/token.m3u8")

        self.assertNotIn("-reconnect", cmd)
        self.assertNotIn("-reconnect_at_eof", cmd)
        self.assertNotIn("-reconnect_streamed", cmd)
        self.assertLess(cmd.index("-allowed_extensions"), cmd.index("-i"))
        self.assertEqual(["0"], self._maps(cmd))

    def test_proxied_chaturbate_llhls_uses_source_url_for_best_mapping(self):
        cmd = self._build(
            "http://127.0.0.1:8080/api/proxy/hls/token.m3u8",
            source_url="https://edge30-ash.live.mmcdn.com/live/test/llhls.m3u8",
        )

        self.assertEqual(["0:v:4", "0:a:0"], self._maps(cmd))

    def test_chaturbate_llhls_height_cap_selects_matching_video_stream(self):
        cmd = self._build(
            "https://edge30-ash.live.mmcdn.com/live/test/llhls.m3u8",
            max_height=720,
        )

        self.assertEqual(["0:v:3", "0:a:0"], self._maps(cmd))

    def test_provider_headers_are_passed_before_input(self):
        with (
            patch("app.ffmpeg_runner.ffmpeg_http_proxy_url", return_value=None),
            patch("app.ffmpeg_runner.get_outbound_proxy_url", return_value=None),
            patch("app.ffmpeg_runner.is_socks_proxy", return_value=False),
        ):
            cmd = _build_ffmpeg_command(
                "ffmpeg",
                "https://example.com/live/test/playlist.m3u8",
                "tee-output",
                input_headers={
                    "Referer": "https://example.com/model",
                    "Cookie": "session=secret",
                },
            )

        input_index = cmd.index("-i")
        self.assertLess(cmd.index("-headers"), input_index)
        headers = cmd[cmd.index("-headers") + 1]
        self.assertIn("Referer: https://example.com/model", headers)
        self.assertIn("Cookie: session=secret", headers)


if __name__ == "__main__":
    unittest.main()
