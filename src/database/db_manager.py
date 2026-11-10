import sqlite3
import os
import sys
import hashlib
import random
import json
from datetime import datetime
import platform

if platform.system() == "Windows":
    BASE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "NeuroQuant")
else:
    BASE_DIR = os.path.join(os.path.expanduser("~"), ".neuroquant")

DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "neuroquant.db")

def get_connection():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def _merge_exact_duplicate_patients(cursor):
    """Merge only unambiguous duplicate profiles; keep every linked analysis row."""
    rows = cursor.execute("""SELECT rowid, id, patient_id, age, gender, full_name, date_of_birth,
                                  phone, email, address, emergency_contact, emergency_phone,
                                  medical_history, allergies, current_medications
                           FROM patients ORDER BY rowid""").fetchall()
    groups = {}
    for row in rows:
        name = " ".join(str(row[5] or "").casefold().split())
        dob = str(row[6] or "").strip()
        if name and dob:
            groups.setdefault((name, dob), []).append(row)

    fields = ("gender", "full_name", "date_of_birth", "phone", "email", "address",
              "emergency_contact", "emergency_phone", "medical_history", "allergies", "current_medications")
    for duplicates in groups.values():
        if len(duplicates) < 2:
            continue
        canonical = duplicates[0]
        # Preserve all non-conflicting profile data. If two records disagree on
        # a populated field, leave them separate for clinician review.
        merged = {}
        conflict = False
        for offset, field in enumerate(fields, start=4):
            values = [str(row[offset] or "").strip() for row in duplicates]
            distinct = {value.casefold() for value in values if value}
            if len(distinct) > 1:
                conflict = True
                break
            merged[field] = next((value for value in values if value), "")
        if conflict:
            continue
        cursor.execute("""UPDATE patients SET age = ?, gender = ?, full_name = ?, date_of_birth = ?,
                       phone = ?, email = ?, address = ?, emergency_contact = ?, emergency_phone = ?,
                       medical_history = ?, allergies = ?, current_medications = ? WHERE id = ?""",
                       (max([int(row[3] or 0) for row in duplicates]), *(merged[field] for field in fields[0:]), canonical[1]))
        for duplicate in duplicates[1:]:
            cursor.execute("UPDATE segmentation_results SET patient_id = ? WHERE patient_id = ?",
                           (canonical[1], duplicate[1]))
            cursor.execute("DELETE FROM patients WHERE id = ?", (duplicate[1],))

def _remove_identical_duplicate_metrics(cursor):
    rows = cursor.execute("""SELECT id, segmentation_id, total_tumor_mm3, core_mm3, edema_mm3,
                                  intracranial_mm3, frontal_mm3, parietal_mm3, occipital_mm3,
                                  temporal_mm3, cerebellum_mm3, spinal_cord_mm3
                           FROM volumetric_metrics ORDER BY id""").fetchall()
    groups = {}
    for row in rows:
        groups.setdefault(row[1], []).append(row)
    for records in groups.values():
        if len(records) < 2:
            continue
        seen = set()
        for record in records:
            signature = record[2:]
            if signature in seen:
                cursor.execute("DELETE FROM volumetric_metrics WHERE id = ?", (record[0],))
            else:
                seen.add(signature)

def generate_uid(prefix):
    return f"{prefix}_{random.randint(100,999)}_{random.randint(100,999)}"

