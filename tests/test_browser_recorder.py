import asyncio
import queue
import inspect
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.browser_recorder import (
    BrowserCaptureManager,
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

    def test_manager_uses_provider_profile_and_session_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = object()
            manager = BrowserCaptureManager(str(root), session_store=store)

            original_start = BrowserCaptureSession.start
            try:
                BrowserCaptureSession.start = lambda self: None
                session = manager.start_session(
                    source_type="xcams",
                    page_url="https://www.xcams.com/chat/model",
                    person="model",
                    record=False,
                )
            finally:
                BrowserCaptureSession.start = original_start

            self.assertEqual(root / "provider-browser", session.browser_root)
            self.assertIs(store, session.session_store)

    def test_completion_callback_runs_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            completed = []
            session = BrowserCaptureSession(
                session_id="abc123",
                source_type="xcams",
                page_url="https://www.xcams.com/chat/model",
                sessions_dir=root / "sessions" / "abc123",
                records_dir_for_person=root / "records" / "model",
                person="model",
                record=True,
                on_complete=completed.append,
            )

            session._complete()
            session._complete()

            self.assertEqual(completed, [session])
            self.assertTrue(session._done_evt.is_set())

    def test_manager_stop_keeps_session_while_thread_is_still_alive(self):
        class ThreadStub:
            def __init__(self):
                self.alive = True
                self.join_timeout = None

            def is_alive(self):
                return self.alive

            def join(self, timeout=None):
                self.join_timeout = timeout

        with tempfile.TemporaryDirectory() as tmp:
            manager = BrowserCaptureManager(tmp)
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
            thread = ThreadStub()
            session._thread = thread
            manager._sessions[session.id] = session

            self.assertTrue(manager.stop_session(session.id))
            self.assertTrue(session._stop_evt.is_set())
            self.assertEqual(thread.join_timeout, 10)
            self.assertIn(session.id, manager._sessions)

            thread.alive = False
            self.assertEqual(manager.list_status(), [])
            self.assertNotIn(session.id, manager._sessions)


class BrowserCaptureIndexLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_natural_and_error_completion_each_schedule_index_once(self):
        from app import main as app_main

        class FinishingSession(BrowserCaptureSession):
            async def _run(self):
                if getattr(self, "forced_error", None):
                    self.error = self.forced_error
                self._complete()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            loop = asyncio.get_running_loop()
            sessions = []
            with patch.object(
                app_main,
                "_index_browser_capture_recording",
                new=AsyncMock(),
            ) as index_mock:
                for suffix, error in (("natural", None), ("error", "page closed")):
                    session = FinishingSession(
                        session_id=suffix,
                        source_type="xcams",
                        page_url="https://www.xcams.com/chat/model",
                        sessions_dir=root / "sessions" / suffix,
                        records_dir_for_person=root / "records" / "model",
                        person="model",
                        record=True,
                        on_complete=app_main._browser_capture_completion_callback(loop),
                    )
                    session.forced_error = error
                    session.start()
                    await asyncio.to_thread(session._thread.join, 2)
                    self.assertFalse(session.thread_is_alive())
                    self.assertIsNotNone(session._index_future)
                    await asyncio.wrap_future(session._index_future)
                    session._complete()
                    sessions.append(session)

            self.assertEqual(index_mock.await_count, 2)
            self.assertEqual(
                [call.args[0] for call in index_mock.await_args_list],
                sessions,
            )

    async def test_manual_stop_uses_completion_index_without_duplicate(self):
        from app import main as app_main

        class StoppableSession(BrowserCaptureSession):
            async def _run(self):
                try:
                    while not self._stop_evt.is_set():
                        await asyncio.sleep(0.01)
                finally:
                    self._complete()

        class NoFFmpegSessions:
            def get_session(self, _session_id):
                return None

            def stop_session(self, _session_id):
                return False

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = BrowserCaptureManager(tmp)
            session = StoppableSession(
                session_id="manual",
                source_type="xcams",
                page_url="https://www.xcams.com/chat/model",
                sessions_dir=root / "sessions" / "manual",
                records_dir_for_person=root / "records" / "model",
                person="model",
                record=True,
                on_complete=app_main._browser_capture_completion_callback(
                    asyncio.get_running_loop()
                ),
            )
            manager._sessions[session.id] = session

            with (
                patch.object(app_main, "manager", new=NoFFmpegSessions()),
                patch.object(app_main, "browser_capture_manager", new=manager),
                patch.object(
                    app_main,
                    "_index_browser_capture_recording",
                    new=AsyncMock(),
                ) as index_mock,
            ):
                session.start()
                response = await app_main.api_stop(session.id)

            self.assertEqual(response, {"stopped": True, "id": session.id})
            index_mock.assert_awaited_once_with(session)
            self.assertNotIn(session.id, manager._sessions)


if __name__ == "__main__":
    unittest.main()
