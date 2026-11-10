"""Read the batch number off the batch sheet beside each balance.

The sheet moves between batches: seen live both sideways (reading bottom to top,
characters only ~10 px tall - too small for v1's Calibri template matching) and
flat on a clipboard. EasyOCR reads both, so:

- where: anywhere in that balance's crop (tools/select_crop.py), upright or
  turned 90 degrees either way;
- what it can be: 2 letters then 7 digits (PATTERN); anything else is no reading;
- agreement over time: a number is only accepted after the same reading in most
  of the recent reads (BatchVote), so a one-off misread never becomes the batch.

A full read (find the text, then read it) takes ~1.3 s on the CPU; re-reading just the
spot where the number was last time takes ~0.05 s. So each balance's BatchWatcher checks
that spot first and searches the whole crop only when the number is not there, and the
watchers share one EasyOCR engine (shared_reader), one read at a time: a balance whose
sheet just changed gets the engine almost to itself, and a new sheet is accepted in ~5 s.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import Counter, deque

import cv2
import numpy as np

log = logging.getLogger(__name__)

PATTERN = re.compile(r"^[A-Z]{2}[0-9]{7}$")   # 2 letters + 7 digits, e.g. WB2610003
READ_EVERY_SEC = 0.5       # pause between a watcher's reads (lets the other balances in)
SPOT_PAD = 6               # px kept round the number's last spot for the quick re-read
MIN_CONFIDENCE = 0.3       # EasyOCR's own score; correct live reads scored 0.9-1.0
ALLOWED = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
# Look-alikes EasyOCR may return in a digit position (seen: WB26100O3; the right balance's
# far, serif-font sheet AP2610002 as AP26ICO02 and AP26LOCDZ).
TO_DIGIT = str.maketrans("OQDCILZ", "0000112")
# The same in a letter position, for a small_sheet (seen on the right balance: W82610010).
TO_LETTER = str.maketrans("801526", "BOISZG")
SMALL_SHEET_MAG = 1.5      # text finder enlarges a small_sheet this much: read 9 of 10 frames right, 0 without
SIDEWAYS_EVERY = 4         # a small_sheet tries sideways only every so many misses (a try costs ~1.6 s of the shared engine)


def to_batch(text: str, letters: bool = False) -> str | None:
    """'WB26100O3' -> 'WB2610003'; None unless it is 2 letters + 7 digits.
    letters: also fix look-alikes in the letter positions ('W82610010' -> 'WB2610010')."""
    text = re.sub(r"\s+", "", text).upper()
    if len(text) != 9:
        return None
    text = (text[:2].translate(TO_LETTER) if letters else text[:2]) + text[2:].translate(TO_DIGIT)
    return text if PATTERN.match(text) else None


class BatchReader:
    def __init__(self):
        import easyocr  # slow import (torch): only when batch reading is used
        self.ocr = easyocr.Reader(["en"], gpu=False, verbose=False)

    def read(self, area: np.ndarray, spot: list[int] | None = None, small: bool = False,
             sideways: bool = True) -> tuple[str | None, list[int] | None]:
        """(batch number in a BGR image (a balance's crop) or None, its spot [x0, x1, y0, y1]).
        spot: where it was last time, re-read there alone first (quick).
        small: a small_sheet. Upright first, enlarged, with look-alike letters fixed; turned
        only if that finds no number and `sideways`: together with upright text, a turned
        guess can win and lose the real number (seen live: no read in 10 frames)."""
        gray = cv2.cvtColor(area, cv2.COLOR_BGR2GRAY)
        if spot:  # the margin can catch a stray mark: "1AP2610002" -> any 9 characters that fit
            found = {b for _, text, conf in self.ocr.recognize(gray, horizontal_list=[spot], free_list=[],
                                                                 allowlist=ALLOWED)
                     if conf >= MIN_CONFIDENCE
                     for b in (to_batch(text[i:i + 9], small) for i in range(len(text) - 8)) if b}
            if len(found) == 1:
                return found.pop(), spot
        hits: dict = {}
        for options in ([{"mag_ratio": SMALL_SHEET_MAG}] + ([{"rotation_info": [90, 270]}] if sideways else [])
                        if small else [{"rotation_info": [90, 270]}]):
            hits = {b: box for box, text, conf in self.ocr.readtext(gray, allowlist=ALLOWED, **options)
                    if conf >= MIN_CONFIDENCE and (b := to_batch(text, small))}
            if hits:
                break
        if len(hits) != 1:  # two different numbers: no guess
            return None, None
        b, box = hits.popitem()
        (h, w), xs, ys = gray.shape, [p[0] for p in box], [p[1] for p in box]
        return b, [int(max(0, min(xs) - SPOT_PAD)), int(min(w, max(xs) + SPOT_PAD)),
                   int(max(0, min(ys) - SPOT_PAD)), int(min(h, max(ys) + SPOT_PAD))]


_ocr: BatchReader | None = None
_ocr_lock = threading.Lock()  # held while starting the engine and during each read


def shared_reader() -> BatchReader:
    """The one EasyOCR engine; raises like BatchReader() if it cannot start."""
    global _ocr
    with _ocr_lock:
        if _ocr is None:
            _ocr = BatchReader()
        return _ocr


class BatchVote:
    """Accept a camera reading only once it dominates the recent reads. Only the sheet
    there now counts: the batch lapses after hold_sec without a read, and a different
    number read twice in a row drops it at once (a swapped sheet never lends its number
    to the next batch's presses)."""

    def __init__(self, window: int = 5, needed: int = 3, hold_sec: float = 20.0):
        self.reads: deque[str | None] = deque(maxlen=window)
        self.needed, self.hold_sec = needed, hold_sec
        self.batch: str | None = None      # accepted camera batch
        self.seen_at: float = 0.0          # last read of the accepted batch

    def update(self, text: str | None, t: float) -> str | None:
        if self.batch and text and text != self.batch and self.reads and self.reads[-1] == text:
            self.batch = None              # another sheet: start agreeing on it from scratch
            self.reads.clear()
            self.reads.append(text)
        self.reads.append(text)
        counts = Counter(r for r in self.reads if r)
        if counts:
            top, n = counts.most_common(1)[0]
            others = sum(counts.values()) - n
            if n >= self.needed and others <= 1:
                self.batch = top
        if self.batch and text == self.batch:
            self.seen_at = t
        if self.batch and t - self.seen_at > self.hold_sec:
            self.batch = None              # sheet gone for a while: no camera batch
        return self.batch


class BatchWatcher(threading.Thread):
    """Reads the sheet every READ_EVERY_SEC; `batch` is the accepted batch or None."""

    def __init__(self, stream, area, name: str = "", small: bool = False):
        """area(raw camera frame) -> the image to search for the sheet; name: the balance, for the log;
        small: its Scale.small_sheet."""
        super().__init__(daemon=True, name=f"batch-watcher-{name}")
        self.stream, self.area, self.label, self.small = stream, area, name, small
        self.misses = 0                    # reads in a row that found no number
        self.vote = BatchVote()
        self.batch: str | None = None
        self.spot: list[int] | None = None  # where the number was in the last read
        self.last_read: str | None = None   # what the last read found, accepted or not (live view)
        self.error: str | None = None      # why batch reading is off, shown in the preview
        self._halt = threading.Event()  # not _stop: that name is a Thread internal

    def run(self) -> None:
        try:
            reader = shared_reader()
        except ImportError:
            self.error = "EasyOCR not installed"
            log.error("Batch reading is OFF: EasyOCR is not installed - run setup.bat. "
                      "Presses are logged without a batch until then.")
            return
        except Exception as e:  # e.g. model files missing on a PC without internet
            self.error = "EasyOCR failed to start"
            log.error("Batch reading is OFF: EasyOCR failed to start (%s). Its model files "
                      "belong in %%USERPROFILE%%\\.EasyOCR\\model - copy them from a PC where "
                      "batch reading works. Presses are logged without a batch.", e)
            return
        while not self._halt.wait(READ_EVERY_SEC):
            try:
                with _ocr_lock:  # frame taken once it is our turn: waiting with an old one cost seconds
                    frame, t = self.stream.read()
                    if frame is None:
                        continue
                    text, self.spot = reader.read(self.area(frame), self.spot, self.small,
                                                  self.misses % SIDEWAYS_EVERY == SIDEWAYS_EVERY - 1)
                    self.last_read = text
                    self.misses = 0 if text else self.misses + 1
            except Exception:  # one bad read must not end batch reading for the session
                log.exception("Batch read failed; trying again")
                continue
            before, self.batch = self.batch, self.vote.update(text, t)
            if self.batch != before:
                log.info("%s batch sheet: %s", self.label, f"camera reads {self.batch}" if self.batch
                         else "no longer read")

    def stop(self) -> None:
        self._halt.set()
