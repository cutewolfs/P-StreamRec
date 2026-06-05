import re
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

    def test_settings_header_status_badge_is_removed(self):
        html = (ROOT / "static" / "settings.html").read_text()

        self.assertNotIn("settings-page-kicker", html)
        self.assertNotIn("Settings status", html)
        self.assertNotIn("settingsApiPill", html)
        self.assertNotIn("settingsApiDot", html)

    def test_settings_application_tab_is_removed(self):
        html = (ROOT / "static" / "settings.html").read_text()
        js = (ROOT / "static" / "settings.js").read_text()

        self.assertNotIn('data-tab="application"', html)
        self.assertNotIn('id="tab-application"', html)
        self.assertNotIn('id="appVersionSetting"', html)
        self.assertNotIn('id="apiStatus"', html)
        self.assertNotIn("appVersionSetting", js)

    def test_flaresolverr_settings_url_can_be_edited(self):
        html = (ROOT / "static" / "settings.html").read_text()
        js = (ROOT / "static" / "settings.js").read_text()

        self.assertIn("flareUrlInput", html)
        self.assertIn("flareSaveBtn", html)
        self.assertIn("saveFlareSolverrUrl", js)
        self.assertIn("loadFlareSolverrSettings", js)
        self.assertIn("/api/settings/flaresolverr", js)
        self.assertIn("FlareSolverr URL saved", js)
        self.assertNotIn("configured via environment variables", html)

    def test_flaresolverr_is_not_configured_by_environment_variables(self):
        files = [
            "app/main.py",
            "app/core/config.py",
            "app/providers/browser.py",
            "app/services/flaresolverr.py",
            "docker-compose.yml",
            "README.md",
            "static/wiki.html",
        ]
        text = "\n".join((ROOT / path).read_text() for path in files)

        self.assertNotIn("FLARESOLVERR_URL", text)
        self.assertNotIn("PSTREAMREC_FLARESOLVERR_URL", text)
        self.assertNotIn("FLARESOLVERR_MAX_TIMEOUT", text)
        self.assertNotIn("PSTREAMREC_FLARESOLVERR_TIMEOUT_MS", text)
        self.assertNotRegex(text, r"os\.getenv\([^)]*FLARESOLVERR")

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

    def test_media_page_has_profile_filter_and_continuous_playback(self):
        html = (ROOT / "static" / "media.html").read_text()
        header = (ROOT / "static" / "header.html").read_text()
        js = (ROOT / "static" / "media.js").read_text()
        css = (ROOT / "static" / "styles.css").read_text()

        self.assertNotIn('href="/recordings"', header)
        self.assertNotIn('data-page="recordings"', header)
        self.assertNotIn("Recordings</a>", header)
        self.assertIn('href="/media" class="nav-link" data-page="media">Media</a>', header)
        self.assertNotIn('href="/stash"', header)
        self.assertIn("<title>Media - P-StreamRec</title>", html)
        self.assertIn("mediaNewProfileBtn", html)
        self.assertIn("mediaProfileFilter", html)
        self.assertNotIn("mediaProfileDetail", html)
        self.assertNotIn("mediaProfileFilterBtn", html)
        self.assertNotIn("mediaProfileClearBtn", html)
        self.assertNotIn("mediaUploadBtn", html)
        self.assertNotIn("mediaUploadForm", html)
        self.assertNotIn("/uploads", html)
        self.assertIn("Date of birth", html)
        self.assertIn("Profile image URL", html)
        self.assertIn("Babepedia page URL", html)
        self.assertIn("Fetch Babepedia image", html)
        self.assertIn("profileSourcesList", html)
        self.assertIn("profileAddSourceBtn", html)
        self.assertIn("Add source", html)
        self.assertIn("mediaUnwatchedOnlyToggle", html)
        self.assertIn("Unwatched", html)
        self.assertIn("profileImageUrl", js)
        self.assertIn("streamSources", js)
        self.assertIn("channelUsername", js)
        self.assertIn("channelUsernameFromUrl", js)
        self.assertIn("selectedProfile", js)
        self.assertIn("filterProfile", js)
        self.assertIn("formatProfileMediaCounts", js)
        self.assertIn("mediaProfileFilter", js)
        self.assertIn("media-profile-menu-btn", js)
        self.assertIn('data-profile-action="settings"', js)
        self.assertNotIn("renderProfileDetail", js)
        self.assertNotIn("filterSelectedProfile", js)
        self.assertNotIn("clearSelectedProfile", js)
        self.assertIn("showNextPrompt", js)
        self.assertIn("nextVideoItem", js)
        self.assertIn("data-next-action", js)
        self.assertNotIn("openUploadModal", js)
        self.assertNotIn("uploadMediaFiles", js)
        self.assertNotIn("/uploads", js)
        self.assertNotIn("<label>Channel<input", js)
        self.assertNotIn('data-source-field="channelUsername" type="text"', js)
        self.assertNotIn("profile.thumbnail", js)
        self.assertIn("unwatchedOnly", js)
        self.assertIn("params.set('watched', 'unwatched')", js)
        self.assertIn("toLocaleString('en-US'", js)
        self.assertIn("Unwatched videos", js)
        self.assertIn("Watched", js)
        self.assertIn(".media-unwatched-toggle", css)
        self.assertIn(".media-next-prompt", css)
        self.assertIn(".media-profile-menu-btn", css)
        self.assertNotIn(".media-profile-detail", css)
        self.assertNotIn(".media-upload-modal", css)
        self.assertNotIn("M&eacute;dia", header)
        self.assertNotIn("Non vues", html)
        self.assertNotIn("Videos non vues", js)
        self.assertNotIn("Deja vu", js)

    def test_watch_page_uses_set_recording_profile_flow(self):
        html = (ROOT / "static" / "watch.html").read_text()
        js = (ROOT / "static" / "watch.js").read_text()

        self.assertIn("Set recording", html)
        self.assertIn("recordingModal", html)
        self.assertIn("recordingProfileSearch", html)
        self.assertIn("Existing profile", html)
        self.assertIn("New profile", html)
        self.assertIn("/api/media-profiles/link-live", js)
        self.assertIn("openRecordingModal", js)
        self.assertIn("submitCreateRecordingProfile", js)
        self.assertNotIn("Auto-Record", html)
        self.assertNotIn("Auto-Record", js)
        self.assertNotIn("toggleAutoRecord", js)

    def test_recordings_page_redirects_to_media(self):
        main = (ROOT / "app" / "main.py").read_text()

        self.assertIn('@app.get("/recordings")', main)
        self.assertIn('RedirectResponse(url="/media", status_code=307)', main)
        self.assertNotIn('return FileResponse(str(STATIC_DIR / "recordings.html"))', main)

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
