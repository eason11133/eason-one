from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def _taskkill_tree(pid: int) -> None:
    subprocess.run(
        ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _stop_server(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return

    print(f"\nStopping Eason One server process tree (PID {proc.pid})...", flush=True)

    if os.name == "nt":
        # The Flask child is launched as its own Windows process group.  This
        # keeps the user's Ctrl+C on the launcher, where KeyboardInterrupt is
        # deterministic, instead of racing PowerShell/Flask/Werkzeug handlers.
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
            proc.wait(timeout=4)
            return
        except (AttributeError, OSError, subprocess.TimeoutExpired):
            pass

        _taskkill_tree(proc.pid)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return

    proc.terminate()
    try:
        proc.wait(timeout=4)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description="Eason One Headquarters process owner")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    command = [
        sys.executable,
        "-m",
        "flask",
        "--app",
        "run.py",
        "run",
        "--host",
        "127.0.0.1",
        "--port",
        str(args.port),
    ]

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

    proc = subprocess.Popen(
        command,
        cwd=str(repo_root),
        creationflags=creationflags,
    )

    print(f"Server PID   : {proc.pid}", flush=True)
    print("Ctrl+C owner : Eason launcher (Flask isolated process group)", flush=True)

    try:
        while True:
            return_code = proc.poll()
            if return_code is not None:
                return return_code
            time.sleep(0.20)
    except KeyboardInterrupt:
        _stop_server(proc)
        return 130
    finally:
        if proc.poll() is None:
            _stop_server(proc)


if __name__ == "__main__":
    raise SystemExit(main())
