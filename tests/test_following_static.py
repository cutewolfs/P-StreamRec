import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FollowingStaticTests(unittest.TestCase):
    def test_following_page_uses_single_manageable_list(self):
        js = (ROOT / "static" / "following.js").read_text()
        css = (ROOT / "static" / "styles.css").read_text()

        self.assertIn("renderGlobalFollowingSection(models, filterFollowingModels(models))", js)
        self.assertIn("function renderFollowingManagementToolbar(models, filteredModels)", js)
        self.assertIn("function renderFollowingList(models)", js)
        self.assertIn("function renderFollowingRow(model)", js)
        self.assertIn("All providers", js)
        self.assertIn("following-list-row", js)
        self.assertIn("unfollowFollowingModel", js)
        self.assertIn("updateFollowingFilter", js)
        self.assertIn("\\'search\\'", js)
        self.assertIn("\\'source\\'", js)
        self.assertIn("\\'status\\'", js)
        self.assertIn(".following-management-toolbar", css)
        self.assertIn(".following-list-row", css)
        self.assertNotIn("providersForFollowing(models).map", js)
        self.assertRegex(js, r"return viewersB - viewersA")

    def test_following_meta_shows_local_provider_online_counts(self):
        js = (ROOT / "static" / "following.js").read_text()
        css = (ROOT / "static" / "styles.css").read_text()

        self.assertIn("function renderConnectedProviderMeta(models)", js)
        self.assertIn("connectedFollowingProviders(models).map", js)
        self.assertIn("isPubliclyOnline(model)", js)
        self.assertIn("online + '/' + providerModels.length", js)
        self.assertIn("following-provider-counter", js)
        self.assertIn(".following-provider-counter", css)
        self.assertNotIn("sorted by viewers", js)

    def test_following_header_has_no_provider_sync_buttons(self):
        html = (ROOT / "static" / "following.html").read_text()
        js = (ROOT / "static" / "following.js").read_text()
        css = (ROOT / "static" / "styles.css").read_text()

        for removed_text in (
            "lastSynced",
            "Not synced yet",
            "syncBtn",
            "syncIcon",
            "Sync Now",
            "Provider sync",
            "data-sync-provider",
            "renderFollowingSyncControls",
            "following-sync-controls",
        ):
            self.assertNotIn(removed_text, html)
            self.assertNotIn(removed_text, js)
        self.assertNotIn("following_last_synced", js)
        self.assertNotIn("updateLastSynced", js)
        self.assertIn("can_sync_following", js)
        self.assertIn("function syncCapableFollowingProviders()", js)
        self.assertIn("function syncSingleProvider(sourceType, button, silent)", js)
        self.assertNotIn(".following-sync-controls", css)
        self.assertNotIn(".last-synced", css)

    def test_following_page_has_no_redundant_header_block(self):
        html = (ROOT / "static" / "following.html").read_text()
        js = (ROOT / "static" / "following.js").read_text()

        self.assertNotIn('id="syncControls"', html)
        self.assertNotIn('id="loginBanner"', html)
        self.assertNotIn("<h1", html)
        self.assertNotIn("renderFollowingSyncControls", js)
        self.assertIn("No follows saved yet.", html)

    def test_following_sync_surfaces_skipped_reason_as_error(self):
        js = (ROOT / "static" / "following.js").read_text()

        self.assertIn("data.trusted === false", js)
        self.assertIn("data.skippedReason || data.message || 'Following sync skipped'", js)
        self.assertIn("showNotification(data.skippedReason || data.message || 'Following sync skipped', 'error')", js)


if __name__ == "__main__":
    unittest.main()
