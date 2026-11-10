"""The balances in the camera view (SCALES): switch LED press detection and
seven-segment display reading.

Ported from Live Balance v1 (press/led_watch.py, roi/register.py, ocr/seven_segment.py,
ocr/nearby.py, preprocess/image_ops.py) with v1's left- and centre-balance calibration.
All coordinates are in the 1920-wide normalized frame.
"""
from __future__ import annotations

import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]  # project folder (this file is in app/)

UNIT = "g"
MINUS_FRACTION = 0.2    # fullest row of the minus zone at least this inked = negative reading (measured: 0.00 vs >= 0.38)
LED_WINDOW = (80, 60)   # px searched around the LED (tolerates the switch box moving a bit)


@dataclass(frozen=True)
class Scale:
    """One balance: its calibration (in coordinates of its reference frame, the picture
    the display ROI and digit layout were fitted on) and its files. Centre keeps the unsuffixed file names from before
    there was more than one balance."""
    key: str                # in URLs and file names
    name: str
    balance_id: str         # label on the balance, printed on every sheet
    roi: tuple[int, int, int, int]       # display x, y, w, h
    slant: float
    digit_box: tuple[float, float, float, float]  # x0, x1, y0, y1 as fractions of the ROI after deslant
    tilt: float
    led_v1: tuple[int, int]  # push-button LED until calibrated with tools/select_crop.py
    crop_v1: tuple[int, int, int, int]  # live view + batch sheet area (x, y, w, h) until selected
    search: int = 70        # max shift searched for the display, px each way
    digits: int = 6         # digit positions on the display
    decimals: int = 3
    capacity: float = 300   # g; a higher reading must be a misread and is dropped
    minus: tuple[float, float, float, float] | None = None  # minus sign zone (x0, x1, y0, y1 of the ROI)
    suffix: str = ""
    reference: str = "reference.jpg"  # in config/; v1's frame unless refitted on a newer one
    small_sheet: bool = False  # batch number small/far in the camera's view: batch.BatchReader.read
    led: tuple[int, int] = field(init=False)

    def __post_init__(self):
        f = self.led_file
        object.__setattr__(self, "led", tuple(json.loads(f.read_text())["ref"]) if f.exists() else self.led_v1)

    @property
    def reference_frame(self) -> Path: return ROOT / "config" / self.reference
    @property
    def crop_file(self) -> Path: return ROOT / "config" / f"crop{self.suffix}.json"
    @property
    def led_file(self) -> Path: return ROOT / "config" / f"led{self.suffix}.json"
    def load_crop(self) -> tuple[int, int, int, int]:
        """Crop from crop_file (tools/select_crop.py), else crop_v1."""
        if not self.crop_file.exists():
            return self.crop_v1
        c = json.loads(self.crop_file.read_text(encoding="utf-8"))
        return c["x"], c["y"], c["w"], c["h"]

    @property
    def press_csv(self) -> Path: return ROOT / "data" / f"presses{self.suffix}.csv"
    @property
    def deleted_csv(self) -> Path: return ROOT / "data" / f"deleted_presses{self.suffix}.csv"
    @property
    def live_dir(self) -> Path: return ROOT / "data" / "live" / self.key


SCALES = {s.key: s for s in [
    # v1 scale_1. Since v1 the balance sits ~105 px higher and turned 3.5 deg: wider search.
    Scale("left", "Left", "ADR/QC/AB/001", (333, 632, 98, 38), 0.1, (0.14, 0.89, 0.21, 0.83), 0.16,
          (545, 684), (230, 170, 545, 790), search=130, suffix="_left"),
    Scale("centre", "Centre", "ADR/QC/AB/002", (914, 646, 103, 47), 0.16, (0.1, 0.87, 0.13, 0.73), 0.12,
          (955, 734), (820, 190, 380, 600)),
    # 0.0000 .. 199.9999 g. v1's scale_3 calibration no longer fitted (moved ~(-145, -90) px,
    # read 0.0088 for 0.0089), so refitted on 7 frames of 29-10-2026 (0.0089, 0.0000, -0.0003..-0.0005).
    # Re-fitted 31-10-2026 on a new reference after the balance was moved (68 px, 2.2 deg): at the
    # old fit the wide-angle lens made the tens "1" fall outside its cell (14.5121 read 4.5121).
    Scale("right", "Right", "ADR/QC/AB/003", (1289, 673, 120, 48), 0.255, (0.1, 0.965, 0.18, 0.7), 0.075,
          (1267, 793), (1260, 220, 480, 620), digits=7, decimals=4, capacity=200,
          minus=(0.05, 0.12, 0.22, 0.45), suffix="_right", reference="reference_right.jpg", small_sheet=True),
]}
DEFAULT = "centre"

