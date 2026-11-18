"""
Supabase Storage helper for the NeuroQuant clinical chat.

Chat *messages* stay in Firestore. Only the attachment bytes live in Supabase;
the message stores the public URL of the uploaded object.

Uses the project's *publishable* key (safe for a desktop client). NEVER put the
secret key (sb_secret_...) in this app: it bypasses all security rules.
Optional env vars override the defaults below:
    SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, SUPABASE_BUCKET
"""
import os
import re
import time
import uuid
import mimetypes
import urllib.request
import urllib.error
import urllib.parse
import json
from datetime import datetime
from app_config import get_setting

SUPABASE_URL = get_setting("SUPABASE_URL")
SUPABASE_KEY = get_setting("SUPABASE_PUBLISHABLE_KEY")
BUCKET = get_setting("SUPABASE_BUCKET", "neuro")
CLINICAL_BUCKET = get_setting("SUPABASE_CLINICAL_BUCKET", "neuro")
FOLDER_SERVICE_URL = get_setting(
    "NEUROQUANT_FOLDER_SERVICE_URL",
    f"{SUPABASE_URL.rstrip('/')}/functions/v1/neuro-folder-service" if SUPABASE_URL else "",
)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB


class BucketMissingError(RuntimeError):
    """The Supabase bucket does not exist (Supabase reports this as HTTP 400 with
    statusCode 404 / "NoSuchBucket", not as a plain HTTP 404)."""
    def __init__(self, bucket: str, message: str):
        super().__init__(message)
        self.bucket = bucket


# bucket -> time.monotonic() before which we do not hit the network again
_BUCKET_RETRY_AFTER = {}
_BUCKET_RETRY_SECONDS = 300


def _is_bucket_missing(body: str) -> bool:
    low = (body or "").lower()
    return "nosuchbucket" in low or "bucket not found" in low


def _bucket_missing_message(bucket: str) -> str:
    return (f"Supabase storage bucket '{bucket}' does not exist. Create this exact bucket "
            "(private) in the configured Supabase project and add an upload policy for it. "
            "The file remains saved locally.")


def is_configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


def _headers(extra=None):
    # New-style publishable keys (sb_publishable_...) are NOT JWTs: send them as `apikey` only.
    h = {"apikey": SUPABASE_KEY, "User-Agent": "NeuroQuant/1.0"}
    if extra:
        h.update(extra)
    return h


def public_url(object_key: str) -> str:
    return f"{SUPABASE_URL.rstrip('/')}/storage/v1/object/public/{BUCKET}/{urllib.parse.quote(object_key, safe='/')}"


def _safe_name(name: str) -> str:
    stem, ext = os.path.splitext(name)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "file"
    ext = re.sub(r"[^A-Za-z0-9.]+", "", ext)
    return f"{stem[:60]}{ext[:12]}"


def upload_attachment(local_path: str, folder: str = "general") -> dict:
    """Upload a local file and return {"url", "name", "size", "mime"}.

    Raises on any failure so the caller can show a message.
    """
    size = os.path.getsize(local_path)
    if size == 0:
        raise ValueError("The selected file is empty.")
    if size > MAX_UPLOAD_BYTES:
        raise ValueError(f"File is too large ({size / 1048576:.1f} MB). Limit is {MAX_UPLOAD_BYTES // 1048576} MB.")

    name = os.path.basename(local_path)
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    category = "images" if mime.startswith("image/") else "recordings" if mime.startswith(("audio/", "video/")) else "files"
    object_key = f"{category}/{_safe_name(folder)}/{datetime.utcnow():%Y%m%d}/{uuid.uuid4().hex[:8]}_{_safe_name(name)}"

    with open(local_path, "rb") as f:
        data = f.read()

    endpoint = f"{SUPABASE_URL.rstrip('/')}/storage/v1/object/{BUCKET}/{urllib.parse.quote(object_key, safe='/')}"
    req = urllib.request.Request(endpoint, data=data, method="POST",
                                 headers=_headers({"Content-Type": mime, "x-upsert": "false", "Cache-Control": "max-age=3600"}))
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        hint = ""
        low = body.lower()
        if "row-level security" in low or e.code in (401, 403):
            hint = "\n\nSupabase refused the upload: run the storage policy SQL for the 'neuro' bucket."
        elif "bucket not found" in low or e.code == 404:
            hint = f"\n\nBucket '{BUCKET}' was not found. Check the bucket name."
        raise RuntimeError(f"Supabase error {e.code}: {body[:300]}{hint}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach Supabase: {e.reason}") from None
    return {"url": public_url(object_key), "name": name, "size": size, "mime": mime}


