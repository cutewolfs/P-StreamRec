import unittest

from app.tasks.convert import _video_stream_map_from_probe


class ConvertStreamSelectionTests(unittest.TestCase):
    def test_video_stream_map_picks_highest_resolution_video(self):
        probe = {
            "streams": [
                {"width": 640, "height": 360, "bit_rate": "800000"},
                {"width": 1280, "height": 720, "bit_rate": "2500000"},
                {"width": 1920, "height": 1080, "bit_rate": "5000000"},
            ]
        }

        self.assertEqual("0:v:2", _video_stream_map_from_probe(probe))

    def test_video_stream_map_falls_back_to_first_video(self):
        self.assertEqual("0:v:0", _video_stream_map_from_probe({}))


if __name__ == "__main__":
    unittest.main()
