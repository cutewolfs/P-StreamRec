import queue
import inspect
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app.browser_recorder import (
    BrowserCaptureSession,
    BrowserWebSocketMP4CaptureSession,
    _decode_base64_payload,
    _looks_like_mp4_fragment,
)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 5, 27, 12, 34, 56)
        return value if tz is None else value.replace(tzinfo=tz)


class BrowserCaptureSessionTests(unittest.TestCase):
    def test_recorder_data_url_parsing_allows_codec_commas(self):
        source = inspect.getsource(BrowserCaptureSession._run)

        self.assertIn("const marker = ';base64,'", source)
        self.assertNotIn("split(',', 2)", source)

    def test_write_chunk_records_and_replays_first_chunk_to_late_subscriber(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = BrowserCaptureSession(
                session_id="abc123",
                source_type="xcams",
                page_url="https://www.xcams.com/chat/model",
                sessions_dir=root / "sessions" / "abc123",
                records_dir_for_person=root / "records" / "model",
                person="model",
                record=True,
            )

            session._write_chunk(b"webm-data")
            late = session.subscribe()

            self.assertEqual(b"webm-data", (root / "records/model" / session.record_filename).read_bytes())
            self.assertEqual(b"webm-data", late.get_nowait())

    def test_live_subscriber_receives_future_chunks_without_recording_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = BrowserCaptureSession(
                session_id="abc123",
                source_type="xcams",
                page_url="https://www.xcams.com/chat/model",
                sessions_dir=root / "sessions" / "abc123",
                records_dir_for_person=root / "records" / "model",
                person="model",
                record=False,
            )
            subscriber = session.subscribe()

            session._write_chunk(b"live-chunk")

            self.assertEqual(b"live-chunk", subscriber.get_nowait())
            self.assertFalse((root / "records/model" / session.record_filename).exists())
            with self.assertRaises(queue.Empty):
                subscriber.get_nowait()

    def test_base64_payload_decoder_accepts_data_urls_and_urlsafe_payloads(self):
        self.assertEqual(b"abc", _decode_base64_payload("data:video/webm;base64,YWJj"))
        self.assertEqual(b"\xfb\xff", _decode_base64_payload("-_8"))

    def test_mp4_fragment_detector_accepts_common_fragment_boxes(self):
        self.assertTrue(_looks_like_mp4_fragment(b"\x00\x00\x00\x18ftypisom"))
        self.assertTrue(_looks_like_mp4_fragment(b"\x00\x00\x00\x10moofdata"))
        self.assertFalse(_looks_like_mp4_fragment(b"not-a-fragment"))

    def test_websocket_mp4_session_uses_mp4_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = BrowserWebSocketMP4CaptureSession(
                session_id="abc123",
                source_type="livejasmin",
                page_url="https://www.livejasmin.com/en/chat/model",
                sessions_dir=root / "sessions" / "abc123",
                records_dir_for_person=root / "records" / "model",
                person="model",
                record=True,
            )

            self.assertTrue(session.record_filename.endswith(".mp4"))
            self.assertTrue(session.record_path.endswith(".mp4"))
            self.assertEqual("/streams/browser/abc123/live.mp4", session.playback_url)

    def test_username_timestamp_filename_applies_to_browser_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("app.browser_recorder.datetime", FixedDatetime):
                session = BrowserCaptureSession(
                    session_id="abc123",
                    source_type="xcams",
                    page_url="https://www.xcams.com/chat/model",
                    sessions_dir=root / "sessions" / "abc123",
                    records_dir_for_person=root / "records" / "model",
                    person="model",
                    record=True,
                    filename_format="username_timestamp",
                )

        self.assertEqual("model_20260527-123456.webm", session.record_filename)

    def test_username_timestamp_browser_capture_collision_adds_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = root / "records" / "model"
            records.mkdir(parents=True)
            (records / "model_20260527-123456.webm").write_bytes(b"existing")

            with patch("app.browser_recorder.datetime", FixedDatetime):
                session = BrowserCaptureSession(
                    session_id="abc123",
                    source_type="xcams",
                    page_url="https://www.xcams.com/chat/model",
                    sessions_dir=root / "sessions" / "abc123",
                    records_dir_for_person=records,
                    person="model",
                    record=True,
                    filename_format="username_timestamp",
                )

        self.assertEqual("model_20260527-123456_abc123.webm", session.record_filename)


if __name__ == "__main__":
    unittest.main()