def upload_clinical_artifact(local_path: str, folder: str, owner_key: str = "") -> dict:
    """Upload a clinical artifact to a private bucket and return its object path.

    Configure SUPABASE_CLINICAL_BUCKET to an existing private bucket with an
    appropriate Storage insert policy. No public URL is generated.
    """
    if time.monotonic() < _BUCKET_RETRY_AFTER.get(CLINICAL_BUCKET, 0.0):
        # Already known to be missing: fail fast instead of repeating a doomed upload.
        raise BucketMissingError(CLINICAL_BUCKET, _bucket_missing_message(CLINICAL_BUCKET))
    size = os.path.getsize(local_path)
    if size == 0:
        raise ValueError("The selected file is empty.")
    if size > MAX_UPLOAD_BYTES:
        raise ValueError(f"File is too large ({size / 1048576:.1f} MB). Limit is {MAX_UPLOAD_BYTES // 1048576} MB.")

    name = os.path.basename(local_path)
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    # Keep one remote object per segmentation/view. Re-running an analysis
    # replaces its GIF instead of accumulating orphaned UUID-named copies.
    owner_segment = f"{_safe_name(owner_key)}/" if owner_key else ""
    object_key = f"segmentation_gifs/{owner_segment}{_safe_name(folder)}/{_safe_name(name)}"
    endpoint = (f"{SUPABASE_URL.rstrip('/')}/storage/v1/object/"
                f"{urllib.parse.quote(CLINICAL_BUCKET, safe='')}/{urllib.parse.quote(object_key, safe='/')}")
    with open(local_path, "rb") as source:
        request = urllib.request.Request(
            endpoint, data=source.read(), method="POST",
            headers=_headers({"Content-Type": mime, "x-upsert": "true", "Cache-Control": "no-store"})
        )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            response.read()
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        if _is_bucket_missing(body):
            _BUCKET_RETRY_AFTER[CLINICAL_BUCKET] = time.monotonic() + _BUCKET_RETRY_SECONDS
            raise BucketMissingError(CLINICAL_BUCKET, _bucket_missing_message(CLINICAL_BUCKET)) from None
        if error.code in (401, 403) or "row-level security" in body.lower():
            raise RuntimeError(
                f"Supabase denied access to private bucket '{CLINICAL_BUCKET}'. This desktop upload uses only the publishable key, "
                "so the bucket needs a valid authenticated upload session or a secure server-side upload endpoint. "
                "A private bucket policy alone cannot authenticate this client; local GIFs are preserved."
            ) from None
        raise RuntimeError(f"Private Supabase upload failed ({error.code}): {body[:300]}") from None
    except urllib.error.URLError as error:
        raise RuntimeError(f"Could not reach Supabase: {error.reason}") from None
    return {"bucket": CLINICAL_BUCKET, "object_path": object_key, "name": name, "size": size, "mime": mime}


