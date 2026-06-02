import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SettingsStaticTests(unittest.TestCase):
    def test_settings_header_status_badge_is_removed(self):
        html = (ROOT / "static" / "settings.html").read_text()

        self.assertNotIn("settings-page-kicker", html)
        self.assertNotIn("Settings status", html)
        self.assertNotIn("settingsApiPill", html)
        self.assertNotIn("settingsApiDot", html)

    def test_recording_settings_exposes_check_interval_control(self):
        html = (ROOT / "static" / "settings.html").read_text()
        js = (ROOT / "static" / "settings.js").read_text()

        self.assertIn('id="checkIntervalInput"', html)
        self.assertIn("check_interval_seconds", html)
        self.assertIn("function normalizeCheckIntervalSeconds", js)
        self.assertIn("function setCheckIntervalInput", js)
        self.assertIn("data.check_interval_seconds", js)

    def test_tests_center_covers_local_diagnostics(self):
        js = (ROOT / "static" / "settings.js").read_text()
        test_ids = set(re.findall(r"id: '([^']+)'", js))

        self.assertTrue(
            {
                "api",
                "routes",
                "providers",
                "system",
                "recording",
                "following",
                "processes",
                "flaresolverr",
                "recordings",
                "media-imports",
                "blacklist",
            }.issubset(test_ids)
        )
        self.assertNotIn("chaturbate", test_ids)
        self.assertNotIn("cam4", test_ids)
        self.assertNotRegex(js, r"id:\s*'chaturbate'")
        self.assertNotRegex(js, r"id:\s*'cam4'")
        self.assertNotRegex(js, r"name:\s*'Chaturbate account'")
        self.assertNotRegex(js, r"name:\s*'CAM4 account'")
        self.assertIn("Legacy dashboard route still exists", js)
        self.assertIn("Removed provider still registered", js)
        self.assertIn("Missing status for", js)
        self.assertIn("Chaturbate and CAM4 should expose account login", js)
        self.assertIn("Chaturbate and CAM4 should expose remote sync", js)

    def test_following_page_lists_local_follow_providers(self):
        js = (ROOT / "static" / "following.js").read_text()

        self.assertIn("function providersForFollowing(models)", js)
        self.assertIn("providerBySource[sourceType] = Object.assign({}, provider", js)
        self.assertNotIn("if (caps.can_sync_following === true) {\n      providerBySource[sourceType]", js)
        self.assertIn("No local follows saved for this provider.", js)

    def test_media_page_has_unwatched_video_filter(self):
        html = (ROOT / "static" / "media.html").read_text()
        header = (ROOT / "static" / "header.html").read_text()
        js = (ROOT / "static" / "media.js").read_text()
        css = (ROOT / "static" / "styles.css").read_text()

        self.assertIn('data-page="media">Media</a>', header)
        self.assertIn("<title>Media - P-StreamRec</title>", html)
        self.assertIn("mediaUnwatchedOnlyToggle", html)
        self.assertIn("Unwatched", html)
        self.assertIn("unwatchedOnly", js)
        self.assertIn("params.set('watched', 'unwatched')", js)
        self.assertIn("toLocaleString('en-US'", js)
        self.assertIn("Unwatched videos", js)
        self.assertIn("Watched", js)
        self.assertIn(".media-unwatched-toggle", css)
        self.assertNotIn("M&eacute;dia", header)
        self.assertNotIn("Non vues", html)
        self.assertNotIn("Videos non vues", js)
        self.assertNotIn("Deja vu", js)

    def test_provider_settings_has_account_controls_for_sync_capable_providers(self):
        js = (ROOT / "static" / "settings.js").read_text()
        css = (ROOT / "static" / "styles.css").read_text()

        self.assertIn("Import Session", js)
        self.assertIn("function importProviderSession", js)
        self.assertIn("loginProvider", js)
        self.assertIn("reconnectProvider", js)
        self.assertIn("provider-session-import", js)
        self.assertIn("provider-login", js)
        self.assertIn("supportsAccount = caps.can_login === true", js)
        self.assertIn("providerAccountControls(source, status, caps)", js)
        self.assertIn(".provider-session-import", css)
        self.assertIn(".provider-login", css)

    def test_provider_settings_keeps_local_status_for_non_account_providers(self):
        js = (ROOT / "static" / "settings.js").read_text()

        self.assertIn("supportsAccount ? providerStatusText(status) : 'Local'", js)
        self.assertIn("Live, recording and local follows", js)
        self.assertIn("Live, recording, remote sync and follow", js)
        self.assertIn("Credentials Saved", js)
        self.assertIn("Session Required", js)
        self.assertIn("Login Failed", js)
        self.assertIn("Saved credentials are stored", js)
        self.assertIn("automatic login was blocked", js)
        self.assertIn("Browser session data is saved", js)
        self.assertIn("function providerStatusNeedsSessionImport", js)
        self.assertIn("function providerStatusLoginFailed", js)
        self.assertIn("var canSync = connected && caps.can_sync_following === true", js)
        self.assertNotIn("function providerConnectionProviders(providers)", js)
        self.assertIn("No providers configured.", js)
        self.assertIn("Import a verified browser session", js)

    def test_provider_settings_lists_all_providers_with_capability_checks(self):
        js = (ROOT / "static" / "settings.js").read_text()
        css = (ROOT / "static" / "styles.css").read_text()
        html = (ROOT / "static" / "settings.html").read_text()

        self.assertIn("providers = providers || []", js)
        self.assertIn("function providerCapabilityChecks(caps)", js)
        self.assertIn("providerCapabilityCheck('Discover', !!caps.can_discover)", js)
        self.assertIn("providerCapabilityCheck('Record', !!caps.can_record)", js)
        self.assertIn("providerCapabilityCheck('Follow / Unfollow', !!caps.can_follow)", js)
        self.assertIn("providerCapabilityCheck('Sync', !!caps.can_sync_following)", js)
        self.assertIn("provider-capability-icon", js)
        self.assertIn("&#10003;", js)
        self.assertIn("&#10005;", js)
        self.assertIn(".provider-capability-list", css)
        self.assertIn(".provider-capability-icon", css)
        self.assertIn(".provider-capability.is-enabled .provider-capability-icon", css)
        self.assertIn(".provider-capability.is-disabled .provider-capability-icon", css)
        self.assertNotIn(".provider-capability:has(input:checked)", css)
        self.assertIn(".status-indicator.available", css)
        self.assertIn("<h3>Providers</h3>", html)

    def test_provider_settings_maps_login_error_codes(self):
        js = (ROOT / "static" / "settings.js").read_text()

        self.assertNotIn("Automatic Stripchat account login failed;", js)
        self.assertIn("Automatic account login failed. Check credentials", js)
        self.assertIn("function providerStatusError", js)
        self.assertIn("providerConnectionError", js)

    def test_provider_settings_does_not_expose_removed_subscription_sources(self):
        js = (ROOT / "static" / "settings.js").read_text()

        self.assertNotIn("function headerValue", js)
        self.assertNotIn("function onlyFansPayloadFromObject", js)
        self.assertNotIn("onlyfans", js.lower())
        self.assertNotIn("manyvids", js.lower())
        self.assertNotIn("fansly", js.lower())
        self.assertIn("userAgent:", js)
        self.assertIn("providerSessionPayload", js)


if __name__ == "__main__":
    unittest.main()
