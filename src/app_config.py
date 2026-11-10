"""Load desktop application settings from the project or installation .env file."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _env_file() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / ".env"
    return Path(__file__).resolve().parents[1] / ".env"


def _secure_permissions(path: Path) -> None:
    """Restrict the environment file to its owner where supported."""
    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return
    try:
        import subprocess
        account = os.environ.get("USERNAME")
        if account:
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{account}:(F)"],
                capture_output=True, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    except Exception:
        pass


def load_env() -> None:
    """Load simple KEY=VALUE entries without adding a runtime dependency."""
    path = _env_file()
    if not path.is_file():
        return
    try:
        values = {}
        for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key:
                values[key] = value
        for key, value in values.items():
            os.environ[key] = value
        _secure_permissions(path)
    except OSError:
        return


def get_setting(name: str, default: str = "") -> str:
    load_env()
    return os.environ.get(name, default).strip()


load_env()
