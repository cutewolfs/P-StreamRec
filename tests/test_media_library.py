import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import main as app_main
from app.core.database import Database


def mp4_box(box_type: bytes, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + box_type + payload


class MediaLibraryApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_db = app_main.db
        self.original_output_dir = app_main.OUTPUT_DIR
        self.original_profile_images_dir = app_main.PROFILE_IMAGES_DIR
        self.original_range_chunk_size = app_main.RECORDING_RANGE_CHUNK_SIZE

        self.tmpdir = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.tmpdir.name)
        app_main.OUTPUT_DIR = self.output_dir
        app_main.PROFILE_IMAGES_DIR = self.output_dir / "profile-images"
        app_main.db = Database(self.output_dir / "streamrec.db")
        await app_main.db.initialize()

        self.records_dir = self.output_dir / "records" / "model"
        self.records_dir.mkdir(parents=True)
        self.video = self.records_dir / "clip.mp4"
        self.video.write_bytes(b"0123456789")
        self.video_thumb = self.output_dir / "thumbnails" / "model" / "clip.jpg"
        self.video_thumb.parent.mkdir(parents=True, exist_ok=True)
        self.video_thumb.write_bytes(b"thumb")
        self.photo = self.records_dir / "photo.jpg"
        self.photo.write_bytes(b"\xff\xd8\xff\xe0photo")
        self.ts_file = self.records_dir / "raw.ts"
        self.ts_file.write_bytes(b"ts should stay out of media")
        self.empty_dir = self.output_dir / "records" / "empty_model"
        self.empty_dir.mkdir(parents=True)
        old = time.time() - 120
        os.utime(self.video, (old, old))
        os.utime(self.photo, (old + 10, old + 10))
        os.utime(self.ts_file, (old + 20, old + 20))

        await app_main.db.add_or_update_recording(
            username="model",
            filename="clip.mp4",
            file_path=str(self.video),
            file_size=self.video.stat().st_size,
            recording_id="rec_clip",
            duration_seconds=12,
            thumbnail_path=str(self.video_thumb),
            is_converted=True,
            media_kind="recording",
            created_at=int(old),
        )

        self.client = TestClient(app_main.app)

    async def asyncTearDown(self):
        app_main.db = self.original_db
        app_main.OUTPUT_DIR = self.original_output_dir
        app_main.PROFILE_IMAGES_DIR = self.original_profile_images_dir
        app_main.RECORDING_RANGE_CHUNK_SIZE = self.original_range_chunk_size
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
        self.assertEqual(data["profiles"][0]["profileImageUrl"], "")
        self.assertNotIn("thumbnail", data["profiles"][0])
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
        self.assertNotIn("raw.ts", items)

    async def test_indexes_manual_video_with_duration_and_thumbnail(self):
        manual = self.records_dir / "manual_import.mp4"
        manual.write_bytes(b"manual video")
        old = time.time() - 120
        os.utime(manual, (old, old))
        thumb = self.output_dir / "thumbnails" / "model" / "manual_import.jpg"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"thumb")

        with (
            patch.object(app_main, "get_video_duration", new=AsyncMock(return_value=61)),
            patch.object(app_main, "get_media_created_at", new=AsyncMock(return_value=1704164645)),
            patch.object(app_main, "generate_import_thumbnail", new=AsyncMock(return_value=str(thumb))),
        ):
            response = self.client.get("/api/media-library?kind=video&search=manual_import")

        self.assertEqual(response.status_code, 200)
        item = response.json()["items"][0]
        self.assertEqual(item["filename"], "manual_import.mp4")
        self.assertTrue(item["isImported"])
        self.assertEqual(item["duration"], 61)
        self.assertEqual(item["durationStr"], "1m01s")
        self.assertEqual(item["thumbnail"], "/api/recording-thumbnail/model/manual_import.jpg")
        self.assertEqual(item["createdAt"], 1704164645)

        recs = await app_main.db.get_recordings("model")
        indexed = next(rec for rec in recs if rec["filename"] == "manual_import.mp4")
        self.assertEqual(indexed["media_kind"], "import")
        self.assertEqual(indexed["duration_seconds"], 61)
        self.assertEqual(indexed["thumbnail_path"], str(thumb))
        self.assertEqual(indexed["created_at"], 1704164645)

    async def test_lazy_media_library_listing_does_not_probe_manual_video(self):
        manual = self.empty_dir / "lazy_manual.mp4"
        manual.write_bytes(b"manual video")
        old = time.time() - 120
        os.utime(manual, (old, old))

        with (
            patch.object(app_main, "get_video_duration", new=AsyncMock(return_value=61)) as duration_mock,
            patch.object(app_main, "get_media_created_at", new=AsyncMock(return_value=1704164645)) as created_mock,
            patch.object(app_main, "generate_import_thumbnail", new=AsyncMock(return_value="thumb")) as thumb_mock,
            patch.object(
                app_main,
                "create_playable_mp4_copy",
                new=AsyncMock(return_value=(True, manual, None)),
            ) as convert_mock,
        ):
            response = self.client.get("/api/media-library?metadata=lazy&kind=video&search=lazy_manual")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total"], 1)
        item = data["items"][0]
        self.assertEqual(item["filename"], "lazy_manual.mp4")
        self.assertEqual(item["duration"], 0)
        self.assertTrue(item["recordingId"])
        self.assertTrue(item["url"].startswith("/streams/library/empty_model/"))
        self.assertFalse(item["isImported"])
        self.assertFalse(item["isRecording"])
        duration_mock.assert_not_awaited()
        created_mock.assert_not_awaited()
        thumb_mock.assert_not_awaited()
        convert_mock.assert_not_awaited()
        self.assertEqual([], await app_main.db.get_recordings("empty_model"))

    async def test_filters_media_library(self):
        response = self.client.get("/api/media-library?kind=image&search=photo")
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["filename"], "photo.jpg")

    async def test_marks_media_library_video_as_watched(self):
        unwatched_before = self.client.get("/api/media-library?kind=video&watched=unwatched")
        self.assertEqual(unwatched_before.status_code, 200)
        self.assertEqual(unwatched_before.json()["total"], 1)

        position = self.client.post(
            "/api/playback-position/rec_clip",
            json={"username": "model", "position": 11, "duration": 12},
        )
        self.assertEqual(position.status_code, 200)
        self.assertTrue(position.json()["isWatched"])

        response = self.client.get("/api/media-library?kind=video")
        self.assertEqual(response.status_code, 200)
        item = response.json()["items"][0]

        self.assertEqual(item["recordingId"], "rec_clip")
        self.assertEqual(item["playbackProgress"], 92)
        self.assertTrue(item["isWatched"])
        self.assertIsNotNone(item["watchedAt"])

        unwatched_after = self.client.get("/api/media-library?kind=video&watched=unwatched")
        self.assertEqual(unwatched_after.status_code, 200)
        self.assertEqual(unwatched_after.json()["total"], 0)

        watched_only = self.client.get("/api/media-library?kind=video&watched=watched")
        self.assertEqual(watched_only.status_code, 200)
        self.assertEqual(watched_only.json()["total"], 1)

        replay = self.client.post(
            "/api/playback-position/rec_clip",
            json={"username": "model", "position": 1, "duration": 12},
        )
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["isWatched"])

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

        ts_media = self.client.get("/streams/library/model/raw.ts")
        self.assertEqual(ts_media.status_code, 400)

        delete_ts = self.client.delete("/api/media-library/model/raw.ts")
        self.assertEqual(delete_ts.status_code, 400)
        self.assertTrue(self.ts_file.exists())

    async def test_initial_open_range_includes_large_mp4_metadata(self):
        app_main.RECORDING_RANGE_CHUNK_SIZE = 64
        ftyp = (32).to_bytes(4, "big") + b"ftyp" + b"isom" + (b"\0" * 20)
        moov = (96).to_bytes(4, "big") + b"moov" + (b"\0" * 88)
        mdat = (72).to_bytes(4, "big") + b"mdat" + (b"1" * 64)
        large_metadata = self.records_dir / "large_metadata.mp4"
        large_metadata.write_bytes(ftyp + moov + mdat)

        response = self.client.head(
            "/streams/library/model/large_metadata.mp4",
            headers={"Range": "bytes=0-"},
        )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 0-127/200")
        self.assertEqual(response.headers["content-length"], "128")

    async def test_web_upload_endpoint_removed_and_direct_image_files_are_listed(self):
        upload = self.client.post(
            "/api/media-profiles/empty_model/uploads",
            content=b"\xff\xd8\xff\xe0portrait",
        )
        self.assertIn(upload.status_code, {404, 405})

        portrait = self.empty_dir / "portrait.jpg"
        portrait.write_bytes(b"\xff\xd8\xff\xe0portrait")
        raw_ts = self.empty_dir / "raw.ts"
        raw_ts.write_bytes(b"transport stream")

        listing = self.client.get("/api/media-library?username=empty_model&kind=image")
        self.assertEqual(listing.status_code, 200)
        items = {item["filename"]: item for item in listing.json()["items"]}
        self.assertEqual("image", items["portrait.jpg"]["type"])
        self.assertNotIn("raw.ts", items)

    async def test_direct_mp4_file_indexes_import_record(self):
        media_file = self.empty_dir / "uploaded.mp4"
        media_file.write_bytes(b"video bytes")
        old = time.time() - 120
        os.utime(media_file, (old, old))
        thumb = self.output_dir / "thumbnails" / "empty_model" / "upload_thumb.jpg"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"thumb")

        with (
            patch.object(app_main, "get_video_duration", new=AsyncMock(return_value=33)),
            patch.object(app_main, "get_media_created_at", new=AsyncMock(return_value=1704164645)),
            patch.object(app_main, "generate_import_thumbnail", new=AsyncMock(return_value=str(thumb))),
        ):
            listing = self.client.get("/api/media-library?username=empty_model&kind=video")

        self.assertEqual(listing.status_code, 200)
        item = listing.json()["items"][0]
        self.assertEqual("uploaded.mp4", item["filename"])
        self.assertEqual("video", item["type"])
        self.assertTrue(item["url"].startswith("/streams/media/"))

        recs = await app_main.db.get_recordings("empty_model")
        self.assertEqual(1, len(recs))
        self.assertEqual("import", recs[0]["media_kind"])
        self.assertEqual("uploaded.mp4", recs[0]["filename"])
        self.assertEqual(33, recs[0]["duration_seconds"])

        self.assertEqual(recs[0]["recording_id"], item["recordingId"])
        self.assertTrue(item["browserPlayable"])

    async def test_non_faststart_mp4_file_creates_playable_copy(self):
        media_file = self.empty_dir / "slow_start.mp4"
        media_file.write_bytes(
            mp4_box(b"ftyp", b"isom0000")
            + mp4_box(b"mdat", b"1" * 16)
            + mp4_box(b"moov", b"0" * 16)
        )
        old = time.time() - 120
        os.utime(media_file, (old, old))
        converted = self.output_dir / "media_imports" / "empty_model" / "converted.mp4"
        converted.parent.mkdir(parents=True, exist_ok=True)
        converted.write_bytes(b"mp4 copy")
        thumb = self.output_dir / "thumbnails" / "empty_model" / "upload_thumb.jpg"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"thumb")

        with (
            patch.object(app_main, "get_video_duration", new=AsyncMock(return_value=44)),
            patch.object(app_main, "get_media_created_at", new=AsyncMock(return_value=1704164645)),
            patch.object(app_main, "generate_import_thumbnail", new=AsyncMock(return_value=str(thumb))),
            patch.object(
                app_main,
                "create_playable_mp4_copy",
                new=AsyncMock(return_value=(True, converted, None)),
            ) as convert_mock,
        ):
            listing = self.client.get("/api/media-library?username=empty_model&kind=video")

        convert_mock.assert_awaited_once()
        rec = (await app_main.db.get_recordings("empty_model"))[0]
        self.assertEqual(str(converted), rec["playable_path"])
        self.assertEqual(str(converted), rec["mp4_path"])

        item = listing.json()["items"][0]
        self.assertTrue(item["url"].startswith("/streams/media/"))
        self.assertTrue(item["browserPlayable"])

    async def test_direct_mkv_file_creates_playable_mp4_copy(self):
        media_file = self.empty_dir / "bonus.mkv"
        media_file.write_bytes(b"mkv bytes")
        old = time.time() - 120
        os.utime(media_file, (old, old))
        converted = self.output_dir / "media_imports" / "empty_model" / "converted.mp4"
        converted.parent.mkdir(parents=True, exist_ok=True)
        converted.write_bytes(b"mp4 copy")
        thumb = self.output_dir / "thumbnails" / "empty_model" / "upload_thumb.jpg"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"thumb")

        with (
            patch.object(app_main, "get_video_duration", new=AsyncMock(return_value=44)),
            patch.object(app_main, "get_media_created_at", new=AsyncMock(return_value=1704164645)),
            patch.object(app_main, "generate_import_thumbnail", new=AsyncMock(return_value=str(thumb))),
            patch.object(
                app_main,
                "create_playable_mp4_copy",
                new=AsyncMock(return_value=(True, converted, None)),
            ),
        ):
            listing = self.client.get("/api/media-library?username=empty_model&kind=video")

        rec = (await app_main.db.get_recordings("empty_model"))[0]
        self.assertTrue(rec["file_path"].endswith("/records/empty_model/bonus.mkv"))
        self.assertEqual(str(converted), rec["playable_path"])
        self.assertEqual(str(converted), rec["mp4_path"])

        item = listing.json()["items"][0]
        self.assertTrue(item["url"].startswith("/streams/media/"))
        self.assertTrue(item["browserPlayable"])

    async def test_recordings_api_streams_nested_record_path_by_id(self):
        nested_dir = self.output_dir / "records" / "model" / "videos" / "record"
        nested_dir.mkdir(parents=True)
        nested = nested_dir / "nested.ts"
        nested.write_bytes(b"nested-recording")
        await app_main.db.add_or_update_recording(
            username="model",
            filename="nested.ts",
            file_path=str(nested),
            file_size=nested.stat().st_size,
            recording_id="rec_nested",
            duration_seconds=12,
            is_converted=False,
            media_kind="recording",
            created_at=int(time.time()) - 120,
        )

        listing = self.client.get("/api/recordings/model?show_ts=true")
        self.assertEqual(listing.status_code, 200)
        items = {item["recordingId"]: item for item in listing.json()["recordings"]}
        self.assertEqual("/streams/recordings/rec_nested", items["rec_nested"]["url"])

        stream = self.client.get(
            "/streams/recordings/rec_nested",
            headers={"Range": "bytes=0-5"},
        )
        self.assertEqual(stream.status_code, 206)
        self.assertEqual(stream.content, b"nested")

        legacy_url = self.client.get(
            "/streams/records/model/nested.ts",
            headers={"Range": "bytes=0-5"},
        )
        self.assertEqual(legacy_url.status_code, 206)
        self.assertEqual(legacy_url.content, b"nested")

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
                "birthDate": "1999-04-03",
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
        self.assertEqual(data["birthDate"], "1999-04-03")
        self.assertEqual(data["birth_date"], "1999-04-03")
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
        self.assertEqual(profiles["empty_model"]["streamSources"][0]["channelUsername"], "empty_model")

    async def test_updates_profile_with_multiple_stream_sources(self):
        response = self.client.put(
            "/api/media-profiles/empty_model",
            json={
                "displayName": "Multi Source",
                "streamSources": [
                    {
                        "sourceType": "chaturbate",
                        "channelUsername": "empty_one",
                        "channelUrl": "https://chaturbate.com/empty_one/",
                        "recordQuality": "1080p",
                        "retentionDays": 7,
                        "autoRecord": True,
                    },
                    {
                        "sourceType": "chaturbate",
                        "channelUsername": "empty_two",
                        "recordQuality": "720p",
                        "retentionDays": 0,
                        "autoRecord": True,
                    },
                    {
                        "sourceType": "cam4",
                        "channelUsername": "empty_cam4",
                        "channelUrl": "https://www.cam4.com/empty_cam4",
                        "recordQuality": "best",
                        "retentionDays": 30,
                        "autoRecord": False,
                    },
                ],
            },
        )
        self.assertEqual(response.status_code, 200)
        profile = response.json()["profile"]
        self.assertEqual(len(profile["streamSources"]), 3)
        sources = {item["channelUsername"]: item for item in profile["streamSources"]}
        self.assertEqual(sources["empty_one"]["recordPath"], "empty_model/videos/record")
        self.assertEqual(sources["empty_two"]["retentionDays"], 0)

        self.assertIsNotNone(await app_main.db.get_model("empty_one", source_type="chaturbate"))
        self.assertIsNotNone(await app_main.db.get_model("empty_two", source_type="chaturbate"))
        self.assertIsNotNone(await app_main.db.get_model("empty_cam4", source_type="cam4"))

        listing = self.client.get("/api/media-library")
        profiles = {item["username"]: item for item in listing.json()["profiles"]}
        self.assertEqual(len(profiles["empty_model"]["streamSources"]), 3)

    async def test_links_live_to_existing_media_profile(self):
        create = self.client.put(
            "/api/media-profiles/empty_model",
            json={"displayName": "Existing Profile", "streamSources": []},
        )
        self.assertEqual(create.status_code, 200)

        response = self.client.post(
            "/api/media-profiles/link-live",
            json={
                "profileUsername": "empty_model",
                "liveUsername": "channel_one",
                "sourceType": "chaturbate",
                "channelUrl": "https://chaturbate.com/channel_one/",
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["source"]["channelUsername"], "channel_one")
        self.assertTrue(data["source"]["autoRecord"])
        self.assertEqual(data["profile"]["streamSources"][0]["channelUsername"], "channel_one")
        self.assertIn("https://chaturbate.com/channel_one/", data["profile"]["streamUrls"])
        model = await app_main.db.get_model("channel_one", source_type="chaturbate")
        self.assertIsNotNone(model)
        self.assertTrue(model["auto_record"])

    async def test_links_live_and_creates_media_profile(self):
        response = self.client.post(
            "/api/media-profiles/link-live",
            json={
                "createProfile": True,
                "profileUsername": "brand_new",
                "displayName": "Brand New",
                "liveUsername": "brand_new_live",
                "sourceType": "cam4",
            },
        )
        self.assertEqual(response.status_code, 200)
        profile = response.json()["profile"]
        self.assertEqual(profile["username"], "brand_new")
        self.assertEqual(profile["displayName"], "Brand New")
        self.assertEqual(profile["streamSources"][0]["sourceType"], "cam4")
        self.assertEqual(profile["streamSources"][0]["channelUsername"], "brand_new_live")
        self.assertEqual(profile["streamSources"][0]["recordPath"], "brand_new/videos/record")

    async def test_resolves_and_serves_dedicated_profile_image(self):
        async def fake_download(username, image_url):
            self.assertEqual(username, "empty_model")
            self.assertEqual(image_url, "https://images.example/empty.jpg")
            app_main.PROFILE_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
            image_path = app_main.PROFILE_IMAGES_DIR / "empty_model.jpg"
            image_path.write_bytes(b"\xff\xd8\xff\xe0profile")
            return {
                "path": str(image_path),
                "size": image_path.stat().st_size,
                "contentType": "image/jpeg",
            }

        with (
            patch.object(
                app_main,
                "_resolve_profile_image_from_babepedia",
                new=AsyncMock(return_value={
                    "imageUrl": "https://images.example/empty.jpg",
                    "sourceUrl": "https://www.babepedia.com/babe/Empty_Model",
                }),
            ),
            patch.object(app_main, "_download_profile_image", new=AsyncMock(side_effect=fake_download)),
        ):
            response = self.client.post(
                "/api/media-profiles/empty_model/profile-image/resolve",
                json={"query": "Empty Model"},
            )

        self.assertEqual(response.status_code, 200)
        profile = response.json()["profile"]
        self.assertTrue(profile["profileImageUrl"].startswith("/api/media-profiles/empty_model/profile-image?v="))
        self.assertEqual(profile["profileImageSourceUrl"], "https://www.babepedia.com/babe/Empty_Model")

        image = self.client.get("/api/media-profiles/empty_model/profile-image")
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.content, b"\xff\xd8\xff\xe0profile")
        self.assertTrue(image.headers["content-type"].startswith("image/jpeg"))

        listing = self.client.get("/api/media-library")
        profiles = {item["username"]: item for item in listing.json()["profiles"]}
        self.assertTrue(profiles["empty_model"]["profileImageUrl"].startswith("/api/media-profiles/empty_model/profile-image?v="))
        self.assertEqual(profiles["empty_model"]["profileImageSourceUrl"], "https://www.babepedia.com/babe/Empty_Model")

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
