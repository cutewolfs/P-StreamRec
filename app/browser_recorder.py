from __future__ import annotations

import asyncio
import base64
import os
import queue
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Dict, Optional

from .core.config import MIN_RECORDING_SECONDS
from .logger import logger
from .providers.browser import DEFAULT_USER_AGENT
from .recording_names import (
    FILENAME_FORMAT_TIMESTAMP,
    recording_base_name,
)


def _decode_base64_payload(raw: str) -> bytes:
    raw = (raw or "").strip().rsplit(",", 1)[-1]
    raw = "".join(raw.split())
    raw = raw.replace("-", "+").replace("_", "/")
    raw += "=" * (-len(raw) % 4)
    return base64.b64decode(raw)


def _looks_like_mp4_fragment(data: bytes) -> bool:
    if len(data) < 8:
        return False
    box_type = data[4:8]
    return box_type in {b"ftyp", b"moov", b"moof", b"mdat"} or b"moof" in data[:128]


class BrowserCaptureSession:
    def __init__(
        self,
        session_id: str,
        source_type: str,
        page_url: str,
        sessions_dir: Path,
        records_dir_for_person: Path,
        person: str,
        display_name: Optional[str] = None,
        record: bool = True,
        browser_root: Optional[Path] = None,
        file_extension: str = "webm",
        filename_format: str = FILENAME_FORMAT_TIMESTAMP,
    ):
        self.id = session_id
        self.source_type = source_type
        self.page_url = page_url
        self.sessions_dir = sessions_dir
        self.records_dir_for_person = records_dir_for_person
        self.person = person
        self.name = display_name or person or session_id
        self.record = bool(record)
        self.browser_root = browser_root
        self.file_extension = (file_extension or "webm").strip(".").lower() or "webm"
        self.filename_format = filename_format
        self.created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        self.start_time = time.time()
        self.start_date = datetime.now().strftime("%Y-%m-%d")
        self.start_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.record_base = self._unique_record_base(
            recording_base_name(person, self.start_timestamp, session_id, filename_format)
        )
        self.record_filename = f"{self.record_base}.{self.file_extension}"
        self.record_path = str(records_dir_for_person / self.record_filename)
        self.playback_url = f"/streams/browser/{self.id}/live.{self.file_extension}"
        self.bytes_written = 0
        self.last_progress_at = self.start_time
        self.completed_at: Optional[str] = None
        self.error: Optional[str] = None
        self._stop_evt = threading.Event()
        self._ready_evt = threading.Event()
        self._done_evt = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._subscribers: list[queue.Queue[bytes]] = []
        self._first_chunk: Optional[bytes] = None

    def _unique_record_base(self, base: str) -> str:
        first_path = self.records_dir_for_person / f"{base}.{self.file_extension}"
        if not first_path.exists():
            return base
        return f"{base}_{self.id[:6]}"

    def start(self) -> None:
        self._thread = threading.Thread(
            target=lambda: asyncio.run(self._run()),
            name=f"browser-capture-{self.id}",
            daemon=True,
        )
        self._thread.start()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._done_evt.is_set())

    def seconds_since_progress(self) -> float:
        return max(0.0, time.time() - self.last_progress_at)

    def record_path_today(self) -> str:
        return self.record_path

    def wait_until_ready(self, timeout: float = 25) -> bool:
        self._ready_evt.wait(timeout=max(0.0, float(timeout or 0)))
        if self.error:
            raise RuntimeError(self.error)
        return self.bytes_written > 0 or (not self.record and self._first_chunk is not None)

    def stop(self, timeout: float = 10) -> None:
        self._stop_evt.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def subscribe(self) -> queue.Queue[bytes]:
        q: queue.Queue[bytes] = queue.Queue(maxsize=90)
        with self._lock:
            if self._first_chunk:
                q.put_nowait(self._first_chunk)
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[bytes]) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def has_subscribers(self) -> bool:
        with self._lock:
            return bool(self._subscribers)

    def _publish_chunk(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            if self._first_chunk is None:
                self._first_chunk = data
            subscribers = list(self._subscribers)

        for q in subscribers:
            try:
                q.put_nowait(data)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(data)
                except queue.Empty:
                    pass

    def _write_chunk(self, data: bytes) -> None:
        if not data:
            return
        if self.record:
            self.records_dir_for_person.mkdir(parents=True, exist_ok=True)
            with open(self.record_path, "ab") as f:
                f.write(data)
        self.bytes_written += len(data)
        self.last_progress_at = time.time()
        self._publish_chunk(data)
        self._ready_evt.set()

    async def _run(self) -> None:
        browser = None
        context = None
        try:
            from playwright.async_api import async_playwright

            self.sessions_dir.mkdir(parents=True, exist_ok=True)
            if self.record:
                self.records_dir_for_person.mkdir(parents=True, exist_ok=True)

            async with async_playwright() as playwright:
                launch_args = ["--autoplay-policy=no-user-gesture-required"]
                if self.browser_root:
                    user_data_dir = self.browser_root / self.source_type
                    user_data_dir.mkdir(parents=True, exist_ok=True)
                    context = await playwright.chromium.launch_persistent_context(
                        str(user_data_dir),
                        headless=os.getenv("PSTREAMREC_BROWSER_HEADLESS", "true").lower() not in {"0", "false", "no"},
                        user_agent=DEFAULT_USER_AGENT,
                        viewport={"width": 1280, "height": 720},
                        args=launch_args,
                    )
                    page = context.pages[0] if context.pages else await context.new_page()
                else:
                    browser = await playwright.chromium.launch(
                        headless=os.getenv("PSTREAMREC_BROWSER_HEADLESS", "true").lower() not in {"0", "false", "no"},
                        args=launch_args,
                    )
                    context = await browser.new_context(
                        user_agent=DEFAULT_USER_AGENT,
                        viewport={"width": 1280, "height": 720},
                    )
                    page = await context.new_page()

                async def receive_chunk(_source, payload):
                    raw = ""
                    if isinstance(payload, dict):
                        raw = str(payload.get("data") or "")
                    if not raw:
                        return
                    try:
                        self._write_chunk(_decode_base64_payload(raw))
                    except Exception as exc:
                        logger.error(
                            "Browser capture chunk write failed",
                            session_id=self.id,
                            source_type=self.source_type,
                            error=str(exc),
                        )

                await page.expose_binding("__pstreamrecChunk", receive_chunk)
                await page.goto(self.page_url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(12000)
                try:
                    await page.mouse.click(640, 360)
                except Exception:
                    pass
                await page.wait_for_timeout(3000)

                await page.evaluate(
                    """
                    async () => {
                      const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
                      async function pickVideo() {
                        const deadline = Date.now() + 35000;
                        while (Date.now() < deadline) {
                          const videos = Array.from(document.querySelectorAll('video'));
                          for (const video of videos) {
                            try { await video.play(); } catch (e) {}
                            if (
                              typeof video.captureStream === 'function' &&
                              video.readyState >= 2 &&
                              video.videoWidth > 0
                            ) {
                              return video;
                            }
                          }
                          await sleep(500);
                        }
                        return null;
                      }

                      const video = await pickVideo();
                      if (!video) throw new Error('No playable video element');
                      const stream = video.captureStream();
                      if (!stream.getTracks().length) throw new Error('Video captureStream returned no tracks');

                      let mime = 'video/webm;codecs=vp8,opus';
                      if (!MediaRecorder.isTypeSupported(mime)) mime = 'video/webm';
                      const recorder = new MediaRecorder(stream, { mimeType: mime });
                      window.__pstreamrecRecorder = recorder;
                      window.__pstreamrecStopRecorder = () => new Promise(resolve => {
                        if (!window.__pstreamrecRecorder || window.__pstreamrecRecorder.state === 'inactive') {
                          resolve();
                          return;
                        }
                        window.__pstreamrecRecorder.addEventListener('stop', resolve, { once: true });
                        window.__pstreamrecRecorder.stop();
                      });
                      recorder.ondataavailable = event => {
                        if (!event.data || !event.data.size) return;
                        const reader = new FileReader();
                        reader.onloadend = () => {
                          const result = String(reader.result || '');
                          const marker = ';base64,';
                          const markerIndex = result.indexOf(marker);
                          const encoded = markerIndex >= 0
                            ? result.slice(markerIndex + marker.length)
                            : (result.split(',').pop() || '');
                          window.__pstreamrecChunk({ data: encoded, size: event.data.size, mime });
                        };
                        reader.readAsDataURL(event.data);
                      };
                      recorder.start(1000);
                    }
                    """
                )

                self._ready_evt.wait(0.1)
                while not self._stop_evt.is_set():
                    await page.wait_for_timeout(500)

                try:
                    await page.evaluate(
                        "() => window.__pstreamrecStopRecorder ? window.__pstreamrecStopRecorder() : undefined"
                    )
                    await page.wait_for_timeout(1000)
                except Exception:
                    pass
        except Exception as exc:
            self.error = str(exc)
            logger.error(
                "Browser capture failed",
                session_id=self.id,
                source_type=self.source_type,
                page_url=self.page_url,
                error=str(exc),
                exc_info=True,
            )
            self._ready_evt.set()
        finally:
            try:
                if context:
                    await context.close()
            except Exception:
                pass
            try:
                if browser:
                    await browser.close()
            except Exception:
                pass
            self.completed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            self._done_evt.set()
            self._cleanup_short_recording()

    def _cleanup_short_recording(self) -> None:
        if not self.record:
            return
        elapsed = time.time() - self.start_time
        if self.bytes_written > 0 and elapsed >= MIN_RECORDING_SECONDS:
            return
        try:
            if os.path.exists(self.record_path):
                os.remove(self.record_path)
                logger.warning(
                    "Browser capture fragment removed",
                    session_id=self.id,
                    person=self.person,
                    bytes_written=self.bytes_written,
                    elapsed_seconds=f"{elapsed:.1f}",
                    min_seconds=MIN_RECORDING_SECONDS,
                )
        except Exception as exc:
            logger.error(
                "Browser capture cleanup failed",
                session_id=self.id,
                person=self.person,
                error=str(exc),
            )


class BrowserWebSocketMP4CaptureSession(BrowserCaptureSession):
    def __init__(self, *args, **kwargs):
        kwargs["file_extension"] = "mp4"
        super().__init__(*args, **kwargs)

    async def _run(self) -> None:
        browser = None
        context = None
        try:
            from playwright.async_api import async_playwright

            self.sessions_dir.mkdir(parents=True, exist_ok=True)
            if self.record:
                self.records_dir_for_person.mkdir(parents=True, exist_ok=True)

            async with async_playwright() as playwright:
                launch_args = ["--autoplay-policy=no-user-gesture-required"]
                if self.browser_root:
                    user_data_dir = self.browser_root / self.source_type
                    user_data_dir.mkdir(parents=True, exist_ok=True)
                    context = await playwright.chromium.launch_persistent_context(
                        str(user_data_dir),
                        headless=os.getenv("PSTREAMREC_BROWSER_HEADLESS", "true").lower() not in {"0", "false", "no"},
                        user_agent=DEFAULT_USER_AGENT,
                        viewport={"width": 1280, "height": 720},
                        args=launch_args,
                    )
                    page = context.pages[0] if context.pages else await context.new_page()
                else:
                    browser = await playwright.chromium.launch(
                        headless=os.getenv("PSTREAMREC_BROWSER_HEADLESS", "true").lower() not in {"0", "false", "no"},
                        args=launch_args,
                    )
                    context = await browser.new_context(
                        user_agent=DEFAULT_USER_AGENT,
                        viewport={"width": 1280, "height": 720},
                    )
                    page = await context.new_page()

                cdp = await context.new_cdp_session(page)

                def receive_frame(event) -> None:
                    payload = str(((event.get("response") or {}).get("payloadData") or ""))
                    if not payload or not payload.lstrip().startswith("A"):
                        return
                    try:
                        data = _decode_base64_payload(payload)
                    except Exception:
                        return
                    if not _looks_like_mp4_fragment(data):
                        return
                    self._write_chunk(data)

                cdp.on("Network.webSocketFrameReceived", receive_frame)
                await cdp.send("Network.enable")
                await page.goto(self.page_url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(12000)
                try:
                    await page.mouse.click(640, 360)
                except Exception:
                    pass

                while not self._stop_evt.is_set():
                    await page.wait_for_timeout(500)
        except Exception as exc:
            self.error = str(exc)
            logger.error(
                "Browser websocket MP4 capture failed",
                session_id=self.id,
                source_type=self.source_type,
                page_url=self.page_url,
                error=str(exc),
                exc_info=True,
            )
            self._ready_evt.set()
        finally:
            try:
                if context:
                    await context.close()
            except Exception:
                pass
            try:
                if browser:
                    await browser.close()
            except Exception:
                pass
            self.completed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            self._done_evt.set()
            self._cleanup_short_recording()


class BrowserCaptureManager:
    def __init__(self, base_output_dir: str):
        self.base_output_dir = Path(base_output_dir)
        self.sessions_root = self.base_output_dir / "browser-sessions"
        self.records_root = self.base_output_dir / "records"
        self.browser_root = self.base_output_dir / "provider-browser"
        self._lock = threading.Lock()
        self._sessions: Dict[str, BrowserCaptureSession] = {}

    def start_session(
        self,
        source_type: str,
        page_url: str,
        person: str,
        display_name: Optional[str] = None,
        record: bool = True,
        capture_mode: str = "media_recorder",
        filename_format: str = FILENAME_FORMAT_TIMESTAMP,
    ) -> BrowserCaptureSession:
        with self._lock:
            self._prune_finished_locked()
            if record:
                for session in self._sessions.values():
                    if session.record and session.person == person and session.is_running():
                        raise RuntimeError(f"Une session est déjà en cours pour '{person}'.")
            else:
                for session in self._sessions.values():
                    if (
                        not session.record
                        and session.person == person
                        and session.source_type == source_type
                        and session.is_running()
                    ):
                        return session

            session_id = uuid.uuid4().hex[:10]
            session_cls = (
                BrowserWebSocketMP4CaptureSession
                if (capture_mode or "").strip().lower() == "websocket_mp4"
                else BrowserCaptureSession
            )
            session = session_cls(
                session_id=session_id,
                source_type=source_type,
                page_url=page_url,
                sessions_dir=self.sessions_root / session_id,
                records_dir_for_person=self.records_root / person,
                person=person,
                display_name=display_name,
                record=record,
                browser_root=None,
                filename_format=filename_format,
            )
            session.capture_mode = (capture_mode or "media_recorder").strip().lower()
            self._sessions[session_id] = session
            session.start()
            return session

    def get(self, session_id: str) -> Optional[BrowserCaptureSession]:
        with self._lock:
            self._prune_finished_locked()
            return self._sessions.get(session_id)

    def stop_session(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            return False
        session.stop()
        with self._lock:
            self._sessions.pop(session_id, None)
        return True

    def stop_live_if_idle(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
        if session and not session.record and not session.has_subscribers():
            self.stop_session(session_id)

    def list_status(self, recording_only: bool = True) -> list[dict]:
        with self._lock:
            self._prune_finished_locked()
            out = []
            for session in self._sessions.values():
                if recording_only and not session.record:
                    continue
                out.append({
                    "id": session.id,
                    "person": session.person,
                    "name": session.name,
                    "input_url": session.page_url,
                    "created_at": session.created_at,
                    "running": session.is_running(),
                    "playback_url": session.playback_url,
                    "record_path": session.record_path if session.record else None,
                    "start_date": session.start_date,
                    "bytes_written": session.bytes_written,
                    "seconds_since_progress": int(session.seconds_since_progress()),
                    "source_type": session.source_type,
                    "capture_type": "browser",
                    "capture_mode": getattr(session, "capture_mode", "media_recorder"),
                })
            return out

    def _prune_finished_locked(self) -> int:
        pruned = 0
        for session_id, session in list(self._sessions.items()):
            if session.is_running():
                continue
            self._sessions.pop(session_id, None)
            pruned += 1
        return pruned
