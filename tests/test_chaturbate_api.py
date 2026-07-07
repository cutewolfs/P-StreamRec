import json
import unittest

from app.services.chaturbate_api import ChaturbateAPI, _FakeResponse
from app.services.chaturbate_auth import ChaturbateAuthService


class _FakeAuth:
    def get_user_agent(self):
        return "P-StreamRec-Test"

    def get_cookies(self):
        return {}


class _CookieAuth(_FakeAuth):
    def get_cookies(self):
        return {"sessionid": "abc", "csrftoken": "csrf"}


class _MutableCookieAuth(_FakeAuth):
    def __init__(self):
        self._cookies = {"sessionid": "real-session", "csrftoken": "real-csrf"}
        self._user_agent = "P-StreamRec-Test"

    def get_cookies(self):
        return dict(self._cookies)

    def get_user_agent(self):
        return self._user_agent


class _FakeFlareSolverr:
    def __init__(self):
        self.urls = []

    async def solve_challenge(self, url):
        self.urls.append(url)
        return {
            "cookies": {"cf_clearance": "solved"},
            "user_agent": "Solved-UA",
        }


def _json_response(payload):
    return _FakeResponse(
        200,
        json.dumps(payload).encode("utf-8"),
        {},
        "application/json",
    )


class _RoomlistAPI(ChaturbateAPI):
    def __init__(self, response, scrape_response=None):
        super().__init__(_FakeAuth())
        self.response = response
        self.scrape_response = scrape_response or {
            "models": [],
            "total": 0,
            "page": 1,
            "limit": 24,
            "total_pages": 1,
        }
        self.requests = []
        self.scraped = False

    async def _rate_limit(self):
        return None

    async def _request(self, method, url, headers=None, **kwargs):
        self.requests.append({"method": method, "url": url, "headers": headers or {}, "kwargs": kwargs})
        return self.response

    async def _scrape_live_models(self, page, limit, gender, search):
        self.scraped = True
        result = dict(self.scrape_response)
        result["page"] = page
        result["limit"] = limit
        return result


class _FollowedAPI(ChaturbateAPI):
    def __init__(self, responses, flaresolverr=None):
        super().__init__(_CookieAuth(), flaresolverr=flaresolverr)
        self.responses = list(responses)
        self.requests = []

    async def _rate_limit(self):
        return None

    async def _request(self, method, url, headers=None, **kwargs):
        self.requests.append({"method": method, "url": url, "headers": headers or {}, "kwargs": kwargs})
        return self.responses.pop(0)


