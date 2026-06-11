import unittest

from app.resolvers.chaturbate import _parse_master_playlist, _pick_variant_info


CURRENT_LLHLS_MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio_aac_96",NAME="Audio",DEFAULT=YES,AUTOSELECT=YES,CHANNELS="2",URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=546000,RESOLUTION=428x240,AUDIO="audio_aac_96"
chunklist_0_video.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=896000,RESOLUTION=640x360,AUDIO="audio_aac_96"
chunklist_1_video.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1696000,RESOLUTION=960x540,AUDIO="audio_aac_96"
chunklist_2_video.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3096000,RESOLUTION=1280x720,AUDIO="audio_aac_96"
chunklist_3_video.m3u8
"""


class ChaturbateResolverTests(unittest.TestCase):
    def test_llhls_parser_tracks_actual_video_variant_indexes(self):
        variants = _parse_master_playlist(CURRENT_LLHLS_MASTER)

        self.assertEqual([0, 1, 2, 3], [item["index"] for item in variants])
        self.assertEqual([240, 360, 540, 720], [item["height"] for item in variants])

    def test_best_variant_uses_highest_available_not_fixed_1080_index(self):
        picked = _pick_variant_info(_parse_master_playlist(CURRENT_LLHLS_MASTER), None)

        self.assertEqual(3, picked["index"])
        self.assertEqual(720, picked["height"])
        self.assertEqual("chunklist_3_video.m3u8", picked["url"])

    def test_height_cap_selects_best_variant_below_cap(self):
        picked = _pick_variant_info(_parse_master_playlist(CURRENT_LLHLS_MASTER), 540)

        self.assertEqual(2, picked["index"])
        self.assertEqual(540, picked["height"])


if __name__ == "__main__":
    unittest.main()
