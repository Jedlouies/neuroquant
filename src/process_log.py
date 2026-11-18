"""Small timestamped console logger for development and support diagnostics."""

from datetime import datetime
import sys
import threading


def log_process(stage: str, message: str, *, error: bool = False) -> None:
    # Use a numeric local offset instead of the OS timezone label.
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    level = "ERROR" if error else "INFO"
    stream = sys.stderr if error else sys.stdout
    with threading.RLock():
        print(f"[{timestamp}] [{level}] [{stage}] {message}", file=stream, flush=True)
