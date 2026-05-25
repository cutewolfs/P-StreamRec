import unittest

from app import main


class UpdateAvailabilityTests(unittest.TestCase):
    def test_dev_build_never_reports_update(self):
        self.assertFalse(main._is_update_available("dev", "2026.21.3"))
        self.assertFalse(main._is_update_available("vdev", "2026.21.3"))

    def test_matching_versions_do_not_report_update(self):
        self.assertFalse(main._is_update_available("2026.21.3", "v2026.21.3"))
        self.assertFalse(main._is_update_available("2026.21", "2026.21.0"))

    def test_newer_latest_version_reports_update(self):
        self.assertTrue(main._is_update_available("2026.21.3", "2026.21.4"))
        self.assertTrue(main._is_update_available("2026.21.3", "2026.22.0"))

    def test_older_latest_version_does_not_report_update(self):
        self.assertFalse(main._is_update_available("2026.22.0", "2026.21.9"))


if __name__ == "__main__":
    unittest.main()
