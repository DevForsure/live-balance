"""Live Balance v2 recorder: each balance's weight (balance.SCALES), read once per switch press.

Started by run.py (live-balance-start.bat); on its own: python app/recorder.py [--window].
Writes every press to that balance's press log (Scale.press_csv, e.g. data/presses.csv)
and a live picture + status to data/live/<balance>/.

Threaded RTSP reader holds only the latest frame (no OpenCV buffer lag) and reconnects
on failure. Every frame is stretched to 1920 wide, camera height kept, like v1.
The switch LED is checked on every frame; the frame it lights up on is the one read.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from dotenv import load_dotenv

import balance
import batch

ROOT = Path(__file__).resolve().parents[1]  # project folder (this file is in app/)

# Same camera settings as v1 config/config.yaml
TRANSPORT = "tcp"           # tcp is more reliable than udp for most NVRs
RECONNECT_DELAY_SEC = 5.0
TIMEOUT_SEC = 10.0
FRAME_WIDTH = 1920          # camera stream is 960 wide; stretched to 1920 like v1
FRAME_HEIGHT = None         # None keeps the camera's own height
PRESS_FIELDS = ["press_time", "batch", "weight", "unit", "status", "read_time"]
# Live picture + status for the batch page's Live panel, refreshed this often.
LIVE_EVERY_SEC = 1.0
# Display unreadable on the press frame (e.g. a hand in front): keep trying later
# frames this long. Below the 2 s press gap, so presses never overlap.
PRESS_TIMEOUT_SEC = 1.5
# Camera frames kept for the main loop while it is busy (~3 s at 15 fps); beyond that the
# PC is overloaded and the oldest are dropped rather than the loop falling ever further behind.
FRAME_QUEUE = 45

PLACEHOLDER_URL = "rtsp://username:password@192.168.1.100:554/Streaming/Channels/101"

log = logging.getLogger("live_balance_v2")


def mask_url(url: str) -> str:
    """Hide the user:password part of an RTSP URL for logging."""
    return re.sub(r"(://)[^@/]*@", r"\1***@", url)


def normalize_frame(frame: np.ndarray, width: int | None = FRAME_WIDTH,
                    height: int | None = FRAME_HEIGHT) -> np.ndarray:
    """Resize to (width, height) with Lanczos. None keeps that dimension unchanged."""
    h, w = frame.shape[:2]
    new_w, new_h = width or w, height or h
    if (new_w, new_h) == (w, h):
        return frame
    return cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)


class RTSPStream:
    def __init__(self, url: str, transport: str = TRANSPORT,
                 reconnect_delay: float = RECONNECT_DELAY_SEC, timeout_sec: float = TIMEOUT_SEC):
        if not url:
            raise ValueError("Camera URL is empty - set LIVE_BALANCE_RTSP_URL in .env")
        if url == PLACEHOLDER_URL:
            raise ValueError("LIVE_BALANCE_RTSP_URL in .env is still the example value - "
                             "put your camera's IP, username and password there")
        self.url = url
        self.reconnect_delay = reconnect_delay
        self.timeout_ms = int(timeout_sec * 1000)
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = f"rtsp_transport;{transport}"
        self._cap: cv2.VideoCapture | None = None
        self._frame: np.ndarray | None = None
        self._frame_time = 0.0
        self._new: deque[tuple[np.ndarray, float]] = deque(maxlen=FRAME_QUEUE)  # frames not yet taken by read_new
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "RTSPStream":
        self._thread = threading.Thread(target=self._run, daemon=True, name="rtsp-reader")
        self._thread.start()
        return self

    def _open(self) -> bool:
        log.info("Connecting to %s", mask_url(self.url))
        self._cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG, [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self.timeout_ms,
            cv2.CAP_PROP_READ_TIMEOUT_MSEC, self.timeout_ms,
        ])
        self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        ok = self._cap.isOpened()
        if ok:
            log.info("RTSP connected")
        else:
            log.warning("RTSP connect failed (check IP/port/credentials, and that this PC "
                        "can reach the camera); retrying in %ss", self.reconnect_delay)
        return ok

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._cap is None or not self._cap.isOpened():
                if not self._open():
                    time.sleep(self.reconnect_delay)
                    continue
            ok, frame = self._cap.read()
            if not ok:
                log.warning("Frame read failed, reconnecting in %ss", self.reconnect_delay)
                self._cap.release()
                self._cap = None
                time.sleep(self.reconnect_delay)
                continue
            with self._lock:
                self._frame = frame
                self._frame_time = time.time()
                self._new.append((frame, self._frame_time))

    def read(self) -> tuple[np.ndarray | None, float]:
        """Return (latest frame copy, capture timestamp). Frame is None until connected."""
        with self._lock:
            if self._frame is None:
                return None, 0.0
            return self._frame.copy(), self._frame_time

    def read_new(self) -> list[tuple[np.ndarray, float]]:
        """Every (frame, capture timestamp) since the last call, oldest first. The switch LED
        is lit for only 2-3 camera frames, so frames that came in while the loop was busy
        must still be looked at (seen live: 3 presses missed in one cycle)."""
        with self._lock:
            frames = list(self._new)
            self._new.clear()
        return frames

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        if self._cap:
            self._cap.release()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


class PressReader:
    """One weight per switch press: read on the frame the LED lights up on, or the
    first readable frame after it within PRESS_TIMEOUT_SEC."""

    def __init__(self, reference: np.ndarray, scale: balance.Scale):
        self.scale = scale
        self.detector = balance.PressDetector()
        self.registrar = balance.Registrar(reference, scale)
        self.pending: float | None = None  # camera time of a press not yet read
        # shortcut: in memory only, so the first press after a restart is never a repeat.
        self.last_weight: str | None = None  # last accepted weight

    def update(self, frame: np.ndarray, t: float) -> tuple[float, str | None, str, float] | None:
        """Feed every new normalized frame. Returns (press_time, weight, status,
        read_time) once per press, when it is decided; weight None = not read."""
        if self.detector.update(balance.red_pixels(balance.led_patch(frame, self.scale, self.registrar.warp)), t):
            self.pending = t
            log.info("%s switch pressed", self.scale.name)
        if self.pending is None:
            return None
        weight = balance.read_weight(self.registrar.align(frame), self.scale)
        if weight is not None and weight == self.last_weight:
            status = "rejected_repeat"  # pressed again without adding anything
        elif weight is not None:
            status = "ok" if t == self.pending else "ok_late"  # late: press frame unreadable
            self.last_weight = weight
        elif t - self.pending >= PRESS_TIMEOUT_SEC:
            status = "view_blocked" if self.registrar.score < balance.MIN_SCORE else "unreadable"
        else:
            return None
        press, self.pending = self.pending, None
        return press, weight, status, t


def write_press(path: Path, press_t: float, batch_no: str, weight: str | None, status: str,
                read_t: float) -> None:
    new = not path.exists()
    path.parent.mkdir(exist_ok=True)
    stamp = lambda t: datetime.fromtimestamp(t).isoformat(timespec="milliseconds")
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(PRESS_FIELDS)
        w.writerow([stamp(press_t), batch_no, weight or "", balance.UNIT, status, stamp(read_t)])


def upgrade_press_csv(path: Path) -> None:
    """Rewrite a press log from before a column was added (e.g. `batch`)."""
    if not path.exists():
        return
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header, rows = reader.fieldnames, list(reader)
    if header == PRESS_FIELDS:
        return
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, PRESS_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows({k: r.get(k) or "" for k in PRESS_FIELDS} for r in rows)
    tmp.replace(path)


def draw_batch(view: np.ndarray, watcher: batch.BatchWatcher) -> None:
    """Sheet reading on the live view: a box round the number where it was last read (blue =
    the accepted batch, orange = read but not accepted) and the accepted batch top left."""
    accepted, read, spot = watcher.batch, watcher.last_read, watcher.spot
    font = cv2.FONT_HERSHEY_SIMPLEX
    if spot and read:
        colour = (255, 160, 0) if read == accepted else (0, 140, 255)
        x0, x1, y0, y1 = spot
        cv2.rectangle(view, (x0, y0), (x1, y1), colour, 2)
        cv2.putText(view, read if read == accepted else f"{read}?", (x0, max(14, y0 - 6)), font, 0.5, colour, 1, cv2.LINE_AA)
    text = f"batch {accepted}" if accepted else "batch not read"
    (tw, th), _ = cv2.getTextSize(text, font, 0.6, 1)
    cv2.rectangle(view, (0, 0), (tw + 12, th + 12), (40, 40, 40), -1)  # the bench behind it is near white
    cv2.putText(view, text, (6, th + 6), font, 0.6, (255, 200, 80) if accepted else (200, 200, 200), 1, cv2.LINE_AA)


def live_view(frame: np.ndarray, reader: PressReader, crop: tuple[int, int, int, int],
              watcher: batch.BatchWatcher) -> tuple[np.ndarray, str | None]:
    """(the balance's crop with where the system looks: the display box (follows a
    moved balance; green = found, orange = not found), the switch LED box (red = lit) and
    the sheet reading (draw_batch), what the display shows now or None)."""
    x, y, w, h = crop
    view = frame[y:y + h, x:x + w].copy()
    patch = reader.registrar.align(frame)  # refresh where the display is, once per live picture
    found = reader.registrar.score >= balance.MIN_SCORE
    reading = balance.read_weight(patch, reader.scale) if found else None
    rx, ry, rw, rh = reader.scale.roi
    corners = np.array([[rx, ry], [rx + rw, ry], [rx + rw, ry + rh], [rx, ry + rh]], np.float64)
    warp = reader.registrar.warp
    box = corners @ warp[:, :2].T + warp[:, 2] - (x, y)  # reference -> frame -> crop coordinates
    colour = (60, 200, 60) if found else (0, 165, 255)
    cv2.polylines(view, [box.astype(np.int32)], True, colour, 2)
    cv2.putText(view, "display" if found else "display not found", tuple(box[0].astype(int) - (0, 6)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 1, cv2.LINE_AA)
    (lx, ly), (lw, lh) = balance.led_centre(reader.scale, warp), balance.LED_WINDOW
    lit = reader.detector.lit
    colour = (40, 40, 255) if lit else (200, 200, 200)
    cv2.rectangle(view, (lx - lw // 2 - x, ly - lh // 2 - y), (lx + lw // 2 - x, ly + lh // 2 - y), colour, 2)
    cv2.putText(view, "switch ON" if lit else "switch", (lx - lw // 2 - x, ly + lh // 2 - y + 16),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 1, cv2.LINE_AA)
    draw_batch(view, watcher)
    return view, reading


def publish_live(live_dir: Path, view: np.ndarray | None, status: dict) -> None:
    """Latest picture and status for the batch page (written whole, never half a file)."""
    live_dir.mkdir(parents=True, exist_ok=True)
    if view is not None:
        ok, jpg = cv2.imencode(".jpg", view, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            _write_whole(live_dir / "live.jpg", jpg.tobytes())
    _write_whole(live_dir / "status.json", json.dumps(status).encode("utf-8"))


class LiveWriter(threading.Thread):
    """Writes the live files off the main loop. Seen live (30-10-2026): one Windows rename
    of live.jpg hung for good inside the OS and froze press recording for 50 min. Now a
    hung write only stalls the live view; each balance's newest update replaces any older
    one still waiting, so nothing piles up. run.py restarts a recorder whose live view stalls."""

    def __init__(self):
        super().__init__(daemon=True, name="live-writer")
        self._lock, self._ready = threading.Lock(), threading.Event()
        self._latest: dict[Path, tuple[np.ndarray | None, dict]] = {}

    def submit(self, live_dir: Path, view: np.ndarray | None, status: dict) -> None:
        with self._lock:
            self._latest[live_dir] = (view, status)
        self._ready.set()

    def run(self) -> None:
        while self._ready.wait():
            with self._lock:
                todo, self._latest = self._latest, {}
                self._ready.clear()
            for live_dir, (view, status) in todo.items():
                try:
                    publish_live(live_dir, view, status)
                except Exception:
                    log.exception("live view update for %s failed; recording continues", live_dir.name)


def _write_whole(path: Path, data: bytes) -> None:
    """Write to a temp file, then swap it in. On Windows the swap fails while the batch
    page is reading the file (seen live: the recorder crashed on it), so retry briefly and
    otherwise skip this update - a missed live picture must never stop recording.
    The temp name carries the process id: a temp file left locked by a hung old recorder
    (seen live) must not block the new one."""
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.tmp{path.suffix}")
    tmp.write_bytes(data)
    for _ in range(5):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            time.sleep(0.02)
    log.debug("live file %s busy, skipped this update", path.name)


class Station:
    """One balance's recorder state: press reader, batch sheet watcher, crop, last press."""

    def __init__(self, scale: balance.Scale, stream: RTSPStream):
        self.scale, self.crop = scale, scale.load_crop()
        reference = cv2.imread(str(scale.reference_frame))
        if reference is None:
            raise SystemExit(f"Missing {scale.reference_frame} ({scale.name} balance reference frame)")
        x, y, w, h = self.crop
        log.info("%s balance crop: x=%d y=%d w=%d h=%d", scale.name, x, y, w, h)
        self.reader = PressReader(reference, scale)
        self.watcher = batch.BatchWatcher(stream, lambda f: normalize_frame(f)[y:y + h, x:x + w], scale.name,
                                          scale.small_sheet)
        self.last_press = None   # shown on the batch page
        upgrade_press_csv(scale.press_csv)

    def update(self, frame: np.ndarray, t: float) -> None:
        if not (result := self.reader.update(frame, t)):
            return
        press_t, weight, status, read_t = result
        batch_no = self.watcher.batch or ""  # batch the camera agreed on at the press
        write_press(self.scale.press_csv, press_t, batch_no, weight, status, read_t)
        self.last_press = {"time": datetime.fromtimestamp(press_t).isoformat(timespec="seconds"),
                           "weight": weight, "unit": balance.UNIT, "status": status, "batch": batch_no}
        log.info("%s PRESS = %s %s [%s, batch %s, read %d ms after press]", self.scale.name, weight or "no weight",
                 balance.UNIT, status, batch_no or "not read", (read_t - press_t) * 1000)

    def publish(self, writer: LiveWriter, frame: np.ndarray | None, frame_t: float, now: float,
                started: str) -> np.ndarray | None:
        view, reading = live_view(frame, self.reader, self.crop, self.watcher) if frame is not None else (None, None)
        writer.submit(self.scale.live_dir, view, {
            "display_weight": reading,  # the page shows tablet 0 while the pan is tared/empty
            "updated": datetime.now().isoformat(timespec="seconds"), "started": started,
            "frame_age_sec": round(now - frame_t, 1) if frame_t else None,
            "display_found": self.reader.registrar.score >= balance.MIN_SCORE,
            "switch_lit": self.reader.detector.lit,
            "batch": self.watcher.batch, "batch_error": self.watcher.error, "last_press": self.last_press})
        return view


