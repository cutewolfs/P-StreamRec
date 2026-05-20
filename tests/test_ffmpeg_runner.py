import unittest
from unittest.mock import patch

from app.ffmpeg_runner import _build_ffmpeg_command


class FFmpegCommandTests(unittest.TestCase):
    def _build(self, input_url):
        with (
            patch("app.ffmpeg_runner.ffmpeg_http_proxy_url", return_value=None),
            patch("app.ffmpeg_runner.get_outbound_proxy_url", return_value=None),
            patch("app.ffmpeg_runner.is_socks_proxy", return_value=False),
        ):
            return _build_ffmpeg_command("ffmpeg", input_url, "tee-output")

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
        self.assertIn("-i", cmd)


if __name__ == "__main__":
    unittest.main()