def upload_ai_chat_attachment(local_path: str, username: str, password: str, session_id: str) -> dict:
    """Upload an AI chat image through a short-lived, account-verified URL."""
    if not SUPABASE_KEY or not FOLDER_SERVICE_URL:
        raise RuntimeError("AI image backup needs SUPABASE_PUBLISHABLE_KEY and NEUROQUANT_FOLDER_SERVICE_URL in the desktop .env file.")
    size = os.path.getsize(local_path)
    if size <= 0:
        raise ValueError("The selected image is empty.")
    if size > MAX_UPLOAD_BYTES:
        raise ValueError(f"Image is too large ({size / 1048576:.1f} MB). Limit is {MAX_UPLOAD_BYTES // 1048576} MB.")
    name = os.path.basename(local_path)
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if not mime.startswith("image/"):
        raise ValueError("Only image attachments can be backed up from AI chat.")

    payload = json.dumps({
        "action": "createAIChatUploadUrl",
        "username": username,
        "password": password,
        "sessionId": session_id,
        "attachmentId": uuid.uuid4().hex,
        "fileName": name,
    }).encode("utf-8")
    function_request = urllib.request.Request(
        FOLDER_SERVICE_URL, data=payload, method="POST",
        headers={"Content-Type": "application/json", "apikey": SUPABASE_KEY, "User-Agent": "NeuroQuant/1.0"},
    )
    try:
        with urllib.request.urlopen(function_request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            details = json.loads(error.read().decode("utf-8", "replace"))
            message = details.get("error") or details.get("message") or f"Request failed ({error.code})."
        except Exception:
            message = f"Request failed ({error.code})."
        raise RuntimeError(f"Could not authorize private AI image upload: {message}") from None
    except urllib.error.URLError as error:
        raise RuntimeError(f"Could not reach the private AI image upload service: {error.reason}") from None

    signed_url = result.get("signedUrl")
    object_path = result.get("objectPath")
    if not signed_url or not object_path:
        raise RuntimeError("The upload service did not return a signed URL and object path.")
    with open(local_path, "rb") as source:
        request = urllib.request.Request(
            signed_url, data=source.read(), method="PUT",
            headers={"Content-Type": mime, "x-upsert": "true", "User-Agent": "NeuroQuant/1.0"},
        )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            response.read()
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace").strip()
        raise RuntimeError(f"Supabase rejected the private AI image upload ({error.code}): {body[:300] or error.reason}") from None
    except urllib.error.URLError as error:
        raise RuntimeError(f"Could not upload the private AI image: {error.reason}") from None
    return {"bucket": result.get("bucket", CLINICAL_BUCKET), "object_path": object_path, "name": name, "size": size, "mime": mime}


def _call_chat_attachment_service(action: str, username: str, password: str, **values) -> dict:
    if not SUPABASE_KEY or not FOLDER_SERVICE_URL:
        raise RuntimeError("Chat attachments need SUPABASE_PUBLISHABLE_KEY and NEUROQUANT_FOLDER_SERVICE_URL in the desktop .env file.")
    payload = json.dumps({"action": action, "username": username, "password": password, **values}).encode("utf-8")
    request = urllib.request.Request(
        FOLDER_SERVICE_URL, data=payload, method="POST",
        headers={"Content-Type": "application/json", "apikey": SUPABASE_KEY, "User-Agent": "NeuroQuant/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            details = json.loads(error.read().decode("utf-8", "replace"))
            message = details.get("error") or details.get("message") or f"Request failed ({error.code})."
        except Exception:
            message = f"Request failed ({error.code})."
        raise RuntimeError(message) from None
    except urllib.error.URLError as error:
        raise RuntimeError(f"Could not reach the secure chat attachment service: {error.reason}") from None


def upload_collab_chat_attachment(local_path: str, username: str, password: str, target: str = "general") -> dict:
    """Upload a collaboration attachment privately and return its initial signed URL."""
    size = os.path.getsize(local_path)
    if size <= 0:
        raise ValueError("The selected file is empty.")
    if size > MAX_UPLOAD_BYTES:
        raise ValueError(f"File is too large ({size / 1048576:.1f} MB). Limit is {MAX_UPLOAD_BYTES // 1048576} MB.")
    name = os.path.basename(local_path)
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    scope = "general" if target == "general" else "direct"
    result = _call_chat_attachment_service(
        "createCollabChatUploadUrl", username, password,
        scope=scope, target=target, fileName=name, attachmentId=uuid.uuid4().hex,
    )
    signed_url = result.get("signedUrl")
    object_path = result.get("objectPath")
    if not signed_url or not object_path:
        raise RuntimeError("The secure upload service did not return an upload URL and object path.")
    with open(local_path, "rb") as source:
        request = urllib.request.Request(
            signed_url, data=source.read(), method="PUT",
            headers={"Content-Type": mime, "User-Agent": "NeuroQuant/1.0"},
        )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            response.read()
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace").strip()
        raise RuntimeError(f"Supabase rejected the private attachment upload ({error.code}): {body[:300] or error.reason}") from None
    except urllib.error.URLError as error:
        raise RuntimeError(f"Could not upload the private chat attachment: {error.reason}") from None
    read_result = _call_chat_attachment_service(
        "createCollabChatDownloadUrls", username, password, objectPaths=[object_path]
    )
    url = (read_result.get("urls") or {}).get(object_path)
    if not url:
        raise RuntimeError("The attachment uploaded, but a secure view link could not be created.")
    return {"url": url, "object_path": object_path, "name": name, "size": size, "mime": mime}


def refresh_collab_chat_attachment_urls(username: str, password: str, object_paths: list[str]) -> dict:
    return _call_chat_attachment_service(
        "createCollabChatDownloadUrls", username, password, objectPaths=object_paths
    ).get("urls", {})


def fetch_bytes(url: str, timeout: int = 20) -> bytes:
    """Download an attachment (public bucket URL)."""
    req = urllib.request.Request(url, headers={"User-Agent": "NeuroQuant/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()
