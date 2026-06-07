import json
import unittest

from app.services.chaturbate_api import ChaturbateAPI, _FakeResponse


class _FakeAuth:
    def get_user_agent(self):
        return "P-StreamRec-Test"

    def get_cookies(self):
        return {}


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
        self.requests.append({"method": method, "url": url, "headers": headers or {}})
        return self.response

    async def _scrape_live_models(self, page, limit, gender, search):
        self.scraped = True
        result = dict(self.scrape_response)
        result["page"] = page
        result["limit"] = limit
        return result


class ChaturbateRoomlistTests(unittest.IsolatedAsyncioTestCase):
    async def test_roomlist_uses_ajax_json_headers(self):
        api = _RoomlistAPI(_json_response({"rooms": [{"username": "alice"}], "total_count": 1}))

        await api.get_live_models(page=1, limit=24)

        headers = api.requests[0]["headers"]
        self.assertEqual("application/json", headers["Accept"])
        self.assertEqual("XMLHttpRequest", headers["X-Requested-With"])
        self.assertEqual("https://chaturbate.com/", headers["Referer"])

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


if __name__ == "__main__":
    unittest.main()
