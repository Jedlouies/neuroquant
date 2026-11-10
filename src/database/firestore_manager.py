import os
import sys
from pathlib import Path
import hashlib
import json
from app_config import get_setting

# Safe fallback for firebase_admin to prevent launch crashes
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    HAS_FIREBASE = True
except ImportError:
    firebase_admin = None
    credentials = None
    firestore = None
    HAS_FIREBASE = False

_db = None

def get_db():
    """Returns the active Firestore client, auto-initializing if not already connected."""
    global _db
    if not HAS_FIREBASE:
        return None
    if _db is not None:
        return _db
    return initialize_firestore()

def initialize_firestore():
    """Initializes the Firestore app using the service account key."""
    global _db
    if not HAS_FIREBASE:
        print("[Firestore] Warning: firebase_admin package is not installed. Operating offline.")
        return None
    if _db is not None:
        return _db
    
    try:
        service_account = {
            "type": get_setting("FIREBASE_TYPE", "service_account"),
            "project_id": get_setting("FIREBASE_PROJECT_ID"),
            "private_key_id": get_setting("FIREBASE_PRIVATE_KEY_ID"),
            "private_key": get_setting("FIREBASE_PRIVATE_KEY").replace("\\n", "\n"),
            "client_email": get_setting("FIREBASE_CLIENT_EMAIL"),
            "client_id": get_setting("FIREBASE_CLIENT_ID"),
            "auth_uri": get_setting("FIREBASE_AUTH_URI", "https://accounts.google.com/o/oauth2/auth"),
            "token_uri": get_setting("FIREBASE_TOKEN_URI", "https://oauth2.googleapis.com/token"),
            "auth_provider_x509_cert_url": get_setting("FIREBASE_AUTH_PROVIDER_CERT_URL", "https://www.googleapis.com/oauth2/v1/certs"),
            "client_x509_cert_url": get_setting("FIREBASE_CLIENT_CERT_URL"),
            "universe_domain": get_setting("FIREBASE_UNIVERSE_DOMAIN", "googleapis.com"),
        }
        required = ("project_id", "private_key_id", "private_key", "client_email", "client_id")
        if not all(service_account[field] for field in required):
            print("[Firestore] Warning: Firebase credentials are not configured in the root .env file.")
            return None
            
        if not firebase_admin._apps:
            cred = credentials.Certificate(service_account)
            firebase_admin.initialize_app(cred)
            
        _db = firestore.client()
        print("[Firestore] Connected to Firebase successfully.")
        return _db
    except Exception as e:
        print(f"[Firestore] Initialization Error: {e}")
        return None

def update_user_status(user_id: str, display_name: str, designation: str, is_online: bool):
    """Updates the user's profile and online status in the database."""
    db = get_db()
    if not db: return
    try:
        db.collection("users").document(user_id).set({
            "user_id": user_id,
            "display_name": display_name,
            "designation": designation,
            "is_online": is_online,
            "last_seen": firestore.SERVER_TIMESTAMP
        }, merge=True)
    except Exception as e:
        print(f"[Firestore] Status Update Error: {e}")

def _get_dm_chat_id(uid1: str, uid2: str):
    if not uid1 or not uid2: return "unknown_dm"
    return f"dm_{min(uid1, uid2)}_{max(uid1, uid2)}"

def send_chat_message(sender_id: str, display_name: str, designation: str, text: str, msg_type: str = "text", file_path: str = "", target: str = "general",
                      reply_to: dict = None, attachments: list = None):
    db = get_db()
    if not db: return False
    try:
        data = {
            "sender_id": sender_id,
            "display_name": display_name,
            "designation": designation,
            "text": text,
            "msg_type": msg_type,       
            "file_path": file_path,
            "liked_by": [],
            "timestamp": firestore.SERVER_TIMESTAMP
        }
        if reply_to:
            data["reply_to"] = reply_to          # {"id", "name", "text"}
        if attachments:
            data["attachments"] = attachments    # [{"name", "url"}, ...] for multi-image messages
        if target == "general":
            db.collection("collab_chat").add(data)
        else:
            chat_id = _get_dm_chat_id(sender_id, target)
            db.collection("direct_messages").document(chat_id).collection("messages").add(data)
        return True
    except Exception as e:
        print(f"[Firestore] Error sending message: {e}")
        return False

