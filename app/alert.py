"""Alerts to Telegram and/or email when Live Balance needs a person (run.py calls Monitor).

Settings in .env (either channel may be left out; with neither, alerts only go to run.log):
    LB_ALERT_TELEGRAM_TOKEN   bot token from @BotFather
    LB_ALERT_TELEGRAM_CHAT    chat id(s), comma separated (python app/alert.py --find-chat)
    LB_ALERT_SMTP_HOST, LB_ALERT_SMTP_PORT (465 = SSL, 587 = STARTTLS)
    LB_ALERT_SMTP_USER, LB_ALERT_SMTP_PASSWORD (Gmail: an app password)
    LB_ALERT_EMAIL_TO         address(es), comma separated

Usage:  python app/alert.py "text"      send a test alert now
        python app/alert.py --find-chat list chats that messaged the bot (to get the chat id)
"""
from __future__ import annotations

import json
import logging
import smtplib
import socket
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]  # project folder (this file is in app/)
LIVE_DIR = ROOT / "data" / "live"
CHECK_EVERY_SEC = 10
REPEAT_SEC = 6 * 3600     # a problem that lasts is reported again this often
EVENT_QUIET_SEC = 1800    # the same one-off event (e.g. a crash restart) at most this often
CAMERA_GRACE_SEC = 120    # no camera picture this long = alert
DISPLAY_GRACE_SEC = 600   # balance display not found this long = alert (hands block it briefly all day)
DAILY_AT = 9              # hour of the daily "all OK" message: its absence means the PC or app is down

log = logging.getLogger("alert")


def config() -> dict:
    return {k: v.strip() for k, v in dotenv_values(ROOT / ".env").items() if k.startswith("LB_ALERT_") and v}


def _telegram(cfg: dict, text: str) -> None:
    for chat in cfg["LB_ALERT_TELEGRAM_CHAT"].split(","):
        data = urllib.parse.urlencode({"chat_id": chat.strip(), "text": text}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{cfg['LB_ALERT_TELEGRAM_TOKEN']}/sendMessage",
                               data, timeout=15).read()


