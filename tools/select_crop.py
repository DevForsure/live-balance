"""Set up one balance's view: its crop (config/crop[_left].json) and switch LED (config/led[_left].json).

Usage:  python tools/select_crop.py left                 # grab a frame from the camera
        python tools/select_crop.py left path/to/img.png # use a saved 1920-wide frame
        (balance: left, centre or right; centre if left out)
Step 1: drag a box around the balance, its switch and the batch sheet;
        ENTER/SPACE saves, C keeps the current crop.
Step 2: click the switch LED; ENTER saves, ESC keeps the current LED.
The LED is saved relative to where the balance was in the reference frame, so it keeps
following the balance when the camera or the bench moves afterwards.
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import cv2
import numpy as np
from dotenv import load_dotenv

import balance
from recorder import RTSPStream, normalize_frame

args = sys.argv[1:]
if args and args[0] in balance.SCALES:
    scale = balance.SCALES[args.pop(0)]
elif args and not Path(args[0]).exists():
    sys.exit(f"Unknown balance {args[0]!r}: use one of {', '.join(balance.SCALES)}")
else:
    scale = balance.SCALES[balance.DEFAULT]
print(f"Setting up the {scale.name} balance ({scale.balance_id})")
if args:
    frame = cv2.imread(args[0])
else:
    load_dotenv(ROOT / ".env")
    with RTSPStream(os.getenv("LIVE_BALANCE_RTSP_URL", "")) as stream:
        frame = None
        for _ in range(100):
            frame, _ = stream.read()
            if frame is not None:
                break
            time.sleep(0.2)
if frame is None:
    sys.exit("Could not get a frame.")
# Everything is stored in normalized-frame coordinates, same as the recorder works in.
frame = normalize_frame(frame)

# --- step 1: crop --------------------------------------------------------------
shown = frame.copy()
x, y, w, h = scale.load_crop()
cv2.rectangle(shown, (x, y), (x + w, y + h), (0, 200, 255), 2)  # current crop, orange
shrink = min(1.0, 1280 / frame.shape[1])  # display scaled down, saved at full resolution
win = f"1/2 {scale.name} crop: drag a box round balance, switch and sheet - ENTER save, C keep current (orange)"
box = cv2.selectROI(win, cv2.resize(shown, None, fx=shrink, fy=shrink), showCrosshair=True)
cv2.destroyAllWindows()
if box[2] and box[3]:
    crop = {k: int(round(v / shrink)) for k, v in zip("xywh", box)}
    scale.crop_file.parent.mkdir(exist_ok=True)
    scale.crop_file.write_text(json.dumps(crop, indent=2), encoding="utf-8")
    x, y, w, h = crop["x"], crop["y"], crop["w"], crop["h"]
    print(f"Crop saved: {crop}")
else:
    print("Crop unchanged.")

# --- step 2: switch LED --------------------------------------------------------
registrar = balance.Registrar(cv2.imread(str(scale.reference_frame)), scale)
registrar.align(frame)
if registrar.score < balance.MIN_SCORE:
    print("WARNING: the balance display was not found in this frame (blocked?), so the LED "
          "cannot be tied to it; it is saved as if the balance had not moved.")
    registrar.warp = np.eye(2, 3)
zoom = min(3.0, 900 / h)  # show the crop enlarged for an exact click
area = cv2.resize(frame[y:y + h, x:x + w], None, fx=zoom, fy=zoom, interpolation=cv2.INTER_LINEAR)
(cx, cy), (lw, lh) = balance.led_centre(scale, registrar.warp), balance.LED_WINDOW
cv2.rectangle(area, (int((cx - lw // 2 - x) * zoom), int((cy - lh // 2 - y) * zoom)),
              (int((cx + lw // 2 - x) * zoom), int((cy + lh // 2 - y) * zoom)), (0, 200, 255), 2)
clicked = []


def on_click(event, px, py, *_):
    if event == cv2.EVENT_LBUTTONDOWN:
        clicked[:] = [(x + px / zoom, y + py / zoom)]
        img = area.copy()
        cv2.drawMarker(img, (px, py), (0, 0, 255), cv2.MARKER_CROSS, 28, 2)
        cv2.imshow(win, img)


win = f"2/2 {scale.name} switch LED: click the LED - ENTER save, ESC keep current (orange box)"
cv2.imshow(win, area)
cv2.setMouseCallback(win, on_click)
while (key := cv2.waitKey(50) & 0xFF) not in (13, 32, 27):
    if cv2.getWindowProperty(win, cv2.WND_PROP_VISIBLE) < 1:  # window closed: keep current
        key = 27
        break
cv2.destroyAllWindows()
if key == 27 or not clicked:
    sys.exit("LED unchanged.")
# frame point -> reference point: undo the balance's movement (warp maps reference -> frame)
A, b = registrar.warp[:, :2], registrar.warp[:, 2]
ref = np.linalg.solve(A, np.array(clicked[0]) - b)
scale.led_file.write_text(json.dumps({"ref": [int(round(v)) for v in ref],
                                        "frame": [int(round(v)) for v in clicked[0]]}, indent=2))
print(f"LED saved: at {tuple(int(v) for v in clicked[0])} in this frame "
      f"({tuple(int(round(v)) for v in ref)} in the reference). Restart the app to use it.")