def main() -> None:
    parser = argparse.ArgumentParser(description="Live Balance v2 recorder")
    parser.add_argument("--window", action="store_true", help="also show the camera windows (troubleshooting)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(ROOT / ".env")
    url = os.getenv("LIVE_BALANCE_RTSP_URL", "")
    started = datetime.now().isoformat(timespec="seconds")
    frame, frame_t, published = None, 0.0, 0.0
    writer = LiveWriter()
    writer.start()

    with RTSPStream(url) as stream:
        stations = [Station(scale, stream) for scale in balance.SCALES.values()]
        for st in stations:
            st.watcher.start()
        log.info("Recording: %s. Ctrl+C to stop.", ", ".join(s.name for s in balance.SCALES.values()))
        try:
            while True:
                for raw, t in stream.read_new():  # each camera frame once
                    frame_t, frame = t, normalize_frame(raw)
                    for st in stations:
                        st.update(frame, t)
                now = time.time()
                if now - published >= LIVE_EVERY_SEC:
                    published = now
                    for st in stations:
                        try:  # the live view is a convenience: its failures must never stop recording
                            view = st.publish(writer, frame, frame_t, now, started)
                            if args.window and view is not None:
                                cv2.imshow(f"Live Balance v2 - {st.scale.name}", view)
                        except Exception:
                            log.exception("%s live view update failed; recording continues", st.scale.name)
                if args.window:
                    if cv2.waitKey(10) & 0xFF == ord("q"):
                        break
                else:
                    time.sleep(0.01)
        except KeyboardInterrupt:
            log.info("Stopped.")
        for st in stations:
            st.watcher.stop()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