def _email(cfg: dict, text: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = "Live Balance: " + text.splitlines()[0][:80]
    msg["From"], msg["To"] = cfg["LB_ALERT_SMTP_USER"], cfg["LB_ALERT_EMAIL_TO"]
    msg.set_content(text)
    port = int(cfg.get("LB_ALERT_SMTP_PORT", "465"))
    if port == 465:
        smtp = smtplib.SMTP_SSL(cfg["LB_ALERT_SMTP_HOST"], port, timeout=20, context=ssl.create_default_context())
    else:
        smtp = smtplib.SMTP(cfg["LB_ALERT_SMTP_HOST"], port, timeout=20)
        smtp.starttls(context=ssl.create_default_context())
    with smtp:
        smtp.login(cfg["LB_ALERT_SMTP_USER"], cfg["LB_ALERT_SMTP_PASSWORD"])
        smtp.send_message(msg)


def send_now(text: str) -> list[str]:
    """Send to every configured channel; returns what went wrong (never raises)."""
    text = f"{text}\n({socket.gethostname()}, {datetime.now():%d-%m-%Y %H:%M})"
    cfg, errors = config(), []
    channels = [("Telegram", _telegram, ("LB_ALERT_TELEGRAM_TOKEN", "LB_ALERT_TELEGRAM_CHAT")),
                ("email", _email, ("LB_ALERT_SMTP_HOST", "LB_ALERT_SMTP_USER", "LB_ALERT_SMTP_PASSWORD",
                                   "LB_ALERT_EMAIL_TO"))]
    for name, fn, keys in channels:
        if all(k in cfg for k in keys):
            try:
                fn(cfg, text)
            except Exception as e:  # no internet, wrong token/password: logged, the app carries on
                errors.append(f"{name}: {e}")
    if errors:
        log.warning("alert not delivered: %s", "; ".join(errors))
    return errors


def send(text: str) -> None:
    """Log the alert and send it in the background: a slow network must never hold up the launcher."""
    log.warning("ALERT: %s", text.replace("\n", " | "))
    threading.Thread(target=send_now, args=(text,), daemon=True, name="alert").start()


def presses_today() -> str:
    today, parts = date.today().isoformat(), []
    for f in sorted(ROOT.glob("data/presses*.csv")):
        name = f.stem.removeprefix("presses").removeprefix("_") or "centre"
        with f.open(encoding="utf-8") as fh:
            parts.append(f"{name} {sum(line.startswith(today) for line in fh)}")
    return ", ".join(parts) or "none"


class Monitor:
    """Turns what the launcher sees and the recorder's live status files into alerts:
    one when a problem has lasted its grace time, again every REPEAT_SEC while it lasts,
    and one when it clears."""

    def __init__(self, send=send):
        self.send = send
        self.since: dict[str, float] = {}     # problem -> first seen
        self.told: dict[str, float] = {}      # problem -> last alerted
        self.texts: dict[str, str] = {}       # problem -> its short label
        self.events: dict[str, float] = {}    # event text -> last sent
        self.checked = 0.0
        self.daily_done = date.today() if datetime.now().hour >= DAILY_AT else None

    def event(self, text: str, now: float | None = None) -> None:
        """Something that happened once (a restart): alert at once, but the same text at most
        every EVENT_QUIET_SEC so a crash loop cannot flood the phone."""
        now = now or time.time()
        if now - self.events.get(text, -1e18) >= EVENT_QUIET_SEC:
            self.events[text] = now
            self.send(text)

    def check(self, now: float | None = None) -> None:
        now = now or time.time()
        if now - self.checked < CHECK_EVERY_SEC:
            return
        self.checked = now
        problems = {}
        for f in sorted(LIVE_DIR.glob("*/status.json")):
            name = f.parent.name.capitalize()
            try:
                s = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue  # being swapped this instant; the hung-recorder watchdog covers a dead one
            if now - f.stat().st_mtime > 30:
                continue  # stale: the recorder is down, reported by the launcher's own events
            if s.get("frame_age_sec") is None or s["frame_age_sec"] > 10:
                problems["camera"] = (CAMERA_GRACE_SEC, "Camera gives no picture",
                                      "presses are NOT being recorded. Check the camera/NVR and its network cable.")
            elif not s.get("display_found"):
                problems[f"display-{name}"] = (DISPLAY_GRACE_SEC, f"{name} balance display not found",
                                               f"for {DISPLAY_GRACE_SEC // 60} min: view blocked or balance moved. "
                                               "Its presses are logged without a weight.")
            if s.get("batch_error"):
                problems[f"batch-{name}"] = (0, f"{name} balance batch reading is OFF",
                                             f"{s['batch_error']}: its cycles will not be counted.")
        for key, (grace, label, detail) in problems.items():
            first = self.since.setdefault(key, now)
            if now - first >= grace and now - self.told.get(key, -1e18) >= REPEAT_SEC:
                self.told[key], self.texts[key] = now, label
                self.send(f"PROBLEM: {label} - {detail}")
        for key in list(self.since):
            if key not in problems:
                del self.since[key]
                if self.told.pop(key, None) is not None:
                    self.send(f"OK again: {self.texts.pop(key)} - fixed.")
        if self.daily_done != date.today() and datetime.fromtimestamp(now).hour >= DAILY_AT:
            self.daily_done = date.today()
            open_ = [self.texts[k] for k in self.told]
            self.send("Daily check: Live Balance running. Presses today so far: " + presses_today()
                      + (". Open problems: " + "; ".join(open_) if open_ else ". No open problems."))


def find_chats() -> None:
    cfg = config()
    if "LB_ALERT_TELEGRAM_TOKEN" not in cfg:
        sys.exit("Put LB_ALERT_TELEGRAM_TOKEN in .env first.")
    url = f"https://api.telegram.org/bot{cfg['LB_ALERT_TELEGRAM_TOKEN']}/getUpdates"
    try:
        updates = json.loads(urllib.request.urlopen(url, timeout=15).read())["result"]
    except urllib.error.HTTPError as e:
        sys.exit("Telegram rejected the token (401): copy it again from @BotFather (/mybots - your bot - "
                 "API Token) into .env." if e.code == 401 else f"Telegram error {e.code}.")
    chats = {(u.get("message") or u.get("channel_post") or {}).get("chat", {}).get("id"):
             (u.get("message") or u.get("channel_post") or {}).get("chat", {}) for u in updates}
    chats.pop(None, None)
    if not chats:
        sys.exit("No chats yet: send any message to the bot (or add it to a group and post there), then retry.")
    for cid, c in chats.items():
        print(f"chat id {cid}: {c.get('title') or c.get('first_name', '')} ({c.get('type')})")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if sys.argv[1:] == ["--find-chat"]:
        find_chats()
    else:
        errs = send_now(" ".join(sys.argv[1:]) or "Test alert from Live Balance.")
        cfg = config()
        print("No alert channel set up in .env." if not any(k in cfg for k in
              ("LB_ALERT_TELEGRAM_TOKEN", "LB_ALERT_SMTP_HOST")) else "\n".join(errs) or "Sent.")
