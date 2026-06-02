import unittest

from app.tasks.convert import _select_best_video_stream_map


class ConvertTaskTests(unittest.TestCase):
    def test_selects_highest_resolution_video_stream(self):
        probe_data = {
            "streams": [
                {"index": 0, "width": 640, "height": 360, "bit_rate": "530000"},
                {"index": 1, "width": 1280, "height": 720, "bit_rate": "1800000"},
                {"index": 3, "width": 1920, "height": 1080, "bit_rate": "3500000"},
            ]
        }

        self.assertEqual("0:3", _select_best_video_stream_map(probe_data))

    def test_breaks_resolution_ties_by_bitrate(self):
        probe_data = {
            "streams": [
                {"index": 0, "width": 1280, "height": 720, "bit_rate": "1200000"},
                {"index": 2, "width": 1280, "height": 720, "bit_rate": "2500000"},
            ]
        }

        self.assertEqual("0:2", _select_best_video_stream_map(probe_data))

    def test_falls_back_to_first_video_when_probe_has_no_video_streams(self):
        self.assertEqual("0:v:0", _select_best_video_stream_map({"streams": []}))


if __name__ == "__main__":
    unittest.main()