class ChaturbateRoomlistTests(unittest.IsolatedAsyncioTestCase):
    async def test_flaresolverr_cookie_merge_preserves_authenticated_session(self):
        auth = _MutableCookieAuth()
        api = ChaturbateAPI(auth)
        headers = {"User-Agent": auth.get_user_agent()}

        applied = api._apply_flaresolverr_solution(headers, {
            "cookies": {
                "sessionid": "anonymous-session",
                "csrftoken": "anonymous-csrf",
                "cf_clearance": "solved",
                "__cf_bm": "bot-token",
            },
            "user_agent": "Solved-UA",
        })

        self.assertTrue(applied)
        self.assertEqual("real-session", auth._cookies["sessionid"])
        self.assertEqual("real-csrf", auth._cookies["csrftoken"])
        self.assertEqual("solved", auth._cookies["cf_clearance"])
        self.assertEqual("bot-token", auth._cookies["__cf_bm"])
        self.assertEqual("Solved-UA", headers["User-Agent"])
        self.assertIn("sessionid=real-session", headers["Cookie"])
        self.assertNotIn("anonymous-session", headers["Cookie"])

    async def test_auth_flaresolverr_cookie_merge_preserves_authenticated_session(self):
        auth = ChaturbateAuthService(db=object())
        cookies = {"sessionid": "real-session", "csrftoken": "real-csrf"}
        headers = {}

        applied = auth._apply_flaresolverr_solution(headers, cookies, {
            "cookies": {
                "sessionid": "anonymous-session",
                "csrftoken": "anonymous-csrf",
                "cf_clearance": "solved",
            },
            "user_agent": "Solved-UA",
        })

        self.assertTrue(applied)
        self.assertEqual("real-session", cookies["sessionid"])
        self.assertEqual("real-csrf", cookies["csrftoken"])
        self.assertEqual("solved", cookies["cf_clearance"])
        self.assertEqual("Solved-UA", headers["User-Agent"])
        self.assertIn("sessionid=real-session", headers["Cookie"])
        self.assertNotIn("anonymous-session", headers["Cookie"])

    async def test_roomlist_uses_ajax_json_headers(self):
        api = _RoomlistAPI(_json_response({"rooms": [{"username": "alice"}], "total_count": 1}))

        await api.get_live_models(page=1, limit=24)

        headers = api.requests[0]["headers"]
        self.assertEqual("application/json", headers["Accept"])
        self.assertEqual("XMLHttpRequest", headers["X-Requested-With"])
        self.assertEqual("https://chaturbate.com/", headers["Referer"])
        self.assertFalse(api.requests[0]["kwargs"].get("allow_redirects", True))

    async def test_roomlist_parses_current_chaturbate_fields(self):
        api = _RoomlistAPI(_json_response({
            "rooms": [
                {
                    "username": "alice",
                    "display_age": "19",
                    "gender": "f",
                    "current_show": "public",
                    "num_users": "42",
                    "room_subject": "Goal text",
                    "tags": ["French", "Cosplay"],
                    "img": "//thumb.live.mmcdn.com/riw/alice.jpg",
                }
            ],
            "total_count": "25",
        }))

        result = await api.get_live_models(page=2, limit=10)

        self.assertFalse(api.scraped)
        self.assertEqual(25, result["total"])
        self.assertEqual(3, result["total_pages"])
        self.assertEqual(1, len(result["models"]))
        model = result["models"][0]
        self.assertEqual("alice", model["username"])
        self.assertEqual("alice", model["display_name"])
        self.assertEqual(19, model["age"])
        self.assertEqual(42, model["viewers"])
        self.assertEqual("Goal text", model["subject"])
        self.assertEqual("https://thumb.live.mmcdn.com/riw/alice.jpg", model["thumbnail"])
        self.assertEqual(["French", "Cosplay"], model["tags"])
        self.assertEqual("public", model["room_status"])

    async def test_non_json_roomlist_response_falls_back(self):
        fallback = {
            "models": [{"username": "fallback"}],
            "total": 1,
            "page": 1,
            "limit": 24,
            "total_pages": 1,
        }
        api = _RoomlistAPI(
            _FakeResponse(200, b"<html>challenge</html>", {}, "text/html"),
            scrape_response=fallback,
        )

        result = await api.get_live_models(page=1, limit=12)

        self.assertTrue(api.scraped)
        self.assertEqual([{"username": "fallback"}], result["models"])
        self.assertEqual(12, result["limit"])

    async def test_malformed_room_items_are_skipped(self):
        api = _RoomlistAPI(_json_response({
            "rooms": [
                "not-a-room",
                {"display_name": "missing username"},
                {"username": "valid"},
            ],
            "total_count": 3,
        }))

        result = await api.get_live_models(page=1, limit=10)

        self.assertEqual(["valid"], [item["username"] for item in result["models"]])

    async def test_followed_models_parse_followed_cams_html(self):
        html = """
        <html><body>
          <li class="room_list_room" data-room="_alice_">
            <a href="/_alice_/"><img data-src="//thumb.example.test/alice.jpg" alt="Alice"></a>
            <span class="cams">1,234</span>
          </li>
        </body></html>
        """
        api = _FollowedAPI([_FakeResponse(200, html.encode("utf-8"), {}, "text/html")])

        result = await api.get_followed_models()

        self.assertTrue(result.trusted)
        self.assertEqual("_alice_", result[0]["username"])
        self.assertTrue(result[0]["is_online"])
        self.assertEqual(1234, result[0]["viewers"])
        self.assertEqual("https://thumb.example.test/alice.jpg", result[0]["thumbnail_url"])
        self.assertFalse(api.requests[0]["kwargs"].get("allow_redirects", True))

    async def test_followed_models_parse_embedded_json_payload(self):
        html = """
        <html><body>
          <script type="application/json">
            {"props":{"followed":[
              {"username":"_alice_","display_name":"Alice","is_online":true,
               "viewers":"123","thumbnail_url":"//thumb.example.test/alice.jpg",
               "room_status":"public","tags":["French"]}
            ]}}
          </script>
        </body></html>
        """
        api = _FollowedAPI([_FakeResponse(200, html.encode("utf-8"), {}, "text/html")])

        result = await api.get_followed_models()

        self.assertTrue(result.trusted)
        self.assertEqual("_alice_", result[0]["username"])
        self.assertEqual("Alice", result[0]["display_name"])
        self.assertEqual(123, result[0]["viewers"])
        self.assertEqual("https://thumb.example.test/alice.jpg", result[0]["thumbnail_url"])
        self.assertEqual(["French"], result[0]["tags"])

    async def test_followed_models_use_roomlist_api_for_react_shell(self):
        html = """
        <html><body>
          <a href="/followed-cams/">Followed Cams</a>
          <div id="roomlist_root" data-testid="room-list">
            <li class="roomCard placeholder camBgColor"></li>
          </div>
        </body></html>
        """
        api = _FollowedAPI([
            _FakeResponse(200, html.encode("utf-8"), {}, "text/html"),
            _json_response({
                "rooms": [{
                    "username": "alice",
                    "display_name": "Alice",
                    "current_show": "public",
                    "num_users": "42",
                    "img": "//thumb.example.test/alice.jpg",
                    "tags": ["French"],
                }],
                "total_count": 1,
            }),
        ])

        result = await api.get_followed_models()

        self.assertTrue(result.trusted)
        self.assertEqual(["alice"], [item["username"] for item in result])
        self.assertEqual("https://thumb.example.test/alice.jpg", result[0]["thumbnail_url"])
        self.assertIn("follow=true", api.requests[1]["url"])
        self.assertEqual("application/json", api.requests[1]["headers"]["Accept"])
        self.assertEqual("XMLHttpRequest", api.requests[1]["headers"]["X-Requested-With"])

    async def test_followed_models_reject_roomlist_api_above_safety_limit(self):
        import app.services.chaturbate_api as cb_api

        html = """
        <html><body>
          <a href="/followed-cams/">Followed Cams</a>
          <div id="roomlist_root" data-testid="room-list"></div>
        </body></html>
        """
        original = cb_api.PSTREAMREC_MAX_FOLLOW_SYNC_ITEMS
        cb_api.PSTREAMREC_MAX_FOLLOW_SYNC_ITEMS = 2
        try:
            api = _FollowedAPI([
                _FakeResponse(200, html.encode("utf-8"), {}, "text/html"),
                _json_response({
                    "rooms": [{"username": "alice"}, {"username": "bella"}],
                    "total_count": 36000,
                }),
            ])
            result = await api.get_followed_models()
        finally:
            cb_api.PSTREAMREC_MAX_FOLLOW_SYNC_ITEMS = original

        self.assertFalse(result.trusted)
        self.assertEqual([], list(result))
        self.assertIn("safety limit", result.skipped_reason)

    async def test_followed_models_reject_full_roomlist_page_without_total(self):
        html = """
        <html><body>
          <a href="/followed-cams/">Followed Cams</a>
          <div id="roomlist_root" data-testid="room-list"></div>
        </body></html>
        """
        api = _FollowedAPI([
            _FakeResponse(200, html.encode("utf-8"), {}, "text/html"),
            _json_response({
                "rooms": [{"username": f"model_{i}"} for i in range(90)],
            }),
        ])

        result = await api.get_followed_models()

        self.assertFalse(result.trusted)
        self.assertEqual([], list(result))
        self.assertIn("total", result.skipped_reason)

    async def test_followed_models_skip_untrusted_login_redirect(self):
        api = _FollowedAPI([_FakeResponse(302, b"", {"Location": "/auth/login/"}, "text/html")])

        result = await api.get_followed_models()

        self.assertFalse(result.trusted)
        self.assertEqual([], list(result))
        self.assertIn("redirected", result.skipped_reason)

    async def test_followed_models_retry_login_redirect_with_flaresolverr(self):
        html = """
        <html><body>
          <li class="room_list_room" data-room="alice">
            <img src="//thumb.example.test/alice.jpg">
            <span class="cams">42</span>
          </li>
        </body></html>
        """
        flaresolverr = _FakeFlareSolverr()
        api = _FollowedAPI([
            _FakeResponse(302, b"", {"Location": "/auth/login/?next=/followed-cams/"}, "text/html"),
            _FakeResponse(200, html.encode("utf-8"), {}, "text/html"),
        ], flaresolverr=flaresolverr)

        result = await api.get_followed_models()

        self.assertTrue(result.trusted)
        self.assertEqual(["alice"], [item["username"] for item in result])
        self.assertEqual(["https://chaturbate.com/followed-cams/"], flaresolverr.urls)
        self.assertEqual(2, len(api.requests))
        retry_headers = api.requests[1]["headers"]
        self.assertEqual("Solved-UA", retry_headers["User-Agent"])
        self.assertIn("cf_clearance=solved", retry_headers["Cookie"])

    async def test_followed_models_skip_when_safety_limit_is_exceeded(self):
        import app.services.chaturbate_api as cb_api

        html = """
        <li class="room_list_room" data-room="alice"><img src="//thumb/a.jpg"></li>
        <li class="room_list_room" data-room="bella"><img src="//thumb/b.jpg"></li>
        """
        original = cb_api.PSTREAMREC_MAX_FOLLOW_SYNC_ITEMS
        cb_api.PSTREAMREC_MAX_FOLLOW_SYNC_ITEMS = 1
        try:
            api = _FollowedAPI([_FakeResponse(200, html.encode("utf-8"), {}, "text/html")])
            result = await api.get_followed_models()
        finally:
            cb_api.PSTREAMREC_MAX_FOLLOW_SYNC_ITEMS = original

        self.assertFalse(result.trusted)
        self.assertIn("safety limit", result.skipped_reason)


if __name__ == "__main__":
    unittest.main()