def delete_chat_message(target: str, user_id: str, message_id: str):
    db = get_db()
    if not db: return False
    try:
        if target == "general":
            db.collection("collab_chat").document(message_id).delete()
        else:
            chat_id = _get_dm_chat_id(user_id, target)
            db.collection("direct_messages").document(chat_id).collection("messages").document(message_id).delete()
        return True
    except Exception as e:
        print(f"[Firestore] Error deleting message: {e}")
        return False

def toggle_message_reaction(target: str, user_id: str, message_id: str, has_liked: bool):
    db = get_db()
    if not db: return
    try:
        if target == "general":
            doc_ref = db.collection("collab_chat").document(message_id)
        else:
            chat_id = _get_dm_chat_id(user_id, target)
            doc_ref = db.collection("direct_messages").document(chat_id).collection("messages").document(message_id)
        
        if has_liked:
            doc_ref.update({"liked_by": firestore.ArrayRemove([user_id])})
        else:
            doc_ref.update({"liked_by": firestore.ArrayUnion([user_id])})
    except Exception as e:
        print(f"[Firestore] Error reacting: {e}")

def listen_for_messages(target, user_id, callback):
    db = get_db()
    if not db: return None
    
    def on_snapshot(col_snapshot, changes, read_time):
        msgs = []
        for doc in col_snapshot:
            data = doc.to_dict()
            data['id'] = doc.id
            msgs.append(data)
        callback(msgs)
    
    if target == "general":
        query = db.collection("collab_chat").order_by("timestamp", direction=firestore.Query.ASCENDING).limit_to_last(100)
    else:
        chat_id = _get_dm_chat_id(user_id, target)
        query = db.collection("direct_messages").document(chat_id).collection("messages").order_by("timestamp", direction=firestore.Query.ASCENDING).limit_to_last(100)
        
    return query.on_snapshot(on_snapshot)

def listen_for_all_users(callback):
    db = get_db()
    if not db: return None
    query = db.collection("users")
    return query.on_snapshot(callback)

def save_ai_chat_session_to_cloud(user_id: str, session: dict):
    """Mirror one AI chat into Firestore without storing machine-local paths."""
    db = get_db()
    if not db:
        raise RuntimeError("Firestore is not configured or available.")
    session_id = str(session.get("id") or "").strip()
    if not user_id or not session_id:
        raise ValueError("An account and chat ID are required for cloud chat sync.")

    user_ref = db.collection("ai_chat_sessions").document(str(user_id))
    session_ref = user_ref.collection("sessions").document(session_id)
    messages = session.get("messages") or []
    session_ref.set({
        "user_id": str(user_id),
        "session_id": session_id,
        "title": str(session.get("title") or "Brain tumor chat"),
        "updated_at": firestore.SERVER_TIMESTAMP,
        "message_count": len(messages),
    }, merge=True)

    batch = db.batch()
    pending = 0
    active_ids = set()
    for index, message in enumerate(messages):
        message_id = str(message.get("id") or f"message_{index}")
        active_ids.add(message_id)
        cloud_refs = message.get("cloud_attachments") or {}
        attachments = []
        for path in message.get("attachments") or []:
            remote = cloud_refs.get(path) or {}
            attachments.append({
                "name": os.path.basename(str(path)),
                "bucket": remote.get("bucket", ""),
                "object_path": remote.get("object_path", ""),
                "sync_error": remote.get("sync_error", ""),
            })
        payload = {
            "message_id": message_id,
            "sender": str(message.get("sender") or "user"),
            "text": str(message.get("text") or ""),
            "timestamp": str(message.get("timestamp") or ""),
            "updated_at": firestore.SERVER_TIMESTAMP,
            "attachments": attachments,
            "pending": bool(message.get("pending", False)),
        }
        batch.set(session_ref.collection("messages").document(message_id), payload, merge=True)
        pending += 1
        if pending >= 450:
            batch.commit()
            batch = db.batch()
            pending = 0
    if pending:
        batch.commit()

    # Remove cloud messages explicitly deleted from the local conversation.
    for document in session_ref.collection("messages").stream():
        if document.id not in active_ids:
            document.reference.delete()
    user_ref.set({"user_id": str(user_id), "last_chat_sync": firestore.SERVER_TIMESTAMP}, merge=True)
    return True

