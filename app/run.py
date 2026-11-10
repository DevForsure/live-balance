"""Starts the whole Live Balance v2 app: the recorder (recorder.py) and the batch page
(web.py), each in its own process, so a crash in one never stops the other.
Either one that stops is restarted after RESTART_DELAY_SEC.

Usage:  python app/run.py           start (only one copy can run: a second exits at once)
        python app/run.py --stop    stop a running app
live-balance-start.bat runs this without a console window; install_autostart.bat starts it at logon.
Output of each part goes to data/logs/<part>.log.
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import alert

ROOT = Path(__file__).resolve().parents[1]  # project folder (this file is in app/)
LOG_DIR = ROOT / "data" / "logs"
PID_FILE = ROOT / "data" / "run.pid"
LOCK_PORT = 8089           # held on 127.0.0.1 while running: two recorders would log every press twice
RESTART_DELAY_SEC = 5
LOG_MAX_BYTES = 5_000_000  # a part's log is moved to <part>.log.1 when it grows past this
PARTS = {"recorder": "app/recorder.py", "web": "app/web.py"}  # relative to ROOT
LIVE_DIR = ROOT / "data" / "live"  # the recorder refreshes <balance>/status.json every second
STALE_SEC = 120  # no status refreshed this long = recorder hung (seen live: 50 min frozen, still "running")
REBOOT_AFTER = 3  # hangs in a row (~6 min) = restarting is not clearing it: seen live, only a reboot did
DUMP_WAIT_SEC = 40  # longest a stack dump may delay the kill: Windows being stuck can stick the dump too
PY_SPY = ROOT / ".venv" / "Scripts" / "py-spy.exe"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

log = logging.getLogger("run")


def launch(name: str) -> subprocess.Popen:
    path = LOG_DIR / f"{name}.log"
    if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
        path.replace(path.with_suffix(".log.1"))
    out = path.open("a", encoding="utf-8")
    log.info("starting %s", name)
    return subprocess.Popen([sys.executable, PARTS[name]], cwd=ROOT, stdout=out,
                            stderr=subprocess.STDOUT, creationflags=NO_WINDOW)


def kill_tree(p: subprocess.Popen) -> None:
    """The .venv pythonw.exe is a starter that runs the real Python as its child, so
    terminate() alone leaves the real recorder running; /T ends both."""
    subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)


def newest_status() -> float:
    return max((f.stat().st_mtime for f in LIVE_DIR.glob("*/status.json")), default=0.0)


def recorder_stale(since: float) -> bool:
    """True if a recorder started at `since` has not refreshed any live status for STALE_SEC."""
    return time.time() - max(newest_status(), since) > STALE_SEC


def write_stacks(pid: int, path: Path) -> None:
    """Where the recorder is stuck, into `path`. pid is the .venv starter: py-spy needs its
    child, the real Python. --native also shows a stack stuck inside Windows."""
    with path.open("w", encoding="utf-8") as out:
        try:
            found = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ParentProcessId={pid}').ProcessId"],
                capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW).stdout.split()
            out.write(f"starter {pid}, real Python {found}\n\n")
            for child in found or [str(pid)]:
                for extra in ([], ["--native"]):
                    out.write(f"--- py-spy dump --pid {child} {' '.join(extra)}\n")
                    out.flush()
                    subprocess.run([PY_SPY, "dump", "--pid", child, *extra], stdout=out, stderr=subprocess.STDOUT,
                                   timeout=10, creationflags=NO_WINDOW)
        except Exception as e:  # a failed dump says so in the file: "stuck too hard to read" is a finding as well
            out.write(f"\ndump failed: {e}\n")


def save_stacks(p: subprocess.Popen) -> None:
    path = LOG_DIR / f"hang-{datetime.now():%Y%m%d-%H%M%S}.txt"
    t = threading.Thread(target=write_stacks, args=(p.pid, path), daemon=True, name="stack-dump")
    t.start()
    t.join(DUMP_WAIT_SEC)
    log.warning("stacks of the hung recorder: %s%s", path, " (dump still stuck, killing anyway)" if t.is_alive() else "")


def stop() -> None:
    if not PID_FILE.exists():
        sys.exit("Live Balance is not running.")
    pid = PID_FILE.read_text().strip()
    # /T: the recorder and the batch page are child processes of the launcher
    subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True)
    PID_FILE.unlink(missing_ok=True)
    print("Live Balance stopped.")


def main() -> None:
    if "--stop" in sys.argv:
        return stop()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(LOG_DIR / "run.log", encoding="utf-8")]
    if sys.stderr:  # None when started without a console (pythonw)
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        log.info("Live Balance is already running - not starting a second copy.")
        return
    PID_FILE.write_text(str(os.getpid()))
    monitor = alert.Monitor()
    monitor.event("Live Balance started (PC or app restarted). If nobody restarted it, check the PC.")
    procs = {name: launch(name) for name in PARTS}
    started_at = {name: time.time() for name in PARTS}
    stopped_at: dict[str, float] = {}
    hung: set[str] = set()
    hangs = 0  # recorder hangs in a row, with no live status from any restart in between
    try:
        while True:
            time.sleep(1)
            try:  # a failed restart (e.g. its log file busy) is retried next round, never ends the launcher
                for name, p in procs.items():
                    if p.poll() is None:
                        if name == "recorder" and recorder_stale(started_at[name]):
                            hangs += 1
                            log.warning("recorder hung (no live status for %d s, %d in a row) - restarting it",
                                        STALE_SEC, hangs)
                            monitor.event(f"PROBLEM: recorder hung for {STALE_SEC // 60} min - presses were NOT "
                                          "recorded meanwhile. Restarted it automatically; re-weigh anything "
                                          "weighed in the last few minutes.")
                            if hangs >= REBOOT_AFTER:
                                monitor.event(f"REBOOT THE PC: recorder hung {REBOOT_AFTER} times in a row, restarting "
                                              "it is not helping. Presses are NOT being recorded until the PC is "
                                              "rebooted. Re-weigh anything weighed since the first alert.")
                            if hangs <= REBOOT_AFTER:  # a long hang needs no more dumps of the same thing
                                save_stacks(p)
                            kill_tree(p)
                            hung.add(name)  # its exit next round is this restart, not a crash
                            started_at[name] = time.time()  # one that will not die counts as hung again later
                        elif name == "recorder" and newest_status() > started_at[name]:
                            hangs = 0
                        continue
                    if name not in stopped_at:
                        log.warning("%s stopped (exit code %s) - restarting in %d s", name, p.returncode,
                                    RESTART_DELAY_SEC)
                        if name not in hung:
                            monitor.event(f"{'Recorder' if name == 'recorder' else 'Batch page'} crashed and was "
                                          f"restarted automatically. Details: data/logs/{name}.log")
                        hung.discard(name)
                        stopped_at[name] = time.time()
                    elif time.time() - stopped_at[name] >= RESTART_DELAY_SEC:
                        del stopped_at[name]
                        procs[name] = launch(name)
                        started_at[name] = time.time()
                monitor.check()
            except Exception:
                log.exception("launcher check failed; trying again")
    except KeyboardInterrupt:
        pass
    except BaseException:  # pythonw has no console: leave a trace of whatever ends the launcher
        log.exception("launcher stopped unexpectedly")
        raise
    finally:
        for p in procs.values():
            kill_tree(p)
        PID_FILE.unlink(missing_ok=True)
        log.info("Live Balance stopped.")


if __name__ == "__main__":
    main()
