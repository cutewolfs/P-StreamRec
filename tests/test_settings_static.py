import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SettingsStaticTests(unittest.TestCase):
    def test_recording_filename_format_control_is_wired(self):
        html = (ROOT / "static" / "settings.html").read_text()
        js = (ROOT / "static" / "settings.js").read_text()

        self.assertIn("filenameFormatSelect", html)
        self.assertIn("username_timestamp", html)
        self.assertIn("Recording Filename Format", html)
        self.assertIn("filenameFormatSelect", js)
        self.assertIn("data.filename_format || 'timestamp'", js)
        self.assertIn("updateRecordingSetting('filename_format', this.value)", html)


if __name__ == "__main__":
    unittest.main()