def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            display_name TEXT,
            designation TEXT
        )
    """)

    # Radiologist accounts are independent of neurologist accounts. Keeping
    # them in a separate table permits the same username in both roles.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS radiologist_users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            display_name TEXT,
            designation TEXT
        )
    """)

    try:
        cursor.execute("ALTER TABLE users ADD COLUMN display_name TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN designation TEXT")
    except sqlite3.OperationalError:
        pass

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS patients (
            id TEXT PRIMARY KEY,
            patient_id TEXT UNIQUE NOT NULL,
            age INTEGER,
            gender TEXT,
            full_name TEXT,
            date_of_birth TEXT,
            phone TEXT,
            email TEXT,
            address TEXT,
            emergency_contact TEXT,
            emergency_phone TEXT,
            medical_history TEXT,
            allergies TEXT,
            current_medications TEXT
        )
    """)

    for column, column_type in (
        ("full_name", "TEXT"), ("date_of_birth", "TEXT"), ("phone", "TEXT"),
        ("email", "TEXT"), ("address", "TEXT"), ("emergency_contact", "TEXT"),
        ("emergency_phone", "TEXT"), ("medical_history", "TEXT"), ("allergies", "TEXT"),
        ("current_medications", "TEXT")
    ):
        try:
            cursor.execute(f"ALTER TABLE patients ADD COLUMN {column} {column_type}")
        except sqlite3.OperationalError:
            pass

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS segmentation_results (
            id TEXT PRIMARY KEY,
            patient_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            radiologist TEXT NOT NULL,
            scan_date TEXT NOT NULL,
            pathology TEXT,
            mass_effect TEXT,
            clinical_impression TEXT,
            FOREIGN KEY(patient_id) REFERENCES patients(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS volumetric_metrics (
            id TEXT PRIMARY KEY,
            segmentation_id TEXT NOT NULL,
            total_tumor_mm3 REAL,
            core_mm3 REAL,
            edema_mm3 REAL,
            intracranial_mm3 REAL,
            frontal_mm3 REAL,
            parietal_mm3 REAL,
            occipital_mm3 REAL,
            temporal_mm3 REAL,
            cerebellum_mm3 REAL,
            spinal_cord_mm3 REAL,
            FOREIGN KEY(segmentation_id) REFERENCES segmentation_results(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id TEXT PRIMARY KEY,
            segmentation_id TEXT NOT NULL,
            report_type TEXT NOT NULL,
            file_path TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            FOREIGN KEY(segmentation_id) REFERENCES segmentation_results(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS segmentation_artifacts (
            id TEXT PRIMARY KEY,
            segmentation_id TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            file_path TEXT NOT NULL,
            supabase_url TEXT,
            supabase_object_path TEXT,
            sync_status TEXT NOT NULL DEFAULT 'local',
            sync_error TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(segmentation_id) REFERENCES segmentation_results(id) ON DELETE CASCADE
        )
    """)
    try:
        cursor.execute("ALTER TABLE segmentation_results ADD COLUMN source_scan_key TEXT")
    except sqlite3.OperationalError:
        pass
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_segmentation_source_scan_key ON segmentation_results(source_scan_key) WHERE source_scan_key IS NOT NULL")
    for column, column_type in (
        ("supabase_url", "TEXT"), ("supabase_object_path", "TEXT"),
        ("sync_status", "TEXT NOT NULL DEFAULT 'local'"), ("sync_error", "TEXT")
    ):
        try:
            cursor.execute(f"ALTER TABLE segmentation_artifacts ADD COLUMN {column} {column_type}")
        except sqlite3.OperationalError:
            pass
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_segmentation_artifacts_seg ON segmentation_artifacts(segmentation_id)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ai_chat_sessions (
            session_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            title TEXT NOT NULL,
            messages_json TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ai_chat_sessions_user_updated ON ai_chat_sessions(username, updated_at DESC)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS workflow_cases (
            case_id TEXT PRIMARY KEY,
            patient_code TEXT NOT NULL,
            mri_path TEXT NOT NULL UNIQUE,
            ct_path TEXT,
            mask_path TEXT,
            status TEXT NOT NULL DEFAULT 'Importing',
            queue_position INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_workflow_cases_order ON workflow_cases(status, queue_position, created_at)")

    _merge_exact_duplicate_patients(cursor)
    _remove_identical_duplicate_metrics(cursor)
    artifact_groups = cursor.execute("""SELECT segmentation_id, artifact_type, file_path
        FROM segmentation_artifacts GROUP BY segmentation_id, artifact_type, file_path
        HAVING COUNT(*) > 1""").fetchall()
    for segmentation_id, artifact_type, file_path in artifact_groups:
        records = cursor.execute("""SELECT id FROM segmentation_artifacts
            WHERE segmentation_id = ? AND artifact_type = ? AND file_path = ?
            ORDER BY CASE WHEN sync_status = 'synced' OR supabase_object_path IS NOT NULL
                          THEN 0 ELSE 1 END, created_at DESC""",
            (segmentation_id, artifact_type, file_path)).fetchall()
        for duplicate in records[1:]:
            cursor.execute("DELETE FROM segmentation_artifacts WHERE id = ?", (duplicate[0],))

    # Only insert default admin if the database has ZERO users
    # Once replaced during setup, admin will NEVER be re-created
    cursor.execute("SELECT COUNT(*) FROM users")
    user_count = cursor.fetchone()[0]
    if user_count == 0:
        pwd_hash, salt = hash_password("admin")
        uid = generate_uid("USR")
        cursor.execute(
            "INSERT INTO users (id, username, password_hash, salt, display_name, designation) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, "admin", pwd_hash, salt, "Administrator", "Chief Specialist")
        )

    cursor.execute("SELECT COUNT(*) FROM radiologist_users")
    if cursor.fetchone()[0] == 0:
        pwd_hash, salt = hash_password("admin")
        cursor.execute(
            "INSERT INTO radiologist_users (id, username, password_hash, salt, display_name, designation) VALUES (?, ?, ?, ?, ?, ?)",
            (generate_uid("RAD"), "admin", pwd_hash, salt, "Radiologist", "Radiologist")
        )

    conn.commit()
    conn.close()

def save_ai_chat_session(username, session_id, title, messages, updated_at):
    conn = get_connection()
    try:
        _ensure_ai_chat_table(conn)
        conn.execute(
            """INSERT OR REPLACE INTO ai_chat_sessions
               (session_id, username, title, messages_json, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, username, title, json.dumps(messages, ensure_ascii=False), float(updated_at))
        )
        conn.commit()
    finally:
        conn.close()

def save_segmentation_artifact(segmentation_id, artifact_type, file_path):
    conn = get_connection()
    try:
        existing = conn.execute("""SELECT id FROM segmentation_artifacts
                                  WHERE segmentation_id = ? AND artifact_type = ?
                                  ORDER BY created_at DESC""", (segmentation_id, artifact_type)).fetchall()
        now = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
        if existing:
            for duplicate in existing[1:]:
                conn.execute("DELETE FROM segmentation_artifacts WHERE id = ?", (duplicate[0],))
            conn.execute("UPDATE segmentation_artifacts SET file_path = ?, created_at = ? WHERE id = ?", (file_path, now, existing[0][0]))
        else:
            conn.execute("""INSERT INTO segmentation_artifacts
                            (id, segmentation_id, artifact_type, file_path, created_at)
                            VALUES (?, ?, ?, ?, ?)""", (generate_uid("ART"), segmentation_id, artifact_type, file_path, now))
        conn.commit()
    finally:
        conn.close()

def update_segmentation_artifact_sync(segmentation_id, file_path, status, supabase_url=None,
                                      supabase_object_path=None, error=None):
    conn = get_connection()
    try:
        conn.execute("""UPDATE segmentation_artifacts SET sync_status = ?, supabase_url = ?,
                        supabase_object_path = ?, sync_error = ?
                        WHERE segmentation_id = ? AND file_path = ?""",
                     (status, supabase_url, supabase_object_path, error, segmentation_id, file_path))
        conn.commit()
    finally:
        conn.close()

def fetch_patient_choices():
    conn = get_connection()
    try:
        return conn.execute("SELECT id, patient_id, full_name FROM patients ORDER BY patient_id").fetchall()
    finally:
        conn.close()

def fetch_patient_ai_context(patient_internal_id):
    """Load a patient's local profile, analysis history, metrics, and generated artifacts."""
    conn = get_connection()
    try:
        patient_row = conn.execute("""SELECT patient_id, full_name, age, gender, date_of_birth,
                                            phone, email, address, emergency_contact, emergency_phone,
                                            medical_history, allergies, current_medications
                                     FROM patients WHERE id = ?""", (patient_internal_id,)).fetchone()
        if not patient_row:
            return None
        patient_keys = ("patient_id", "full_name", "age", "gender", "date_of_birth", "phone", "email",
                        "address", "emergency_contact", "emergency_phone", "medical_history", "allergies",
                        "current_medications")
        analyses = conn.execute("""SELECT sr.id, sr.scan_date, sr.radiologist, sr.pathology,
                                         sr.mass_effect, sr.clinical_impression,
                                         vm.total_tumor_mm3, vm.core_mm3, vm.edema_mm3,
                                         vm.intracranial_mm3, vm.frontal_mm3, vm.parietal_mm3,
                                         vm.occipital_mm3, vm.temporal_mm3, vm.cerebellum_mm3,
                                         vm.spinal_cord_mm3
                                  FROM segmentation_results sr
                                  LEFT JOIN volumetric_metrics vm ON vm.segmentation_id = sr.id
                                  WHERE sr.patient_id = ? ORDER BY sr.scan_date DESC""",
                                 (patient_internal_id,)).fetchall()
        analysis_keys = ("segmentation_id", "scan_date", "radiologist", "pathology", "mass_effect",
                         "clinical_impression", "total_tumor_mm3", "core_mm3", "edema_mm3",
                         "intracranial_mm3", "frontal_mm3", "parietal_mm3", "occipital_mm3",
                         "temporal_mm3", "cerebellum_mm3", "spinal_cord_mm3")
        records = [dict(zip(analysis_keys, row)) for row in analyses]
        for record in records:
            artifacts = conn.execute("""SELECT artifact_type, file_path FROM segmentation_artifacts
                                        WHERE segmentation_id = ? ORDER BY created_at""",
                                     (record["segmentation_id"],)).fetchall()
            record["artifacts"] = [{"type": row[0], "path": row[1]} for row in artifacts]
        return {"patient": dict(zip(patient_keys, patient_row)), "segmentations": records}
    finally:
        conn.close()

def fetch_segmentation_artifacts():
    conn = get_connection()
    try:
        return conn.execute("""SELECT a.id, p.patient_id, a.segmentation_id, a.artifact_type,
                                     a.file_path, a.created_at
                              FROM segmentation_artifacts a
                              JOIN segmentation_results sr ON sr.id = a.segmentation_id
                              JOIN patients p ON p.id = sr.patient_id
                              ORDER BY a.created_at DESC""").fetchall()
    finally:
        conn.close()

def load_ai_chat_sessions(username, limit=50):
    conn = get_connection()
    try:
        _ensure_ai_chat_table(conn)
        rows = conn.execute(
            """SELECT session_id, title, messages_json, updated_at
               FROM ai_chat_sessions WHERE username = ?
               ORDER BY updated_at DESC LIMIT ?""",
            (username, int(limit))
        ).fetchall()
        sessions = []
        for session_id, title, messages_json, updated_at in rows:
            try:
                messages = json.loads(messages_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(messages, list):
                sessions.append({
                    "id": session_id,
                    "title": title,
                    "messages": messages,
                    "updated": float(updated_at)
                })
        return sessions
    finally:
        conn.close()

def _ensure_ai_chat_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_chat_sessions (
            session_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            title TEXT NOT NULL,
            messages_json TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_chat_sessions_user_updated ON ai_chat_sessions(username, updated_at DESC)")
    _repair_swapped_ai_chat_rows(conn)


def _repair_swapped_ai_chat_rows(conn):
    """One-time repair for rows written while save_ai_chat_session had its
    session_id/username arguments swapped. Those rows hold the chat UUID in the
    `username` column and the real username in `session_id`."""
    try:
        conn.execute("""
            UPDATE ai_chat_sessions
               SET session_id = username, username = session_id
             WHERE length(username) = 36 AND username LIKE '________-____-____-____-____________'
               AND NOT (length(session_id) = 36 AND session_id LIKE '________-____-____-____-____________')
               AND NOT EXISTS (SELECT 1 FROM ai_chat_sessions other
                                WHERE other.session_id = ai_chat_sessions.username)
        """)
        conn.commit()
    except sqlite3.Error:
        pass


def delete_ai_chat_session(username, session_id):
    conn = get_connection()
    try:
        _ensure_ai_chat_table(conn)
        conn.execute("DELETE FROM ai_chat_sessions WHERE session_id = ? AND username = ?", (session_id, username))
        conn.commit()
    finally:
        conn.close()

# --- AUTHENTICATION ---

def hash_password(password, salt=None):
    if salt is None:
        salt = os.urandom(16).hex()
    hash_obj = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
    return hash_obj.hex(), salt

def register_user(username, password):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        pwd_hash, salt = hash_password(password)
        uid = generate_uid("USR")
        cursor.execute("INSERT INTO users (id, username, password_hash, salt) VALUES (?, ?, ?, ?)", (uid, username, pwd_hash, salt))
        conn.commit()
        return True, "Account created successfully."
    except sqlite3.IntegrityError:
        return False, "Username already exists."
    finally:
        conn.close()

def authenticate_user(username, password):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, password_hash, salt FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    conn.close()
    if row:
        user_id, stored_hash, salt = row
        attempt_hash, _ = hash_password(password, salt)
        if attempt_hash == stored_hash:
            return True
    return False

def register_radiologist_user(username, password):
    conn = get_connection()
    try:
        pwd_hash, salt = hash_password(password)
        conn.execute("INSERT INTO radiologist_users (id, username, password_hash, salt) VALUES (?, ?, ?, ?)",
                     (generate_uid("RAD"), username, pwd_hash, salt))
        conn.commit()
        return True, "Radiologist account created successfully."
    except sqlite3.IntegrityError:
        return False, "Radiologist username already exists."
    finally:
        conn.close()

def authenticate_radiologist_user(username, password):
    conn = get_connection()
    try:
        row = conn.execute("SELECT password_hash, salt FROM radiologist_users WHERE username = ?", (username,)).fetchone()
    finally:
        conn.close()
    if not row:
        return False
    attempt_hash, _ = hash_password(password, row[1])
    return attempt_hash == row[0]

def change_radiologist_password(username, new_password):
    pwd_hash, salt = hash_password(new_password)
    conn = get_connection()
    try:
        conn.execute("UPDATE radiologist_users SET password_hash = ?, salt = ? WHERE username = ?", (pwd_hash, salt, username))
        conn.commit()
    finally:
        conn.close()

def change_radiologist_username(old_username, new_username):
    conn = get_connection()
    try:
        conn.execute("UPDATE radiologist_users SET username = ? WHERE username = ?", (new_username, old_username))
        if conn.total_changes == 0:
            return False, "Radiologist account was not found."
        conn.commit()
        return True, "Radiologist username updated successfully."
    except sqlite3.IntegrityError:
        return False, "Radiologist username already exists."
    finally:
        conn.close()

def save_local_radiologist_profile(username, display_name, designation):
    conn = get_connection()
    try:
        conn.execute("UPDATE radiologist_users SET display_name = ?, designation = ? WHERE username = ?",
                     (display_name, designation, username))
        conn.commit()
    finally:
        conn.close()

def get_radiologist_credentials(username):
    conn = get_connection()
    try:
        row = conn.execute("SELECT id, password_hash, salt, display_name, designation FROM radiologist_users WHERE username = ?", (username,)).fetchone()
        if not row:
            return None
        return {"id": row[0], "password_hash": row[1], "salt": row[2], "display_name": row[3], "designation": row[4]}
    finally:
        conn.close()

def change_user_password(username, new_password):
    conn = get_connection()
    cursor = conn.cursor()
    pwd_hash, salt = hash_password(new_password)
    cursor.execute("UPDATE users SET password_hash = ?, salt = ? WHERE username = ?", (pwd_hash, salt, username))
    conn.commit()
    conn.close()

def change_username(old_username, new_username):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE users SET username = ? WHERE username = ?", (new_username, old_username))
        conn.commit()
        return True, "Username updated successfully."
    except sqlite3.IntegrityError:
        return False, "Username already exists in the system."
    finally:
        conn.close()

def get_user_id_by_username(username):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def save_local_user_profile(username, display_name, designation):
    """Saves profile metadata to local SQLite for offline resilience."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET display_name = ?, designation = ? WHERE username = ?",
        (display_name, designation, username)
    )
    conn.commit()
    conn.close()

def get_local_user_profile(username):
    """Retrieves profile metadata from local SQLite."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT display_name, designation FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    conn.close()
    if row and row[0]:
        return row[0], (row[1] or "Attending Physician")
    return username, "Attending Physician"


def save_patient_record(details, preserve_existing=False):
    """Save or update an intake profile before scan analysis begins."""
    details = details or {}
    patient_code = str(details.get("patient_name") or "").strip()
    if not patient_code:
        raise ValueError("Patient ID / Scan Designation is required.")
    try:
        age = int(str(details.get("patient_age") or "0").replace("yo", "").strip())
    except ValueError:
        age = 0
    fields = (
        "full_name", "date_of_birth", "phone", "email", "address", "emergency_contact",
        "emergency_phone", "medical_history", "allergies", "current_medications",
    )
    values = [str(details.get(field) or "").strip() for field in fields]
    conn = get_connection()
    try:
        row = conn.execute("SELECT id FROM patients WHERE lower(trim(patient_id))=lower(trim(?)) LIMIT 1",
                           (patient_code,)).fetchone()
        if not row and values[0] and values[1] and not preserve_existing:
            row = conn.execute("""SELECT id FROM patients
                WHERE lower(trim(coalesce(full_name,'')))=lower(trim(?))
                  AND trim(coalesce(date_of_birth,''))=trim(?) ORDER BY id LIMIT 1""",
                (values[0], values[1])).fetchone()
        if row:
            if preserve_existing:
                conn.execute("""UPDATE patients SET patient_id=?, age=?,
                    full_name=CASE WHEN trim(?)<>'' THEN ? ELSE full_name END WHERE id=?""",
                    (patient_code, age, values[0], values[0], row[0]))
            else:
                assignments = ", ".join(f"{field}=?" for field in fields)
                conn.execute(f"UPDATE patients SET patient_id=?, age=?, gender=?, {assignments} WHERE id=?",
                             (patient_code, age, details.get("patient_gender") or "Unknown", *values, row[0]))
            internal_id = row[0]
        else:
            internal_id = generate_uid("PNT")
            columns = ("id", "patient_id", "age", "gender", *fields)
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(f"INSERT INTO patients ({', '.join(columns)}) VALUES ({placeholders})",
                         (internal_id, patient_code, age, details.get("patient_gender") or "Unknown", *values))
        conn.commit()
        return internal_id
    finally:
        conn.close()


def create_workflow_case(patient_code, mri_path):
    """Create or reset a local MRI case for the secretary-to-doctor workflow."""
    mri_path = os.path.abspath(os.fspath(mri_path))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    try:
        existing = conn.execute("SELECT case_id, queue_position FROM workflow_cases WHERE mri_path=?",
                                (mri_path,)).fetchone()
        if existing:
            case_id, position = existing
            conn.execute("""UPDATE workflow_cases SET patient_code=?, ct_path=NULL, mask_path=NULL,
                status='Importing', updated_at=? WHERE case_id=?""", (patient_code, now, case_id))
        else:
            case_id = generate_uid("CASE")
            position = conn.execute("SELECT COALESCE(MAX(queue_position),0)+1 FROM workflow_cases").fetchone()[0]
            conn.execute("""INSERT INTO workflow_cases
                (case_id, patient_code, mri_path, status, queue_position, created_at, updated_at)
                VALUES (?, ?, ?, 'Importing', ?, ?, ?)""", (case_id, patient_code, mri_path, position, now, now))
        conn.commit()
        return case_id
    finally:
        conn.close()


def fetch_workflow_cases(ready_only=False):
    conn = get_connection()
    try:
        status_filter = "WHERE c.status='Ready for doctor'" if ready_only else ""
        return conn.execute(f"""SELECT c.case_id, c.patient_code, coalesce(p.full_name,''),
                coalesce(p.age,0), coalesce(p.gender,''), c.mri_path, coalesce(c.ct_path,''),
                coalesce(c.mask_path,''), c.status, c.queue_position, c.created_at
            FROM workflow_cases c LEFT JOIN patients p ON p.patient_id=c.patient_code
            {status_filter} ORDER BY c.queue_position, c.created_at""").fetchall()
    finally:
        conn.close()


def update_workflow_case(case_id, status=None, ct_path=None, mask_path=None):
    allowed = {"Importing", "Segmenting", "Ready for doctor", "Import failed", "Segmentation failed"}
    conn = get_connection()
    try:
        fields, values = [], []
        if status is not None:
            if status not in allowed:
                raise ValueError("Unsupported case workflow status")
            fields.append("status=?")
            values.append(status)
        if ct_path is not None:
            fields.append("ct_path=?")
            values.append(os.path.abspath(os.fspath(ct_path)) if ct_path else None)
        if mask_path is not None:
            fields.append("mask_path=?")
            values.append(os.path.abspath(os.fspath(mask_path)) if mask_path else None)
        fields.append("updated_at=?")
        values.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        values.append(case_id)
        conn.execute(f"UPDATE workflow_cases SET {', '.join(fields)} WHERE case_id=?", values)
        conn.commit()
    finally:
        conn.close()


def reorder_workflow_case(case_id, direction):
    if direction not in (-1, 1):
        return
    conn = get_connection()
    try:
        rows = conn.execute("SELECT case_id, queue_position FROM workflow_cases ORDER BY queue_position, created_at").fetchall()
        index = next((i for i, row in enumerate(rows) if row[0] == case_id), None)
        target = index + direction if index is not None else -1
        if index is None or target < 0 or target >= len(rows):
            return
        first, second = rows[index], rows[target]
        conn.execute("UPDATE workflow_cases SET queue_position=? WHERE case_id=?", (second[1], first[0]))
        conn.execute("UPDATE workflow_cases SET queue_position=? WHERE case_id=?", (first[1], second[0]))
        conn.commit()
    finally:
        conn.close()


def remove_workflow_case(case_id):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM workflow_cases WHERE case_id=?", (case_id,))
        conn.commit()
    finally:
        conn.close()


# --- CORE DATA OPERATIONS ---

def save_history_record(active_username, subject_name, patient_age, patient_gender, radiologist, total_tumor, core, edema, pathology, mass_effect="Unknown", impression="Unknown", lobe_metrics=None, patient_details=None, source_scan_key=None):
    conn = get_connection()
    cursor = conn.cursor()
    current_time_str = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
    
    try:
        user_id = get_user_id_by_username(active_username)
        if not user_id:
            raise Exception("Active system user not found in database.")

        try:
            age_int = int(str(patient_age).replace('yo', '').replace(' ', ''))
        except ValueError:
            age_int = 0

        patient_details = patient_details or {}
        detail_fields = (
            "full_name", "date_of_birth", "phone", "email", "address", "emergency_contact",
            "emergency_phone", "medical_history", "allergies", "current_medications"
        )
        detail_values = [str(patient_details.get(key) or "").strip() for key in detail_fields]
        full_name, date_of_birth = detail_values[0], detail_values[1]
        cursor.execute("""SELECT id FROM patients
                          WHERE patient_id = ? OR lower(trim(patient_id)) = lower(trim(?))
                          ORDER BY CASE WHEN patient_id = ? THEN 0 ELSE 1 END LIMIT 1""",
                       (subject_name, subject_name, subject_name))
        patient_row = cursor.fetchone()
        if not patient_row and full_name and date_of_birth:
            patient_row = cursor.execute("""SELECT id FROM patients
                WHERE lower(trim(coalesce(full_name, ''))) = lower(trim(?))
                  AND trim(coalesce(date_of_birth, '')) = trim(?) ORDER BY id LIMIT 1""", (full_name, date_of_birth)).fetchone()
        if patient_row:
            patient_id = patient_row[0]
            assignments = ", ".join(f"{field} = ?" for field in detail_fields)
            cursor.execute(
                f"UPDATE patients SET age = ?, gender = ?, {assignments} WHERE id = ?",
                (age_int, patient_gender, *detail_values, patient_id)
            )
        else:
            patient_id = generate_uid("PNT")
            columns = ", ".join(("id", "patient_id", "age", "gender", *detail_fields))
            placeholders = ", ".join("?" for _ in range(4 + len(detail_fields)))
            cursor.execute(
                f"INSERT INTO patients ({columns}) VALUES ({placeholders})",
                (patient_id, subject_name, age_int, patient_gender, *detail_values)
            )

        # Identical voxel data can legitimately belong to different patients.
        # Scope the content fingerprint to the resolved patient before using it
        # as the idempotency key for repeat imports of that patient's scan.
        scoped_scan_key = (hashlib.sha256(f"{patient_id}:{source_scan_key}".encode("utf-8")).hexdigest()
                           if source_scan_key else None)
        existing_seg = cursor.execute("SELECT id FROM segmentation_results WHERE source_scan_key = ?", (scoped_scan_key,)).fetchone() if scoped_scan_key else None
        seg_id = existing_seg[0] if existing_seg else generate_uid("SEG")
        if existing_seg:
            cursor.execute("""UPDATE segmentation_results SET patient_id = ?, user_id = ?, radiologist = ?,
                scan_date = ?, pathology = ?, mass_effect = ?, clinical_impression = ? WHERE id = ?""", (patient_id, user_id, radiologist, current_time_str, pathology, mass_effect, impression, seg_id))
        else:
            cursor.execute("""INSERT INTO segmentation_results
                (id, patient_id, user_id, radiologist, scan_date, pathology, mass_effect, clinical_impression, source_scan_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (seg_id, patient_id, user_id, radiologist, current_time_str, pathology, mass_effect, impression, scoped_scan_key))

        if lobe_metrics is None:
            lobe_metrics = {}
            
        existing_metric = cursor.execute("SELECT id FROM volumetric_metrics WHERE segmentation_id = ? ORDER BY id LIMIT 1", (seg_id,)).fetchone() if existing_seg else None
        if existing_metric:
            cursor.execute("""UPDATE volumetric_metrics SET total_tumor_mm3=?, core_mm3=?, edema_mm3=?,
                intracranial_mm3=?, frontal_mm3=?, parietal_mm3=?, occipital_mm3=?, temporal_mm3=?,
                cerebellum_mm3=?, spinal_cord_mm3=? WHERE id=?""", (total_tumor, core, edema, lobe_metrics.get("total_intracranial_mm3", 0.0),
                 lobe_metrics.get("frontal_mm3", 0.0), lobe_metrics.get("parietal_mm3", 0.0),
                 lobe_metrics.get("occipital_mm3", 0.0), lobe_metrics.get("temporal_mm3", 0.0),
                 lobe_metrics.get("cerebellum_mm3", 0.0), lobe_metrics.get("spinal_cord_mm3", 0.0), existing_metric[0]))
        else:
            met_id = generate_uid("MET")
            cursor.execute("""
            INSERT INTO volumetric_metrics (
                id, segmentation_id, total_tumor_mm3, core_mm3, edema_mm3, 
                intracranial_mm3, frontal_mm3, parietal_mm3, occipital_mm3, 
                temporal_mm3, cerebellum_mm3, spinal_cord_mm3
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            met_id, seg_id, total_tumor, core, edema,
            lobe_metrics.get("total_intracranial_mm3", 0.0),
            lobe_metrics.get("frontal_mm3", 0.0),
            lobe_metrics.get("parietal_mm3", 0.0),
            lobe_metrics.get("occipital_mm3", 0.0),
            lobe_metrics.get("temporal_mm3", 0.0),
            lobe_metrics.get("cerebellum_mm3", 0.0),
            lobe_metrics.get("spinal_cord_mm3", 0.0)
        ))

        conn.commit()
        return seg_id
    except Exception as e:
        print(f"Database Save Error: {e}")
        conn.rollback()
    finally:
        conn.close()

def save_report_log(segmentation_id, report_type, file_path):
    conn = get_connection()
    cursor = conn.cursor()
    rep_id = generate_uid("REP")
    current_time_str = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
    cursor.execute("""
        INSERT INTO reports (id, segmentation_id, report_type, file_path, generated_at)
        VALUES (?, ?, ?, ?, ?)
    """, (rep_id, segmentation_id, report_type, file_path, current_time_str))
    conn.commit()
    conn.close()

def fetch_recent_history(search_term=""):
    conn = get_connection()
    cursor = conn.cursor()
    
    base_query = """
        SELECT 
            sr.id, p.patient_id, p.age, p.gender, sr.radiologist, sr.scan_date,
            vm.total_tumor_mm3, vm.core_mm3, vm.edema_mm3, sr.pathology
        FROM segmentation_results sr
        JOIN patients p ON sr.patient_id = p.id
        JOIN volumetric_metrics vm ON vm.segmentation_id = sr.id
    """

    if search_term.strip():
        term = f"%{search_term.strip()}%"
        query = base_query + """
            WHERE p.patient_id LIKE ? OR sr.radiologist LIKE ? OR sr.pathology LIKE ? OR sr.scan_date LIKE ?
            ORDER BY sr.scan_date DESC
        """
        cursor.execute(query, (term, term, term, term))
    else:
        query = base_query + " ORDER BY sr.scan_date DESC LIMIT 30"
        cursor.execute(query)
        
    rows = cursor.fetchall()
    conn.close()
    return rows

def fetch_table_patients():
    conn = get_connection()
    c = conn.cursor()
    c.execute("""SELECT id, patient_id, full_name, age, gender, date_of_birth, phone, email,
                       address, emergency_contact, emergency_phone, medical_history,
                       allergies, current_medications
                FROM patients ORDER BY id DESC""")
    rows = c.fetchall()
    conn.close()
    return rows

def fetch_patient_details(patient_id):
    conn = get_connection()
    try:
        row = conn.execute("""SELECT patient_id, age, gender, full_name, date_of_birth, phone, email,
                                    address, emergency_contact, emergency_phone, medical_history,
                                    allergies, current_medications
                             FROM patients WHERE patient_id = ?""", (patient_id,)).fetchone()
        if not row:
            return None
        keys = ("patient_name", "patient_age", "patient_gender", "full_name", "date_of_birth", "phone",
                "email", "address", "emergency_contact", "emergency_phone", "medical_history",
                "allergies", "current_medications")
        return dict(zip(keys, row))
    finally:
        conn.close()

def fetch_patient_history():
    """Return one detailed longitudinal record per patient scan."""
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT p.patient_id, p.age, p.gender, sr.scan_date, sr.radiologist,
                   sr.pathology, sr.mass_effect, sr.clinical_impression,
                   vm.total_tumor_mm3, vm.core_mm3, vm.edema_mm3,
                   vm.intracranial_mm3, vm.frontal_mm3, vm.parietal_mm3,
                   vm.occipital_mm3, vm.temporal_mm3, vm.cerebellum_mm3,
                   vm.spinal_cord_mm3, COUNT(DISTINCT r.id) AS report_count
            FROM patients p
            LEFT JOIN segmentation_results sr ON sr.patient_id = p.id
            LEFT JOIN volumetric_metrics vm ON vm.segmentation_id = sr.id
            LEFT JOIN reports r ON r.segmentation_id = sr.id
            GROUP BY p.id, sr.id
            ORDER BY p.patient_id, sr.scan_date DESC
        """).fetchall()
    finally:
        conn.close()

def fetch_table_segmentations():
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT sr.id, p.patient_id, sr.radiologist, sr.scan_date, sr.pathology, sr.mass_effect 
        FROM segmentation_results sr 
        JOIN patients p ON sr.patient_id = p.id 
        ORDER BY sr.scan_date DESC
    """)
    rows = c.fetchall()
    conn.close()
    return rows

def fetch_table_metrics():
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT id, segmentation_id, total_tumor_mm3, core_mm3, edema_mm3, 
               intracranial_mm3, frontal_mm3, parietal_mm3, occipital_mm3, 
               temporal_mm3, cerebellum_mm3, spinal_cord_mm3 
        FROM volumetric_metrics ORDER BY id DESC
    """)
    rows = c.fetchall()
    conn.close()
    return rows

def fetch_table_reports():
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id, segmentation_id, report_type, file_path, generated_at FROM reports ORDER BY generated_at DESC")
    rows = c.fetchall()
    conn.close()
    return rows

# Append to src/database/db_manager.py

def get_user_credentials(username):
    """Fetches user hash, salt, display_name, and designation for cloud sync."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT password_hash, salt, display_name, designation FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            "password_hash": row[0],
            "salt": row[1],
            "display_name": row[2] or username,
            "designation": row[3] or "Attending Physician"
        }
    return None

def export_workstation_backup(username):
    """Packages all clinical data owned by this user into a dictionary for cloud backup."""
    user_id = get_user_id_by_username(username)
    if not user_id:
        return None

    conn = get_connection()
    cursor = conn.cursor()

    # 1. Segmentation results for this user
    cursor.execute("""
        SELECT id, patient_id, user_id, radiologist, scan_date, pathology, mass_effect, clinical_impression
        FROM segmentation_results WHERE user_id = ?
    """, (user_id,))
    seg_rows = cursor.fetchall()
    segmentations = [
        {
            "id": r[0], "patient_id": r[1], "user_id": r[2], "radiologist": r[3],
            "scan_date": r[4], "pathology": r[5], "mass_effect": r[6], "clinical_impression": r[7]
        }
        for r in seg_rows
    ]
    seg_ids = [s["id"] for s in segmentations]
    patient_ids = list({s["patient_id"] for s in segmentations})

    # 2. Patients related to those segmentations
    patients = []
    if patient_ids:
        placeholders = ",".join("?" for _ in patient_ids)
        cursor.execute(f"""SELECT id, patient_id, age, gender, full_name, date_of_birth, phone,
                                   email, address, emergency_contact, emergency_phone,
                                   medical_history, allergies, current_medications
                            FROM patients WHERE id IN ({placeholders})""", patient_ids)
        patient_keys = ("id", "patient_id", "age", "gender", "full_name", "date_of_birth", "phone",
                        "email", "address", "emergency_contact", "emergency_phone", "medical_history",
                        "allergies", "current_medications")
        patients = [dict(zip(patient_keys, row)) for row in cursor.fetchall()]

    # 3. Volumetric metrics
    metrics = []
    if seg_ids:
        placeholders = ",".join("?" for _ in seg_ids)
        cursor.execute(f"""
            SELECT id, segmentation_id, total_tumor_mm3, core_mm3, edema_mm3, intracranial_mm3,
                   frontal_mm3, parietal_mm3, occipital_mm3, temporal_mm3, cerebellum_mm3, spinal_cord_mm3
            FROM volumetric_metrics WHERE segmentation_id IN ({placeholders})
        """, seg_ids)
        metrics = [
            {
                "id": r[0], "segmentation_id": r[1], "total_tumor_mm3": r[2], "core_mm3": r[3],
                "edema_mm3": r[4], "intracranial_mm3": r[5], "frontal_mm3": r[6], "parietal_mm3": r[7],
                "occipital_mm3": r[8], "temporal_mm3": r[9], "cerebellum_mm3": r[10], "spinal_cord_mm3": r[11]
            }
            for r in cursor.fetchall()
        ]

    # 4. Reports
    reports = []
    if seg_ids:
        placeholders = ",".join("?" for _ in seg_ids)
        cursor.execute(f"""
            SELECT id, segmentation_id, report_type, file_path, generated_at
            FROM reports WHERE segmentation_id IN ({placeholders})
        """, seg_ids)
        reports = [
            {"id": r[0], "segmentation_id": r[1], "report_type": r[2], "file_path": r[3], "generated_at": r[4]}
            for r in cursor.fetchall()
        ]

    conn.close()

    return {
        "owner_id": username,
        "backup_date": datetime.now().strftime("%Y-%m-%d %I:%M:%S %p"),
        "patients": patients,
        "segmentation_results": segmentations,
        "volumetric_metrics": metrics,
        "reports": reports
    }

def restore_workstation_backup(user_profile, backup_data):
    """Restores user profile and clinical tables into local SQLite."""
    conn = get_connection()
    cursor = conn.cursor()

    username = user_profile["username"]
    pwd_hash = user_profile["password_hash"]
    salt = user_profile["salt"]
    display_name = user_profile.get("display_name", username)
    designation = user_profile.get("designation", "Attending Physician")

    # 1. Restore/Upsert user
    cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
    existing_user = cursor.fetchone()
    if existing_user:
        local_user_id = existing_user[0]
        cursor.execute("""
            UPDATE users SET password_hash = ?, salt = ?, display_name = ?, designation = ?
            WHERE username = ?
        """, (pwd_hash, salt, display_name, designation, username))
    else:
        local_user_id = generate_uid("USR")
        cursor.execute("""
            INSERT INTO users (id, username, password_hash, salt, display_name, designation)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (local_user_id, username, pwd_hash, salt, display_name, designation))

    # Remove default admin if recovering on a fresh install
    cursor.execute("DELETE FROM users WHERE username = 'admin'")

    if not backup_data:
        conn.commit()
        conn.close()
        return True

    # 2. Restore Patients
    for p in backup_data.get("patients", []):
        cursor.execute("""
            INSERT OR REPLACE INTO patients (
                id, patient_id, age, gender, full_name, date_of_birth, phone, email, address,
                emergency_contact, emergency_phone, medical_history, allergies, current_medications
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            p["id"], p["patient_id"], p.get("age"), p.get("gender"), p.get("full_name"),
            p.get("date_of_birth"), p.get("phone"), p.get("email"), p.get("address"),
            p.get("emergency_contact"), p.get("emergency_phone"), p.get("medical_history"),
            p.get("allergies"), p.get("current_medications")
            ))

    # 3. Restore Segmentation Results
    for s in backup_data.get("segmentation_results", []):
        cursor.execute("""
            INSERT OR REPLACE INTO segmentation_results (
                id, patient_id, user_id, radiologist, scan_date, pathology, mass_effect, clinical_impression
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (s["id"], s["patient_id"], local_user_id, s["radiologist"], s["scan_date"], s["pathology"], s["mass_effect"], s["clinical_impression"]))

    # 4. Restore Volumetric Metrics
    for m in backup_data.get("volumetric_metrics", []):
        cursor.execute("""
            INSERT OR REPLACE INTO volumetric_metrics (
                id, segmentation_id, total_tumor_mm3, core_mm3, edema_mm3, intracranial_mm3,
                frontal_mm3, parietal_mm3, occipital_mm3, temporal_mm3, cerebellum_mm3, spinal_cord_mm3
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            m["id"], m["segmentation_id"], m["total_tumor_mm3"], m["core_mm3"], m["edema_mm3"], m["intracranial_mm3"],
            m["frontal_mm3"], m["parietal_mm3"], m["occipital_mm3"], m["temporal_mm3"], m["cerebellum_mm3"], m["spinal_cord_mm3"]
        ))

    # 5. Restore Reports
    for r in backup_data.get("reports", []):
        cursor.execute("""
            INSERT OR REPLACE INTO reports (id, segmentation_id, report_type, file_path, generated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (r["id"], r["segmentation_id"], r["report_type"], r["file_path"], r["generated_at"]))

    conn.commit()
    conn.close()
    return True
