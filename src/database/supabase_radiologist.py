"""Supabase Storage helpers for radiologist scan intake in the existing neuro bucket."""
import mimetypes
import os
import re
from urllib.parse import quote
from pathlib import Path

import requests

from database import supabase_storage
from app_config import get_setting


SUPABASE_URL = supabase_storage.SUPABASE_URL.rstrip("/")
SUPABASE_KEY = supabase_storage.SUPABASE_KEY
BUCKET = get_setting("SUPABASE_RADIOLOGIST_BUCKET", "neuro")
SCAN_SUFFIXES = (".nii", ".nii.gz", ".nrrd", ".mha", ".mhd", ".hdr", ".img", ".dcm", ".dicom", ".ima")


def _has_internet_connection():
    """Check general internet access before reporting a Supabase API outage."""
    for url in (
        "https://www.google.com/generate_204",
        "https://www.cloudflare.com/cdn-cgi/trace",
    ):
        try:
            response = requests.get(url, timeout=(2, 3), stream=True)
            response.close()
            if response.status_code < 500:
                return True
        except requests.RequestException:
            continue
    return False


def _connection_error_message(exc=None, timed_out=False):
    if not _has_internet_connection():
        return "No internet connection detected. Connect this computer to the internet, then try again."
    if exc is not None and ("name resolution" in str(exc).lower() or "failed to resolve" in str(exc).lower()):
        return (
            "Internet is available, but NeuroQuant could not find the Supabase project address. "
            "Check that SUPABASE_URL matches the Project URL shown in Supabase Settings → API."
        )
    if timed_out:
        return "Internet is available, but Supabase did not respond in time. Try again shortly."
    return "Internet is available, but NeuroQuant could not connect to Supabase. Check the project URL and try again."


def _headers(extra=None):
    headers = {"apikey": SUPABASE_KEY, "User-Agent": "NeuroQuant/6.0"}
    if extra:
        headers.update(extra)
    return headers


def _json_request(method, endpoint, *, payload=None, timeout=30):
    try:
        response = requests.request(
            method, f"{SUPABASE_URL}{endpoint}", headers=_headers(), json=payload, timeout=timeout
        )
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(_connection_error_message(exc)) from None
    except requests.exceptions.Timeout:
        raise RuntimeError(_connection_error_message(timed_out=True)) from None
    if not response.ok:
        try:
            details = response.json().get("msg") or response.json().get("message") or response.json().get("error_description")
        except (ValueError, AttributeError):
            details = response.text[:300]
        raise RuntimeError(f"Supabase request failed ({response.status_code}): {details or response.reason}")
    if not response.content:
        return None
    return response.json()