def delete_ai_chat_session_from_cloud(user_id: str, session_id: str):
    """Remove one mirrored AI chat (and its messages) from Firestore."""
    db = get_db()
    if not db or not user_id or not session_id:
        return False
    try:
        session_ref = db.collection("ai_chat_sessions").document(str(user_id)).collection("sessions").document(str(session_id))
        for document in session_ref.collection("messages").stream():
            document.reference.delete()
        session_ref.delete()
        return True
    except Exception as e:
        print(f"[Firestore] Error deleting AI chat: {e}")
        return False

# --- ACCOUNT RECOVERY & WORKSTATION BACKUP ---

def _account_collection(role: str) -> str:
    """Keep same-named accounts isolated by role in Firestore."""
    return "radiologist_users" if role == "radiologist" else "users"

def _backup_collection(role: str) -> str:
    return "radiologist_workstation_backups" if role == "radiologist" else "workstation_backups"

def save_user_credentials_to_cloud(user_id: str, password_hash: str, salt: str, display_name: str, designation: str, role: str = "clinician"):
    db = get_db()
    if not db: return False
    try:
        db.collection(_account_collection(role)).document(user_id).set({
            "user_id": user_id,
            "role": role,
            "password_hash": password_hash,
            "salt": salt,
            "display_name": display_name,
            "designation": designation,
            "last_updated": firestore.SERVER_TIMESTAMP
        }, merge=True)
        return True
    except Exception as e:
        print(f"[Firestore] Credentials sync error: {e}")
        return False

def push_workstation_backup_to_cloud(user_id: str, backup_payload: dict, role: str = "clinician"):
    db = get_db()
    if not db or not backup_payload: return False
    try:
        db.collection(_backup_collection(role)).document(user_id).set(backup_payload)
        return True
    except Exception as e:
        print(f"[Firestore] Workstation backup error: {e}")
        return False

def fetch_workstation_backup_from_cloud(user_id: str, role: str = "clinician"):
    db = get_db()
    if not db: return None
    try:
        doc = db.collection(_backup_collection(role)).document(user_id).get()
        return doc.to_dict() if doc.exists else {}
    except Exception as e:
        print(f"[Firestore] Fetch backup error: {e}")
        return None

def verify_cloud_user_recovery(username: str, password_attempt: str, role: str = "clinician"):
    """
    Verifies user presence. If cloud credentials exist, checks password.
    If no security hash is stored yet, hashes the entered password, saves it
    to Firestore, and authorizes recovery.
    """
    db = get_db()
    if not db:
        return False, "Unable to reach Firebase cloud database. Please verify internet connection and the Firebase settings in .env.", None
    try:
        doc = db.collection(_account_collection(role)).document(username).get()
        if not doc.exists:
            return False, f"Account '{username}' does not exist in the cloud registry.", None

        data = doc.to_dict() or {}
        stored_hash = data.get("password_hash")
        salt = data.get("salt")

        # 1. If password hash is already registered in cloud, verify match
        if stored_hash and salt:
            attempt_hash = hashlib.pbkdf2_hmac('sha256', password_attempt.encode(), salt.encode(), 100000).hex()
            if attempt_hash != stored_hash:
                return False, "Incorrect password. Recovery rejected.", None
            return True, "Authentication verified.", data

        # 2. If no cloud credentials exist yet, adopt the entered password
        from database.db_manager import hash_password
        new_hash, new_salt = hash_password(password_attempt)
        data["password_hash"] = new_hash
        data["salt"] = new_salt

        # Sync credentials to Firestore so future recoveries are protected
        save_user_credentials_to_cloud(
            username,
            new_hash,
            new_salt,
            data.get("display_name", username),
            data.get("designation", "Neurologist" if role != "radiologist" else "Radiologist"),
            role=role
        )

        return True, "Authentication verified.", data

    except Exception as e:
        return False, f"Cloud verification error: {e}", None
