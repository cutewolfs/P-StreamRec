import unittest
from fastapi.testclient import TestClient

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

    def test_automatic_docker_update_is_disabled_without_explicit_secure_opt_in(self):
        original_enabled = main.DOCKER_UPDATE_ENABLED
        original_password = main.PASSWORD
        main.DOCKER_UPDATE_ENABLED = False
        main.PASSWORD = ""
        try:
            disabled = TestClient(main.app).post("/api/system/update")
            main.DOCKER_UPDATE_ENABLED = True
            unprotected = TestClient(main.app).post("/api/system/update")
        finally:
            main.DOCKER_UPDATE_ENABLED = original_enabled
            main.PASSWORD = original_password

        self.assertEqual(404, disabled.status_code)
        self.assertEqual(403, unprotected.status_code)


if __name__ == "__main__":
    unittest.main()