def _safe_segment(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._")[:80] or "scan"


def user_scan_root(display_name):
    """Storage namespace organized by the radiologist's display name."""
    label = re.sub(r"[/\\\x00-\x1f]+", "_", str(display_name or "")).strip(" .")[:80]
    return f"radiologist/{label or 'Radiologist'}/scans"


def scan_files_in_folder(folder):
    """Collect scan files recursively; DICOM files without extensions are retained."""
    folder = Path(folder)
    found = []
    for path in folder.rglob("*"):
        if not path.is_file() or path.name.startswith("."):
            continue
        low = path.name.casefold()
        if low.endswith(SCAN_SUFFIXES) or not path.suffix:
            found.append(str(path))
    return sorted(found, key=str.casefold)


def is_scan_file(path):
    name = Path(path).name.casefold()
    return name.endswith(SCAN_SUFFIXES) or not Path(name).suffix


def upload_scan(local_path, object_key):
    """Stream one file to the configured bucket using its publishable key and RLS policy."""
    path = Path(local_path)
    mime = "application/dicom" if path.suffix.casefold() in (".dcm", ".dicom") else (mimetypes.guess_type(path.name)[0] or "application/octet-stream")
    url = f"{SUPABASE_URL}/storage/v1/object/{quote(BUCKET, safe='')}/{quote(object_key, safe='/')}"
    try:
        with path.open("rb") as source:
            response = requests.post(
                url,
                data=source,
                headers=_headers({"Content-Type": mime, "x-upsert": "false", "Cache-Control": "no-store"}),
                timeout=(30, 900),
            )
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(_connection_error_message(exc)) from None
    except requests.exceptions.Timeout:
        raise RuntimeError(_connection_error_message(timed_out=True)) from None
    if not response.ok:
        try:
            message = response.json().get("message") or response.json().get("error")
        except ValueError:
            message = response.text[:300]
        if response.status_code in (401, 403):
            raise RuntimeError("Supabase denied this upload. Check the neuro bucket upload policy and project key.")
        raise RuntimeError(f"Could not upload {path.name} ({response.status_code}): {message or response.reason}")


def list_user_scans(display_name, legacy_username=None):
    """List scans for a display-name folder, including this account's legacy folder."""
    roots = [user_scan_root(display_name)]
    if legacy_username:
        roots.append(f"radiologist_uploads/{_safe_segment(legacy_username)}/scans")
    collected = {}
    for root in roots:
        for item in _list_scans_under_prefix(root):
            collected[item["path"]] = item
    return sorted(collected.values(), key=lambda item: (item["updated_at"], item["path"]), reverse=True)


def list_all_radiologist_scans():
    """List radiologist uploads across local accounts for the clinician cloud importer."""
    collected = {}
    for root in ("radiologist", "radiologist_uploads"):
        for item in _list_scans_under_prefix(root):
            collected[item["path"]] = item
    return sorted(collected.values(), key=lambda item: (item["updated_at"], item["path"]), reverse=True)


def _list_scans_under_prefix(root):
    collected = {}
    visited_folders = set()

    def join_key(prefix, name):
        prefix = str(prefix or "").strip("/")
        name = str(name or "").strip("/")
        if not prefix:
            return name
        if name == prefix or name.startswith(prefix + "/"):
            return name
        return f"{prefix}/{name}" if name else prefix

    def walk(prefix):
        prefix = prefix.strip("/")
        if prefix in visited_folders:
            return
        visited_folders.add(prefix)
        offset = 0
        while True:
            entries = _json_request(
                "POST", f"/storage/v1/object/list/{quote(BUCKET, safe='')}",
                payload={"prefix": prefix.rstrip("/") + "/", "limit": 1000, "offset": offset,
                         "sortBy": {"column": "name", "order": "asc"}},
            ) or []
            if isinstance(entries, dict):
                entries = entries.get("data") or entries.get("objects") or []
            for entry in entries:
                name = entry.get("name", "")
                if not name:
                    continue
                full_key = join_key(prefix, name)
                if entry.get("id") is None and entry.get("metadata") is None:
                    walk(full_key)
                else:
                    metadata = entry.get("metadata") or {}
                    collected[full_key] = {
                        "name": Path(full_key).name, "path": full_key,
                        "size": metadata.get("size", 0),
                        "updated_at": entry.get("updated_at") or entry.get("created_at") or "",
                    }
            if len(entries) < 1000:
                break
            offset += len(entries)

    walk(root)
    return sorted(collected.values(), key=lambda item: (item["updated_at"], item["path"]), reverse=True)


def download_scan(object_key, destination, progress_callback=None, expected_size=0):
    """Download a stored scan object to a local case folder."""
    url = f"{SUPABASE_URL}/storage/v1/object/{quote(BUCKET, safe='')}/{quote(object_key, safe='/')}"
    try:
        response = requests.get(url, headers=_headers(), timeout=(30, 900), stream=True)
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(_connection_error_message(exc)) from None
    except requests.exceptions.Timeout:
        raise RuntimeError(_connection_error_message(timed_out=True)) from None
    if not response.ok:
        if response.status_code in (401, 403):
            raise RuntimeError("Supabase denied access to this scan. Check the neuro bucket SELECT policy for radiologist uploads.")
        raise RuntimeError(f"Could not download {Path(object_key).name} ({response.status_code}): {response.text[:300]}")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        content_length = int(response.headers.get("content-length") or expected_size or 0)
    except (TypeError, ValueError):
        content_length = int(expected_size or 0)
    downloaded = 0
    with destination.open("wb") as target:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                target.write(chunk)
                downloaded += len(chunk)
                if progress_callback:
                    progress_callback(downloaded, content_length)
    response.close()
    if progress_callback:
        progress_callback(downloaded, max(content_length, downloaded))


def delete_user_scans(object_paths):
    if not object_paths:
        return
    requested = sorted(set(str(path).strip("/") for path in object_paths if path))
    try:
        _json_request(
            "DELETE", f"/storage/v1/object/{quote(BUCKET, safe='')}",
            payload={"prefixes": requested}, timeout=60,
        )
    except RuntimeError as exc:
        message = str(exc)
        if "403" in message or "401" in message or "permission" in message.lower() or "policy" in message.lower():
            raise RuntimeError(
                "Supabase denied deletion. The neuro bucket needs both SELECT and DELETE policies "
                "for radiologist/ and radiologist_uploads/ paths. Run supabase/radiologist_storage_policies.sql.\n\n"
                f"Supabase response: {message}"
            ) from None
        raise

    # Storage can return a successful HTTP response while RLS filters out rows.
    # Verify removal so the UI cannot silently claim that an object was deleted.
    remaining = {item.get("path") for item in list_all_radiologist_scans()}
    not_removed = [path for path in requested if path in remaining]
    if not_removed:
        examples = ", ".join(Path(path).name for path in not_removed[:3])
        raise RuntimeError(
            "Supabase accepted the delete request but the files are still listed. "
            "Check that the neuro bucket has an anon DELETE policy (and SELECT policy) "
            "for radiologist/ and radiologist_uploads/ paths. "
            f"Still present: {examples}."
        )
