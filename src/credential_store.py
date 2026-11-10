"""Credential access backed by the application's root .env file."""

import os
import tempfile
from pathlib import Path

from app_config import get_setting


def _write_env_value(name: str, value: str) -> None:
    """Update one root .env entry while preserving unrelated settings."""
    path = Path(__file__).resolve().parents[1] / ".env"
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    except OSError as error:
        raise OSError(f"Could not read application .env file at {path}: {error}") from error
    replacement = f"{name}={value}" if value else None
    updated = []
    found = False
    for line in lines:
        if line.strip().partition("=")[0].strip() == name:
            if replacement is not None:
                updated.append(replacement)
            found = True
        else:
            updated.append(line)
    if not found and replacement is not None:
        updated.append(replacement)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".env-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as env_file:
            env_file.write("\n".join(updated).rstrip() + "\n")
        os.replace(temporary_name, path)
        from app_config import _secure_permissions
        _secure_permissions(path)
        os.environ[name] = value
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def read_api_key() -> str:
    return get_setting("OPENAI_API_KEY")


def save_api_key(api_key: str):
    _write_env_value("OPENAI_API_KEY", api_key.strip())


def _folder_password_target(username: str) -> str:
    import hashlib
    account_key = hashlib.sha256((username or "default").strip().lower().encode("utf-8")).hexdigest()[:24]
    return f"MOBILE_FOLDER_PASSWORD_{account_key.upper()}"


def read_mobile_folder_password(username: str) -> str:
    return get_setting(_folder_password_target(username))


def save_mobile_folder_password(username: str, password: str):
    _write_env_value(_folder_password_target(username), password)
