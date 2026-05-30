import os
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import main as app_main
from app.core.database import Database


class MediaLibraryApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_db = app_main.db
        self.original_output_dir = app_main.OUTPUT_DIR

        self.tmpdir = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.tmpdir.name)
        app_main.OUTPUT_DIR = self.output_dir
        app_main.db = Database(self.output_dir / "streamrec.db")
        await app_main.db.initialize()

        self.records_dir = self.output_dir / "records" / "model"
        self.records_dir.mkdir(parents=True)
        self.video = self.records_dir / "clip.mp4"
        self.video.write_bytes(b"0123456789")
        self.photo = self.records_dir / "photo.jpg"
        self.photo.write_bytes(b"\xff\xd8\xff\xe0photo")
        self.empty_dir = self.output_dir / "records" / "empty_model"
        self.empty_dir.mkdir(parents=True)
        old = time.time() - 120
        os.utime(self.video, (old, old))
        os.utime(self.photo, (old + 10, old + 10))

        await app_main.db.add_or_update_recording(
            username="model",
            filename="clip.mp4",
            file_path=str(self.video),
            file_size=self.video.stat().st_size,
            recording_id="rec_clip",
            duration_seconds=12,
            is_converted=True,
            media_kind="recording",
            created_at=int(old),
        )

        self.client = TestClient(app_main.app)

    async def asyncTearDown(self):
        app_main.db = self.original_db
        app_main.OUTPUT_DIR = self.original_output_dir
        self.tmpdir.cleanup()

    async def test_lists_videos_and_photos_from_records_folder(self):
        response = self.client.get("/api/media-library")
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertEqual(data["stats"]["total"], 2)
        self.assertEqual(data["stats"]["videos"], 1)
        self.assertEqual(data["stats"]["images"], 1)
        self.assertEqual(data["profiles"][0]["username"], "model")
        self.assertEqual(data["profiles"][0]["latestTitle"], "photo")
        self.assertEqual(data["profiles"][0]["thumbnail"], "/streams/library/model/photo.jpg")
        profiles = {profile["username"]: profile for profile in data["profiles"]}
        self.assertIn("empty_model", profiles)
        self.assertEqual(profiles["empty_model"]["total"], 0)
        self.assertTrue(profiles["empty_model"]["folderExists"])

        items = {item["filename"]: item for item in data["items"]}
        self.assertEqual(items["clip.mp4"]["type"], "video")
        self.assertTrue(items["clip.mp4"]["isRecording"])
        self.assertEqual(items["clip.mp4"]["duration"], 12)
        self.assertEqual(items["photo.jpg"]["type"], "image")
        self.assertEqual(items["photo.jpg"]["thumbnail"], items["photo.jpg"]["url"])

    async def test_filters_media_library(self):
        response = self.client.get("/api/media-library?kind=image&search=photo")
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["filename"], "photo.jpg")

    async def test_streams_media_files_securely(self):
        video = self.client.get(
            "/streams/library/model/clip.mp4",
            headers={"Range": "bytes=0-3"},
        )
        self.assertEqual(video.status_code, 206)
        self.assertEqual(video.content, b"0123")

        photo = self.client.get("/streams/library/model/photo.jpg")
        self.assertEqual(photo.status_code, 200)
        self.assertEqual(photo.content, b"\xff\xd8\xff\xe0photo")
        self.assertTrue(photo.headers["content-type"].startswith("image/jpeg"))

        traversal = self.client.get("/streams/library/model/%2E%2E/secret.jpg")
        self.assertIn(traversal.status_code, {400, 404})

    async def test_deletes_photo_and_indexed_video(self):
        photo = self.client.delete("/api/media-library/model/photo.jpg")
        self.assertEqual(photo.status_code, 200)
        self.assertFalse(self.photo.exists())

        photo_listing = self.client.get("/api/media-library?kind=image")
        self.assertEqual(photo_listing.status_code, 200)
        self.assertEqual(photo_listing.json()["total"], 0)

        video = self.client.delete("/api/media-library/model/clip.mp4")
        self.assertEqual(video.status_code, 200)
        self.assertFalse(self.video.exists())
        self.assertEqual(await app_main.db.get_recordings("model"), [])

        missing = self.client.delete("/api/media-library/model/clip.mp4")
        self.assertEqual(missing.status_code, 404)

    async def test_updates_profile_metadata_and_stream_settings(self):
        response = self.client.put(
            "/api/media-profiles/empty_model",
            json={
                "displayName": "Empty Model",
                "firstName": "Empty",
                "lastName": "Model",
                "age": 25,
                "country": "Canada",
                "socialUrls": ["https://social.example/empty"],
                "streamUrls": ["https://stream.example/empty"],
                "recordQuality": "720p",
                "retentionDays": 14,
                "autoRecord": True,
                "sourceType": "chaturbate",
            },
        )
        self.assertEqual(response.status_code, 200)

        profile = self.client.get("/api/media-profiles/empty_model")
        self.assertEqual(profile.status_code, 200)
        data = profile.json()
        self.assertEqual(data["displayName"], "Empty Model")
        self.assertEqual(data["firstName"], "Empty")
        self.assertEqual(data["age"], 25)
        self.assertEqual(data["country"], "Canada")
        self.assertEqual(data["socialUrls"], ["https://social.example/empty"])
        self.assertEqual(data["streamUrls"], ["https://stream.example/empty"])
        self.assertEqual(data["recordQuality"], "720p")
        self.assertEqual(data["retentionDays"], 14)
        self.assertTrue(data["autoRecord"])

        listing = self.client.get("/api/media-library")
        profiles = {item["username"]: item for item in listing.json()["profiles"]}
        self.assertEqual(profiles["empty_model"]["displayName"], "Empty Model")
        self.assertEqual(profiles["empty_model"]["recordQuality"], "720p")

    async def test_deletes_profile_folder_metadata_and_recordings(self):
        response = self.client.put(
            "/api/media-profiles/empty_model",
            json={
                "displayName": "To Delete",
                "recordQuality": "best",
                "retentionDays": 30,
                "autoRecord": False,
                "sourceType": "chaturbate",
            },
        )
        self.assertEqual(response.status_code, 200)

        delete = self.client.delete("/api/media-profiles/empty_model")
        self.assertEqual(delete.status_code, 200)
        self.assertFalse(self.empty_dir.exists())
        self.assertIsNone(await app_main.db.get_media_profile("empty_model"))
        self.assertIsNone(await app_main.db.get_model("empty_model"))

        missing = self.client.get("/api/media-profiles/empty_model")
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()