# --- switch LED ------------------------------------------------------------


def red_pixels(patch: np.ndarray) -> int:
    """Pixels that are clearly LED-red (skin, wood and the white box are not).
    Unlit LED shows 0, lit one several hundred (v1 measured >= 550)."""
    p = patch.astype(np.int16)
    b, g, r = p[..., 0], p[..., 1], p[..., 2]
    return int(((r - np.maximum(g, b) > 70) & (r > 140)).sum())


def led_centre(scale: Scale, warp: np.ndarray | None = None) -> tuple[int, int]:
    """LED position in the current frame. The switch box sits beside the balance, so it
    follows the balance's measured movement (Registrar.warp, reference -> frame): exact
    when the camera moved, close enough for LED_WINDOW when the bench was shifted
    (seen live: balance moved (-70, -57) px, switch (-55, -33))."""
    if warp is None:
        return scale.led
    x, y = warp @ np.array([*scale.led, 1.0])
    return int(round(x)), int(round(y))


def led_patch(frame: np.ndarray, scale: Scale, warp: np.ndarray | None = None) -> np.ndarray:
    (x, y), (w, h) = led_centre(scale, warp), LED_WINDOW
    return frame[max(0, y - h // 2):y + h // 2, max(0, x - w // 2):x + w // 2]


class PressDetector:
    """Turns a stream of red-pixel counts into press events, with on/off hysteresis."""

    def __init__(self, on_px: int = 25, off_px: int = 8, min_gap_sec: float = 2.0):
        self.on_px, self.off_px, self.min_gap = on_px, off_px, min_gap_sec
        self.lit = False
        self._last_press = -1e9

    def update(self, red_px: int, t: float) -> bool:
        """True exactly once per press: on the frame the LED turns on."""
        if not self.lit and red_px >= self.on_px:
            self.lit = True
            if t - self._last_press >= self.min_gap:  # ignore bounce / double taps
                self._last_press = t
                return True
        elif self.lit and red_px <= self.off_px:
            self.lit = False
        return False

# --- follow a moved balance --------------------------------------------------
# Balances get nudged often (shifted, turned, slid along the bench), while the digit
# layout only fits within ~1 px. So each read, the reference's edge map around the
# display (buttons, logo, panel outline) is found in the frame: coarse rotate+slide,
# then affine ECC; the display is resampled back into reference coordinates.
# v1 measured on frames moved +-50 px / +-5 deg: read 117 / rejected 7 / wrong 0 of 124.

PAD_X, PAD_Y = 60, 40   # face context around the ROI used for matching
ANGLES = np.arange(-6.0, 6.01, 1.0)
MIN_SCORE = 0.4         # below this the match is not trusted (display blocked / gone)
PATCH_PAD = 6           # margin kept around the ROI in the aligned patch
# Each search is centred on the last placement found (balances creep further over days than
# any one search reaches: seen live 147 px). Lost: also around the reference, this far, at
# most every WIDE_EVERY_SEC (it is ~2x slower; a hand over the display must not stall the loop).
WIDE_SEARCH = 250
WIDE_EVERY_SEC = 2.0


def _edges(img: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    gray = cv2.GaussianBlur(gray, (5, 5), 0).astype(np.float32)
    return cv2.magnitude(cv2.Sobel(gray, cv2.CV_32F, 1, 0), cv2.Sobel(gray, cv2.CV_32F, 0, 1))


def _h(m: np.ndarray) -> np.ndarray:
    return np.vstack([m, [0, 0, 1]]).astype(np.float64)


def _translate(x: float, y: float) -> np.ndarray:
    return np.array([[1, 0, x], [0, 1, y], [0, 0, 1]], np.float64)


class Registrar:
    """Reference edge template of one balance and its last known placement."""

    def __init__(self, reference: np.ndarray, scale: Scale):
        self.scale = scale
        x, y, w, h = scale.roi
        self.x0, self.y0 = x - PAD_X, y - PAD_Y
        self.tmpl = _edges(reference[self.y0:y + h + PAD_Y, self.x0:x + w + PAD_X])
        self.warp = np.eye(2, 3)          # reference -> frame
        self.logged = np.eye(2, 3)        # placement last reported in the log
        self.score = 0.0                  # latest match score
        self.wide_at = -1e9               # time of the last wide search

    def find(self, frame: np.ndarray, around: np.ndarray | None = None,
             n: int | None = None) -> tuple[np.ndarray, float]:
        """Affine 2x3 mapping reference coords -> frame coords, and the match score.
        Searched within n px (default scale.search) of placement `around` (default the last one)."""
        th, tw = self.tmpl.shape
        n = n or self.scale.search
        dx, dy = np.rint((self.warp if around is None else around) @ [self.x0, self.y0, 1.0]
                         - [self.x0, self.y0]).astype(int)
        sx0 = int(np.clip(self.x0 + dx - n, 0, max(0, frame.shape[1] - tw - 2 * n)))
        sy0 = int(np.clip(self.y0 + dy - n, 0, max(0, frame.shape[0] - th - 2 * n)))
        area = _edges(frame[sy0:sy0 + th + 2 * n, sx0:sx0 + tw + 2 * n])

        best = (-1.0, 0.0, 0, 0)
        for a in ANGLES:  # coarse: rotate the template about its centre, slide it
            rot = cv2.getRotationMatrix2D((tw / 2, th / 2), float(a), 1.0)
            res = cv2.matchTemplate(area, cv2.warpAffine(self.tmpl, rot, (tw, th)), cv2.TM_CCOEFF_NORMED)
            _, s, _, (bx, by) = cv2.minMaxLoc(res)
            if s > best[0]:
                best = (s, a, bx, by)
        score, a, bx, by = best
        # template point p sits at frame R_a p + offset; reference x = p + (x0, y0)
        rot = _h(cv2.getRotationMatrix2D((tw / 2, th / 2), float(a), 1.0))
        warp = _translate(sx0 + bx, sy0 + by) @ rot @ _translate(-self.x0, -self.y0)

        # fine: affine ECC between the template and the frame resampled through `warp`
        to_patch = warp @ _translate(self.x0, self.y0)
        patch = cv2.warpAffine(frame, to_patch[:2], (tw, th),
                               flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP)
        ecc = np.eye(2, 3, dtype=np.float32)
        try:
            _, ecc = cv2.findTransformECC(self.tmpl, _edges(patch), ecc, cv2.MOTION_AFFINE,
                                          (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 100, 1e-5),
                                          None, 5)
            warp = warp @ _translate(self.x0, self.y0) @ _h(ecc) @ _translate(-self.x0, -self.y0)
        except cv2.error:
            pass  # ECC did not converge: keep the coarse placement
        return warp[:2], float(score)

    def align(self, frame: np.ndarray) -> np.ndarray:
        """The display area resampled into reference coordinates; the ROI sits at
        (PATCH_PAD, PATCH_PAD) in it. Keeps the last good placement if not found."""
        warp, self.score = self.find(frame)
        if self.score < MIN_SCORE and time.monotonic() - self.wide_at >= WIDE_EVERY_SEC:
            self.wide_at = time.monotonic()
            wide, score = self.find(frame, np.eye(2, 3), WIDE_SEARCH)
            if score > self.score:
                warp, self.score = wide, score
        if self.score >= MIN_SCORE:
            self.warp = warp
            self._log_move()
        x, y, w, h = self.scale.roi
        to_patch = _h(self.warp) @ _translate(x - PATCH_PAD, y - PATCH_PAD)
        return cv2.warpAffine(frame, to_patch[:2], (w + 2 * PATCH_PAD, h + 2 * PATCH_PAD),
                              flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                              borderMode=cv2.BORDER_REPLICATE)

    def describe(self, warp: np.ndarray) -> tuple[float, float, float, float]:
        """(dx, dy, rotation deg, horizontal scale) of the ROI centre vs the reference."""
        x, y, w, h = self.scale.roi
        c = np.array([x + w / 2, y + h / 2, 1.0])
        dx, dy = warp @ c - c[:2]
        return float(dx), float(dy), float(np.degrees(np.arctan2(warp[1, 0], warp[0, 0]))), \
            float(np.hypot(warp[0, 0], warp[1, 0]))

    def _log_move(self) -> None:
        new, old = self.describe(self.warp), self.describe(self.logged)
        if (max(abs(new[0] - old[0]), abs(new[1] - old[1])) > 3 or abs(new[2] - old[2]) > 1
                or abs(new[3] - old[3]) > 0.015):
            log.info("%s balance moved: offset (%+.0f, %+.0f) px, rotation %+.1f deg, "
                     "width x%.3f vs reference (match %.2f)", self.scale.name, *new, self.score)
            self.logged = self.warp.copy()

# --- seven-segment display ---------------------------------------------------

#            a  b  c  d  e  f  g
SEGMENTS = {
    (1, 1, 1, 1, 1, 1, 0): "0",
    (0, 1, 1, 0, 0, 0, 0): "1",
    (1, 1, 0, 1, 1, 0, 1): "2",
    (1, 1, 1, 1, 0, 0, 1): "3",
    (0, 1, 1, 0, 0, 1, 1): "4",
    (1, 0, 1, 1, 0, 1, 1): "5",
    (1, 0, 1, 1, 1, 1, 1): "6",
    (0, 0, 1, 1, 1, 1, 1): "6",  # 6 without top bar
    (1, 1, 1, 0, 0, 0, 0): "7",
    (1, 1, 1, 0, 0, 1, 0): "7",  # 7 with left post
    (1, 1, 1, 1, 1, 1, 1): "8",
    (1, 1, 1, 1, 0, 1, 1): "9",
    (1, 1, 1, 0, 0, 1, 1): "9",  # 9 without bottom bar
}
# Segment sampling zones as (x0, x1, y0, y1) fractions of the digit cell.
ZONES = {
    "a": (0.35, 0.65, 0.00, 0.25),
    "b": (0.65, 1.00, 0.12, 0.45),
    "c": (0.65, 1.00, 0.55, 0.88),
    "d": (0.35, 0.65, 0.75, 1.00),
    "e": (0.00, 0.35, 0.55, 0.88),
    "f": (0.00, 0.35, 0.12, 0.45),
    "g": (0.35, 0.65, 0.35, 0.65),
}
ON_FRACTION = 0.25     # share of a zone that must be ink for the segment to be on
# A lit horizontal bar fills at least one whole row of its narrow zone; stroke ends,
# blur or the inside of a "0" never do (averaging made 0/8 and 1/7 too close to call).
HORIZONTAL = ("a", "d", "g")
BAR_FRACTION = 0.75    # share of the fullest row in a horizontal zone that must be ink
BLANK_FRACTION = 0.03  # a cell with less ink than this is an unlit leading digit
UPSCALE = 4.0
_THRESHOLDS = np.array([BAR_FRACTION if k in HORIZONTAL else ON_FRACTION for k in ZONES])


def prepare_display(crop: np.ndarray) -> np.ndarray:
    """Binary image with the dark LCD segments white on black. Darkest channel +
    black-hat keeps thin dark strokes and drops lighting gradients and glare."""
    gray = crop.min(axis=2)
    gray = cv2.resize(gray, None, fx=UPSCALE, fy=UPSCALE, interpolation=cv2.INTER_CUBIC)
    k = max(15, (gray.shape[0] // 2) | 1)  # wider than a segment stroke
    blackhat = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT,
                                cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    _, binary = cv2.threshold(blackhat, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # Bezel edges are long horizontal lines; a segment is never wider than ~1/6 of the ROI.
    line_len = max(3, binary.shape[1] // 4)
    lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN,
                             cv2.getStructuringElement(cv2.MORPH_RECT, (line_len, 1)))
    lines = cv2.dilate(lines, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 5)))
    return cv2.subtract(binary, lines)


def digit_cells(mask: np.ndarray, scale: Scale) -> list[np.ndarray]:
    """Undo the italic slant, then cut the digit box into scale.digits equal cells."""
    h, w = mask.shape
    m = np.float32([[1, scale.slant, -scale.slant * h / 2], [0, 1, 0]])
    upright = cv2.warpAffine(mask, m, (w, h), flags=cv2.INTER_NEAREST)
    fx0, fx1, fy0, fy1 = scale.digit_box
    n = scale.digits
    x0, cw = fx0 * w, (fx1 - fx0) * w / n
    cells = []
    for i in range(n):
        dy = scale.tilt * ((i + 0.5) / n - 0.5) * h  # display sits slightly rotated
        y0, y1 = max(0, int(fy0 * h + dy)), min(h, int(fy1 * h + dy))
        cells.append(upright[y0:y1, int(x0 + i * cw):int(x0 + (i + 1) * cw)])
    return cells


def read_cell(cell: np.ndarray) -> str | None:
    """One digit, " " for an unlit cell, None if the segments match no digit exactly."""
    if cell.size == 0 or cell.mean() / 255 < BLANK_FRACTION:
        return " "
    h, w = cell.shape
    ink = []
    for name, (x0, x1, y0, y1) in ZONES.items():
        zone = cell[int(y0 * h):max(int(y1 * h), int(y0 * h) + 1),
                    int(x0 * w):max(int(x1 * w), int(x0 * w) + 1)] / 255
        ink.append(zone.mean(axis=1).max() if name in HORIZONTAL else zone.mean())
    pattern = tuple(int(v) for v in np.array(ink) > _THRESHOLDS)
    if sum(pattern) < 2:  # every digit has >= 2 segments; 0-1 = bezel edge / dust
        return " "
    return SEGMENTS.get(pattern)


def read_at(patch: np.ndarray, scale: Scale, dx: int = 0, dy: int = 0) -> str | None:
    """Display text like "12.345" from an aligned patch, ROI shifted by (dx, dy), or None."""
    _, _, w, h = scale.roi
    x = y = PATCH_PAD
    mask = prepare_display(patch[y + dy:y + dy + h, x + dx:x + dx + w])
    cells = [read_cell(c) for c in digit_cells(mask, scale)]
    if None in cells:
        return None
    digits = "".join(cells).lstrip()
    # Unlit cells only on the left, and at least one digit before the point ("0.237").
    d = scale.decimals
    if " " in digits or len(digits) <= d:
        return None
    text = f"{digits[:-d]}.{digits[-d:]}"
    if scale.minus:  # below zero after a tare: never log -0.0004 as 0.0004
        mh, mw = mask.shape
        x0, x1, y0, y1 = scale.minus
        if (mask[int(y0 * mh):int(y1 * mh), int(x0 * mw):int(x1 * mw)] / 255).mean(axis=1).max() >= MINUS_FRACTION:
            text = "-" + text
    return text if float(text) <= scale.capacity else None


def read_weight(patch: np.ndarray, scale: Scale, radius: int = 2, min_votes: int = 3) -> str | None:
    """Weight on the display in an aligned patch (Registrar.align), e.g. "0.066",
    or None if not read with confidence.

    The layout fits within ~1 px, so read every shift within `radius` px and take the
    value most shifts agree on (v1: ~30 wrong in 336 single reads 1-2 px off, 4 this way).
    Rejected if too few votes, a close runner-up, or the centre read disagrees.
    """
    votes: Counter[str] = Counter()
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if (text := read_at(patch, scale, dx, dy)) is not None:
                votes[text] += 1
    if not votes:
        return None
    (text, n), *rest = votes.most_common(2)
    centre = read_at(patch, scale)
    if n < min_votes or (rest and rest[0][1] * 2 >= n) or (centre is not None and centre != text):
        return None
    return text
