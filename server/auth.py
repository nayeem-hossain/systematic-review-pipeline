from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Optional, Dict, Any

AUTH_DB_FILE = Path("runs_web/users.json")
AUTH_DB_FILE.parent.mkdir(parents=True, exist_ok=True)

# Cryptographic encryption for sensitive fields (e.g. API keys)
_fernet = None
_enc_key = os.environ.get("APP_ENCRYPTION_KEY")
if _enc_key:
    try:
        from cryptography.fernet import Fernet
        _fernet = Fernet(_enc_key.encode("utf-8") if isinstance(_enc_key, str) else _enc_key)
    except Exception as e:
        print(f"Warning: Failed to initialize Fernet encryption with APP_ENCRYPTION_KEY: {e}")


def encrypt_sensitive_data(val: str) -> str:
    if not val or not _fernet:
        return val
    try:
        return _fernet.encrypt(val.encode("utf-8")).decode("utf-8")
    except Exception:
        return val


def decrypt_sensitive_data(val: str) -> str:
    if not val or not _fernet:
        return val
    try:
        return _fernet.decrypt(val.encode("utf-8")).decode("utf-8")
    except Exception:
        return val


def encrypt_api_keys_dict(api_keys: Dict[str, str]) -> Dict[str, str]:
    if not api_keys or not _fernet:
        return api_keys
    encrypted = {}
    for k, v in api_keys.items():
        if v:
            encrypted[k] = encrypt_sensitive_data(v)
        else:
            encrypted[k] = ""
    return encrypted


def decrypt_api_keys_dict(api_keys: Dict[str, str]) -> Dict[str, str]:
    if not api_keys or not _fernet:
        return api_keys
    decrypted = {}
    for k, v in api_keys.items():
        if v:
            decrypted[k] = decrypt_sensitive_data(v)
        else:
            decrypted[k] = ""
    return decrypted


def _get_db_connection():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        return None

    if db_url.startswith("postgres://") or db_url.startswith("postgresql://"):
        import psycopg2
        url_clean = db_url.replace("postgres://", "postgresql://", 1)
        conn = psycopg2.connect(url_clean)
        return conn, "postgres"
    elif db_url.startswith("sqlite://"):
        sqlite_path = db_url.replace("sqlite:///", "").replace("sqlite://", "")
        conn = sqlite3.connect(sqlite_path)
        return conn, "sqlite"
    return None, None


def _init_db_schema_if_needed():
    res = _get_db_connection()
    if not res or res[0] is None:
        return
    conn, db_type = res
    try:
        with conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id VARCHAR(64) PRIMARY KEY,
                    email VARCHAR(255) UNIQUE NOT NULL,
                    pwd_hash VARCHAR(255) NOT NULL,
                    pwd_salt VARCHAR(255) NOT NULL,
                    api_keys TEXT
                );
            """)
            cur.close()
    finally:
        conn.close()


# Ensure DB schema exists if DATABASE_URL is set
if os.environ.get("DATABASE_URL"):
    _init_db_schema_if_needed()


def _load_users() -> Dict[str, Dict[str, Any]]:
    res = _get_db_connection()
    if res and res[0] is not None:
        conn, db_type = res
        try:
            users = {}
            cur = conn.cursor()
            cur.execute("SELECT user_id, email, pwd_hash, pwd_salt, api_keys FROM users;")
            rows = cur.fetchall()
            cur.close()
            for r in rows:
                uid, email, pwd_hash, salt, raw_keys_str = r[0], r[1], r[2], r[3], r[4]
                raw_keys = json.loads(raw_keys_str) if raw_keys_str else {}
                users[email] = {
                    "user_id": uid,
                    "email": email,
                    "hash": pwd_hash,
                    "salt": salt,
                    "api_keys": decrypt_api_keys_dict(raw_keys),
                }
            return users
        finally:
            conn.close()

    if not AUTH_DB_FILE.exists():
        return {}
    try:
        data = json.loads(AUTH_DB_FILE.read_text(encoding="utf-8"))
        # Decrypt API keys if encrypted
        for _, udata in data.items():
            if "api_keys" in udata:
                udata["api_keys"] = decrypt_api_keys_dict(udata["api_keys"])
        return data
    except Exception:
        return {}


def _save_users(users: Dict[str, Dict[str, Any]]) -> None:
    res = _get_db_connection()
    if res and res[0] is not None:
        conn, db_type = res
        try:
            with conn:
                cur = conn.cursor()
                for email, udata in users.items():
                    enc_keys = encrypt_api_keys_dict(udata.get("api_keys", {}))
                    keys_json = json.dumps(enc_keys)
                    ph = "%s" if db_type == "postgres" else "?"
                    cur.execute(f"""
                        INSERT INTO users (user_id, email, pwd_hash, pwd_salt, api_keys)
                        VALUES ({ph}, {ph}, {ph}, {ph}, {ph})
                        ON CONFLICT (email) DO UPDATE SET
                            user_id = EXCLUDED.user_id,
                            pwd_hash = EXCLUDED.pwd_hash,
                            pwd_salt = EXCLUDED.pwd_salt,
                            api_keys = EXCLUDED.api_keys;
                    """, (udata["user_id"], udata["email"], udata["hash"], udata["salt"], keys_json))
                cur.close()
            return
        finally:
            conn.close()

    # JSON fallback
    save_data = {}
    for email, udata in users.items():
        ud_copy = dict(udata)
        if "api_keys" in ud_copy:
            ud_copy["api_keys"] = encrypt_api_keys_dict(ud_copy["api_keys"])
        save_data[email] = ud_copy
    AUTH_DB_FILE.write_text(json.dumps(save_data, indent=2), encoding="utf-8")

def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000)
    return key.hex(), salt

def register_user(email: str, password: str) -> Dict[str, Any]:
    email_clean = email.strip().lower()
    if not email_clean or "@" not in email_clean:
        raise ValueError("Invalid email address")
    if len(password) < 6:
        raise ValueError("Password must be at least 6 characters")

    users = _load_users()
    if email_clean in users:
        raise ValueError("User with this email already exists")

    pwd_hash, salt = hash_password(password)
    user_id = f"usr_{secrets.token_hex(6)}"
    user_data = {
        "user_id": user_id,
        "email": email_clean,
        "hash": pwd_hash,
        "salt": salt,
        "api_keys": {},
    }
    users[email_clean] = user_data
    _save_users(users)
    return {"user_id": user_id, "email": email_clean, "api_keys": {}}

def authenticate_user(email: str, password: str) -> Optional[Dict[str, Any]]:
    email_clean = email.strip().lower()
    users = _load_users()
    user_data = users.get(email_clean)
    if not user_data:
        return None

    computed_hash, _ = hash_password(password, user_data["salt"])
    if secrets.compare_digest(computed_hash, user_data["hash"]):
        return {
            "user_id": user_data["user_id"],
            "email": user_data["email"],
            "api_keys": user_data.get("api_keys", {}),
        }
    return None

def get_user_profile(user_id: str) -> Optional[Dict[str, Any]]:
    users = _load_users()
    for _, u_data in users.items():
        if u_data.get("user_id") == user_id:
            return {
                "user_id": u_data["user_id"],
                "email": u_data["email"],
                "api_keys": u_data.get("api_keys", {}),
            }
    return None

def update_user_profile(user_id: str, new_email: Optional[str] = None, new_password: Optional[str] = None, api_keys: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    users = _load_users()
    target_email = None
    target_user = None

    for email_k, u_data in users.items():
        if u_data.get("user_id") == user_id:
            target_email = email_k
            target_user = u_data
            break

    if not target_user or not target_email:
        raise ValueError("User not found")

    if new_email and new_email.strip().lower() != target_email:
        clean_new = new_email.strip().lower()
        if "@" not in clean_new:
            raise ValueError("Invalid email format")
        if clean_new in users:
            raise ValueError("Email already in use by another user")
        # Remove old key
        del users[target_email]
        target_user["email"] = clean_new
        target_email = clean_new

    if new_password:
        if len(new_password) < 6:
            raise ValueError("Password must be at least 6 characters")
        pwd_hash, salt = hash_password(new_password)
        target_user["hash"] = pwd_hash
        target_user["salt"] = salt

    if api_keys is not None:
        target_user["api_keys"] = api_keys

    users[target_email] = target_user
    _save_users(users)

    return {
        "user_id": target_user["user_id"],
        "email": target_user["email"],
        "api_keys": target_user.get("api_keys", {}),
    }

def delete_user_account(user_id: str) -> bool:
    res = _get_db_connection()
    if res and res[0] is not None:
        conn, db_type = res
        try:
            with conn:
                cur = conn.cursor()
                ph = "%s" if db_type == "postgres" else "?"
                cur.execute(f"DELETE FROM users WHERE user_id = {ph};", (user_id,))
                deleted = cur.rowcount > 0
                cur.close()
                if deleted:
                    return True
            return False
        finally:
            conn.close()

    users = _load_users()
    target_email = None

    for email_k, u_data in users.items():
        if u_data.get("user_id") == user_id:
            target_email = email_k
            break

    if target_email and target_email in users:
        del users[target_email]
        _save_users(users)
        return True
    return False
